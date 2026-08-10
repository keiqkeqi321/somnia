from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any


TURN_STARTED = "turn_started"
ASSISTANT_DELTA = "assistant_delta"
THINKING_DELTA = "thinking_delta"
THINKING_FINISHED = "thinking_finished"
ASSISTANT_COMPLETED = "assistant_completed"
TOOL_STARTED = "tool_started"
TOOL_FINISHED = "tool_finished"
SUBAGENT_ACTIVITY = "subagent_activity"
AUTHORIZATION_REQUESTED = "authorization_requested"
MODE_SWITCH_REQUESTED = "mode_switch_requested"
QUESTION_REQUESTED = "question_requested"
INTERRUPT_REQUESTED = "interrupt_requested"
INTERRUPT_COMPLETED = "interrupt_completed"
SESSION_UPDATED = "session_updated"
TODO_UPDATED = "todo_updated"
CONTEXT_USAGE_UPDATED = "context_usage_updated"
LOOP_USER_MESSAGE_INJECTED = "loop_user_message_injected"
ERROR = "error"

EVENT_TYPES = frozenset(
    {
        TURN_STARTED,
        ASSISTANT_DELTA,
        THINKING_DELTA,
        THINKING_FINISHED,
        ASSISTANT_COMPLETED,
        TOOL_STARTED,
        TOOL_FINISHED,
        SUBAGENT_ACTIVITY,
        AUTHORIZATION_REQUESTED,
        MODE_SWITCH_REQUESTED,
        QUESTION_REQUESTED,
        INTERRUPT_REQUESTED,
        INTERRUPT_COMPLETED,
        SESSION_UPDATED,
        TODO_UPDATED,
        CONTEXT_USAGE_UPDATED,
        LOOP_USER_MESSAGE_INJECTED,
        ERROR,
    }
)

TERMINAL_EVENT_TYPES = frozenset({ASSISTANT_COMPLETED, INTERRUPT_COMPLETED, ERROR})


@dataclass(slots=True)
class AppServiceEvent:
    type: str
    session_id: str | None = None
    turn_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


def make_event(
    event_type: str,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    **payload: Any,
) -> AppServiceEvent:
    return AppServiceEvent(
        type=event_type,
        session_id=session_id,
        turn_id=turn_id,
        payload=dict(payload),
    )
