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

    def read_and_drain(self, recipient: str) -> list[dict]:
        path = self._path(recipient)
        if not path.exists():
            return []
        with get_lock(path):
            lines = path.read_text(encoding="utf-8").splitlines()
            atomic_write_text(path, "")
        return [json.loads(line) for line in lines if line.strip()]
