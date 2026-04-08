from __future__ import annotations

from typing import Any

from openagent.config.models import MCPServerSettings
from openagent.mcp.transport_http import StreamableHTTPTransport
from openagent.mcp.transport_stdio import StdioTransport


class MCPClient:
    def __init__(self, settings: MCPServerSettings):
        self.settings = settings
        if settings.transport == "http":
            if not settings.url:
                raise ValueError(f"MCP server '{settings.name}' requires a url for http transport")
            self.transport = StreamableHTTPTransport(
                url=settings.url,
                headers=settings.http_headers,
                timeout_seconds=settings.timeout_seconds,
                startup_timeout_seconds=settings.startup_timeout_seconds,
            )
        else:
            self.transport = StdioTransport(
                command=settings.command,
                args=settings.args,
                cwd=settings.cwd,
                env=settings.env or None,
                timeout_seconds=settings.timeout_seconds,
            )
        self.initialized = False

    def initialize(self) -> None:
        if self.initialized:
            return
        if hasattr(self.transport, "start"):
            self.transport.start()
        self.transport.request(
            "initialize",
            {
                "protocolVersion": self.settings.protocol_version,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "OpenAgent", "version": "0.1.0"},
            },
            startup=True,
        )
        self.transport.notify("notifications/initialized", {})
        self.initialized = True

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        response = self.transport.request("tools/list", {})
        result = response.get("result", {})
        return list(result.get("tools", []))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        response = self.transport.request(
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
            },
        )
        return response.get("result", {})

    def close(self) -> None:
        self.transport.close()
