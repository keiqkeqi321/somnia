from __future__ import annotations

from contextlib import contextmanager
from threading import Lock, local
from typing import Any, Callable, Iterator
import uuid

from open_somnia.app_service.events import AUTHORIZATION_REQUESTED, MODE_SWITCH_REQUESTED
from open_somnia.app_service.models import InteractionRequestState
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.execution_mode import DEFAULT_EXECUTION_MODE, normalize_execution_mode


class InteractionService:
    REQUEST_TIMEOUT_SECONDS = 300.0

    def __init__(self, runtime: OpenAgentRuntime, emit_event: Callable[..., None]) -> None:
        self.runtime = runtime
        self._emit_event = emit_event
        self._lock = Lock()
        self._pending: dict[str, InteractionRequestState] = {}
        self._active_session_id: str | None = None
        self._active_turn_id: str | None = None
        self._active = local()

    @contextmanager
    def bind_turn(self, *, session_id: str, turn_id: str, runtime: OpenAgentRuntime | None = None) -> Iterator[None]:
        target_runtime = runtime or self.runtime
        previous_authorization_handler = target_runtime.authorization_request_handler
        previous_mode_switch_handler = target_runtime.mode_switch_request_handler
        previous_session_id = getattr(self._active, "session_id", None)
        previous_turn_id = getattr(self._active, "turn_id", None)
        with self._lock:
            self._active_session_id = session_id
            self._active_turn_id = turn_id
        self._active.session_id = session_id
        self._active.turn_id = turn_id
        target_runtime.authorization_request_handler = self._request_authorization
        target_runtime.mode_switch_request_handler = self._request_mode_switch
        try:
            yield
        finally:
            target_runtime.authorization_request_handler = previous_authorization_handler
            target_runtime.mode_switch_request_handler = previous_mode_switch_handler
            self._active.session_id = previous_session_id
            self._active.turn_id = previous_turn_id
            with self._lock:
                if self._active_turn_id == turn_id:
                    self._active_session_id = None
                    self._active_turn_id = None

    def pending_requests(self) -> list[InteractionRequestState]:
        with self._lock:
            return list(self._pending.values())

    def resolve_request(self, request_id: str, response: dict[str, Any]) -> bool:
        with self._lock:
            request = self._pending.pop(request_id, None)
        if request is None:
            return False
        request.response = dict(response)
        request.completed.set()
        return True

    def resolve_authorization(
        self,
        request_id: str,
        *,
        scope: str,
        approved: bool = True,
        reason: str = "",
    ) -> bool:
        normalized_scope = str(scope).strip().lower()
        if normalized_scope not in {"once", "workspace", "deny"}:
            raise ValueError("scope must be one of: once, workspace, deny.")
        status = "approved" if approved and normalized_scope in {"once", "workspace"} else "denied"
        resolved_scope = normalized_scope if status == "approved" else "deny"
        return self.resolve_request(
            request_id,
            {
                "status": status,
                "scope": resolved_scope,
                "reason": str(reason).strip(),
            },
        )

    def resolve_mode_switch(
        self,
        request_id: str,
        *,
        approved: bool,
        active_mode: str | None = None,
        reason: str = "",
    ) -> bool:
        resolved_mode = normalize_execution_mode(active_mode or getattr(self.runtime, "execution_mode", DEFAULT_EXECUTION_MODE))
        return self.resolve_request(
            request_id,
            {
                "approved": bool(approved),
                "active_mode": resolved_mode,
                "reason": str(reason).strip(),
            },
        )

    def cancel_turn_requests(self, turn_id: str, *, reason: str) -> int:
        with self._lock:
            to_cancel = [
                self._pending.pop(request_id)
                for request_id, request in list(self._pending.items())
                if request.turn_id == turn_id
            ]
        cancelled = 0
        current_mode = normalize_execution_mode(getattr(self.runtime, "execution_mode", DEFAULT_EXECUTION_MODE))
        for request in to_cancel:
            if request.kind == "authorization":
                request.response = {
                    "status": "denied",
                    "scope": "deny",
                    "reason": str(reason).strip(),
                }
            else:
                request.response = {
                    "approved": False,
                    "active_mode": current_mode,
                    "reason": str(reason).strip(),
                }
            request.completed.set()
            cancelled += 1
        return cancelled

    def _request_authorization(
        self,
        *,
        tool_name: str,
        reason: str,
        argument_summary: str = "",
        execution_mode: str = DEFAULT_EXECUTION_MODE,
    ) -> dict[str, str]:
        request = self._create_request(
            "authorization",
            {
                "tool_name": str(tool_name).strip(),
                "reason": str(reason).strip(),
                "argument_summary": str(argument_summary).strip(),
                "execution_mode": normalize_execution_mode(execution_mode),
            },
        )
        self._emit_event(
            AUTHORIZATION_REQUESTED,
            session_id=request.session_id,
            turn_id=request.turn_id,
            request_id=request.id,
            **request.payload,
        )
        if not request.completed.wait(timeout=self.REQUEST_TIMEOUT_SECONDS):
            with self._lock:
                self._pending.pop(request.id, None)
            return {
                "status": "denied",
                "scope": "deny",
                "reason": "Authorization request timed out.",
            }
        return request.response or {
            "status": "denied",
            "scope": "deny",
            "reason": "Authorization denied.",
        }

    def _request_mode_switch(
        self,
        *,
        target_mode: str,
        reason: str = "",
        current_mode: str = DEFAULT_EXECUTION_MODE,
    ) -> dict[str, Any]:
        request = self._create_request(
            "mode_switch",
            {
                "target_mode": normalize_execution_mode(target_mode),
                "current_mode": normalize_execution_mode(current_mode),
                "reason": str(reason).strip(),
            },
        )
        self._emit_event(
            MODE_SWITCH_REQUESTED,
            session_id=request.session_id,
            turn_id=request.turn_id,
            request_id=request.id,
            **request.payload,
        )
        if not request.completed.wait(timeout=self.REQUEST_TIMEOUT_SECONDS):
            with self._lock:
                self._pending.pop(request.id, None)
            return {
                "approved": False,
                "active_mode": normalize_execution_mode(current_mode),
                "reason": "Mode switch request timed out.",
            }
        response = request.response or {
            "approved": False,
            "active_mode": normalize_execution_mode(current_mode),
            "reason": "Mode switch denied.",
        }
        self.runtime.execution_mode = normalize_execution_mode(response.get("active_mode", current_mode))
        return response

    def _create_request(self, kind: str, payload: dict[str, Any]) -> InteractionRequestState:
        session_id = getattr(self._active, "session_id", None) or self._active_session_id
        turn_id = getattr(self._active, "turn_id", None) or self._active_turn_id
        with self._lock:
            request = InteractionRequestState(
                id=uuid.uuid4().hex[:8],
                kind=kind,
                session_id=session_id,
                turn_id=turn_id,
                payload=dict(payload),
            )
            self._pending[request.id] = request
        return request
