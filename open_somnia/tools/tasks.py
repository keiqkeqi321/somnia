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
        acceptance = task.get("acceptance") or []
        if acceptance:
            done_count = sum(1 for value in (task.get("acceptance_done") or []) if value)
            acceptance_tag = f" [{done_count}/{len(acceptance)}]"
        else:
            acceptance_tag = ""
        labels = task.get("labels") or []
        ready_tag = " (ready)" if "ready-for-agent" in labels else ""
        other_labels = [lbl for lbl in labels if lbl != "ready-for-agent"]
        labels_tag = f" {{{', '.join(other_labels)}}}" if other_labels else ""
        lines.append(
            f"{marker} #{task['id']}: {task['subject']}{owner}{preferred_owner}{blocked}{acceptance_tag}{ready_tag}{labels_tag}"
        )
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
            acceptance_done=payload.get("acceptance_done"),
            labels=payload.get("labels"),
            spec_id=payload.get("spec_id"),
            result=payload.get("result"),
            commit_ref=payload.get("commit_ref"),
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
        force = bool(payload.get("force", False))
        task = task_store.claim(
            int(payload["task_id"]),
            owner,
            session_id=_context_session_id(ctx),
            require_ready_label=not force,
        )
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
                                "acceptance": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Acceptance criteria; all must be checked off before the task can be completed.",
                                },
                                "spec_id": {"type": "string", "description": "Free-form slug grouping this task under a spec/feature/epic."},
                                "labels": {"type": "array", "items": {"type": "string"}, "description": "Free-form labels; use 'ready-for-agent' to make the task auto-claimable."},
                            },
                            "required": ["subject"],
                        },
                    },
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
            description=(
                "Update a task: status, preferred owner, acceptance checks, labels, spec_id, or closure notes "
                "(result / commit_ref). Closing (status=completed) requires all acceptance criteria checked and "
                "all blockers completed. Dependency edges are declared with task_create_batch and cannot be changed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "deleted"],
                    },
                    "preferred_owner": {"type": "string"},
                    "acceptance_done": {
                        "type": "array",
                        "items": {"type": "boolean"},
                        "description": "Replace the whole acceptance check list; length must match the task's acceptance criteria.",
                    },
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "spec_id": {"type": "string"},
                    "result": {"type": "string", "description": "Closure note: what was done, anything notable. Written when completing the task."},
                    "commit_ref": {"type": "string", "description": "Commit SHA / branch / PR link for the work that closed this task."},
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
            description="Claim a task for the current actor. Requires the ready-for-agent label unless force is set.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "force": {
                        "type": "boolean",
                        "description": "Claim even if the task lacks the ready-for-agent label (human override).",
                    },
                },
                "required": ["task_id"],
            },
            handler=claim_task,
        )
    )
