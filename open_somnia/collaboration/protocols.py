from __future__ import annotations

import uuid
from pathlib import Path

from open_somnia.storage.common import now_ts, read_json, write_json


class RequestTracker:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.shutdown_path = self.root / "shutdown_requests.json"
        self.plan_path = self.root / "plan_requests.json"

    def _load(self, path: Path) -> dict:
        return read_json(path, {})

    def _save(self, path: Path, payload: dict) -> None:
        write_json(path, payload)

    def create_shutdown_request(self, target: str) -> dict:
        request_id = uuid.uuid4().hex[:8]
        payload = self._load(self.shutdown_path)
        payload[request_id] = {
            "request_id": request_id,
            "target": target,
            "status": "pending",
            "created_at": now_ts(),
        }
        self._save(self.shutdown_path, payload)
        return payload[request_id]

    def mark_shutdown_response(self, request_id: str, status: str) -> dict | None:
        payload = self._load(self.shutdown_path)
        item = payload.get(request_id)
        if not item:
            return None
        item["status"] = status
        item["updated_at"] = now_ts()
        self._save(self.shutdown_path, payload)
        return item

    def create_plan_request(self, sender: str, plan: str) -> dict:
        request_id = uuid.uuid4().hex[:8]
        payload = self._load(self.plan_path)
        payload[request_id] = {
            "request_id": request_id,
            "from": sender,
            "plan": plan,
            "status": "pending",
            "created_at": now_ts(),
        }
        self._save(self.plan_path, payload)
        return payload[request_id]

    def resolve_plan_request(self, request_id: str, approve: bool, feedback: str = "") -> dict | None:
        payload = self._load(self.plan_path)
        item = payload.get(request_id)
        if not item:
            return None
        item["status"] = "approved" if approve else "rejected"
        item["approve"] = approve
        item["feedback"] = feedback
        item["updated_at"] = now_ts()
        self._save(self.plan_path, payload)
        return item
