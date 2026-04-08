from __future__ import annotations

import queue
import subprocess
import threading
import uuid
from typing import Any

from open_somnia.storage.common import now_ts
from open_somnia.tools.process import run_command
from open_somnia.tools.registry import ToolDefinition


class BackgroundManager:
    def __init__(self, job_store, workspace_root, default_timeout: int, max_output_chars: int):
        self.job_store = job_store
        self.workspace_root = workspace_root
        self.default_timeout = default_timeout
        self.max_output_chars = max_output_chars
        self.notifications: queue.Queue[dict[str, Any]] = queue.Queue()

    def run(self, command: str, timeout: int | None = None) -> str:
        job_id = uuid.uuid4().hex[:8]
        self.job_store.create(
            job_id,
            {
                "id": job_id,
                "command": command,
                "status": "running",
                "result": None,
                "created_at": now_ts(),
            },
        )
        thread = threading.Thread(target=self._execute, args=(job_id, command, timeout or self.default_timeout), daemon=True)
        thread.start()
        return f"Background task {job_id} started: {command[:80]}"

    def _execute(self, job_id: str, command: str, timeout: int) -> None:
        try:
            completed = run_command(
                command,
                shell=True,
                cwd=self.workspace_root,
                timeout=timeout,
            )
            result = completed.combined_output().strip() or "(no output)"
            job = self.job_store.update(job_id, status="completed", result=result[: self.max_output_chars])
        except Exception as exc:
            job = self.job_store.update(job_id, status="error", result=str(exc))
        notification = {
            "task_id": job_id,
            "status": job["status"],
            "result": str(job.get("result", ""))[:500],
        }
        self.notifications.put(notification)
        self.job_store.notify(notification)

    def check(self, job_id: str | None = None) -> str:
        if job_id:
            job = self.job_store.get(job_id)
            if job is None:
                return f"Unknown background task: {job_id}"
            result = job.get("result") or "(running)"
            return f"[{job['status']}] {result}"
        jobs = self.job_store.list_all()
        if not jobs:
            return "No bg tasks."
        return "\n".join(
            f"{job_id}: [{job['status']}] {str(job['command'])[:60]}" for job_id, job in jobs.items()
        )

    def drain(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            try:
                items.append(self.notifications.get_nowait())
            except queue.Empty:
                return items


def register_background_tools(registry, background_manager: BackgroundManager) -> None:
    def background_run(ctx: Any, payload: dict[str, Any]) -> str:
        return background_manager.run(payload["command"], payload.get("timeout"))

    def background_check(ctx: Any, payload: dict[str, Any]) -> str:
        return background_manager.check(payload.get("task_id"))

    registry.register(
        ToolDefinition(
            name="background_run",
            description="Run a shell command in a background thread.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
            handler=background_run,
        )
    )
    registry.register(
        ToolDefinition(
            name="check_background",
            description="Check background task status.",
            input_schema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
            },
            handler=background_check,
        )
    )
