from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable

from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.tools.tool_errors import (
    make_tool_error,
    normalize_tool_output,
    tool_error_from_exception,
    validate_tool_payload,
)


ToolHandler = Callable[[Any, dict[str, Any]], Any]


def _tool_origin(tool: "ToolDefinition") -> str:
    if tool.name.startswith("mcp__"):
        parts = tool.name.split("__", 2)
        if len(parts) == 3 and parts[1]:
            return f"MCP server '{parts[1]}'"
        return "an MCP server"
    module = str(getattr(tool.handler, "__module__", "") or "").strip()
    return f"builtin registration ({module or 'unknown module'})"


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
        self.registration_warnings: list[str] = []
        # MCP servers connect on background threads and register tools while
        # agent turns concurrently snapshot schemas; guard the map so reads
        # never iterate a mutating dict.
        self._lock = threading.RLock()

    def register(self, tool: ToolDefinition) -> None:
        with self._lock:
            previous = self._tools.get(tool.name)
            if previous is not None:
                self.registration_warnings.append(
                    f"Tool name collision: '{tool.name}' from {_tool_origin(tool)} "
                    f"overwrites {_tool_origin(previous)}."
                )
            self._tools[tool.name] = tool

    def unregister_prefix(self, prefix: str) -> int:
        with self._lock:
            names = [name for name in self._tools if name.startswith(prefix)]
            for name in names:
                self._tools.pop(name, None)
            return len(names)

    def schemas(self) -> list[dict[str, Any]]:
        with self._lock:
            return [tool.schema() for tool in list(self._tools.values())]

    def execute(self, ctx: Any, name: str, payload: dict[str, Any]) -> Any:
        with self._lock:
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
        with self._lock:
            return list(self._tools)
