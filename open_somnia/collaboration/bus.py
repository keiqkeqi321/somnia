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
    ) -> str:
        payload = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": now_ts(),
        }
        if extra:
            payload.update(extra)
        self.store.send(recipient, payload)
        with self._condition:
            self._condition.notify_all()
        return f"Sent {msg_type} to {recipient}"

    def peek_inbox(self, recipient: str) -> list[dict]:
        peek = getattr(self.store, "peek", None)
        if not callable(peek):
            return []
        return peek(recipient)

    def has_inbox_messages(self, recipient: str) -> bool:
        return bool(self.peek_inbox(recipient))

    def read_inbox(self, recipient: str) -> list[dict]:
        return self.store.read_and_drain(recipient)

    def wait_for_inbox(
        self,
        recipient: str,
        *,
        timeout_seconds: float = 30,
        poll_interval_seconds: float = 0.2,
        should_interrupt: Callable[[], bool] | None = None,
    ) -> list[dict]:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        poll_interval = max(0.05, float(poll_interval_seconds))
        while True:
            if should_interrupt is not None and should_interrupt():
                raise TurnInterrupted("Interrupted by user.")
            messages = self.read_inbox(recipient)
            if messages:
                return messages
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            with self._condition:
                self._condition.wait(timeout=min(poll_interval, remaining))

    def broadcast(self, sender: str, content: str, names: list[str]) -> str:
        sent = 0
        for name in names:
            if name == sender:
                continue
            self.send(sender, name, content, "broadcast")
            sent += 1
        return f"Broadcast to {sent} teammates"
