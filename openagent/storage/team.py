from __future__ import annotations

import json
import re
from pathlib import Path

from openagent.storage.common import append_jsonl, read_json, read_jsonl, write_json


class TeamStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "team.json"
        self.logs_root = self.root / "logs"
        self.logs_root.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        return read_json(self.path, {"team_name": "default", "members": []})

    def save(self, payload: dict) -> None:
        write_json(self.path, payload)

    def _log_filename(self, name: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name).strip()).strip("._") or "teammate"
        return f"{slug}.jsonl"

    def log_path(self, name: str) -> Path:
        return self.logs_root / self._log_filename(name)

    def reset_log(self, name: str, payload: dict) -> None:
        path = self.log_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, default=str) + "\n", encoding="utf-8")

    def append_log(self, name: str, payload: dict) -> None:
        append_jsonl(self.log_path(name), payload)

    def read_log(self, name: str) -> list[dict]:
        return read_jsonl(self.log_path(name))
