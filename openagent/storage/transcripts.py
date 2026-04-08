from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openagent.storage.common import append_jsonl, atomic_write_text


class TranscriptStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def transcript_path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.jsonl"

    def snapshot_path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.snapshot.json"

    def append(self, session_id: str, entry: dict[str, Any]) -> None:
        append_jsonl(self.transcript_path(session_id), entry)

    def save_snapshot(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        atomic_write_text(
            self.snapshot_path(session_id),
            json.dumps(messages, indent=2, ensure_ascii=False, default=str),
        )

    def load_snapshot(self, session_id: str) -> list[dict[str, Any]]:
        path = self.snapshot_path(session_id)
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))
