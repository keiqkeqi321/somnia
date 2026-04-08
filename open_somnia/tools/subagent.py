from __future__ import annotations

from typing import Any

from open_somnia.tools.registry import ToolDefinition


def register_subagent_tool(registry) -> None:
    def handler(ctx: Any, payload: dict[str, Any]) -> str:
        return ctx.runtime.run_subagent(payload["prompt"], payload.get("agent_type", "Explore"))

    registry.register(
        ToolDefinition(
            name="subagent",
            description="Spawn an isolated subagent for exploration or implementation.",
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "agent_type": {
                        "type": "string",
                        "enum": ["Explore", "general-purpose"],
                    },
                },
                "required": ["prompt"],
            },
            handler=handler,
        )
    )
