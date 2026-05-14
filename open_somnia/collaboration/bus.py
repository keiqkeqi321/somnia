from __future__ import annotations

import time
from threading import Condition
from typing import Callable

from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.storage.common import now_ts
from open_somnia.storage.inbox import InboxStore


class MessageBus:
    def __init__(self, store: InboxStore):
        self.store = store
        self._condition = Condition()

    def send(
        self,
        sender: str,
        recipient: str,
        content: str,
        msg_type: str = "message",
        extra: dict | None = None,
        session_id: str | None = None,
    ) -> str:
        payload = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": now_ts(),
        }
        normalized_session_id = str(session_id or "").strip()
        if normalized_session_id:
            payload["session_id"] = normalized_session_id
        if extra:
            payload.update(extra)
        self.store.send(recipient, payload)
        with self._condition:
            self._condition.notify_all()
        return f"Sent {msg_type} to {recipient}"

    def peek_inbox(self, recipient: str, session_id: str | None = None) -> list[dict]:
        peek = getattr(self.store, "peek", None)
        if not callable(peek):
            return []
        return peek(recipient, session_id=session_id)

    def has_inbox_messages(self, recipient: str, session_id: str | None = None) -> bool:
        return bool(self.peek_inbox(recipient, session_id=session_id))

    def read_inbox(self, recipient: str, session_id: str | None = None) -> list[dict]:
        return self.store.read_and_drain(recipient, session_id=session_id)

    def wait_for_inbox(
        self,
        recipient: str,
        *,
        session_id: str | None = None,
        timeout_seconds: float = 30,
        poll_interval_seconds: float = 0.2,
        should_interrupt: Callable[[], bool] | None = None,
    ) -> list[dict]:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        poll_interval = max(0.05, float(poll_interval_seconds))
        while True:
            if should_interrupt is not None and should_interrupt():
                raise TurnInterrupted("Interrupted by user.")
            messages = self.read_inbox(recipient, session_id=session_id)
            if messages:
                return messages
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            with self._condition:
                self._condition.wait(timeout=min(poll_interval, remaining))

    def broadcast(self, sender: str, content: str, names: list[str], session_id: str | None = None) -> str:
        sent = 0
        for name in names:
            if name == sender:
                continue
            self.send(sender, name, content, "broadcast", session_id=session_id)
            sent += 1
        return f"Broadcast to {sent} teammates"
