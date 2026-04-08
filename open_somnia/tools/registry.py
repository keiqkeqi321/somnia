from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


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
            return f"Unknown tool: {name}"
        runtime = getattr(ctx, "runtime", None)
        authorizer = getattr(runtime, "authorize_tool_call", None)
        if callable(authorizer):
            blocked = authorizer(name, payload, ctx=ctx)
            if blocked is not None:
                return blocked
        return tool.handler(ctx, payload)

    def names(self) -> list[str]:
        return list(self._tools)
