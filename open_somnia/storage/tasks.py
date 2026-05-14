"""任务存储模块.

提供任务的持久化存储和管理功能。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from open_somnia.storage.common import get_lock, now_ts, read_json, write_json


class TaskStore:
    """任务存储类.

    管理任务的创建、读取、更新和删除操作。

    Attributes:
        root: 任务存储的根目录路径。
        meta_path: 元数据文件路径。
    """

    def __init__(self, root: Path) -> None:
        """初始化任务存储.

        Args:
            root: 任务存储的根目录路径。
        """
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.root / "meta.json"

    def _task_path(self, task_id: int) -> Path:
        """获取任务文件路径.

        Args:
            task_id: 任务ID。

        Returns:
            任务文件的完整路径。
        """
        return self.root / f"task_{task_id}.json"

    def _next_id(self) -> int:
        """获取下一个任务ID.

        Returns:
            新的任务ID。
        """
        with get_lock(self.meta_path):
            meta = read_json(self.meta_path, {"next_id": 1})
            task_id = int(meta.get("next_id", 1))
            meta["next_id"] = task_id + 1
            write_json(self.meta_path, meta)
            return task_id

    def create(
        self,
        subject: str,
        description: str = "",
        *,
        preferred_owner: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """创建新任务.

        Args:
            subject: 任务主题。
            description: 任务描述。

        Returns:
            创建的任务字典。
        """
        task_id = self._next_id()
        task = {
            "id": task_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "owner": None,
            "preferred_owner": preferred_owner.strip() if isinstance(preferred_owner, str) and preferred_owner.strip() else None,
            "session_id": session_id.strip() if isinstance(session_id, str) and session_id.strip() else None,
            "blockedBy": [],
            "blocks": [],
            "created_at": now_ts(),
            "updated_at": now_ts(),
        }
        self.save(task)
        return task

    def save(self, task: dict[str, Any]) -> None:
        task = dict(task)
        task["updated_at"] = now_ts()
        write_json(self._task_path(int(task["id"])), task)

    def _matches_session(self, task: dict[str, Any], session_id: str | None) -> bool:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return True
        return task.get("session_id") == normalized_session_id

    def get(self, task_id: int, session_id: str | None = None) -> dict[str, Any]:
        path = self._task_path(task_id)
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        task = json.loads(path.read_text(encoding="utf-8"))
        if not self._matches_session(task, session_id):
            raise ValueError(f"Task {task_id} not found in this session")
        return task

    def list_all(self, session_id: str | None = None) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("task_*.json")):
            task = json.loads(path.read_text(encoding="utf-8"))
            if self._matches_session(task, session_id):
                tasks.append(task)
        return tasks

    def update(
        self,
        task_id: int,
        status: str | None = None,
        add_blocked_by: list[int] | None = None,
        add_blocks: list[int] | None = None,
        preferred_owner: str | None | object = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        task = self.get(task_id, session_id=session_id)
        if status == "deleted":
            path = self._task_path(task_id)
            if path.exists():
                path.unlink()
            return None
        if status:
            task["status"] = status
        if add_blocked_by:
            task["blockedBy"] = sorted(set(task.get("blockedBy", []) + add_blocked_by))
        if add_blocks:
            task["blocks"] = sorted(set(task.get("blocks", []) + add_blocks))
        if preferred_owner is not None:
            task["preferred_owner"] = (
                preferred_owner.strip() if isinstance(preferred_owner, str) and preferred_owner.strip() else None
            )
        self.save(task)
        if status == "completed":
            for other in self.list_all(session_id=session_id):
                if task_id in other.get("blockedBy", []):
                    other["blockedBy"] = [item for item in other.get("blockedBy", []) if item != task_id]
                    self.save(other)
        return task

    def claim(self, task_id: int, owner: str, session_id: str | None = None) -> dict[str, Any]:
        task = self.get(task_id, session_id=session_id)
        task["owner"] = owner
        task["status"] = "in_progress"
        self.save(task)
        return task

    def list_owned_open(self, owner: str, session_id: str | None = None) -> list[dict[str, Any]]:
        owner_name = str(owner).strip()
        if not owner_name:
            return []
        return [
            task
            for task in self.list_all(session_id=session_id)
            if task.get("owner") == owner_name and task.get("status") in {"pending", "in_progress"}
        ]

    def has_open_task(self, owner: str, session_id: str | None = None) -> bool:
        return bool(self.list_owned_open(owner, session_id=session_id))

    def list_claimable(self, session_id: str | None = None) -> list[dict[str, Any]]:
        normalized_session_id = str(session_id or "").strip()
        return [
            task
            for task in self.list_all()
            if task.get("status") == "pending" and not task.get("owner") and not task.get("blockedBy")
            and (not normalized_session_id or task.get("session_id") == normalized_session_id)
        ]

    def list_claimable_for(self, owner: str, session_id: str | None = None) -> list[dict[str, Any]]:
        owner_name = str(owner).strip()
        claimable = self.list_claimable(session_id=session_id)
        preferred = [task for task in claimable if task.get("preferred_owner") == owner_name]
        neutral = [task for task in claimable if not task.get("preferred_owner")]
        return preferred + neutral
