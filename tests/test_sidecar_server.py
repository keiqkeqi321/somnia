from __future__ import annotations

import base64
import json
import os
import socket
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desktop.backend.server import SidecarServer
from open_somnia.config.models import (
    AgentSettings,
    AppSettings,
    ModelTraits,
    ProviderProfileSettings,
    ProviderSettings,
    RuntimeSettings,
    StorageSettings,
)
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import AssistantTurn, ToolCall
from open_somnia.tools.registry import ToolDefinition


class SidecarServerTests(unittest.TestCase):
    def _socket_buffer(self, client: socket.socket) -> bytearray:
        buffers = getattr(self, "_socket_buffers", None)
        if buffers is None:
            buffers = {}
            self._socket_buffers = buffers
        return buffers.setdefault(id(client), bytearray())

    def _stable_test_dir(self, name: str) -> Path:
        root = Path.cwd() / ".tmp-tests" / f"{name}-{time.time_ns()}"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _make_settings(self, root: Path) -> AppSettings:
        data_dir = root / ".open_somnia"
        transcripts_dir = data_dir / "transcripts"
        sessions_dir = data_dir / "sessions"
        tasks_dir = data_dir / "tasks"
        inbox_dir = data_dir / "inbox"
        team_dir = data_dir / "team"
        jobs_dir = data_dir / "jobs"
        requests_dir = data_dir / "requests"
        logs_dir = data_dir / "logs"
        for path in [
            data_dir,
            transcripts_dir,
            sessions_dir,
            tasks_dir,
            inbox_dir,
            team_dir,
            jobs_dir,
            requests_dir,
            logs_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)
        return AppSettings(
            workspace_root=root,
            agent=AgentSettings(name="Somnia"),
            provider=ProviderSettings(
                name="openai",
                provider_type="openai",
                model="fake-model",
                api_key="fake",
                base_url="http://localhost",
            ),
            runtime=RuntimeSettings(),
            storage=StorageSettings(
                data_dir=data_dir,
                transcripts_dir=transcripts_dir,
                sessions_dir=sessions_dir,
                tasks_dir=tasks_dir,
                inbox_dir=inbox_dir,
                team_dir=team_dir,
                jobs_dir=jobs_dir,
                requests_dir=requests_dir,
                logs_dir=logs_dir,
            ),
            provider_profiles={
                "anthropic": ProviderProfileSettings(
                    name="anthropic",
                    provider_type="anthropic",
                    models=["claude-sonnet-4-5"],
                    default_model="claude-sonnet-4-5",
                    api_key="fake",
                    base_url="http://localhost",
                ),
                "openai": ProviderProfileSettings(
                    name="openai",
                    provider_type="openai",
                    models=["fake-model", "fake-model-mini"],
                    model_traits={
                        "fake-model": ModelTraits(context_window_tokens=64_000, supports_reasoning=True),
                        "fake-model-mini": ModelTraits(context_window_tokens=128_000, supports_reasoning=False),
                    },
                    default_model="fake-model",
                    api_key="fake",
                    base_url="http://localhost",
                ),
            },
        )

    def _request_json(self, method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=2.0) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body)

    def _connect_websocket(self, host: str, port: int) -> socket.socket:
        client = socket.create_connection((host, port), timeout=2.0)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET /ws HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        client.sendall(request.encode("ascii"))
        header = self._read_http_header(client)
        self.assertIn("101 Switching Protocols", header)
        return client

    def _read_http_header(self, client: socket.socket) -> str:
        buffer = self._socket_buffer(client)
        while True:
            chunk = client.recv(1024)
            if not chunk:
                break
            buffer.extend(chunk)
            marker = buffer.find(b"\r\n\r\n")
            if marker >= 0:
                break
        marker = buffer.find(b"\r\n\r\n")
        if marker < 0:
            header = bytes(buffer)
            buffer.clear()
            return header.decode("latin-1")
        header_end = marker + 4
        header = bytes(buffer[:header_end])
        del buffer[:header_end]
        return header.decode("latin-1")

    def _read_ws_event(self, client: socket.socket, timeout: float = 2.0) -> dict:
        client.settimeout(timeout)
        first = self._recv_exact(client, 2)
        first_byte, second_byte = first[0], first[1]
        opcode = first_byte & 0x0F
        payload_length = second_byte & 0x7F
        if payload_length == 126:
            payload_length = int.from_bytes(self._recv_exact(client, 2), "big")
        elif payload_length == 127:
            payload_length = int.from_bytes(self._recv_exact(client, 8), "big")
        payload = self._recv_exact(client, payload_length)
        if opcode == 0x8:
            return {"type": "socket_closed", "payload": {}}
        self.assertEqual(opcode, 0x1)
        return json.loads(payload.decode("utf-8"))

    def _recv_exact(self, client: socket.socket, size: int) -> bytes:
        remaining = size
        chunks: list[bytes] = []
        buffer = self._socket_buffer(client)
        if buffer:
            take = min(len(buffer), remaining)
            chunks.append(bytes(buffer[:take]))
            del buffer[:take]
            remaining -= take
        while remaining > 0:
            chunk = client.recv(remaining)
            if not chunk:
                raise ConnectionError("Socket closed while reading frame.")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _close_websocket(self, client: socket.socket) -> None:
        try:
            client.sendall(b"\x88\x00")
        except Exception:
            pass
        buffers = getattr(self, "_socket_buffers", None)
        if buffers is not None:
            buffers.pop(id(client), None)
        client.close()

    def _collect_events_until(self, client: socket.socket, predicate, timeout: float = 2.0) -> list[dict]:
        deadline = time.time() + timeout
        events: list[dict] = []
        while time.time() < deadline:
            event = self._read_ws_event(client, timeout=max(0.05, deadline - time.time()))
            events.append(event)
            if predicate(event):
                break
        return events

    def test_sidecar_runs_turn_without_cli_and_streams_events(self) -> None:
        root = self._stable_test_dir("sidecar-turn")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        server.runtime.complete = self._streaming_complete("Hello")
        try:
            server.start_background()
            self.assertTrue(server.wait_until_ready())
            client = self._connect_websocket(server.host, server.port)
            try:
                status, health = self._request_json("GET", f"{server.base_url}/health")
                self.assertEqual(status, 200)
                self.assertEqual(health["status"], "ready")

                _, session_response = self._request_json("POST", f"{server.base_url}/sessions", {})
                session_id = session_response["session"]["id"]

                _, turn_response = self._request_json(
                    "POST",
                    f"{server.base_url}/turns",
                    {"session_id": session_id, "user_input": "hello"},
                )
                turn_id = turn_response["turn_id"]
                events = self._collect_events_until(
                    client,
                    lambda event: event.get("type") == "assistant_completed" and event.get("turn_id") == turn_id,
                )

                event_types = [event["type"] for event in events]
                self.assertIn("sidecar_ready", event_types)
                self.assertIn("turn_started", event_types)
                self.assertIn("assistant_delta", event_types)
                self.assertIn("assistant_completed", event_types)

                _, session_payload = self._request_json("GET", f"{server.base_url}/sessions/{session_id}")
                self.assertEqual(session_payload["session"]["messages"][-1]["content"], "Hello")
            finally:
                self._close_websocket(client)
        finally:
            server.close()

    def test_mcp_servers_endpoint_includes_tool_previews(self) -> None:
        root = self._stable_test_dir("sidecar-mcp")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        server.runtime.mcp_registry = SimpleNamespace(
            server_summaries=lambda: [
                {
                    "name": "filesystem",
                    "transport": "stdio",
                    "target": "npx",
                    "enabled": True,
                    "status": "connected",
                    "error": "",
                    "tool_count": 1,
                }
            ],
            tool_summaries=lambda server_name: [
                {
                    "name": "read_file",
                    "description": "Read a file.",
                    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                }
            ]
            if server_name == "filesystem"
            else [],
            refresh_server_tools=lambda server_name, registry=None: {
                "name": server_name,
                "transport": "stdio",
                "target": "npx",
                "enabled": True,
                "status": "connected",
                "error": "",
                "tool_count": 2,
                "tools": [
                    {
                        "name": "read_file",
                        "description": "Read a file.",
                        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                    },
                    {
                        "name": "write_file",
                        "description": "Write a file.",
                        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                    },
                ],
            },
            set_server_enabled=lambda server_name, enabled, registry=None: {
                "name": server_name,
                "transport": "stdio",
                "target": "npx",
                "enabled": bool(enabled),
                "status": "connected" if enabled else "disabled",
                "error": "",
                "tool_count": 2 if enabled else 0,
                "tools": [
                    {
                        "name": "read_file",
                        "description": "Read a file.",
                        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                    },
                    {
                        "name": "write_file",
                        "description": "Write a file.",
                        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                    },
                ]
                if enabled
                else [],
            },
            close=lambda: None,
        )
        try:
            server.start_background()
            self.assertTrue(server.wait_until_ready())

            status, payload = self._request_json("GET", f"{server.base_url}/mcp/servers")

            self.assertEqual(status, 200)
            self.assertEqual(payload["servers"][0]["name"], "filesystem")
            self.assertEqual(payload["servers"][0]["tools"][0]["name"], "read_file")
            self.assertEqual(payload["servers"][0]["tools"][0]["input_schema"]["properties"]["path"]["type"], "string")

            status, debug_payload = self._request_json("POST", f"{server.base_url}/mcp/servers/filesystem/debug", {})
            self.assertEqual(status, 200)
            self.assertEqual(debug_payload["tool_count"], 2)
            self.assertEqual(debug_payload["server"]["tools"][1]["name"], "write_file")

            status, toggle_payload = self._request_json("POST", f"{server.base_url}/mcp/servers/filesystem/enabled", {"enabled": False})
            self.assertEqual(status, 200)
            self.assertFalse(toggle_payload["enabled"])
            self.assertEqual(toggle_payload["tool_count"], 0)
        finally:
            server.close()

    def test_workspace_image_endpoint_serves_only_workspace_images(self) -> None:
        root = self._stable_test_dir("sidecar-image")
        image_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
        )
        (root / "qr.png").write_bytes(image_bytes)
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        server.start_background()
        try:
            with urllib.request.urlopen(f"{server.base_url}/workspace/images?path=qr.png", timeout=2.0) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("Content-Type"), "image/png")
                self.assertEqual(response.read(), image_bytes)

            with self.assertRaises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"{server.base_url}/workspace/images?path=../qr.png", timeout=2.0)
            self.assertEqual(exc.exception.code, 403)
        finally:
            server.close()

    def test_saving_mcp_config_reloads_runtime_tools_immediately(self) -> None:
        root = self._stable_test_dir("sidecar-mcp-save")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        server.runtime.registry.register(
            ToolDefinition(
                name="mcp__old__stale",
                description="stale",
                input_schema={"type": "object", "properties": {}},
                handler=lambda ctx, payload: "stale",
            )
        )

        class _FakeMCPRegistry:
            def __init__(self, servers) -> None:
                self.servers = servers

            def register_tools(self, registry) -> None:
                for mcp_server in self.servers:
                    registry.register(
                        ToolDefinition(
                            name=f"mcp__{mcp_server.name}__ping",
                            description="Ping",
                            input_schema={"type": "object", "properties": {}},
                            handler=lambda ctx, payload: "pong",
                        )
                    )

            def close(self) -> None:
                return None

        try:
            server.start_background()
            self.assertTrue(server.wait_until_ready())
            with patch("desktop.backend.server.MCPRegistry", _FakeMCPRegistry):
                result = server.save_config_section(
                    scope="project",
                    section="mcp",
                    content='[mcp_servers.fresh]\ntransport = "stdio"\ncommand = "fresh-command"\n',
                )

            self.assertTrue(result["runtime_reloaded"])
            self.assertIn("fresh", [item.name for item in server.runtime.settings.mcp_servers])
            self.assertIn("mcp__fresh__ping", server.runtime.registry.names())
            self.assertNotIn("mcp__old__stale", server.runtime.registry.names())
        finally:
            server.close()

    def test_sidecar_session_list_returns_lightweight_summaries(self) -> None:
        root = self._stable_test_dir("sidecar-session-summaries")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        try:
            server.start_background()
            self.assertTrue(server.wait_until_ready())
            session = server.service.create_session()
            session.messages.extend(
                [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "Hello from history"},
                ]
            )
            server.runtime.session_manager.save(session)

            listed = next(item for item in server.list_sessions() if item["id"] == session.id)
            self.assertEqual(listed["messages"], [])
            self.assertTrue(listed["is_summary"])
            self.assertIn("Hello from history", listed["preview"])

            detail_payload = server.load_session(session.id)
            self.assertNotEqual(detail_payload["messages"], [])
        finally:
            server.close()

    def test_sidecar_emits_authorization_request_and_accepts_external_resolution(self) -> None:
        root = self._stable_test_dir("sidecar-auth")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCall(
                            "call-1",
                            "request_authorization",
                            {
                                "tool_name": "bash",
                                "reason": "Need to inspect git state",
                                "argument_summary": "git status",
                            },
                        )
                    ],
                ),
                AssistantTurn(stop_reason="end_turn", text_blocks=["Authorized."]),
            ]
        )
        server.runtime.complete = lambda *args, **kwargs: next(turns)
        try:
            server.start_background()
            client = self._connect_websocket(server.host, server.port)
            try:
                _, session_response = self._request_json("POST", f"{server.base_url}/sessions", {})
                session_id = session_response["session"]["id"]
                _, turn_response = self._request_json(
                    "POST",
                    f"{server.base_url}/turns",
                    {"session_id": session_id, "user_input": "inspect repo"},
                )
                turn_id = turn_response["turn_id"]

                events = self._collect_events_until(
                    client,
                    lambda event: event.get("type") == "authorization_requested" and event.get("turn_id") == turn_id,
                )
                request_event = next(event for event in events if event.get("type") == "authorization_requested")
                request_id = request_event["payload"]["request_id"]

                status, resolve_response = self._request_json(
                    "POST",
                    f"{server.base_url}/interactions/{request_id}/authorization",
                    {"scope": "once", "approved": True, "reason": "Allowed once."},
                )
                self.assertEqual(status, 200)
                self.assertTrue(resolve_response["resolved"])

                events.extend(
                    self._collect_events_until(
                        client,
                        lambda event: event.get("type") == "assistant_completed" and event.get("turn_id") == turn_id,
                    )
                )
                self.assertIn("assistant_completed", [event["type"] for event in events])

                _, interactions_payload = self._request_json("GET", f"{server.base_url}/interactions")
                self.assertEqual(interactions_payload["interactions"], [])
            finally:
                self._close_websocket(client)
        finally:
            server.close()

    def test_sidecar_interrupt_endpoint_stops_active_turn(self) -> None:
        root = self._stable_test_dir("sidecar-interrupt")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            while should_interrupt is not None and not should_interrupt():
                time.sleep(0.01)
            raise TurnInterrupted("Interrupted by user.")

        server.runtime.complete = fake_complete
        try:
            server.start_background()
            client = self._connect_websocket(server.host, server.port)
            try:
                _, session_response = self._request_json("POST", f"{server.base_url}/sessions", {})
                session_id = session_response["session"]["id"]
                _, turn_response = self._request_json(
                    "POST",
                    f"{server.base_url}/turns",
                    {"session_id": session_id, "user_input": "long task"},
                )
                turn_id = turn_response["turn_id"]

                status, interrupt_response = self._request_json(
                    "POST",
                    f"{server.base_url}/turns/{turn_id}/interrupt",
                    {},
                )
                self.assertEqual(status, 200)
                self.assertTrue(interrupt_response["interrupted"])

                events = self._collect_events_until(
                    client,
                    lambda event: event.get("type") == "interrupt_completed" and event.get("turn_id") == turn_id,
                )
                self.assertIn("interrupt_completed", [event["type"] for event in events])
            finally:
                self._close_websocket(client)
        finally:
            server.close()

    def test_sidecar_switches_provider_model_over_http(self) -> None:
        root = self._stable_test_dir("sidecar-provider")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        try:
            server.start_background()
            _, providers_payload = self._request_json("GET", f"{server.base_url}/providers")
            provider_names = [provider["name"] for provider in providers_payload["providers"]]
            self.assertEqual(provider_names, ["anthropic", "openai"])

            status, switch_payload = self._request_json(
                "POST",
                f"{server.base_url}/providers/switch",
                {"provider_name": "anthropic", "model": "claude-sonnet-4-5"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(switch_payload["provider"], "anthropic")
            self.assertEqual(switch_payload["model"], "claude-sonnet-4-5")
            self.assertEqual(server.runtime.settings.provider.name, "anthropic")
            self.assertEqual(server.runtime.settings.provider.model, "claude-sonnet-4-5")
        finally:
            server.close()

    def test_sidecar_sets_vision_model_over_http(self) -> None:
        root = self._stable_test_dir("sidecar-vision-model")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        try:
            server.start_background()
            status, payload = self._request_json(
                "POST",
                f"{server.base_url}/vision-model",
                {"provider_name": "openai", "vision_provider": "openai", "vision_model": "fake-model-mini"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["provider"], "openai")
            self.assertEqual(payload["model"], "fake-model")
            self.assertEqual(payload["vision_provider"], "openai")
            self.assertEqual(payload["vision_model"], "fake-model-mini")
            self.assertEqual(server.runtime.settings.provider.vision_provider, "openai")
            self.assertEqual(server.runtime.settings.provider.vision_model, "fake-model-mini")
        finally:
            server.close()

    def test_sidecar_exposes_runtime_status_and_tool_logs(self) -> None:
        root = self._stable_test_dir("sidecar-status")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        try:
            server.start_background()
            self.assertTrue(server.wait_until_ready())

            status, runtime_payload = self._request_json("GET", f"{server.base_url}/runtime/status")
            self.assertEqual(status, 200)
            self.assertEqual(runtime_payload["status"], "ready")
            self.assertIn("execution_mode", runtime_payload)
            self.assertIn("execution_mode_title", runtime_payload)
            self.assertEqual(runtime_payload["open_session_count"], 0)
            self.assertEqual(runtime_payload["pending_interaction_count"], 0)

            session_status, session_response = self._request_json("POST", f"{server.base_url}/sessions", {})
            self.assertEqual(session_status, 201)
            self.assertIn("id", session_response["session"])

            _, updated_runtime_payload = self._request_json("GET", f"{server.base_url}/runtime/status")
            self.assertEqual(updated_runtime_payload["open_session_count"], 1)

            log_entry = server.runtime.tool_log_store.write(
                actor="lead",
                tool_name="bash",
                tool_input={"command": "git status"},
                output="clean",
                category="TOOL",
            )

            list_status, list_payload = self._request_json("GET", f"{server.base_url}/tool-logs?limit=10")
            self.assertEqual(list_status, 200)
            self.assertEqual(len(list_payload["tool_logs"]), 1)
            self.assertEqual(list_payload["tool_logs"][0]["id"], log_entry["id"])
            self.assertEqual(list_payload["tool_logs"][0]["tool_name"], "bash")

            detail_status, detail_payload = self._request_json("GET", f"{server.base_url}/tool-logs/{log_entry['id']}")
            self.assertEqual(detail_status, 200)
            self.assertEqual(detail_payload["tool_log"]["id"], log_entry["id"])
            self.assertEqual(detail_payload["tool_log"]["tool_input"]["command"], "git status")
            self.assertIn("rendered", detail_payload["tool_log"])
        finally:
            server.close()

    def test_sidecar_workspace_path_completion_uses_fast_shared_scanner(self) -> None:
        root = self._stable_test_dir("sidecar-path-completion")
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
        (root / "README.md").write_text("hello", encoding="utf-8")
        (root / "node_modules" / "pkg").mkdir(parents=True)
        (root / "node_modules" / "pkg" / "app.js").write_text("ignored", encoding="utf-8")
        (root / ".local-tools" / "cargo" / "registry").mkdir(parents=True)
        (root / ".local-tools" / "cargo" / "registry" / "app.rs").write_text("ignored", encoding="utf-8")
        (root / ".tmp-system" / "cache").mkdir(parents=True)
        (root / ".tmp-system" / "cache" / "app.log").write_text("ignored", encoding="utf-8")
        (root / "tmp04u5700e").mkdir()
        (root / "tmp04u5700e" / "app.tmp").write_text("ignored", encoding="utf-8")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        server.start_background()
        self.assertTrue(server.wait_until_ready())
        try:
            server._workspace_path_candidates = lambda: (_ for _ in ()).throw(AssertionError("full scan should not run"))

            top_level = server.list_workspace_paths(query="", limit=30)

            self.assertEqual([item["path"] for item in top_level], ["src", "README.md"])
            del server._workspace_path_candidates

            matches = server.list_workspace_paths(query="app", limit=30)
        finally:
            server.close()

        paths = [item["path"] for item in matches]
        self.assertIn("src/app.py", paths)
        self.assertFalse(
            any(
                ignored in path
                for path in paths
                for ignored in ["node_modules", ".local-tools", ".tmp-system", "tmp04u5700e", ".open_somnia"]
            )
        )

    def test_sidecar_thinking_log_endpoint_reads_only_workspace_thinking_logs(self) -> None:
        root = self._stable_test_dir("sidecar-thinking-log")
        settings = self._make_settings(root)
        thinking_root = settings.storage.transcripts_dir / "thinking"
        thinking_root.mkdir(parents=True, exist_ok=True)
        thinking_path = thinking_root / "session.turn.jsonl"
        thinking_path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "thinking_delta", "delta": "private "}),
                    json.dumps({"type": "thinking", "thinking": "reasoning"}),
                ]
            ),
            encoding="utf-8",
        )
        outside_path = root / "outside.jsonl"
        outside_path.write_text(json.dumps({"type": "thinking_delta", "delta": "outside"}), encoding="utf-8")
        server = SidecarServer.from_settings(settings, host="127.0.0.1", port=0)
        server.start_background()
        self.assertTrue(server.wait_until_ready())
        try:
            query = urllib.parse.urlencode({"path": str(thinking_path)})
            status, payload = self._request_json("GET", f"{server.base_url}/thinking-log?{query}")

            self.assertEqual(status, 200)
            self.assertEqual(payload["thinking_log"]["text"], "private reasoning")

            outside_query = urllib.parse.urlencode({"path": str(outside_path)})
            with self.assertRaises(urllib.error.HTTPError) as context:
                self._request_json("GET", f"{server.base_url}/thinking-log?{outside_query}")
            self.assertEqual(context.exception.code, 403)
        finally:
            server.close()

    def _streaming_complete(self, final_text: str):
        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            if text_callback is not None:
                midpoint = max(1, len(final_text) // 2)
                text_callback(final_text[:midpoint])
                text_callback(final_text[midpoint:])
            return AssistantTurn(stop_reason="end_turn", text_blocks=[final_text])

        return fake_complete


if __name__ == "__main__":
    unittest.main()
