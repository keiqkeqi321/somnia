from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from open_somnia.storage.common import append_jsonl, now_ts
from open_somnia.tools.tool_errors import serialize_tool_output


class ToolLogStore:
    def __init__(self, root: Path):
        self.root = root / "tool_logs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.jsonl"

    def write(
        self,
        *,
        actor: str,
        tool_name: str,
        tool_input: dict[str, Any],
        output: Any,
        category: str,
    ) -> dict[str, Any]:
        log_id = uuid.uuid4().hex[:8]
        payload = {
            "id": log_id,
            "timestamp": now_ts(),
            "actor": actor,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "output": serialize_tool_output(output),
            "category": category,
        }
        path = self.root / f"{log_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        append_jsonl(
            self.index_path,
            {
                "id": log_id,
                "timestamp": payload["timestamp"],
                "actor": actor,
                "tool_name": tool_name,
                "category": category,
                "path": str(path),
            },
        )
        return payload

    def get(self, log_id: str) -> dict[str, Any] | None:
        path = self.root / f"{log_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []
        lines = self.index_path.read_text(encoding="utf-8").splitlines()
        items = [json.loads(line) for line in lines if line.strip()]
        return list(reversed(items[-limit:]))
