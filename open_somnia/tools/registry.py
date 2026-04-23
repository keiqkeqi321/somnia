from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.tools.tool_errors import (
    make_tool_error,
    normalize_tool_output,
    tool_error_from_exception,
    validate_tool_payload,
)


ToolHandler = Callable[[Any, dict[str, Any]], Any]


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, ctx: Any, name: str, payload: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            return make_tool_error(name, "unknown_tool", f"Unknown tool: {name}")
        runtime = getattr(ctx, "runtime", None)
        authorizer = getattr(runtime, "authorize_tool_call", None)
        if callable(authorizer):
            blocked = authorizer(name, payload, ctx=ctx)
            if blocked is not None:
                normalized_block = normalize_tool_output(name, blocked, tool.input_schema)
                if isinstance(normalized_block, str):
                    return make_tool_error(name, "tool_access_blocked", normalized_block)
                return normalized_block
        hook_manager = getattr(runtime, "hook_manager", None)
        if hook_manager is not None:
            decision = hook_manager.before_tool_use(ctx, name, payload)
            if decision.action == "deny":
                return make_tool_error(
                    name,
                    "blocked_by_hook",
                    decision.message or f"Blocked by PreToolUse hook for '{name}'.",
                )
            if decision.replacement_input is not None:
                payload.clear()
                payload.update(decision.replacement_input)
        validation_error = validate_tool_payload(name, payload, tool.input_schema)
        if validation_error is not None:
            if hook_manager is not None:
                hook_manager.after_tool_use(ctx, name, payload, result=validation_error)
            return validation_error
        try:
            output = tool.handler(ctx, payload)
        except TurnInterrupted:
            raise
        except Exception as exc:
            if hook_manager is not None:
                hook_manager.after_tool_use(ctx, name, payload, error=exc)
            return tool_error_from_exception(name, exc, tool.input_schema)
        normalized_output = normalize_tool_output(name, output, tool.input_schema)
        if hook_manager is not None:
            hook_manager.after_tool_use(ctx, name, payload, result=normalized_output)
        return normalized_output

    def names(self) -> list[str]:
        return list(self._tools)
