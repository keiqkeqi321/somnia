from __future__ import annotations

from typing import Any

from openagent.config.models import MCPServerSettings
from openagent.mcp.client import MCPClient
from openagent.tools.registry import ToolDefinition


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

    def register_tools(self, registry) -> None:
        for server in self.servers:
            try:
                client = MCPClient(server)
                tools = client.list_tools()
                self.clients[server.name] = client
                self.server_tools[server.name] = [tool["name"] for tool in tools]
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

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
