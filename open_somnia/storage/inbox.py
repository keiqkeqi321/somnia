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

    def _normalize_session_id(self, session_id: str | None) -> str | None:
        value = str(session_id or "").strip()
        return value or None

    def _session_path(self, name: str, session_id: str) -> Path:
        return self.root / "sessions" / session_id / f"{name}.jsonl"

    def _path_for_payload(self, recipient: str, payload: dict) -> Path:
        session_id = self._normalize_session_id(payload.get("session_id"))
        if session_id:
            return self._session_path(recipient, session_id)
        return self._path(recipient)

    def send(self, recipient: str, payload: dict) -> None:
        append_jsonl(self._path_for_payload(recipient, payload), payload)

    def _matches_session(self, message: dict, session_id: str | None) -> bool:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return True
        return message.get("session_id") == normalized_session_id

    def peek(self, recipient: str, session_id: str | None = None) -> list[dict]:
        normalized_session_id = self._normalize_session_id(session_id)
        messages: list[dict] = []
        if normalized_session_id:
            session_path = self._session_path(recipient, normalized_session_id)
            messages.extend(self._read_messages(session_path))
        messages.extend(
            message
            for message in self._read_messages(self._path(recipient))
            if self._matches_session(message, session_id)
        )
        return messages

    def read_and_drain(self, recipient: str, session_id: str | None = None) -> list[dict]:
        normalized_session_id = self._normalize_session_id(session_id)
        matched: list[dict] = []
        if normalized_session_id:
            matched.extend(self._drain_path(self._session_path(recipient, normalized_session_id)))
        else:
            for path in sorted((self.root / "sessions").glob(f"*/{recipient}.jsonl")):
                matched.extend(self._drain_path(path))
        matched.extend(self._drain_legacy_path(recipient, session_id=session_id))
        return matched

    def _read_messages(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        with get_lock(path):
            lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def _drain_path(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        with get_lock(path):
            lines = path.read_text(encoding="utf-8").splitlines()
            messages = [json.loads(line) for line in lines if line.strip()]
            atomic_write_text(path, "")
        return messages

    def _drain_legacy_path(self, recipient: str, session_id: str | None = None) -> list[dict]:
        path = self._path(recipient)
        if not path.exists():
            return []
        with get_lock(path):
            lines = path.read_text(encoding="utf-8").splitlines()
            messages = [json.loads(line) for line in lines if line.strip()]
            matched = [message for message in messages if self._matches_session(message, session_id)]
            remaining = [message for message in messages if not self._matches_session(message, session_id)]
            atomic_write_text(path, "".join(json.dumps(message, ensure_ascii=False) + "\n" for message in remaining))
        return matched
