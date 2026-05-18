from __future__ import annotations

import unittest
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
