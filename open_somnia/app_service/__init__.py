from __future__ import annotations

from typing import Any

from open_somnia.config.models import AppSettings
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.session import AgentSession

from open_somnia.app_service.events import AppServiceEvent
from open_somnia.app_service.interaction_service import InteractionService
from open_somnia.app_service.models import (
    InteractionRequestState,
    ModelDescriptor,
    ProviderDescriptor,
    TurnHandle,
    TurnRunResult,
)
from open_somnia.app_service.provider_service import ProviderService
from open_somnia.app_service.runtime_host import RuntimeHost
from open_somnia.app_service.session_service import SessionService
from open_somnia.app_service.turn_service import TurnService


class AppService:
    def __init__(self, runtime: OpenAgentRuntime) -> None:
        self.runtime = runtime
        self.runtime_host = RuntimeHost(runtime)
        self.session_service = SessionService(runtime)
        self.turn_service = TurnService(self.runtime_host)
        self.provider_service = ProviderService(runtime)
        self.interaction_service = self.runtime_host.interaction_service

    @classmethod
    def from_settings(cls, settings: AppSettings) -> "AppService":
        return cls(OpenAgentRuntime(settings))

    def create_session(self) -> AgentSession:
        return self.session_service.create_session()

    def list_sessions(self) -> list[AgentSession]:
        return self.session_service.list_sessions()

    def load_session(self, session_id: str) -> AgentSession:
        return self.session_service.load_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        return self.session_service.delete_session(session_id)

    def run_turn(
        self,
        session: AgentSession,
        user_input: str | dict[str, Any],
        *,
        take_next_loop_user_message=None,
        prepare_next_loop_user_message=None,
    ) -> TurnHandle:
        return self.turn_service.run_turn(
            session,
            user_input,
            take_next_loop_user_message=take_next_loop_user_message,
            prepare_next_loop_user_message=prepare_next_loop_user_message,
        )

    def interrupt_turn(self, turn_id: str) -> bool:
        return self.turn_service.interrupt_turn(turn_id)

    def queue_loop_injection(self, turn_id: str, user_input: str | dict[str, Any], *, injection_id: str | None = None) -> bool:
        return self.turn_service.queue_loop_injection(turn_id, user_input, injection_id=injection_id)

    def switch_provider_model(self, provider_name: str, model: str) -> str:
        return self.provider_service.switch_provider_model(provider_name, model)

    def set_reasoning_level(self, reasoning_level: str | None) -> str:
        return self.provider_service.set_reasoning_level(reasoning_level)

    def recent_tool_logs(self, limit: int = 10) -> str:
        return self.runtime.recent_tool_logs(limit=limit)

    def render_tool_log(self, log_id: str) -> str:
        return self.runtime.render_tool_log(log_id)

    def list_providers(self) -> list[ProviderDescriptor]:
        return self.provider_service.list_providers()

    def list_models(self, provider_name: str | None = None) -> list[ModelDescriptor]:
        return self.provider_service.list_models(provider_name)

    def pending_interactions(self) -> list[InteractionRequestState]:
        return self.interaction_service.pending_requests()

    def resolve_interaction(self, request_id: str, response: dict[str, Any]) -> bool:
        return self.interaction_service.resolve_request(request_id, response)

    def resolve_authorization(
        self,
        request_id: str,
        *,
        scope: str,
        approved: bool = True,
        reason: str = "",
    ) -> bool:
        return self.interaction_service.resolve_authorization(
            request_id,
            scope=scope,
            approved=approved,
            reason=reason,
        )

    def resolve_mode_switch(
        self,
        request_id: str,
        *,
        approved: bool,
        active_mode: str | None = None,
        reason: str = "",
    ) -> bool:
        return self.interaction_service.resolve_mode_switch(
            request_id,
            approved=approved,
            active_mode=active_mode,
            reason=reason,
        )

    def close(self) -> None:
        self.runtime_host.close()
        self.runtime.close()


__all__ = [
    "AppService",
    "AppServiceEvent",
    "InteractionRequestState",
    "InteractionService",
    "ModelDescriptor",
    "ProviderDescriptor",
    "RuntimeHost",
    "SessionService",
    "TurnHandle",
    "TurnRunResult",
    "TurnService",
]
