from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any


TURN_STARTED = "turn_started"
ASSISTANT_DELTA = "assistant_delta"
ASSISTANT_COMPLETED = "assistant_completed"
TOOL_STARTED = "tool_started"
TOOL_FINISHED = "tool_finished"
AUTHORIZATION_REQUESTED = "authorization_requested"
MODE_SWITCH_REQUESTED = "mode_switch_requested"
INTERRUPT_REQUESTED = "interrupt_requested"
INTERRUPT_COMPLETED = "interrupt_completed"
SESSION_UPDATED = "session_updated"
TODO_UPDATED = "todo_updated"
ERROR = "error"

EVENT_TYPES = frozenset(
    {
        TURN_STARTED,
        ASSISTANT_DELTA,
        ASSISTANT_COMPLETED,
        TOOL_STARTED,
        TOOL_FINISHED,
        AUTHORIZATION_REQUESTED,
        MODE_SWITCH_REQUESTED,
        INTERRUPT_REQUESTED,
        INTERRUPT_COMPLETED,
        SESSION_UPDATED,
        TODO_UPDATED,
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
