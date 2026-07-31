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

    def list_session_summaries(self) -> list[dict]:
        return self.runtime.list_session_summaries()

    def load_session(self, session_id: str) -> AgentSession:
        return self.runtime.load_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        return self.runtime.delete_session(session_id)

    def set_session_provider_model(
        self,
        session_id: str,
        provider_name: str | None,
        model: str | None,
    ) -> AgentSession:
        """Pin one session to a provider/model, or clear the pin.

        Pass ``None``/empty values to make the session follow the workspace
        default. Only this session's future turns are affected; other sessions
        and the global default keep running on their own model.
        """
        session = self.runtime.load_session(session_id)
        self.runtime.set_session_provider_model(session, provider_name, model)
        return session
