from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from open_somnia.storage.sessions import SessionStore
from open_somnia.storage.transcripts import TranscriptStore


@dataclass(slots=True)
class AgentSession:
    id: str
    created_at: float | None = None
    updated_at: float | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)
    todo_items: list[dict[str, Any]] = field(default_factory=list)
    rounds_without_todo: int = 0
    latest_turn_id: str | None = None
    last_turn_file_changes: list[dict[str, Any]] = field(default_factory=list)
    undo_stack: list[dict[str, Any]] = field(default_factory=list)
    pending_file_changes: list[dict[str, Any]] = field(default_factory=list, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AgentSession":
        return cls(
            id=payload["id"],
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
            messages=list(payload.get("messages", [])),
            token_usage=dict(payload.get("token_usage", {})),
            todo_items=list(payload.get("todo_items", [])),
            rounds_without_todo=int(payload.get("rounds_without_todo", 0)),
            latest_turn_id=payload.get("latest_turn_id"),
            last_turn_file_changes=list(payload.get("last_turn_file_changes", [])),
            undo_stack=list(payload.get("undo_stack", [])),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": self.messages,
            "token_usage": self.token_usage,
            "todo_items": self.todo_items,
            "rounds_without_todo": self.rounds_without_todo,
            "latest_turn_id": self.latest_turn_id,
            "last_turn_file_changes": self.last_turn_file_changes,
            "undo_stack": self.undo_stack,
        }


class SessionManager:
    def __init__(self, session_store: SessionStore, transcript_store: TranscriptStore):
        self.session_store = session_store
        self.transcript_store = transcript_store

    def create(self) -> AgentSession:
        return AgentSession.from_payload(self.session_store.create())

    def latest_or_create(self) -> AgentSession:
        latest = self.session_store.latest()
        if latest is None:
            return self.create()
        return AgentSession.from_payload(latest)

    def load(self, session_id: str) -> AgentSession:
        payload = self.session_store.load(session_id)
        if not payload.get("messages"):
            payload["messages"] = self.transcript_store.load_snapshot(session_id)
        return AgentSession.from_payload(payload)

    def list_all(self) -> list[AgentSession]:
        sessions: list[AgentSession] = []
        for payload in self.session_store.list_all():
            if not payload.get("messages"):
                payload["messages"] = self.transcript_store.load_snapshot(payload["id"])
            sessions.append(AgentSession.from_payload(payload))
        return sessions

    def save(self, session: AgentSession) -> None:
        self.session_store.save(session.to_payload())
        self.transcript_store.save_snapshot(session.id, session.messages)
