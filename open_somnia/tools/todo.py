from __future__ import annotations

from typing import Any

from open_somnia.tools.registry import ToolDefinition

TODO_STATUS_MARKERS = {
    "pending": "\u2610",
    "in_progress": "\u23f3",
    "completed": "\u2714",
    "cancelled": "\u2716",
}
TODO_OPEN_STATUSES = frozenset({"pending", "in_progress"})
TODO_CLOSED_STATUSES = frozenset({"completed", "cancelled"})
TODO_VISIBLE_STATUSES = frozenset({"pending", "in_progress", "completed"})
TODO_ALLOWED_STATUSES = TODO_OPEN_STATUSES | TODO_CLOSED_STATUSES


def _normalized_status(item: dict[str, Any]) -> str:
    return str(item.get("status", "pending")).lower()


class TodoManager:
    def update(self, session, items: list[dict[str, Any]]) -> str:
        validated: list[dict[str, str]] = []
        in_progress = 0
        for index, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status = _normalized_status(item)
            active_form = str(item.get("activeForm", "")).strip()
            cancelled_reason = str(item.get("cancelledReason", "")).strip()
            if not content:
                raise ValueError(f"Item {index}: content required")
            if status not in TODO_ALLOWED_STATUSES:
                raise ValueError(f"Item {index}: invalid status '{status}'")
            if not active_form:
                raise ValueError(f"Item {index}: activeForm required")
            if status == "cancelled" and not cancelled_reason:
                raise ValueError(f"Item {index}: cancelledReason required when status is 'cancelled'")
            if status == "in_progress":
                in_progress += 1
            normalized_item = {
                "content": content,
                "status": status,
                "activeForm": active_form,
            }
            if cancelled_reason:
                normalized_item["cancelledReason"] = cancelled_reason
            validated.append(normalized_item)
        if len(validated) > 20:
            raise ValueError("Max 20 todos")
        if in_progress > 1:
            raise ValueError("Only one in_progress allowed")
        session.todo_items = validated
        return self.render(session)

    def render(self, session) -> str:
        visible_items = self.visible_items(session)
        if not visible_items:
            return "No todos."
        lines: list[str] = []
        done = 0
        for item in visible_items:
            status = _normalized_status(item)
            marker = TODO_STATUS_MARKERS.get(status, "•")
            if status == "completed":
                done += 1
            suffix = f" <- {item['activeForm']}" if status == "in_progress" else ""
            lines.append(f"{marker} {item['content']}{suffix}")
        lines.append(f"\n({done}/{len(visible_items)} completed)")
        return "\n".join(lines)

    def visible_items(self, session) -> list[dict[str, Any]]:
        return [item for item in getattr(session, "todo_items", []) if _normalized_status(item) in TODO_VISIBLE_STATUSES]

    def has_open_items(self, session) -> bool:
        return any(_normalized_status(item) in TODO_OPEN_STATUSES for item in getattr(session, "todo_items", []))


def register_todo_tool(registry, todo_manager: TodoManager) -> None:
    def handler(ctx: Any, payload: dict[str, Any]) -> str:
        return todo_manager.update(ctx.session, payload["items"])

    registry.register(
        ToolDefinition(
            name="TodoWrite",
            description="Update the short-lived todo checklist for the current session.",
            input_schema={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "cancelled"],
                                },
                                "activeForm": {"type": "string"},
                                "cancelledReason": {"type": "string"},
                            },
                            "required": ["content", "status", "activeForm"],
                            "allOf": [
                                {
                                    "if": {
                                        "properties": {
                                            "status": {"const": "cancelled"},
                                        }
                                    },
                                    "then": {
                                        "required": ["cancelledReason"],
                                    },
                                }
                            ],
                        },
                    }
                },
                "required": ["items"],
            },
            handler=handler,
        )
    )
