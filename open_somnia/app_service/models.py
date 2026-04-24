from __future__ import annotations

from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Event, Lock
from typing import Any

from open_somnia.app_service.events import AppServiceEvent
from open_somnia.runtime.session import AgentSession


@dataclass(slots=True)
class ModelDescriptor:
    provider_name: str
    name: str
    context_window_tokens: int | None = None
    supports_reasoning: bool | None = None
    supports_adaptive_reasoning: bool | None = None
    is_default: bool = False
    is_active: bool = False


@dataclass(slots=True)
class ProviderDescriptor:
    name: str
    provider_type: str
    default_model: str
    models: list[str] = field(default_factory=list)
    active_model: str | None = None
    reasoning_level: str | None = None
    is_active: bool = False


@dataclass(slots=True)
class InteractionRequestState:
    id: str
    kind: str
    session_id: str | None = None
    turn_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    completed: Event = field(default_factory=Event, repr=False)
    response: dict[str, Any] | None = None


@dataclass(slots=True)
class TurnRunResult:
    session: AgentSession
    text: str = ""
    status: str = "completed"
    open_todo_count: int = 0
    interrupted: bool = False
    error: str | None = None


class TurnHandle:
    def __init__(
        self,
        *,
        turn_id: str,
        session: AgentSession,
        event_queue: Queue[AppServiceEvent],
        done_event: Event,
    ) -> None:
        self.turn_id = turn_id
        self.session = session
        self._event_queue = event_queue
        self._done_event = done_event
        self._result: TurnRunResult | None = None
        self._result_lock = Lock()

    @property
    def result(self) -> TurnRunResult | None:
        with self._result_lock:
            return self._result

    def _set_result(self, result: TurnRunResult) -> None:
        with self._result_lock:
            self._result = result

    def is_done(self) -> bool:
        return self._done_event.is_set()

    def wait(self, timeout: float | None = None) -> TurnRunResult | None:
        if not self._done_event.wait(timeout=timeout):
            return None
        return self.result

    def drain_events(self, *, block: bool = False, timeout: float | None = None) -> list[AppServiceEvent]:
        events: list[AppServiceEvent] = []
        if block:
            try:
                events.append(self._event_queue.get(timeout=timeout))
            except Empty:
                return []
        while True:
            try:
                events.append(self._event_queue.get_nowait())
            except Empty:
                break
        return events
