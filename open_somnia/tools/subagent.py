from __future__ import annotations

from typing import Any

from open_somnia.tools.registry import ToolDefinition


def register_subagent_tool(registry) -> None:
    def handler(ctx: Any, payload: dict[str, Any]) -> str:
        return ctx.runtime.run_subagent(
            payload["prompt"],
            payload.get("agent_type", "Explore"),
            activity_id=getattr(ctx, "trace_id", None),
            should_interrupt=getattr(ctx, "should_interrupt", None),
        )

    registry.register(
        ToolDefinition(
            name="subagent",
            description=(
                "Delegate exploration or implementation to an isolated subagent that runs in a fresh "
                "context and returns a concise summary, keeping your main context clean. "
                "Use this FIRST for broad codebase exploration, research, 'how does X work' questions, "
                "or any task that would require several read/grep/glob steps. "
                "If a task needs more than ~3-5 exploratory tool calls, delegate it to a subagent "
                "instead of doing the exploration yourself."
            ),
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
