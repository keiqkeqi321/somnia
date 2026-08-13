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


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "\u2026"


def render_task_board(tasks: list[dict[str, Any]]) -> str:
    """Human-friendly task board for the REPL: at most two lines per task.

    Line 1: status marker, id, subject, owner, acceptance progress, ready/labels.
    Line 2 (when useful): the blockers with their subject + status (the "key info"
    -- you can see whether what you're waiting on is close or far), or the closure
    note for completed tasks. Blocker subjects are resolved from the passed list.
    """
    if not tasks:
        return "No tasks."
    by_id = {int(task.get("id", 0)): task for task in tasks}
    lines: list[str] = []
    for task in sorted(tasks, key=lambda item: int(item.get("id", 0))):
        lines.append(_task_board_line(task))
        secondary = _task_board_secondary(task, by_id)
        if secondary:
            lines.append(secondary)
    return "\n".join(lines)


def _task_board_line(task: dict[str, Any]) -> str:
    marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(task.get("status"), "[?]")
    subject = _truncate(str(task.get("subject") or "Untitled"), 52)
    owner = f" @{task['owner']}" if task.get("owner") else ""
    acceptance = task.get("acceptance") or []
    acc_tag = ""
    if acceptance:
        done = sum(1 for value in (task.get("acceptance_done") or []) if value)
        acc_tag = f" [{done}/{len(acceptance)}]"
    labels = task.get("labels") or []
    ready = " ready" if "ready-for-agent" in labels else ""
    other = [lbl for lbl in labels if lbl != "ready-for-agent"]
    labels_tag = f" {{{', '.join(other)}}}" if other else ""
    return f"{marker} #{task.get('id')}: {subject}{owner}{acc_tag}{ready}{labels_tag}"


