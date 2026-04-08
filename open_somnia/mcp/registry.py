from __future__ import annotations

from typing import Any

from open_somnia.config.models import MCPServerSettings
from open_somnia.mcp.client import MCPClient
from open_somnia.tools.registry import ToolDefinition


def _render_mcp_result(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in result.get("content", []):
        if item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        else:
            parts.append(str(item))
    text = "\n".join(part for part in parts if part) or "(no content)"
    if result.get("isError"):
        return f"Error: {text}"
    return text


class MCPRegistry:
    def __init__(self, servers: list[MCPServerSettings]):
        self.all_servers = servers
        self.servers = [server for server in servers if server.enabled]
        self.clients: dict[str, MCPClient] = {}
        self.errors: dict[str, str] = {}
        self.server_tools: dict[str, list[str]] = {}
        self.server_tool_details: dict[str, list[dict[str, Any]]] = {}

    def register_tools(self, registry) -> None:
        for server in self.servers:
            try:
                client = MCPClient(server)
                tools = client.list_tools()
                self.clients[server.name] = client
                self.server_tools[server.name] = [tool["name"] for tool in tools]
                self.server_tool_details[server.name] = list(tools)
            except Exception as exc:
                self.errors[server.name] = str(exc)
                continue
            for tool in tools:
                remote_name = tool["name"]
                local_name = f"mcp__{server.name}__{remote_name}"
                input_schema = tool.get("inputSchema") or tool.get("input_schema") or {
                    "type": "object",
                    "properties": {},
                }

                def handler(ctx: Any, payload: dict[str, Any], server_name: str = server.name, name: str = remote_name) -> str:
                    result = self.clients[server_name].call_tool(name, payload)
                    return _render_mcp_result(result)

                registry.register(
                    ToolDefinition(
                        name=local_name,
                        description=f"MCP tool '{remote_name}' from server '{server.name}'. {tool.get('description', '')}".strip(),
                        input_schema=input_schema,
                        handler=handler,
                    )
                )

    def status_lines(self) -> list[str]:
        lines = []
        for server in self.all_servers:
            if not server.enabled:
                target = server.url or server.command or "(unconfigured)"
                lines.append(f"{server.name}: disabled [{server.transport}] {target}")
                continue
            if server.name in self.clients:
                target = server.url or server.command or "(unconfigured)"
                tool_count = len(self.server_tools.get(server.name, []))
                lines.append(f"{server.name}: connected [{server.transport}] {target} tools={tool_count}")
            else:
                lines.append(f"{server.name}: error - {self.errors.get(server.name, 'not initialized')}")
        return lines

    def describe_servers(self) -> str:
        if not self.all_servers:
            return "No MCP servers configured."
        lines: list[str] = []
        for server in self.all_servers:
            target = server.url or server.command or "(unconfigured)"
            if not server.enabled:
                status = "disabled"
            elif server.name in self.clients:
                status = "connected"
            else:
                status = f"error: {self.errors.get(server.name, 'not initialized')}"
            lines.append(f"- {server.name} [{server.transport}] {status}")
            lines.append(f"  target: {target}")
            tools = self.server_tools.get(server.name, [])
            if tools:
                lines.append(f"  tools: {', '.join(tools)}")
        return "\n".join(lines)

    def server_summaries(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for server in self.all_servers:
            target = server.url or server.command or "(unconfigured)"
            tools = list(self.server_tool_details.get(server.name, []))
            if not server.enabled:
                status = "disabled"
            elif server.name in self.clients:
                status = "connected"
            else:
                status = "error"
            summaries.append(
                {
                    "name": server.name,
                    "transport": server.transport,
                    "target": target,
                    "enabled": server.enabled,
                    "status": status,
                    "error": self.errors.get(server.name, ""),
                    "tool_count": len(tools),
                }
            )
        return summaries

    def tool_summaries(self, server_name: str) -> list[dict[str, Any]]:
        tools = self.server_tool_details.get(server_name, [])
        summaries: list[dict[str, Any]] = []
        for tool in tools:
            input_schema = tool.get("inputSchema") or tool.get("input_schema") or {"type": "object", "properties": {}}
            summaries.append(
                {
                    "name": str(tool.get("name", "")),
                    "description": str(tool.get("description", "")).strip(),
                    "input_schema": input_schema,
                }
            )
        return summaries

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
