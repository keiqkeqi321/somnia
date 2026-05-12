from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable

from open_somnia.storage.common import now_ts, read_json, write_json


class SessionStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def _load_index(self) -> dict[str, Any]:
        index = read_json(self.index_path, {"latest": None, "items": []})
        if not isinstance(index.get("items"), list):
            index["items"] = []
        if not isinstance(index.get("summaries"), dict):
            index["summaries"] = {}
        return index

    def _write_index(self, index: dict[str, Any]) -> None:
        write_json(self.index_path, index)

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            elif item.get("type") == "tool_result":
                text = item.get("raw_output", item.get("content"))
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()

    @classmethod
    def _visible_summary(cls, session: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        session_id = str(session.get("id", "")).strip()
        created_at = session.get("created_at")
        updated_at = session.get("updated_at")
        message_list = list(messages if messages is not None else session.get("messages", []) or [])
        has_user = False
        has_assistant = False
        preview = ""
        for message in message_list:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if role == "user" and isinstance(content, str):
                if content.startswith("<background-results>") or content.startswith("<inbox>"):
                    continue
                if content.strip():
                    has_user = True
            elif role == "assistant" and cls._content_text(content):
                has_assistant = True
            if has_user and has_assistant:
                break
        for message in reversed(message_list):
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if role == "user" and isinstance(content, str):
                if content.startswith("<background-results>") or content.startswith("<inbox>"):
                    continue
                text = content.strip()
            elif role == "assistant":
                text = cls._content_text(content)
            else:
                continue
            if text:
                preview = " ".join(text.split())[:80]
                break
        return {
            "id": session_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "has_visible_exchange": bool(has_user and has_assistant),
            "preview": preview or "[no visible messages]",
        }

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
        index = self._load_index()
        index["latest"] = session_id
        if session_id not in index["items"]:
            index["items"].append(session_id)
        index["summaries"][session_id] = self._visible_summary(payload)
        self._write_index(index)
        return payload

    def save(self, session: dict[str, Any]) -> None:
        session = dict(session)
        if "created_at" not in session or session["created_at"] is None:
            existing = read_json(self._path(session["id"]), {})
            session["created_at"] = existing.get("created_at", now_ts())
        session["updated_at"] = now_ts()
        write_json(self._path(session["id"]), session)
        index = self._load_index()
        session_id = str(session["id"])
        if session_id not in index["items"]:
            index["items"].append(session_id)
        index["summaries"][session_id] = self._visible_summary(session)
        self._write_index(index)

    def load(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        if not path.exists():
            raise ValueError(f"Unknown session '{session_id}'")
        return json.loads(path.read_text(encoding="utf-8"))

    def latest(self) -> dict[str, Any] | None:
        index = self._load_index()
        latest = index.get("latest")
        if not latest:
            return None
        path = self._path(latest)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_all(self) -> list[dict[str, Any]]:
        index = self._load_index()
        sessions: list[dict[str, Any]] = []
        for session_id in index.get("items", []):
            path = self._path(session_id)
            if not path.exists():
                continue
            sessions.append(json.loads(path.read_text(encoding="utf-8")))
        sessions.sort(key=lambda item: (float(item.get("updated_at") or 0), float(item.get("created_at") or 0)), reverse=True)
        return sessions

    def list_summaries(self, load_missing_messages: Callable[[str], list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
        index = self._load_index()
        summaries_by_id = dict(index.get("summaries", {}) or {})
        changed = False
        summaries: list[dict[str, Any]] = []
        for session_id in index.get("items", []):
            session_id = str(session_id)
            path = self._path(session_id)
            if not path.exists():
                continue
            summary = summaries_by_id.get(session_id)
            if (
                not isinstance(summary, dict)
                or str(summary.get("id", "")).strip() != session_id
                or "has_visible_exchange" not in summary
                or "preview" not in summary
            ):
                payload = json.loads(path.read_text(encoding="utf-8"))
                messages = list(payload.get("messages", []) or [])
                if not messages and load_missing_messages is not None:
                    messages = load_missing_messages(session_id)
                summary = self._visible_summary(payload, messages)
                summaries_by_id[session_id] = summary
                changed = True
            summaries.append(dict(summary))
        summaries.sort(key=lambda item: (float(item.get("updated_at") or 0), float(item.get("created_at") or 0)), reverse=True)
        if changed:
            index["summaries"] = summaries_by_id
            self._write_index(index)
        return summaries

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if not path.exists():
            return False
        path.unlink()
        index = self._load_index()
        items = [item for item in index.get("items", []) if item != session_id]
        index["items"] = items
        index["latest"] = items[-1] if items else None
        summaries = dict(index.get("summaries", {}) or {})
        summaries.pop(session_id, None)
        index["summaries"] = summaries
        self._write_index(index)
        return True
