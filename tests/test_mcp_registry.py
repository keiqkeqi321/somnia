from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from open_somnia.config.models import MCPServerSettings
from open_somnia.mcp.registry import MCPRegistry
from open_somnia.tools.registry import ToolRegistry


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


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

    def test_failed_tool_registration_closes_client(self) -> None:
        """A client whose list_tools() fails must be closed, not leaked.

        Regression: a partially-initialized client (live portal thread, SSE
        stream, httpx client) used to be dropped without close(), so GC tore
        it down in arbitrary order at interpreter exit — surfacing as
        "Session termination failed: Cannot send a request, as the client
        has been closed."
        """
        registry = MCPRegistry(
            [SimpleNamespace(name="broken", enabled=True, transport="http", url="http://x", command=None, cwd=None)]
        )
        closed: list[bool] = []

        class _FailingClient:
            def list_tools(self):
                raise RuntimeError("API key authentication required in HTTP mode")

            def close(self):
                closed.append(True)

        with patch("open_somnia.mcp.registry.MCPClient", return_value=_FailingClient()):
            registry.register_tools(ToolRegistry())

        self.assertEqual(closed, [True])
        self.assertNotIn("broken", registry.clients)
        self.assertIn("broken", registry.errors)


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


class MCPBackgroundConnectTests(unittest.TestCase):
    """register_tools_background: per-server daemon threads, status flow."""

    def _server(self, name: str = "s1") -> SimpleNamespace:
        return SimpleNamespace(name=name, enabled=True, transport="stdio", url=None, command="python", cwd=None)

    def _fast_client(self):
        return SimpleNamespace(
            list_tools=lambda: [
                {"name": "echo", "description": "", "inputSchema": {"type": "object", "properties": {}}}
            ],
            close=lambda: None,
        )

    def test_connecting_then_connected_with_tools_registered(self) -> None:
        registry = MCPRegistry([self._server()])
        tool_registry = ToolRegistry()
        settled: list[str] = []

        with patch("open_somnia.mcp.registry.MCPClient", side_effect=lambda server: self._fast_client()):
            registry.register_tools_background(tool_registry, on_settled=settled.append)

        self.assertTrue(_wait_for(lambda: "mcp__s1__echo" in tool_registry.names()))
        self.assertTrue(_wait_for(lambda: settled == ["s1"]))
        summary = registry.server_summaries()[0]
        self.assertEqual(summary["status"], "connected")
        self.assertEqual(summary["tool_count"], 1)

    def test_failed_connect_records_error_and_closes_client(self) -> None:
        registry = MCPRegistry([self._server("broken")])
        tool_registry = ToolRegistry()
        closed: list[bool] = []

        class _FailingClient:
            def list_tools(self):
                raise RuntimeError("connect refused")

            def close(self):
                closed.append(True)

        with patch("open_somnia.mcp.registry.MCPClient", return_value=_FailingClient()):
            registry.register_tools_background(tool_registry)

        self.assertTrue(_wait_for(lambda: registry.server_summaries()[0]["status"] == "error"))
        self.assertIn("connect refused", registry.errors["broken"])
        self.assertEqual(closed, [True])
        self.assertEqual(tool_registry.names(), [])

    def test_close_during_connect_drops_late_client(self) -> None:
        registry = MCPRegistry([self._server()])
        tool_registry = ToolRegistry()
        started = threading.Event()
        release = threading.Event()
        clients: list[SimpleNamespace] = []

        class _SlowClient:
            def __init__(self, server):
                self.closed = False
                clients.append(self)

            def list_tools(self):
                started.set()
                release.wait(timeout=10)
                return [{"name": "echo", "description": "", "inputSchema": {"type": "object", "properties": {}}}]

            def close(self):
                self.closed = True

        with patch("open_somnia.mcp.registry.MCPClient", side_effect=lambda server: _SlowClient(server)):
            registry.register_tools_background(tool_registry)
            self.assertTrue(started.wait(5))
            self.assertEqual(registry.server_summaries()[0]["status"], "connecting")
            registry.close()
            release.set()

        self.assertTrue(_wait_for(lambda: clients[0].closed))
        self.assertNotIn("mcp__s1__echo", tool_registry.names())
        self.assertNotIn("s1", registry.clients)

    def test_duplicate_background_connect_runs_once(self) -> None:
        registry = MCPRegistry([self._server()])
        tool_registry = ToolRegistry()
        release = threading.Event()
        constructions: list[str] = []

        class _SlowClient:
            def __init__(self, server):
                constructions.append(server.name)

            def list_tools(self):
                release.wait(timeout=10)
                return [{"name": "echo", "description": "", "inputSchema": {"type": "object", "properties": {}}}]

            def close(self):
                pass

        with patch("open_somnia.mcp.registry.MCPClient", side_effect=lambda server: _SlowClient(server)):
            registry.register_tools_background(tool_registry)
            registry.register_tools_background(tool_registry)
            release.set()

        self.assertTrue(_wait_for(lambda: "mcp__s1__echo" in tool_registry.names()))
        self.assertEqual(constructions, ["s1"])

    def test_refresh_during_connect_reports_connecting_without_double_connect(self) -> None:
        registry = MCPRegistry([self._server()])
        tool_registry = ToolRegistry()
        release = threading.Event()
        constructions: list[str] = []

        class _SlowClient:
            def __init__(self, server):
                constructions.append(server.name)

            def list_tools(self):
                release.wait(timeout=10)
                return [{"name": "echo", "description": "", "inputSchema": {"type": "object", "properties": {}}}]

            def close(self):
                pass

        with patch("open_somnia.mcp.registry.MCPClient", side_effect=lambda server: _SlowClient(server)):
            registry.register_tools_background(tool_registry)
            self.assertTrue(_wait_for(lambda: constructions == ["s1"]))
            summary = registry.refresh_server_tools("s1", registry=tool_registry)
            self.assertEqual(summary["status"], "connecting")
            release.set()

        self.assertTrue(_wait_for(lambda: "mcp__s1__echo" in tool_registry.names()))
        self.assertEqual(constructions, ["s1"])

    def test_schema_snapshots_are_safe_during_background_registration(self) -> None:
        registry = MCPRegistry([self._server()])
        tool_registry = ToolRegistry()
        release = threading.Event()

        class _SlowClient:
            def list_tools(self):
                release.wait(timeout=10)
                return [
                    {"name": f"tool{i}", "description": "", "inputSchema": {"type": "object", "properties": {}}}
                    for i in range(20)
                ]

            def close(self):
                pass

        with patch("open_somnia.mcp.registry.MCPClient", return_value=_SlowClient()):
            registry.register_tools_background(tool_registry)
            for _ in range(200):
                tool_registry.schemas()
                tool_registry.names()
            release.set()

        self.assertTrue(_wait_for(lambda: "mcp__s1__tool19" in tool_registry.names()))


if __name__ == "__main__":
    unittest.main()
