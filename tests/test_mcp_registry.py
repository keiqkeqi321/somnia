from __future__ import annotations

import unittest
from types import SimpleNamespace

from open_somnia.mcp.registry import MCPRegistry


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


if __name__ == "__main__":
    unittest.main()
