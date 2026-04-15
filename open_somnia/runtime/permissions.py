from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from open_somnia.runtime.execution_mode import (
    ACCEPT_EDITS_BADGE,
    AUTHORIZATION_TOOL_NAME,
    DEFAULT_EXECUTION_MODE,
    is_mode_escalation,
    MODE_SWITCH_TOOL_NAME,
    NON_YOLO_EXECUTION_MODES,
    normalize_execution_mode,
    execution_mode_spec,
    tool_block_message,
)
from open_somnia.storage.common import read_json, write_json


class PermissionManager:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def _hook_manager(self):
        getter = getattr(self.runtime, "_hook_manager", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        return getattr(self.runtime, "hook_manager", None)

    def workspace_authorizations_path(self) -> Path | None:
        settings = getattr(self.runtime, "settings", None)
        storage = getattr(settings, "storage", None)
        data_dir = getattr(storage, "data_dir", None)
        if not isinstance(data_dir, Path):
            return None
        return data_dir / self.runtime.WORKSPACE_PERMISSIONS_FILE

    def load_workspace_authorizations(self) -> set[str]:
        path = self.workspace_authorizations_path()
        if path is None:
            return set()
        try:
            payload = read_json(path, {"authorized_tools": []})
        except Exception:
            return set()
        if not isinstance(payload, dict):
            return set()
        raw_tools = payload.get("authorized_tools", [])
        if not isinstance(raw_tools, list):
            return set()
        authorized: set[str] = set()
        for item in raw_tools:
            tool_name = str(item).strip()
            if tool_name:
                authorized.add(tool_name)
        return authorized

    def persist_workspace_authorizations(self) -> None:
        path = self.workspace_authorizations_path()
        if path is None:
            return
        write_json(path, {"authorized_tools": sorted(self.runtime._workspace_authorized_tools)})

    def authorize_tool_call(self, tool_name: str, payload: dict[str, Any], *, ctx=None) -> str | None:
        if tool_name in {AUTHORIZATION_TOOL_NAME, MODE_SWITCH_TOOL_NAME}:
            return None
        if tool_name in self.runtime._workspace_authorized_tools:
            return None
        remaining = self.runtime._once_authorized_tools.get(tool_name, 0)
        if remaining > 0:
            if remaining == 1:
                self.runtime._once_authorized_tools.pop(tool_name, None)
            else:
                self.runtime._once_authorized_tools[tool_name] = remaining - 1
            return None
        if getattr(ctx, "actor", None) == "subagent":
            return None
        if tool_name == "subagent":
            return self._authorize_subagent_call(payload)
        return tool_block_message(getattr(self.runtime, "execution_mode", DEFAULT_EXECUTION_MODE), tool_name)

    def _authorize_subagent_call(self, payload: dict[str, Any]) -> str | None:
        mode = normalize_execution_mode(getattr(self.runtime, "execution_mode", DEFAULT_EXECUTION_MODE))
        if mode in {"accept_edits", "yolo"}:
            return None
        agent_type = str(payload.get("agent_type", "Explore")).strip() or "Explore"
        spec = execution_mode_spec(mode)
        if agent_type == "Explore":
            return (
                f"Blocked in {spec.title}: 'subagent' requires explicit user approval in read-only modes. "
                "Call request_authorization if this subagent is necessary."
            )
        return (
            f"Blocked in {spec.title}: 'subagent' with agent_type='{agent_type}' may edit workspace files. "
            "Use agent_type='Explore'. Call request_mode_switch to "
            f"{ACCEPT_EDITS_BADGE} accept edits on when the task has moved into implementation, "
            "or request_authorization only for a one-off subagent run."
        )

    def request_authorization(self, tool_name: str, reason: str, argument_summary: str = "") -> str:
        normalized_tool = str(tool_name).strip()
        if not normalized_tool:
            return "Authorization request failed: tool_name is required."
        if normalized_tool == AUTHORIZATION_TOOL_NAME:
            return "Authorization not required for request_authorization."
        if normalized_tool in self.runtime._workspace_authorized_tools:
            return json.dumps(
                {"status": "approved", "scope": "workspace", "tool_name": normalized_tool, "cached": True},
                ensure_ascii=False,
            )
        handler = self.runtime.authorization_request_handler
        if not callable(handler):
            return "Authorization request failed: interactive approvals are unavailable in this session."
        hook_manager = self._hook_manager()
        if hook_manager is not None:
            hook_manager.on_user_choice_requested(
                session=None,
                trace_id=None,
                actor="lead",
                execution_mode=getattr(self.runtime, "execution_mode", DEFAULT_EXECUTION_MODE),
                choice_type="authorization",
                choice_payload={
                    "tool_name": normalized_tool,
                    "reason": str(reason).strip(),
                    "argument_summary": str(argument_summary).strip(),
                },
                options=["allow_once", "allow_workspace", "deny"],
            )
        result = handler(
            tool_name=normalized_tool,
            reason=str(reason).strip(),
            argument_summary=str(argument_summary).strip(),
            execution_mode=getattr(self.runtime, "execution_mode", DEFAULT_EXECUTION_MODE),
        )
        if not isinstance(result, dict):
            return "Authorization request failed: invalid approval response."
        status = str(result.get("status", "denied")).strip().lower()
        scope = str(result.get("scope", "deny")).strip().lower()
        if status == "approved":
            if scope == "workspace":
                self.runtime._workspace_authorized_tools.add(normalized_tool)
                self.persist_workspace_authorizations()
            elif scope == "once":
                self.runtime._once_authorized_tools[normalized_tool] = (
                    self.runtime._once_authorized_tools.get(normalized_tool, 0) + 1
                )
        payload = {
            "status": "approved" if status == "approved" else "denied",
            "scope": scope,
            "tool_name": normalized_tool,
            "reason": str(result.get("reason", "")).strip(),
        }
        return json.dumps(payload, ensure_ascii=False)

    def request_mode_switch(self, target_mode: str, reason: str = "") -> str:
        normalized_target = normalize_execution_mode(target_mode)
        if normalized_target == "yolo" or normalized_target not in NON_YOLO_EXECUTION_MODES:
            return (
                "Mode switch request failed: target_mode must be one of "
                "'shortcuts', 'plan', or 'accept_edits'."
            )
        current_mode = normalize_execution_mode(getattr(self.runtime, "execution_mode", DEFAULT_EXECUTION_MODE))
        if normalized_target == current_mode:
            return json.dumps(
                {
                    "status": "unchanged",
                    "current_mode": current_mode,
                    "target_mode": normalized_target,
                    "reason": "Already in requested mode.",
                },
                ensure_ascii=False,
            )
        if not is_mode_escalation(current_mode, normalized_target):
            self.runtime.execution_mode = normalized_target
            return json.dumps(
                {
                    "status": "approved",
                    "current_mode": normalized_target,
                    "target_mode": normalized_target,
                    "reason": f"Switched directly to {execution_mode_spec(normalized_target).title}.",
                },
                ensure_ascii=False,
            )
        handler = self.runtime.mode_switch_request_handler
        if not callable(handler):
            return "Mode switch request failed: interactive mode switching is unavailable in this session."
        hook_manager = self._hook_manager()
        if hook_manager is not None:
            hook_manager.on_user_choice_requested(
                session=None,
                trace_id=None,
                actor="lead",
                execution_mode=current_mode,
                choice_type="mode_switch",
                choice_payload={
                    "current_mode": current_mode,
                    "target_mode": normalized_target,
                    "reason": str(reason).strip(),
                },
                options=[normalized_target, current_mode],
            )
        result = handler(target_mode=normalized_target, reason=str(reason).strip(), current_mode=current_mode)
        if not isinstance(result, dict):
            return "Mode switch request failed: invalid mode switch response."
        approved = bool(result.get("approved"))
        active_mode = normalize_execution_mode(result.get("active_mode", current_mode))
        self.runtime.execution_mode = active_mode
        payload = {
            "status": "approved" if approved else "denied",
            "current_mode": active_mode,
            "target_mode": normalized_target,
            "reason": str(result.get("reason", "")).strip(),
        }
        return json.dumps(payload, ensure_ascii=False)
