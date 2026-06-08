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

    def _normalize_session_id(self, session_id: str | None) -> str | None:
        value = str(session_id or "").strip()
        return value or None

    def _session_root(self, session_id: str) -> Path:
        return self.root / "sessions" / session_id

    def _session_task_path(self, task_id: int, session_id: str) -> Path:
        return self._session_root(session_id) / f"task_{task_id}.json"

    def _path_for_task(self, task: dict[str, Any]) -> Path:
        session_id = self._normalize_session_id(task.get("session_id"))
        if session_id:
            return self._session_task_path(int(task["id"]), session_id)
        return self._task_path(int(task["id"]))

    def _read_task_file(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _locate_task_path(self, task_id: int, session_id: str | None = None) -> Path | None:
        normalized_session_id = self._normalize_session_id(session_id)
        if normalized_session_id:
            session_path = self._session_task_path(task_id, normalized_session_id)
            if session_path.exists():
                return session_path
            legacy_path = self._task_path(task_id)
            if legacy_path.exists():
                task = self._read_task_file(legacy_path)
                if self._matches_session(task, normalized_session_id):
                    return legacy_path
            return None
        legacy_path = self._task_path(task_id)
        if legacy_path.exists():
            return legacy_path
        for path in sorted((self.root / "sessions").glob(f"*/task_{task_id}.json")):
            if path.is_file():
                return path
        return None

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

    def _next_ids(self, count: int) -> list[int]:
        if count <= 0:
            return []
        with get_lock(self.meta_path):
            meta = read_json(self.meta_path, {"next_id": 1})
            first_id = int(meta.get("next_id", 1))
            meta["next_id"] = first_id + count
            write_json(self.meta_path, meta)
            return list(range(first_id, first_id + count))

    def _auto_assign_key(self, session_id: str | None) -> str:
        return self._normalize_session_id(session_id) or "__global__"

    def set_auto_assign_paused(self, session_id: str | None, paused: bool) -> None:
        with get_lock(self.meta_path):
            meta = read_json(self.meta_path, {"next_id": 1})
            paused_sessions = dict(meta.get("auto_assign_paused", {}) or {})
            key = self._auto_assign_key(session_id)
            if paused:
                paused_sessions[key] = True
            else:
                paused_sessions.pop(key, None)
            meta["auto_assign_paused"] = paused_sessions
            write_json(self.meta_path, meta)

    def is_auto_assign_paused(self, session_id: str | None = None) -> bool:
        meta = read_json(self.meta_path, {"next_id": 1})
        paused_sessions = dict(meta.get("auto_assign_paused", {}) or {})
        return bool(paused_sessions.get(self._auto_assign_key(session_id)))

    def _normalize_task_ids(self, values: list[int] | None) -> list[int]:
        ids: list[int] = []
        for value in values or []:
            try:
                task_id = int(value)
            except (TypeError, ValueError):
                continue
            if task_id > 0:
                ids.append(task_id)
        return sorted(set(ids))

    def _incomplete_blockers(self, task: dict[str, Any], session_id: str | None = None) -> list[int]:
        incomplete: list[int] = []
        for blocker_id in self._normalize_task_ids(task.get("blockedBy", [])):
            try:
                blocker = self.get(blocker_id, session_id=session_id)
            except ValueError:
                incomplete.append(blocker_id)
                continue
            if blocker.get("status") != "completed":
                incomplete.append(blocker_id)
        return incomplete

    def incomplete_blockers(self, task_id: int, session_id: str | None = None) -> list[int]:
        return self._incomplete_blockers(self.get(task_id, session_id=session_id), session_id=session_id)

    def _assert_no_cycle(self, task_id: int, blocked_by: list[int], session_id: str | None = None) -> None:
        target = int(task_id)

        def visit(current_id: int, seen: set[int]) -> bool:
            if current_id == target:
                return True
            if current_id in seen:
                return False
            seen.add(current_id)
            try:
                current = self.get(current_id, session_id=session_id)
            except ValueError:
                return False
            return any(visit(next_id, seen) for next_id in self._normalize_task_ids(current.get("blockedBy", [])))

        for blocker_id in blocked_by:
            if blocker_id == target or visit(blocker_id, set()):
                raise ValueError(f"Adding dependency would create a cycle involving task {task_id}")

    def create(
        self,
        subject: str,
        description: str = "",
        *,
        preferred_owner: str | None = None,
        blocked_by: list[int] | None = None,
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
        blocker_ids = self._normalize_task_ids(blocked_by)
        for blocker_id in blocker_ids:
            self.get(blocker_id, session_id=session_id)
        task = {
            "id": task_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "owner": None,
            "preferred_owner": preferred_owner.strip() if isinstance(preferred_owner, str) and preferred_owner.strip() else None,
            "session_id": session_id.strip() if isinstance(session_id, str) and session_id.strip() else None,
            "blockedBy": blocker_ids,
            "blocks": [],
            "created_at": now_ts(),
            "updated_at": now_ts(),
        }
        self.save(task)
        return task

    def create_many(self, items: list[dict[str, Any]], *, session_id: str | None = None) -> list[dict[str, Any]]:
        if not items:
            return []
        task_ids = self._next_ids(len(items))
        key_to_id: dict[str, int] = {}
        for task_id, item in zip(task_ids, items, strict=False):
            key = str(item.get("key") or item.get("id") or "").strip()
            if key:
                if key in key_to_id:
                    raise ValueError(f"Duplicate task key: {key}")
                key_to_id[key] = task_id

        def resolve_ids(values: list[Any] | None) -> list[int]:
            resolved: list[int] = []
            for value in values or []:
                if isinstance(value, str) and value.strip() in key_to_id:
                    resolved.append(key_to_id[value.strip()])
                    continue
                try:
                    resolved.append(int(value))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid task dependency reference: {value}") from exc
            return self._normalize_task_ids(resolved)

        now = now_ts()
        tasks: list[dict[str, Any]] = []
        for task_id, item in zip(task_ids, items, strict=False):
            subject = str(item.get("subject", "")).strip()
            if not subject:
                raise ValueError("Task subject is required")
            blocked_by = resolve_ids(item.get("blocked_by") or item.get("blockedBy") or item.get("depends_on"))
            task = {
                "id": task_id,
                "subject": subject,
                "description": str(item.get("description", "") or ""),
                "status": "pending",
                "owner": None,
                "preferred_owner": (
                    item.get("preferred_owner").strip()
                    if isinstance(item.get("preferred_owner"), str) and item.get("preferred_owner").strip()
                    else None
                ),
                "session_id": session_id.strip() if isinstance(session_id, str) and session_id.strip() else None,
                "blockedBy": blocked_by,
                "blocks": [],
                "created_at": now,
                "updated_at": now,
            }
            tasks.append(task)

        task_by_id = {int(task["id"]): task for task in tasks}

        def visit(task_id: int, stack: set[int]) -> None:
            if task_id in stack:
                raise ValueError(f"Task dependency graph contains a cycle at task {task_id}")
            task = task_by_id.get(task_id)
            if task is None:
                return
            stack.add(task_id)
            for blocker_id in self._normalize_task_ids(task.get("blockedBy", [])):
                if blocker_id not in task_by_id:
                    self.get(blocker_id, session_id=session_id)
                visit(blocker_id, stack)
            stack.remove(task_id)

        for task in tasks:
            visit(int(task["id"]), set())
        for task in tasks:
            self.save(task)
        return tasks

    def save(self, task: dict[str, Any]) -> None:
        task = dict(task)
        task["updated_at"] = now_ts()
        write_json(self._path_for_task(task), task)

    def _matches_session(self, task: dict[str, Any], session_id: str | None) -> bool:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return True
        return task.get("session_id") == normalized_session_id

    def get(self, task_id: int, session_id: str | None = None) -> dict[str, Any]:
        path = self._locate_task_path(task_id, session_id=session_id)
        if path is None:
            raise ValueError(f"Task {task_id} not found")
        task = self._read_task_file(path)
        if not self._matches_session(task, session_id):
            raise ValueError(f"Task {task_id} not found in this session")
        return task

    def list_all(self, session_id: str | None = None) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        normalized_session_id = self._normalize_session_id(session_id)
        if normalized_session_id:
            session_root = self._session_root(normalized_session_id)
            for path in sorted(session_root.glob("task_*.json")):
                if not path.is_file():
                    continue
                task = self._read_task_file(path)
                tasks.append(task)
                seen_ids.add(int(task.get("id", 0)))
        for path in sorted(self.root.glob("task_*.json")):
            task = self._read_task_file(path)
            task_id = int(task.get("id", 0))
            if task_id in seen_ids:
                continue
            if self._matches_session(task, session_id):
                tasks.append(task)
                seen_ids.add(task_id)
        if not normalized_session_id:
            sessions_root = self.root / "sessions"
            for path in sorted(sessions_root.glob("*/task_*.json")):
                if not path.is_file():
                    continue
                task = self._read_task_file(path)
                task_id = int(task.get("id", 0))
                if task_id in seen_ids:
                    continue
                tasks.append(task)
                seen_ids.add(task_id)
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
            path = self._locate_task_path(task_id, session_id=session_id)
            if path is not None and path.exists():
                path.unlink()
            return None
        if add_blocked_by:
            if task.get("status") in {"in_progress", "completed"}:
                raise ValueError(f"Task {task_id} is already {task.get('status')}; dependencies cannot be changed")
            blocker_ids = self._normalize_task_ids(add_blocked_by)
            for blocker_id in blocker_ids:
                self.get(blocker_id, session_id=session_id)
            self._assert_no_cycle(task_id, blocker_ids, session_id=session_id)
            task["blockedBy"] = sorted(set(self._normalize_task_ids(task.get("blockedBy", [])) + blocker_ids))
        if status:
            task["status"] = status
        incomplete_blockers = self._incomplete_blockers(task, session_id=session_id)
        if task.get("status") in {"in_progress", "completed"} and incomplete_blockers:
            blockers = ", ".join(str(item) for item in incomplete_blockers)
            raise ValueError(f"Task {task_id} is blocked by task(s): {blockers}")
        if add_blocks:
            task["blocks"] = sorted(set(task.get("blocks", []) + add_blocks))
        if preferred_owner is not None:
            task["preferred_owner"] = (
                preferred_owner.strip() if isinstance(preferred_owner, str) and preferred_owner.strip() else None
            )
        self.save(task)
        return task

    def claim(self, task_id: int, owner: str, session_id: str | None = None) -> dict[str, Any]:
        task = self.get(task_id, session_id=session_id)
        incomplete_blockers = self._incomplete_blockers(task, session_id=session_id)
        if incomplete_blockers:
            blockers = ", ".join(str(item) for item in incomplete_blockers)
            raise ValueError(f"Task {task_id} is blocked by task(s): {blockers}")
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
        return [
            task
            for task in self.list_all(session_id=session_id)
            if task.get("status") == "pending" and not task.get("owner") and not self._incomplete_blockers(task, session_id=session_id)
        ]

    def list_claimable_for(self, owner: str, session_id: str | None = None) -> list[dict[str, Any]]:
        owner_name = str(owner).strip()
        claimable = self.list_claimable(session_id=session_id)
        preferred = [task for task in claimable if task.get("preferred_owner") == owner_name]
        neutral = [task for task in claimable if not task.get("preferred_owner")]
        return preferred + neutral