def _task_board_secondary(task: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> str:
    status = task.get("status")
    if status == "completed":
        note = str(task.get("result") or "").strip()
        commit = str(task.get("commit_ref") or "").strip()
        parts = []
        if note:
            parts.append(f"result: {_truncate(note, 60)}")
        if commit:
            parts.append(f"commit: {_truncate(commit, 40)}")
        return f"    \u21b3 {' \u00b7 '.join(parts)}" if parts else ""
    blocked = [int(b) for b in (task.get("blockedBy") or [])]
    if not blocked:
        return ""
    pieces: list[str] = []
    for blocker_id in blocked:
        blocker = by_id.get(blocker_id)
        if blocker is None:
            pieces.append(f"#{blocker_id} (missing)")
            continue
        blocker_subject = _truncate(str(blocker.get("subject") or "Untitled"), 24)
        blocker_status = {"pending": "pending", "in_progress": "in_progress", "completed": "done"}.get(
            blocker.get("status"), str(blocker.get("status"))
        )
        pieces.append(f"#{blocker_id} '{blocker_subject}' {blocker_status}")
    return "    \u21b3 blocked by " + ", ".join(pieces)


def register_task_tools(registry, task_store, *, allow_dep_removal: bool = True) -> None:
    def _context_session_id(ctx: Any) -> str | None:
        session = getattr(ctx, "session", None)
        if session is not None:
            # A session created via /new inherits its predecessor's task board
            # (task_session_id); fall back to the session's own id otherwise.
            board_id = getattr(session, "task_session_id", None) or getattr(session, "id", None)
            if board_id:
                return str(board_id)
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
        # Removing dependency edges is gated behind task_remove_blocked_by (it
        # triggers request_authorization); task_update may only add edges.
        if payload.get("remove_blocked_by") or payload.get("depends_on"):
            raise ValueError("Removing dependency edges is not supported here; use task_remove_blocked_by.")
        session_id = _context_session_id(ctx)
        task = task_store.update(
            int(payload["task_id"]),
            payload.get("status"),
            payload.get("add_blocked_by"),
            None,
            payload.get("preferred_owner"),
            acceptance_done=payload.get("acceptance_done"),
            labels=payload.get("labels"),
            spec_id=payload.get("spec_id"),
            result=payload.get("result"),
            commit_ref=payload.get("commit_ref"),
            parent_id=payload.get("parent_id"),
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

    def remove_blocked_by_task(ctx: Any, payload: dict[str, Any]) -> str:
        session_id = _context_session_id(ctx)
        task = task_store.update(
            int(payload["task_id"]),
            remove_blocked_by=payload.get("remove") or payload.get("remove_blocked_by"),
            session_id=session_id,
        )
        # Removing a blocker may unblock this (or another) task -> re-run auto-assign.
        _assign_claimable(ctx, session_id)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def claimable_tasks(ctx: Any, payload: dict[str, Any]) -> str:
        session_id = _context_session_id(ctx)
        claimable = task_store.list_claimable(session_id=session_id)
        ready = [t for t in claimable if "ready-for-agent" in (t.get("labels") or [])]
        not_ready = [t for t in claimable if "ready-for-agent" not in (t.get("labels") or [])]
        return json.dumps(
            {"ready_for_agent": ready, "claimable_unspecced": not_ready},
            indent=2,
            ensure_ascii=False,
        )

    def close_task(ctx: Any, payload: dict[str, Any]) -> str:
        session_id = _context_session_id(ctx)
        task = task_store.update(
            int(payload["task_id"]),
            "completed",
            acceptance_done=payload.get("acceptance_done"),
            result=payload.get("result"),
            commit_ref=payload.get("commit_ref"),
            session_id=session_id,
        )
        _assign_claimable(ctx, session_id)
        return json.dumps(task, indent=2, ensure_ascii=False)

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
                                "parent_id": {"type": ["integer", "string"], "description": "Parent (map/epic) task id or earlier task key."},
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
                "Update a task: status, preferred owner, acceptance checks, labels, spec_id, closure notes "
                "(result / commit_ref), add dependency edges, or parent. Closing (status=completed) requires all "
                "acceptance criteria checked and all blockers completed. Adding edges is allowed here; removing "
                "edges requires task_remove_blocked_by (it asks the user for approval)."
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
                    "add_blocked_by": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Add dependency edges: these tasks must complete before this one.",
                    },
                    "parent_id": {
                        "type": "integer",
                        "description": "Set this task's parent (map/epic) task id. Use 0 to clear.",
                    },
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
    registry.register(
        ToolDefinition(
            name="task_claimable",
            description="Show the work frontier: tasks that are pending, unowned, and unblocked, split into ready-for-agent (auto-claimable) and unspecced-but-unblocked.",
            input_schema={"type": "object", "properties": {}},
            handler=claimable_tasks,
        )
    )
    registry.register(
        ToolDefinition(
            name="task_close",
            description="Close a task as completed. Requires all acceptance criteria checked and all blockers completed. Optionally record a result note and commit_ref.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "acceptance_done": {
                        "type": "array",
                        "items": {"type": "boolean"},
                        "description": "Replace the whole acceptance check list; length must match the task's acceptance criteria.",
                    },
                    "result": {"type": "string", "description": "Closure note: what was done, anything notable."},
                    "commit_ref": {"type": "string", "description": "Commit SHA / branch / PR link for the work that closed this task."},
                },
                "required": ["task_id"],
            },
            handler=close_task,
        )
    )
    if allow_dep_removal:
        registry.register(
            ToolDefinition(
                name="task_remove_blocked_by",
                description=(
                    "Remove dependency edges from a task (re-plan its blockers). This reorganizes the plan, so it "
                    "requires explicit user approval (request_authorization) outside Yolo. Removing a blocker may "
                    "unblock this task and trigger auto-assignment."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "integer"},
                        "remove": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Task ids to remove from this task's blocked-by list.",
                        },
                    },
                    "required": ["task_id"],
                },
                handler=remove_blocked_by_task,
            )
        )
