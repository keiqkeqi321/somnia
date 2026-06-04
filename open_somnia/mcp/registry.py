from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from open_somnia.config.models import MCPServerSettings
from open_somnia.mcp.client import MCPClient
from open_somnia.runtime.messages import (
    guess_image_media_type,
    make_image_reference_block,
    parse_image_data_url,
    render_image_reference_text,
)
from open_somnia.tools.registry import ToolDefinition

LOCAL_IMAGE_LINK_PATTERN = re.compile(r"!?\[[^\]]*]\(([^)\r\n]+)\)")


def _mcp_image_data_url(item: dict[str, Any]) -> tuple[str, str] | None:
    media_type = str(item.get("mimeType") or item.get("mime_type") or "").strip().lower()
    data = re.sub(r"\s+", "", str(item.get("data") or ""))
    if not media_type or not data:
        return None
    data_url = f"data:{media_type};base64,{data}"
    parsed = parse_image_data_url(data_url)
    if parsed is None:
        return None
    return parsed[0], data_url


def _mcp_image_result_blocks(items: list[Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    image_index = 0
    for item in items:
        if not isinstance(item, dict) or str(item.get("type", "")).strip() != "image":
            continue
        parsed = _mcp_image_data_url(item)
        if parsed is None:
            continue
        image_index += 1
        media_type, data_url = parsed
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": data_url},
                "media_type": media_type,
                "origin": "tool_result",
                "label": f"MCP image {image_index}",
            }
        )
    return blocks


def _local_image_reference_blocks(
    text: str,
    *,
    cwd: Path | None = None,
    transport: str | None = None,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in LOCAL_IMAGE_LINK_PATTERN.finditer(text):
        raw_target = match.group(1).strip().strip("<>")
        target = raw_target.split(None, 1)[0].strip().strip("'\"")
        if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
            continue
        media_type = guess_image_media_type(target)
        if media_type is None:
            continue
        normalized_path = target.replace("\\", "/")
        absolute_path = ""
        target_path = Path(target)
        if target_path.is_absolute():
            absolute_path = str(target_path)
        elif cwd is not None:
            absolute_path = str((cwd / target_path).resolve())
        key = f"{normalized_path}\0{absolute_path}"
        if key in seen:
            continue
        seen.add(key)
        blocks.append(
            make_image_reference_block(
                path=normalized_path,
                absolute_path=absolute_path,
                media_type=media_type,
                origin="tool_result",
                transport=transport,
            )
        )
    return blocks


def _render_mcp_result(
    result: dict[str, Any],
    *,
    cwd: Path | None = None,
    transport: str | None = None,
) -> Any:
    parts: list[str] = []
    raw_content = result.get("content", [])
    content_items = raw_content if isinstance(raw_content, list) else []
    for item in content_items:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif isinstance(item, dict) and item.get("type") == "image" and _mcp_image_data_url(item) is not None:
            continue
        else:
            parts.append(str(item))
    text = "\n".join(part for part in parts if part) or "(no content)"
    image_blocks = _mcp_image_result_blocks(content_items)
    local_image_references = _local_image_reference_blocks(text, cwd=cwd, transport=transport)
    if image_blocks and not result.get("isError"):
        references = [
            make_image_reference_block(
                media_type=str(block.get("media_type", "")),
                image_url=str(block.get("image_url", {}).get("url", "")),
                origin="tool_result",
            )
            for block in image_blocks
        ]
        image_summary = "\n".join(render_image_reference_text(reference, delivery=True) for reference in references)
        summary = "\n".join(part for part in (text if text != "(no content)" else "", image_summary) if part)
        return {
            "status": "ok",
            "message": summary,
            "tool_result_text": summary,
            "tool_result_content": [
                {"type": "text", "text": summary},
                *[
                    {
                        "type": "image_url",
                        "image_url": block["image_url"],
                    }
                    for block in image_blocks
                ],
            ],
        }
    if local_image_references and not result.get("isError"):
        image_summary = "\n".join(render_image_reference_text(reference, delivery=True) for reference in local_image_references)
        summary = "\n".join(part for part in (text if text != "(no content)" else "", image_summary) if part)
        return {
            "status": "ok",
            "message": summary,
            "tool_result_text": summary,
            "tool_result_content": [
                {"type": "text", "text": summary},
                *local_image_references,
            ],
        }
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
            self._register_server_tool_definitions(registry, server.name, tools)

    def _register_server_tool_definitions(self, registry, server_name: str, tools: list[dict[str, Any]]) -> None:
        unregister_prefix = getattr(registry, "unregister_prefix", None)
        if callable(unregister_prefix):
            unregister_prefix(f"mcp__{server_name}__")
        server = next((item for item in self.all_servers if item.name == server_name), None)
        server_cwd = getattr(server, "cwd", None)
        for tool in tools:
            remote_name = tool["name"]
            local_name = f"mcp__{server_name}__{remote_name}"
            input_schema = tool.get("inputSchema") or tool.get("input_schema") or {
                "type": "object",
                "properties": {},
            }

            def handler(
                ctx: Any,
                payload: dict[str, Any],
                server_name: str = server_name,
                name: str = remote_name,
                cwd=server_cwd,
                transport: str = str(getattr(server, "transport", "")),
            ) -> str:
                result = self.clients[server_name].call_tool(name, payload)
                return _render_mcp_result(result, cwd=cwd, transport=transport)

            registry.register(
                ToolDefinition(
                    name=local_name,
                    description=f"MCP tool '{remote_name}' from server '{server_name}'. {tool.get('description', '')}".strip(),
                    input_schema=input_schema,
                    handler=handler,
                )
            )

    def refresh_server_tools(self, server_name: str, registry=None) -> dict[str, Any]:
        name = str(server_name or "").strip()
        server = next((item for item in self.all_servers if item.name == name), None)
        if server is None:
            raise ValueError(f"MCP server '{name}' not found")
        if not server.enabled:
            raise ValueError(f"MCP server '{name}' is disabled")
        client = self.clients.get(name)
        if client is None:
            client = MCPClient(server)
            self.clients[name] = client
        tools = client.list_tools()
        self.server_tools[name] = [str(tool.get("name", "")) for tool in tools]
        self.server_tool_details[name] = list(tools)
        self.errors.pop(name, None)
        if registry is not None:
            self._register_server_tool_definitions(registry, name, list(tools))
        return {
            "name": server.name,
            "transport": server.transport,
            "target": server.url or server.command or "(unconfigured)",
            "enabled": server.enabled,
            "status": "connected",
            "error": "",
            "tool_count": len(tools),
            "tools": self.tool_summaries(name),
        }

    def set_server_enabled(self, server_name: str, enabled: bool, registry=None) -> dict[str, Any]:
        name = str(server_name or "").strip()
        server = next((item for item in self.all_servers if item.name == name), None)
        if server is None:
            raise ValueError(f"MCP server '{name}' not found")
        server.enabled = bool(enabled)
        self.servers = [item for item in self.all_servers if item.enabled]
        if not server.enabled:
            unregister_prefix = getattr(registry, "unregister_prefix", None)
            if callable(unregister_prefix):
                unregister_prefix(f"mcp__{name}__")
            client = self.clients.pop(name, None)
            if client is not None:
                client.close()
            self.server_tools.pop(name, None)
            self.server_tool_details.pop(name, None)
            self.errors.pop(name, None)
            return {
                "name": server.name,
                "transport": server.transport,
                "target": server.url or server.command or "(unconfigured)",
                "enabled": False,
                "status": "disabled",
                "error": "",
                "tool_count": 0,
                "tools": [],
            }
        return self.refresh_server_tools(name, registry=registry)

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
