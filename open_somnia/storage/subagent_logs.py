from __future__ import annotations

from pathlib import Path
from typing import Any

from open_somnia.storage.common import append_jsonl, read_jsonl


class SubagentLogStore:
    """Persistent event log for subagent executions, keyed by activity id.

    Subagents run without a session, so their assistant text, tool calls, and
    final summary would otherwise be lost once the activity event stream ends.
    Each execution gets a JSONL file under `logs/subagent_logs/` so the
    desktop can render a preview, mirroring the teammate log shape.
    """

    def __init__(self, root: Path) -> None:
        self.root = root / "subagent_logs"
        self.root.mkdir(parents=True, exist_ok=True)

    def log_path(self, activity_id: str) -> Path:
        return self.root / f"{activity_id}.jsonl"

    def append(self, activity_id: str, payload: dict[str, Any]) -> None:
        append_jsonl(self.log_path(activity_id), {"activity_id": activity_id, **payload})

    def read(self, activity_id: str) -> list[dict[str, Any]]:
        return [entry for entry in read_jsonl(self.log_path(activity_id)) if isinstance(entry, dict)]
