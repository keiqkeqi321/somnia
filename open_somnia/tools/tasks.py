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
        blocked = f" (blocked by: {task['blockedBy']})" if task.get("blockedBy") else ""
        lines.append(f"{marker} #{task['id']}: {task['subject']}{owner}{preferred_owner}{blocked}")
    return "\n".join(lines)


def register_task_tools(registry, task_store) -> None:
    def create_task(ctx: Any, payload: dict[str, Any]) -> str:
        return json.dumps(
            task_store.create(
                payload["subject"],
                payload.get("description", ""),
                preferred_owner=payload.get("preferred_owner"),
            ),
            indent=2,
            ensure_ascii=False,
        )

    def get_task(ctx: Any, payload: dict[str, Any]) -> str:
        return json.dumps(task_store.get(int(payload["task_id"])), indent=2, ensure_ascii=False)

    def update_task(ctx: Any, payload: dict[str, Any]) -> str:
        task = task_store.update(
            int(payload["task_id"]),
            payload.get("status"),
            payload.get("add_blocked_by"),
            payload.get("add_blocks"),
            payload.get("preferred_owner"),
        )
        if task is None:
            return f"Task {payload['task_id']} deleted"
        return json.dumps(task, indent=2, ensure_ascii=False)

    def list_tasks(ctx: Any, payload: dict[str, Any]) -> str:
        return _render_task_list(task_store.list_all())

    def claim_task(ctx: Any, payload: dict[str, Any]) -> str:
        owner = payload.get("owner", ctx.actor)
        task = task_store.claim(int(payload["task_id"]), owner)
        return f"Claimed task #{task['id']} for {owner}"

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
                },
                "required": ["subject"],
            },
            handler=create_task,
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
