from __future__ import annotations

import json
from pathlib import Path

from open_somnia.storage.common import atomic_write_text, append_jsonl, get_lock


class InboxStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.jsonl"

    def send(self, recipient: str, payload: dict) -> None:
        append_jsonl(self._path(recipient), payload)

    def _matches_session(self, message: dict, session_id: str | None) -> bool:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return True
        return message.get("session_id") == normalized_session_id

    def peek(self, recipient: str, session_id: str | None = None) -> list[dict]:
        path = self._path(recipient)
        if not path.exists():
            return []
        with get_lock(path):
            lines = path.read_text(encoding="utf-8").splitlines()
        messages = [json.loads(line) for line in lines if line.strip()]
        return [message for message in messages if self._matches_session(message, session_id)]

    def read_and_drain(self, recipient: str, session_id: str | None = None) -> list[dict]:
        path = self._path(recipient)
        if not path.exists():
            return []
        with get_lock(path):
            lines = path.read_text(encoding="utf-8").splitlines()
            messages = [json.loads(line) for line in lines if line.strip()]
            matched = [message for message in messages if self._matches_session(message, session_id)]
            remaining = [message for message in messages if not self._matches_session(message, session_id)]
            if remaining:
                atomic_write_text(path, "".join(json.dumps(message, ensure_ascii=False) + "\n" for message in remaining))
            else:
                atomic_write_text(path, "")
        return matched
