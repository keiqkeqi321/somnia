from __future__ import annotations

from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.session import AgentSession


class SessionService:
    def __init__(self, runtime: OpenAgentRuntime) -> None:
        self.runtime = runtime

    def create_session(self) -> AgentSession:
        return self.runtime.create_session()

    def list_sessions(self) -> list[AgentSession]:
        return self.runtime.list_sessions()

    def load_session(self, session_id: str) -> AgentSession:
        return self.runtime.load_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        return self.runtime.delete_session(session_id)
