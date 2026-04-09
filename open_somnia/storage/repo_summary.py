from __future__ import annotations

from pathlib import Path
from typing import Any

from open_somnia.storage.common import now_ts, read_json, write_json


class RepoSummaryStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "repo_summary.json"

    def load(self) -> dict[str, Any]:
        return read_json(
            self.path,
            {
                "updated_at": None,
                "summary_text": "",
                "last_scan": None,
                "recent_symbol_queries": [],
            },
        )

    def save(self, payload: dict[str, Any]) -> None:
        write_json(self.path, payload)

    def update_scan(self, *, path: str, summary_text: str) -> dict[str, Any]:
        payload = self.load()
        payload["updated_at"] = now_ts()
        payload["summary_text"] = summary_text
        payload["last_scan"] = {
            "path": path,
            "summary_text": summary_text,
            "updated_at": payload["updated_at"],
        }
        self.save(payload)
        return payload

    def record_symbol_query(
        self,
        *,
        query: str,
        path: str,
        kind: str,
        match_count: int,
    ) -> dict[str, Any]:
        payload = self.load()
        history = list(payload.get("recent_symbol_queries", []))
        history.insert(
            0,
            {
                "query": query,
                "path": path,
                "kind": kind,
                "match_count": match_count,
                "updated_at": now_ts(),
            },
        )
        payload["recent_symbol_queries"] = history[:10]
        payload["updated_at"] = now_ts()
        self.save(payload)
        return payload
