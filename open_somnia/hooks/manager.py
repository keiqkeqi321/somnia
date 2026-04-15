from __future__ import annotations

from pathlib import Path
from typing import Any

from open_somnia.config.models import AppSettings, HookSettings
from open_somnia.hooks.models import (
    HookContext,
    HookDecision,
    HookExecutionError,
    HookExecutionResult,
    normalize_hook_event,
)
from open_somnia.hooks.runner import HookRunner
from open_somnia.storage.common import append_jsonl, now_ts


class HookManager:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        workspace_root = getattr(settings, "workspace_root", None)
        self.workspace_root = workspace_root if isinstance(workspace_root, Path) else Path.cwd()
        storage = getattr(settings, "storage", None)
        logs_dir = getattr(storage, "logs_dir", None)
        self.log_path = logs_dir / "hooks.jsonl" if isinstance(logs_dir, Path) else None
        self.runner = HookRunner(self.workspace_root)
        configured_hooks = getattr(settings, "hooks", []) or []
        self.hooks = [hook for hook in configured_hooks if getattr(hook, "enabled", True)]

    def on_session_start(self, session: Any, *, actor: str = "lead") -> None:
        context = HookContext(
            event="SessionStart",
            session_id=getattr(session, "id", None),
            trace_id=f"{getattr(session, 'id', 'session')}-session-start",
            actor=actor,
            execution_mode=None,
            workspace_root=self.workspace_root,
        )
        self._run_event(context)

    def before_tool_use(self, ctx: Any, tool_name: str, tool_input: dict[str, Any]) -> HookDecision:
        context = HookContext(
            event="PreToolUse",
            session_id=getattr(getattr(ctx, "session", None), "id", None),
            trace_id=getattr(ctx, "trace_id", None),
            actor=getattr(ctx, "actor", None),
            execution_mode=getattr(getattr(ctx, "runtime", None), "execution_mode", None),
            workspace_root=self.workspace_root,
            tool_name=tool_name,
            tool_input=dict(tool_input),
        )
        return self._run_pre_tool_use(context)

    def after_tool_use(
        self,
        ctx: Any,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        result: Any = None,
        error: Exception | None = None,
    ) -> None:
        context = HookContext(
            event="PostToolUse",
            session_id=getattr(getattr(ctx, "session", None), "id", None),
            trace_id=getattr(ctx, "trace_id", None),
            actor=getattr(ctx, "actor", None),
            execution_mode=getattr(getattr(ctx, "runtime", None), "execution_mode", None),
            workspace_root=self.workspace_root,
            tool_name=tool_name,
            tool_input=dict(tool_input),
            tool_result=result,
            tool_error=str(error) if error is not None else None,
        )
        self._run_event(context)

    def on_assistant_response(
        self,
        session: Any,
        *,
        actor: str,
        trace_id: str,
        assistant_message: dict[str, Any],
        text: str,
        execution_mode: str,
    ) -> None:
        context = HookContext(
            event="AssistantResponse",
            session_id=getattr(session, "id", None),
            trace_id=trace_id,
            actor=actor,
            execution_mode=execution_mode,
            workspace_root=self.workspace_root,
            assistant_message=assistant_message,
            text=text,
        )
        self._run_event(context)

    def on_user_choice_requested(
        self,
        *,
        session: Any | None = None,
        trace_id: str | None = None,
        actor: str = "lead",
        execution_mode: str | None = None,
        choice_type: str,
        choice_payload: dict[str, Any],
        options: list[str],
    ) -> None:
        context = HookContext(
            event="UserChoiceRequested",
            session_id=getattr(session, "id", None),
            trace_id=trace_id,
            actor=actor,
            execution_mode=execution_mode,
            workspace_root=self.workspace_root,
            choice_type=choice_type,
            choice_payload=dict(choice_payload),
            options=list(options),
        )
        self._run_event(context)

    def _run_pre_tool_use(self, context: HookContext) -> HookDecision:
        current_input = dict(context.tool_input or {})
        for hook in self._matching_hooks(context.event, tool_name=context.tool_name, actor=context.actor):
            step_context = HookContext(
                event=context.event,
                session_id=context.session_id,
                trace_id=context.trace_id,
                actor=context.actor,
                execution_mode=context.execution_mode,
                workspace_root=context.workspace_root,
                tool_name=context.tool_name,
                tool_input=current_input,
            )
            execution = self._execute_hook(hook, step_context)
            decision = execution.decision
            if decision.action == "deny":
                return decision
            if decision.replacement_input is not None:
                current_input = decision.replacement_input
        if current_input != (context.tool_input or {}):
            return HookDecision(action="replace_input", replacement_input=current_input)
        return HookDecision()

    def _run_event(self, context: HookContext) -> None:
        for hook in self._matching_hooks(context.event, tool_name=context.tool_name, actor=context.actor):
            self._execute_hook(hook, context)

    def _matching_hooks(self, event: str, *, tool_name: str | None = None, actor: str | None = None) -> list[HookSettings]:
        normalized_event = normalize_hook_event(event)
        matched: list[HookSettings] = []
        for hook in self.hooks:
            if normalize_hook_event(hook.event) != normalized_event:
                continue
            matcher = hook.matcher
            expected_tool_name = str(matcher.tool_name or "").strip()
            if expected_tool_name and expected_tool_name != str(tool_name or "").strip():
                continue
            expected_actor = str(matcher.actor or "").strip()
            if expected_actor and expected_actor != str(actor or "").strip():
                continue
            matched.append(hook)
        return matched

    def _execute_hook(self, hook: HookSettings, context: HookContext) -> HookExecutionResult:
        try:
            execution = self.runner.run(hook, context)
        except HookExecutionError as exc:
            self._log_hook_failure(hook, context, str(exc))
            if str(hook.on_error).strip().lower() == "continue":
                return self._continued_failure_result(hook)
            raise
        self._log_hook_success(execution, context)
        return execution

    def _continued_failure_result(self, hook: HookSettings) -> HookExecutionResult:
        return HookExecutionResult(hook=hook, decision=HookDecision(action="continue"), duration_ms=0)

    def _log_hook_success(self, execution: HookExecutionResult, context: HookContext) -> None:
        if self.log_path is None:
            return
        append_jsonl(
            self.log_path,
            {
                "ts": now_ts(),
                "status": "ok",
                "event": context.event,
                "hook": self._hook_identity(execution.hook),
                "tool_name": context.tool_name,
                "actor": context.actor,
                "session_id": context.session_id,
                "trace_id": context.trace_id,
                "duration_ms": execution.duration_ms,
                "action": execution.decision.action,
                "message": execution.decision.message,
            },
        )

    def _log_hook_failure(self, hook: HookSettings, context: HookContext, error: str) -> None:
        if self.log_path is None:
            return
        append_jsonl(
            self.log_path,
            {
                "ts": now_ts(),
                "status": "error",
                "event": context.event,
                "hook": self._hook_identity(hook),
                "tool_name": context.tool_name,
                "actor": context.actor,
                "session_id": context.session_id,
                "trace_id": context.trace_id,
                "message": error,
            },
        )

    def _hook_identity(self, hook: HookSettings) -> str:
        args = " ".join(str(arg) for arg in hook.args)
        return f"{hook.command} {args}".strip() or hook.command
