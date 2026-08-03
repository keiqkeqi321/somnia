from __future__ import annotations

import json
from typing import Any

from open_somnia.tools.registry import ToolDefinition


def _render_task_list(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "No tasks."
    lines: list[str] = []
    for task in tasks:
        marker = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
        }.get(task["status"], "[?]")
        owner = f" @{task['owner']}" if task.get("owner") else ""
        preferred_owner = f" (prefers: {task['preferred_owner']})" if task.get("preferred_owner") else ""
        blocked = f" (depends on: {task['blockedBy']})" if task.get("blockedBy") else ""
        lines.append(f"{marker} #{task['id']}: {task['subject']}{owner}{preferred_owner}{blocked}")
    return "\n".join(lines)


def register_task_tools(registry, task_store) -> None:
    def _context_session_id(ctx: Any) -> str | None:
        session_id = getattr(getattr(ctx, "session", None), "id", None)
        if session_id:
            return str(session_id)
        manager = getattr(getattr(ctx, "runtime", None), "team_manager", None)
        getter = getattr(manager, "_member_session_id", None)
        if callable(getter):
            return getter(getattr(ctx, "actor", ""))
        return None

    def _assign_claimable(ctx: Any, session_id: str | None) -> int:
        manager = getattr(getattr(ctx, "runtime", None), "team_manager", None)
        assign_claimable = getattr(manager, "assign_claimable_tasks", None)
        if callable(assign_claimable):
            return int(assign_claimable(session_id=session_id) or 0)
        return 0

    def create_task_batch(ctx: Any, payload: dict[str, Any]) -> str:
        session_id = _context_session_id(ctx)
        setter = getattr(task_store, "set_auto_assign_paused", None)
        if callable(setter):
            setter(session_id, True)
        try:
            created = task_store.create_many(list(payload.get("tasks") or []), session_id=session_id)
        except Exception:
            if callable(setter):
                setter(session_id, False)
            raise
        if callable(setter):
            setter(session_id, False)
        assigned = _assign_claimable(ctx, session_id)
        return json.dumps(
            {
                "tasks": created,
                "assigned": assigned,
            },
            indent=2,
            ensure_ascii=False,
        )

    def get_task(ctx: Any, payload: dict[str, Any]) -> str:
        return json.dumps(
            task_store.get(int(payload["task_id"]), session_id=_context_session_id(ctx)),
            indent=2,
            ensure_ascii=False,
        )

    def update_task(ctx: Any, payload: dict[str, Any]) -> str:
        if payload.get("add_blocked_by") or payload.get("blocked_by") or payload.get("depends_on"):
            raise ValueError("Task dependencies must be declared with task_create_batch; dependency updates are not allowed")
        session_id = _context_session_id(ctx)
        task = task_store.update(
            int(payload["task_id"]),
            payload.get("status"),
            None,
            None,
            payload.get("preferred_owner"),
            session_id=session_id,
        )
        if payload.get("status") == "completed":
            _assign_claimable(ctx, session_id)
        if task is None:
            return f"Task {payload['task_id']} deleted"
        return json.dumps(task, indent=2, ensure_ascii=False)

    def list_tasks(ctx: Any, payload: dict[str, Any]) -> str:
        return _render_task_list(task_store.list_all(session_id=_context_session_id(ctx)))

    def claim_task(ctx: Any, payload: dict[str, Any]) -> str:
        owner = payload.get("owner", ctx.actor)
        task = task_store.claim(int(payload["task_id"]), owner, session_id=_context_session_id(ctx))
        return f"Claimed task #{task['id']} for {owner}"

    registry.register(
        ToolDefinition(
            name="task_create_batch",
            description="Create multiple persistent tasks as one dependency graph. Dependency references may use earlier task keys.",
            input_schema={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "subject": {"type": "string"},
                                "description": {"type": "string"},
                                "preferred_owner": {"type": "string"},
                                "blocked_by": {"type": "array", "items": {"type": ["integer", "string"]}},
                                "depends_on": {"type": "array", "items": {"type": ["integer", "string"]}},
                            },
                            "required": ["subject"],
                        },
                    },
                },
                "required": ["tasks"],
            },
            deferred=True,
            handler=create_task_batch,
        )
    )
    registry.register(
        ToolDefinition(
            name="task_get",
            description="Get task details by ID.",
            input_schema={
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
            deferred=True,
            handler=get_task,
        )
    )
    registry.register(
        ToolDefinition(
            name="task_update",
            description="Update task status or preferred owner. Dependency edges are declared with task_create_batch and cannot be changed after creation.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "deleted"],
                    },
                    "preferred_owner": {"type": "string"},
                },
                "required": ["task_id"],
            },
            deferred=True,
            handler=update_task,
        )
    )
    registry.register(
        ToolDefinition(
            name="task_list",
            description="List all tasks.",
            input_schema={"type": "object", "properties": {}},
            deferred=True,
            handler=list_tasks,
        )
    )
    registry.register(
        ToolDefinition(
            name="claim_task",
            description="Claim a task for the current actor.",
            input_schema={
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
            deferred=True,
            handler=claim_task,
        )
    )
