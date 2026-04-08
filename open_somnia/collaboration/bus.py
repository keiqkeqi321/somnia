from __future__ import annotations

from open_somnia.storage.common import now_ts
from open_somnia.storage.inbox import InboxStore


class MessageBus:
    def __init__(self, store: InboxStore):
        self.store = store

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
        return f"Sent {msg_type} to {recipient}"

    def read_inbox(self, recipient: str) -> list[dict]:
        return self.store.read_and_drain(recipient)

    def broadcast(self, sender: str, content: str, names: list[str]) -> str:
        sent = 0
        for name in names:
            if name == sender:
                continue
            self.send(sender, name, content, "broadcast")
            sent += 1
        return f"Broadcast to {sent} teammates"
