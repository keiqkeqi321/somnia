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

    def create_task(ctx: Any, payload: dict[str, Any]) -> str:
        return json.dumps(
            task_store.create(
                payload["subject"],
                payload.get("description", ""),
                preferred_owner=payload.get("preferred_owner"),
                blocked_by=payload.get("blocked_by") or payload.get("blockedBy") or payload.get("depends_on"),
                session_id=_context_session_id(ctx),
            ),
            indent=2,
            ensure_ascii=False,
        )

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
        auto_assign = bool(payload.get("auto_assign", True))
        assigned = 0
        if auto_assign:
            if callable(setter):
                setter(session_id, False)
            assigned = _assign_claimable(ctx, session_id)
        return json.dumps(
            {
                "tasks": created,
                "auto_assign_paused": not auto_assign,
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
        session_id = _context_session_id(ctx)
        task = task_store.update(
            int(payload["task_id"]),
            payload.get("status"),
            payload.get("add_blocked_by"),
            payload.get("add_blocks"),
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

    def pause_auto_assign(ctx: Any, payload: dict[str, Any]) -> str:
        setter = getattr(task_store, "set_auto_assign_paused", None)
        if callable(setter):
            setter(_context_session_id(ctx), True)
        return "Task auto-assignment paused for this session."

    def release_auto_assign(ctx: Any, payload: dict[str, Any]) -> str:
        session_id = _context_session_id(ctx)
        setter = getattr(task_store, "set_auto_assign_paused", None)
        if callable(setter):
            setter(session_id, False)
        assigned = _assign_claimable(ctx, session_id)
        return f"Task auto-assignment released for this session. Assigned {assigned} task(s)."

    registry.register(
        ToolDefinition(
            name="task_create",
            description="Create a persistent task.",
            input_schema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "description": {"type": "string"},
                    "preferred_owner": {"type": "string"},
                    "blocked_by": {"type": "array", "items": {"type": "integer"}},
                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["subject"],
            },
            handler=create_task,
        )
    )
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
                    "auto_assign": {"type": "boolean"},
                },
                "required": ["tasks"],
            },
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
            handler=get_task,
        )
    )
    registry.register(
        ToolDefinition(
            name="task_update",
            description="Update task status or dependencies.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "deleted"],
                    },
                    "add_blocked_by": {"type": "array", "items": {"type": "integer"}},
                    "add_blocks": {"type": "array", "items": {"type": "integer"}},
                    "preferred_owner": {"type": "string"},
                },
                "required": ["task_id"],
            },
            handler=update_task,
        )
    )
    registry.register(
        ToolDefinition(
            name="task_list",
            description="List all tasks.",
            input_schema={"type": "object", "properties": {}},
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
            handler=claim_task,
        )
    )
    registry.register(
        ToolDefinition(
            name="task_pause_auto_assign",
            description="Pause automatic teammate task assignment for the current session while building a task graph.",
            input_schema={"type": "object", "properties": {}},
            handler=pause_auto_assign,
        )
    )
    registry.register(
        ToolDefinition(
            name="task_release_auto_assign",
            description="Release automatic teammate task assignment for the current session after the task graph is ready.",
            input_schema={"type": "object", "properties": {}},
            handler=release_auto_assign,
        )
    )
