from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from open_somnia.storage.common import append_jsonl, now_ts, read_json, write_json


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_path = self.root / "jobs.json"
        self.notifications_path = self.root / "notifications.jsonl"

    def create(self, job_id: str, payload: dict[str, Any]) -> None:
        jobs = read_json(self.jobs_path, {})
        jobs[job_id] = payload
        write_json(self.jobs_path, jobs)

    def update(self, job_id: str, **updates: Any) -> dict[str, Any]:
        jobs = read_json(self.jobs_path, {})
        job = dict(jobs.get(job_id, {}))
        job.update(updates)
        job["updated_at"] = now_ts()
        jobs[job_id] = job
        write_json(self.jobs_path, jobs)
        return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        jobs = read_json(self.jobs_path, {})
        return jobs.get(job_id)

    def list_all(self) -> dict[str, Any]:
        return read_json(self.jobs_path, {})

    def notify(self, payload: dict[str, Any]) -> None:
        append_jsonl(self.notifications_path, payload)
