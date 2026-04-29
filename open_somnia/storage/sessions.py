from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from open_somnia.storage.common import now_ts, read_json, write_json


class SessionStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def create(self) -> dict[str, Any]:
        session_id = uuid.uuid4().hex[:12]
        payload = {
            "id": session_id,
            "created_at": now_ts(),
            "updated_at": now_ts(),
            "messages": [],
            "token_usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
            "todo_items": [],
            "rounds_without_todo": 0,
            "latest_turn_id": None,
            "last_turn_file_changes": [],
            "undo_stack": [],
        }
        self.save(payload)
        index = read_json(self.index_path, {"latest": None, "items": []})
        index["latest"] = session_id
        if session_id not in index["items"]:
            index["items"].append(session_id)
        write_json(self.index_path, index)
        return payload

    def save(self, session: dict[str, Any]) -> None:
        session = dict(session)
        if "created_at" not in session or session["created_at"] is None:
            existing = read_json(self._path(session["id"]), {})
            session["created_at"] = existing.get("created_at", now_ts())
        session["updated_at"] = now_ts()
        write_json(self._path(session["id"]), session)

    def load(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        if not path.exists():
            raise ValueError(f"Unknown session '{session_id}'")
        return json.loads(path.read_text(encoding="utf-8"))

    def latest(self) -> dict[str, Any] | None:
        index = read_json(self.index_path, {"latest": None, "items": []})
        latest = index.get("latest")
        if not latest:
            return None
        path = self._path(latest)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_all(self) -> list[dict[str, Any]]:
        index = read_json(self.index_path, {"latest": None, "items": []})
        sessions: list[dict[str, Any]] = []
        for session_id in index.get("items", []):
            path = self._path(session_id)
            if not path.exists():
                continue
            sessions.append(json.loads(path.read_text(encoding="utf-8")))
        sessions.sort(key=lambda item: (float(item.get("updated_at") or 0), float(item.get("created_at") or 0)), reverse=True)
        return sessions

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if not path.exists():
            return False
        path.unlink()
        index = read_json(self.index_path, {"latest": None, "items": []})
        items = [item for item in index.get("items", []) if item != session_id]
        index["items"] = items
        index["latest"] = items[-1] if items else None
        write_json(self.index_path, index)
        return True
