from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from open_somnia.config.models import HookSettings

HOOK_EVENTS = frozenset(
    {
        "SessionStart",
        "PreToolUse",
        "PostToolUse",
        "AssistantResponse",
        "UserChoiceRequested",
        "TurnFailed",
    }
)


def normalize_hook_event(value: str) -> str:
    normalized = str(value).strip().lower().replace("-", "").replace("_", "")
    mapping = {
        "sessionstart": "SessionStart",
        "pretooluse": "PreToolUse",
        "posttooluse": "PostToolUse",
        "assistantresponse": "AssistantResponse",
        "userchoicerequested": "UserChoiceRequested",
        "turnfailed": "TurnFailed",
    }
    event = mapping.get(normalized)
    if event is None:
        raise ValueError(
            "Unsupported hook event "
            f"'{value}'. Expected one of: {', '.join(sorted(HOOK_EVENTS))}."
        )
    return event


@dataclass(slots=True)
class HookContext:
    event: str
    session_id: str | None = None
    trace_id: str | None = None
    actor: str | None = None
    execution_mode: str | None = None
    workspace_root: Path | None = None
    session_path: Path | None = None
    transcript_path: Path | None = None
    snapshot_path: Path | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_result: Any = None
    tool_error: str | None = None
    assistant_message: dict[str, Any] | None = None
    text: str | None = None
    choice_type: str | None = None
    choice_payload: dict[str, Any] | None = None
    options: list[str] | None = None
    error_type: str | None = None
    error_message: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event": self.event,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "execution_mode": self.execution_mode,
            "workspace_root": str(self.workspace_root) if self.workspace_root is not None else None,
            "session_path": str(self.session_path) if self.session_path is not None else None,
            "transcript_path": str(self.transcript_path) if self.transcript_path is not None else None,
            "snapshot_path": str(self.snapshot_path) if self.snapshot_path is not None else None,
        }
        if self.tool_name is not None:
            payload["tool_name"] = self.tool_name
        if self.tool_input is not None:
            payload["tool_input"] = self.tool_input
        if self.tool_result is not None:
            payload["tool_result"] = self.tool_result
        if self.tool_error:
            payload["tool_error"] = self.tool_error
        if self.assistant_message is not None:
            payload["assistant_message"] = self.assistant_message
        if self.text is not None:
            payload["text"] = self.text
        if self.choice_type is not None:
            payload["choice_type"] = self.choice_type
        if self.choice_payload is not None:
            payload["choice_payload"] = self.choice_payload
        if self.options is not None:
            payload["options"] = self.options
        if self.error_type is not None:
            payload["error_type"] = self.error_type
        if self.error_message is not None:
            payload["error_message"] = self.error_message
        return payload


@dataclass(slots=True)
class HookDecision:
    action: str = "continue"
    message: str = ""
    replacement_input: dict[str, Any] | None = None


@dataclass(slots=True)
class HookExecutionResult:
    hook: HookSettings
    decision: HookDecision
    duration_ms: int
    status: str = "ok"
    background: bool = False
    pid: int | None = None
    stdout: str = ""
    stderr: str = ""
    response_payload: dict[str, Any] = field(default_factory=dict)


class HookExecutionError(RuntimeError):
    """Raised when an external hook fails and the failure should stop execution."""
