from __future__ import annotations

from typing import Any

from open_somnia.app_service.models import TurnHandle
from open_somnia.app_service.runtime_host import RuntimeHost
from open_somnia.runtime.session import AgentSession


class TurnService:
    def __init__(self, runtime_host: RuntimeHost) -> None:
        self.runtime_host = runtime_host

    def run_turn(
        self,
        session: AgentSession,
        user_input: str | dict[str, Any],
        *,
        take_next_loop_user_message=None,
        prepare_next_loop_user_message=None,
    ) -> TurnHandle:
        return self.runtime_host.run_turn(
            session,
            user_input,
            take_next_loop_user_message=take_next_loop_user_message,
            prepare_next_loop_user_message=prepare_next_loop_user_message,
        )

    def interrupt_turn(self, turn_id: str) -> bool:
        return self.runtime_host.interrupt_turn(turn_id)
