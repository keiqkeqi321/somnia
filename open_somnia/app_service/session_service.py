from __future__ import annotations

from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.session import AgentSession


class SessionService:
    def __init__(self, runtime: OpenAgentRuntime) -> None:
        self.runtime = runtime

    def create_session(self) -> AgentSession:
        return self.runtime.create_session()

    def new_session(self, session: AgentSession) -> AgentSession:
        """Start a fresh session that succeeds ``session`` and inherits its
        provider/model pin.

        The old session is left untouched and stays resumable; the fresh one
        is validated and persisted by ``set_session_provider_model`` when a
        pin exists.
        """
        fresh = self.create_session()
        provider = getattr(session, "provider_override", None)
        model = getattr(session, "model_override", None)
        if provider and model:
            self.runtime.set_session_provider_model(fresh, provider, model)
        return fresh

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
        *,
        reasoning_level: str | None = None,
    ) -> AgentSession:
        """Pin one session to a provider/model, or clear the pin.

        Pass ``None``/empty values to make the session follow the workspace
        default. Only this session's future turns are affected; other sessions
        and the global default keep running on their own model.
        ``reasoning_level`` (when not ``None``) updates the pinned model's
        stored reasoning level; ``"auto"`` clears it.
        """
        session = self.runtime.load_session(session_id)
        self.runtime.set_session_provider_model(session, provider_name, model, reasoning_level=reasoning_level)
        return session
