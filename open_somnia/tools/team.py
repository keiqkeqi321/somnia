from __future__ import annotations

import json
from typing import Any

from open_somnia.tools.registry import ToolDefinition

VALID_MSG_TYPES = [
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_request",
    "plan_approval_response",
]


def register_team_tools(registry, teammate_manager, bus, tracker) -> None:
    def spawn_teammate(ctx: Any, payload: dict[str, Any]) -> str:
        return teammate_manager.spawn(payload["name"], payload["role"], payload["prompt"])

    def list_teammates(ctx: Any, payload: dict[str, Any]) -> str:
        return teammate_manager.list_all()

    def send_message(ctx: Any, payload: dict[str, Any]) -> str:
        return bus.send(ctx.actor, payload["to"], payload["content"], payload.get("msg_type", "message"))

    def read_inbox(ctx: Any, payload: dict[str, Any]) -> str:
        return json.dumps(bus.read_inbox(ctx.actor), indent=2, ensure_ascii=False)

    def broadcast(ctx: Any, payload: dict[str, Any]) -> str:
        return bus.broadcast(ctx.actor, payload["content"], teammate_manager.member_names())

    def shutdown_request(ctx: Any, payload: dict[str, Any]) -> str:
        request = tracker.create_shutdown_request(payload["teammate"])
        bus.send(
            ctx.actor,
            payload["teammate"],
            "Please shut down.",
            "shutdown_request",
            {"request_id": request["request_id"]},
        )
        return f"Shutdown request {request['request_id']} sent to '{payload['teammate']}'"

    def plan_approval(ctx: Any, payload: dict[str, Any]) -> str:
        result = tracker.resolve_plan_request(payload["request_id"], payload["approve"], payload.get("feedback", ""))
        if result is None:
            return f"Error: Unknown plan request_id '{payload['request_id']}'"
        bus.send(
            ctx.actor,
            result["from"],
            payload.get("feedback", ""),
            "plan_approval_response",
            {
                "request_id": payload["request_id"],
                "approve": payload["approve"],
                "feedback": payload.get("feedback", ""),
            },
        )
        return f"Plan {'approved' if payload['approve'] else 'rejected'} for '{result['from']}'"

    def idle(ctx: Any, payload: dict[str, Any]) -> str:
        return "Lead does not idle."

    registry.register(
        ToolDefinition(
            name="spawn_teammate",
            description="Spawn a persistent autonomous teammate.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "prompt": {"type": "string"},
                },
                "required": ["name", "role", "prompt"],
            },
            handler=spawn_teammate,
        )
    )
    registry.register(
        ToolDefinition(
            name="list_teammates",
            description="List all teammates.",
            input_schema={"type": "object", "properties": {}},
            handler=list_teammates,
        )
    )
    registry.register(
        ToolDefinition(
            name="send_message",
            description="Send a message to a teammate.",
            input_schema={
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "content": {"type": "string"},
                    "msg_type": {"type": "string", "enum": VALID_MSG_TYPES},
                },
                "required": ["to", "content"],
            },
            handler=send_message,
        )
    )
    registry.register(
        ToolDefinition(
            name="read_inbox",
            description="Read and drain the current actor inbox.",
            input_schema={"type": "object", "properties": {}},
            handler=read_inbox,
        )
    )
    registry.register(
        ToolDefinition(
            name="broadcast",
            description="Send a broadcast to all teammates.",
            input_schema={
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
            handler=broadcast,
        )
    )
    registry.register(
        ToolDefinition(
            name="shutdown_request",
            description="Request a teammate to shut down.",
            input_schema={
                "type": "object",
                "properties": {"teammate": {"type": "string"}},
                "required": ["teammate"],
            },
            handler=shutdown_request,
        )
    )
    registry.register(
        ToolDefinition(
            name="plan_approval",
            description="Approve or reject a teammate plan request.",
            input_schema={
                "type": "object",
                "properties": {
                    "request_id": {"type": "string"},
                    "approve": {"type": "boolean"},
                    "feedback": {"type": "string"},
                },
                "required": ["request_id", "approve"],
            },
            handler=plan_approval,
        )
    )
    registry.register(
        ToolDefinition(
            name="idle",
            description="Enter idle state.",
            input_schema={"type": "object", "properties": {}},
            handler=idle,
        )
    )
