from __future__ import annotations

import json
import re
from pathlib import Path

from open_somnia.storage.common import append_jsonl, read_json, read_jsonl, write_json


class TeamStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "team.json"
        self.logs_root = self.root / "logs"
        self.logs_root.mkdir(parents=True, exist_ok=True)

    def _normalize_session_id(self, session_id: str | None) -> str | None:
        value = str(session_id or "").strip()
        return value or None

    def _session_root(self, session_id: str) -> Path:
        return self.root / "sessions" / session_id

    def _session_path(self, session_id: str) -> Path:
        return self._session_root(session_id) / "team.json"

    def _session_logs_root(self, session_id: str) -> Path:
        return self._session_root(session_id) / "logs"

    def load(self, session_id: str | None = None) -> dict:
        normalized_session_id = self._normalize_session_id(session_id)
        if normalized_session_id:
            session_path = self._session_path(normalized_session_id)
            if session_path.exists():
                return read_json(session_path, {"team_name": "default", "members": []})
            legacy = read_json(self.path, {"team_name": "default", "members": []})
            return {
                "team_name": legacy.get("team_name", "default"),
                "members": [
                    member
                    for member in legacy.get("members", [])
                    if member.get("session_id") == normalized_session_id
                ],
            }
        payload = read_json(self.path, {"team_name": "default", "members": []})
        seen: set[tuple[str, str | None]] = {
            (str(member.get("name", "")), self._normalize_session_id(member.get("session_id")))
            for member in payload.get("members", [])
        }
        for path in sorted((self.root / "sessions").glob("*/team.json")):
            session_payload = read_json(path, {"team_name": "default", "members": []})
            for member in session_payload.get("members", []):
                key = (str(member.get("name", "")), self._normalize_session_id(member.get("session_id")))
                if key in seen:
                    continue
                payload.setdefault("members", []).append(member)
                seen.add(key)
        return payload

    def load_legacy(self) -> dict:
        return read_json(self.path, {"team_name": "default", "members": []})

    def save(self, payload: dict, session_id: str | None = None) -> None:
        normalized_session_id = self._normalize_session_id(session_id)
        if normalized_session_id:
            payload = dict(payload)
            payload["members"] = [
                {**member, "session_id": member.get("session_id") or normalized_session_id}
                for member in payload.get("members", [])
            ]
            write_json(self._session_path(normalized_session_id), payload)
            return
        write_json(self.path, payload)

    def _log_filename(self, name: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name).strip()).strip("._") or "teammate"
        return f"{slug}.jsonl"

    def log_path(self, name: str, session_id: str | None = None) -> Path:
        normalized_session_id = self._normalize_session_id(session_id)
        if normalized_session_id:
            return self._session_logs_root(normalized_session_id) / self._log_filename(name)
        return self.logs_root / self._log_filename(name)

    def reset_log(self, name: str, payload: dict, session_id: str | None = None) -> None:
        path = self.log_path(name, session_id=session_id or payload.get("session_id"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, default=str) + "\n", encoding="utf-8")

    def append_log(self, name: str, payload: dict, session_id: str | None = None) -> None:
        normalized_session_id = self._normalize_session_id(session_id or payload.get("session_id"))
        append_jsonl(self.log_path(name, session_id=normalized_session_id), payload)
        legacy_path = self.log_path(name)
        if normalized_session_id and legacy_path.exists():
            append_jsonl(legacy_path, payload)

    def read_log(self, name: str, session_id: str | None = None) -> list[dict]:
        normalized_session_id = self._normalize_session_id(session_id)
        entries: list[dict] = []
        if normalized_session_id:
            entries.extend(read_jsonl(self.log_path(name, session_id=normalized_session_id)))
            if entries:
                return entries
            legacy_entries = [
                entry
                for entry in read_jsonl(self.log_path(name))
                if not entry.get("session_id") or entry.get("session_id") == normalized_session_id
            ]
            entries.extend(legacy_entries)
            return entries
        entries.extend(read_jsonl(self.log_path(name)))
        for path in sorted((self.root / "sessions").glob(f"*/logs/{self._log_filename(name)}")):
            entries.extend(read_jsonl(path))
        return entries
