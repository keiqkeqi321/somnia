from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from open_somnia.config.models import MCPServerSettings
from open_somnia.mcp.registry import MCPRegistry
from open_somnia.tools.registry import ToolRegistry


class MCPRegistryTests(unittest.TestCase):
    def test_server_and_tool_summaries_include_connected_server_data(self) -> None:
        registry = MCPRegistry([SimpleNamespace(name="minimal", enabled=True, transport="stdio", url=None, command="python")])
        registry.clients["minimal"] = SimpleNamespace()
        registry.server_tool_details["minimal"] = [
            {
                "name": "echo",
                "description": "Echo text",
                "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}},
            }
        ]

        summaries = registry.server_summaries()
        tools = registry.tool_summaries("minimal")

        self.assertEqual(summaries[0]["name"], "minimal")
        self.assertEqual(summaries[0]["status"], "connected")
        self.assertEqual(summaries[0]["tool_count"], 1)
        self.assertEqual(tools[0]["name"], "echo")
        self.assertEqual(tools[0]["description"], "Echo text")
        self.assertEqual(tools[0]["input_schema"]["type"], "object")

    def test_set_server_enabled_updates_runtime_tool_registry(self) -> None:
        registry = MCPRegistry([SimpleNamespace(name="minimal", enabled=True, transport="stdio", url=None, command="python")])
        tool_registry = ToolRegistry()
        registry.clients["minimal"] = SimpleNamespace(
            list_tools=lambda: [
                {
                    "name": "echo",
                    "description": "Echo text",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ],
            close=lambda: None,
        )

        enabled = registry.set_server_enabled("minimal", True, registry=tool_registry)

        self.assertEqual(enabled["tool_count"], 1)
        self.assertIn("mcp__minimal__echo", tool_registry.names())

        disabled = registry.set_server_enabled("minimal", False, registry=tool_registry)

        self.assertFalse(disabled["enabled"])
        self.assertNotIn("mcp__minimal__echo", tool_registry.names())

    def test_register_tools_warns_on_duplicate_server_name(self) -> None:
        servers = [
            SimpleNamespace(name="dup", enabled=True, transport="stdio", url=None, command="python", cwd=None),
            SimpleNamespace(name="dup", enabled=True, transport="stdio", url=None, command="python", cwd=None),
        ]
        registry = MCPRegistry(servers)
        tool_registry = ToolRegistry()
        fake_client = SimpleNamespace(
            list_tools=lambda: [
                {"name": "echo", "description": "", "inputSchema": {"type": "object", "properties": {}}}
            ],
        )

        with patch("open_somnia.mcp.registry.MCPClient", return_value=fake_client):
            registry.register_tools(tool_registry)

        self.assertEqual(len(registry.warnings), 1)
        self.assertIn("dup", registry.warnings[0])
        self.assertTrue(any("dup" in line for line in registry.status_lines()))
        self.assertIn("dup", registry.describe_servers())
        self.assertEqual(tool_registry.registration_warnings, [])

    def test_refresh_server_tools_emits_no_collision_warning(self) -> None:
        registry = MCPRegistry(
            [SimpleNamespace(name="minimal", enabled=True, transport="stdio", url=None, command="python", cwd=None)]
        )
        tool_registry = ToolRegistry()
        registry.clients["minimal"] = SimpleNamespace(
            list_tools=lambda: [
                {"name": "echo", "description": "", "inputSchema": {"type": "object", "properties": {}}}
            ],
        )

        registry.refresh_server_tools("minimal", registry=tool_registry)
        registry.refresh_server_tools("minimal", registry=tool_registry)

        self.assertEqual(tool_registry.registration_warnings, [])
        self.assertEqual(registry.warnings, [])


class MCPToolFilterTests(unittest.TestCase):
    def _register(self, server: MCPServerSettings) -> tuple[MCPRegistry, ToolRegistry]:
        registry = MCPRegistry([server])
        tool_registry = ToolRegistry()
        fake_client = SimpleNamespace(
            list_tools=lambda: [
                {"name": "read_file", "description": "Read.", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "write_file", "description": "Write.", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "delete_file", "description": "Delete.", "inputSchema": {"type": "object", "properties": {}}},
            ],
        )
        with patch("open_somnia.mcp.registry.MCPClient", return_value=fake_client):
            registry.register_tools(tool_registry)
        return registry, tool_registry

    def _server(self, **kwargs) -> MCPServerSettings:
        return MCPServerSettings(name="fs", transport="stdio", command="python", **kwargs)

    def test_absent_filters_register_everything(self) -> None:
        _, tool_registry = self._register(self._server())

        self.assertEqual(
            sorted(tool_registry.names()),
            ["mcp__fs__delete_file", "mcp__fs__read_file", "mcp__fs__write_file"],
        )

    def test_include_tools_limits_registration(self) -> None:
        _, tool_registry = self._register(self._server(include_tools=["read_file"]))

        self.assertEqual(tool_registry.names(), ["mcp__fs__read_file"])

    def test_exclude_tools_skips_listed(self) -> None:
        _, tool_registry = self._register(self._server(exclude_tools=["delete_file"]))

        self.assertEqual(sorted(tool_registry.names()), ["mcp__fs__read_file", "mcp__fs__write_file"])

    def test_exclude_wins_on_overlap(self) -> None:
        _, tool_registry = self._register(
            self._server(include_tools=["read_file", "write_file"], exclude_tools=["write_file"])
        )

        self.assertEqual(tool_registry.names(), ["mcp__fs__read_file"])

    def test_empty_include_list_disables_all_tools(self) -> None:
        _, tool_registry = self._register(self._server(include_tools=[]))

        self.assertEqual(tool_registry.names(), [])

    def test_summaries_carry_enabled_state(self) -> None:
        registry, _ = self._register(self._server(exclude_tools=["delete_file"]))

        summaries = {tool["name"]: tool for tool in registry.tool_summaries("fs")}

        self.assertTrue(summaries["read_file"]["enabled"])
        self.assertFalse(summaries["delete_file"]["enabled"])
        server_summary = registry.server_summaries()[0]
        self.assertEqual(server_summary["tool_count"], 3)
        self.assertEqual(server_summary["enabled_tool_count"], 2)


if __name__ == "__main__":
    unittest.main()
