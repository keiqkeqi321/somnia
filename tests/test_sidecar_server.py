from __future__ import annotations

import base64
import json
import os
import socket
import threading
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
                state_dir=data_dir / "state",
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

    def test_runtime_status_stays_available_during_an_active_turn(self) -> None:
        root = self._stable_test_dir("sidecar-status-mid-turn")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        release = threading.Event()

        def blocking_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            if text_callback is not None:
                text_callback("partial ")
            release.wait(timeout=10)
            return AssistantTurn(stop_reason="end_turn", text_blocks=["done"])

        server.runtime.complete = blocking_complete
        try:
            server.start_background()
            self.assertTrue(server.wait_until_ready())
            _, session_response = self._request_json("POST", f"{server.base_url}/sessions", {})
            session_id = session_response["session"]["id"]
            _, turn_response = self._request_json(
                "POST",
                f"{server.base_url}/turns",
                {"session_id": session_id, "user_input": "hello"},
            )
            status, payload = self._request_json("GET", f"{server.base_url}/runtime/status")
            self.assertEqual(status, 200)
            self.assertEqual(
                payload["active_turns"],
                [{"turn_id": turn_response["turn_id"], "session_id": session_id}],
            )
        finally:
            release.set()
            server.close()

    def test_sidecar_expands_init_command_before_starting_turn(self) -> None:
        root = self._stable_test_dir("sidecar-init-command")
        (root / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        session = server.service.create_session()
        seen_inputs: list[str] = []

        class _DoneHandle:
            turn_id = "turn-init"
            result = None

            def __init__(self, target_session) -> None:
                self.session = target_session

            def is_done(self) -> bool:
                return True

            def drain_events(self, *, block: bool = False, timeout: float | None = None) -> list:
                return []

        def fake_run_turn(target_session, user_input):
            seen_inputs.append(user_input)
            return _DoneHandle(target_session)

        server.service.run_turn = fake_run_turn
        try:
            server.start_background()
            self.assertTrue(server.wait_until_ready())
            response = server.start_turn(session.id, "/init --force focus on tests")

            self.assertEqual(response["turn_id"], "turn-init")
            self.assertEqual(len(seen_inputs), 1)
            self.assertIn("Initialize project instructions for this workspace.", seen_inputs[0])
            self.assertIn("Target file: AGENTS.md", seen_inputs[0])
            self.assertIn("Overwrite existing file: yes", seen_inputs[0])
            self.assertIn("focus on tests", seen_inputs[0])
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

    def test_mcp_tool_enabled_endpoint_persists_to_workspace_config(self) -> None:
        root = self._stable_test_dir("sidecar-mcp-tool")
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
                    "enabled_tool_count": 1,
                }
            ],
            tool_summaries=lambda server_name: [
                {
                    "name": "read_file",
                    "description": "Read a file.",
                    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                    "enabled": True,
                }
            ]
            if server_name == "filesystem"
            else [],
            close=lambda: None,
        )
        # Skip the full runtime reload; this test covers routing, persistence,
        # and the response contract, not the reload machinery.
        server.reload_mcp_runtime = lambda: None
        try:
            server.start_background()
            self.assertTrue(server.wait_until_ready())

            status, payload = self._request_json(
                "POST",
                f"{server.base_url}/mcp/servers/filesystem/tools/read_file/enabled",
                {"enabled": False},
            )

            self.assertEqual(status, 200)
            self.assertEqual(payload["tool"], "read_file")
            self.assertFalse(payload["enabled"])
            self.assertEqual(payload["server"]["name"], "filesystem")
            config_text = (root / ".open_somnia" / "open_somnia.toml").read_text(encoding="utf-8")
            self.assertIn("[mcp_servers.filesystem]", config_text)
            self.assertIn('exclude_tools = ["read_file"]', config_text)
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

    def test_saving_provider_config_reloads_unconfigured_runtime_immediately(self) -> None:
        root = self._stable_test_dir("sidecar-provider-save")
        settings = self._make_settings(root)
        settings.provider = ProviderSettings(
            name="unconfigured",
            provider_type="openai",
            model="",
            api_key="",
            context_window_tokens=200_000,
        )
        settings.provider_profiles = {}
        server = SidecarServer.from_settings(settings, host="127.0.0.1", port=0)
        home = root / "home"
        try:
            server.start_background()
            self.assertTrue(server.wait_until_ready())
            with patch("pathlib.Path.home", return_value=home):
                result = server.save_config_section(
                    scope="project",
                    section="provider",
                    content="\n".join(
                        [
                            "[providers]",
                            'default = "mimo"',
                            "",
                            "[providers.mimo]",
                            'provider_type = "anthropic"',
                            'models = ["MiMo-V2.5-Pro"]',
                            'default_model = "MiMo-V2.5-Pro"',
                            'api_key = "fake"',
                            "",
                        ]
                    ),
                )

            self.assertTrue(result["runtime_reloaded"])
            self.assertFalse(result["restart_required"])
            self.assertEqual(server.runtime.settings.provider.name, "mimo")
            self.assertEqual(server.runtime.settings.provider.model, "mimo-v2.5-pro")
            self.assertIn("mimo", [item["name"] for item in server.list_providers()])
        finally:
            server.close()

    def test_saving_runtime_config_reloads_limits_immediately(self) -> None:
        root = self._stable_test_dir("sidecar-runtime-save")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        home = root / "home"
        content = "\n".join(
            [
                "[runtime]",
                "exploration_soft_limit = 7",
                "exploration_hard_streak_limit = 9",
                "exploration_hard_total_limit = 17",
                "command_timeout_seconds = 45",
                "max_tool_output_chars = 12345",
                "",
            ]
        )
        try:
            server.start_background()
            self.assertTrue(server.wait_until_ready())
            with patch("pathlib.Path.home", return_value=home):
                result = server.save_config_section(scope="project", section="runtime", content=content)
                payload = server.config_payload()

            self.assertTrue(result["runtime_reloaded"])
            self.assertFalse(result["restart_required"])
            self.assertEqual(server.runtime.settings.runtime.exploration_soft_limit, 7)
            self.assertEqual(server.runtime.settings.runtime.exploration_hard_streak_limit, 9)
            self.assertEqual(server.runtime.settings.runtime.exploration_hard_total_limit, 17)
            self.assertEqual(server.runtime.background_manager.default_timeout, 45)
            self.assertEqual(server.runtime.background_manager.max_output_chars, 12345)
            project_scope = next(item for item in payload["scopes"] if item["scope"] == "project")
            self.assertIn("exploration_soft_limit = 7", project_scope["sections"]["runtime"])
            self.assertIn("exploration_hard_total_limit = 17", project_scope["sections"]["runtime"])
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

    def test_sidecar_emits_question_request_and_accepts_external_resolution(self) -> None:
        root = self._stable_test_dir("sidecar-question")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCall(
                            "call-1",
                            "ask_user_question",
                            {
                                "question": "Which approach?",
                                "options": ["Option A", "Option B"],
                                "allow_custom": True,
                            },
                        )
                    ],
                ),
                AssistantTurn(stop_reason="end_turn", text_blocks=["Answered."]),
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
                    {"session_id": session_id, "user_input": "choose approach"},
                )
                turn_id = turn_response["turn_id"]

                events = self._collect_events_until(
                    client,
                    lambda event: event.get("type") == "question_requested" and event.get("turn_id") == turn_id,
                )
                request_event = next(event for event in events if event.get("type") == "question_requested")
                request_id = request_event["payload"]["request_id"]
                self.assertEqual(request_event["payload"]["question"], "Which approach?")
                self.assertEqual(request_event["payload"]["options"], ["Option A", "Option B"])

                status, resolve_response = self._request_json(
                    "POST",
                    f"{server.base_url}/interactions/{request_id}/question",
                    {"answer": "Option A", "selected_option": "Option A", "status": "answered"},
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
        config_path = root / ".open_somnia" / "open_somnia.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "\n".join(
                [
                    "[providers]",
                    'default = "openai"',
                    "",
                    "[providers.openai]",
                    'provider_type = "openai"',
                    'models = ["fake-model", "fake-model-mini"]',
                    'default_model = "fake-model"',
                    'api_key = "fake"',
                    'base_url = "http://localhost"',
                ]
            ),
            encoding="utf-8",
        )
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        try:
            server.start_background()
            status, payload = self._request_json(
                "POST",
                f"{server.base_url}/vision-model",
                {"vision_provider": "openai", "vision_model": "fake-model-mini"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["provider"], "openai")
            self.assertEqual(payload["model"], "fake-model")
            self.assertEqual(payload["vision_provider"], "openai")
            self.assertEqual(payload["vision_model"], "fake-model-mini")
            self.assertEqual(server.runtime.settings.vision_provider, "openai")
            self.assertEqual(server.runtime.settings.vision_model, "fake-model-mini")
        finally:
            server.close()

    def test_sidecar_pins_session_model_independently_of_workspace_default(self) -> None:
        root = self._stable_test_dir("sidecar-session-model")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        try:
            server.start_background()
            _, create_payload = self._request_json("POST", f"{server.base_url}/sessions", {})
            session_id = create_payload["session"]["id"]
            # Sanity: a fresh session follows the workspace default (openai/fake-model).
            self.assertIsNone(create_payload["session"].get("provider_override"))
            self.assertIsNone(create_payload["session"].get("model_override"))

            status, pin_payload = self._request_json(
                "POST",
                f"{server.base_url}/sessions/{session_id}/model",
                {"provider_name": "openai", "model": "fake-model-mini"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(pin_payload["pinned"])
            self.assertEqual(pin_payload["provider"], "openai")
            self.assertEqual(pin_payload["model"], "fake-model-mini")
            self.assertEqual(pin_payload["session"]["provider_override"], "openai")
            self.assertEqual(pin_payload["session"]["model_override"], "fake-model-mini")

            # The workspace-wide default is untouched: other sessions keep fake-model.
            self.assertEqual(server.runtime.settings.provider.name, "openai")
            self.assertEqual(server.runtime.settings.provider.model, "fake-model")

            # The pin survives a fresh GET /sessions/{id}.
            _, reload_payload = self._request_json("GET", f"{server.base_url}/sessions/{session_id}")
            self.assertEqual(reload_payload["session"]["provider_override"], "openai")
            self.assertEqual(reload_payload["session"]["model_override"], "fake-model-mini")

            # The pin also appears in the session summaries list.
            _, summaries_payload = self._request_json("GET", f"{server.base_url}/sessions")
            target = next(s for s in summaries_payload["sessions"] if s["id"] == session_id)
            self.assertEqual(target["provider_override"], "openai")
            self.assertEqual(target["model_override"], "fake-model-mini")

            # Clearing the pin (both fields omitted) returns to default-following.
            status, clear_payload = self._request_json(
                "POST",
                f"{server.base_url}/sessions/{session_id}/model",
                {},
            )
            self.assertEqual(status, 200)
            self.assertFalse(clear_payload["pinned"])
            self.assertEqual(clear_payload["model"], "fake-model")
            self.assertIsNone(clear_payload["session"].get("provider_override"))
            self.assertIsNone(clear_payload["session"].get("model_override"))
        finally:
            server.close()

    def test_sidecar_rejects_half_set_session_model_payload(self) -> None:
        root = self._stable_test_dir("sidecar-session-model-validation")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        try:
            server.start_background()
            _, create_payload = self._request_json("POST", f"{server.base_url}/sessions", {})
            session_id = create_payload["session"]["id"]
            # Only provider_name set: ambiguous, must be rejected.
            with self.assertRaises(urllib.error.HTTPError) as context:
                self._request_json(
                    "POST",
                    f"{server.base_url}/sessions/{session_id}/model",
                    {"provider_name": "openai"},
                )
            self.assertEqual(context.exception.code, 400)
            # Unknown model is rejected by the runtime validation.
            with self.assertRaises(urllib.error.HTTPError) as context:
                self._request_json(
                    "POST",
                    f"{server.base_url}/sessions/{session_id}/model",
                    {"provider_name": "openai", "model": "nope"},
                )
            self.assertEqual(context.exception.code, 400)
        finally:
            server.close()

    def test_sidecar_new_session_starts_fresh_and_carries_model_pin(self) -> None:
        root = self._stable_test_dir("sidecar-session-new")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        try:
            server.start_background()
            self.assertTrue(server.wait_until_ready())
            ws = self._connect_websocket(server.host, server.port)
            try:
                _, create_payload = self._request_json("POST", f"{server.base_url}/sessions", {})
                old_id = create_payload["session"]["id"]
                status, _ = self._request_json(
                    "POST",
                    f"{server.base_url}/sessions/{old_id}/model",
                    {"provider_name": "openai", "model": "fake-model-mini"},
                )
                self.assertEqual(status, 200)

                status, new_payload = self._request_json("POST", f"{server.base_url}/sessions/{old_id}/new", {})
                self.assertEqual(status, 200)
                fresh = new_payload["session"]
                self.assertEqual(new_payload["previous_session_id"], old_id)
                self.assertNotEqual(fresh["id"], old_id)
                self.assertEqual(fresh["messages"], [])
                self.assertEqual(fresh["provider_override"], "openai")
                self.assertEqual(fresh["model_override"], "fake-model-mini")

                # The carried pin is persisted, not just in memory.
                _, reload_payload = self._request_json("GET", f"{server.base_url}/sessions/{fresh['id']}")
                self.assertEqual(reload_payload["session"]["provider_override"], "openai")
                self.assertEqual(reload_payload["session"]["model_override"], "fake-model-mini")

                # The old session is untouched and still loads.
                status, old_payload = self._request_json("GET", f"{server.base_url}/sessions/{old_id}")
                self.assertEqual(status, 200)
                self.assertEqual(old_payload["session"]["id"], old_id)

                # The swap is announced as a session_created event.
                events = self._collect_events_until(
                    ws,
                    lambda event: event.get("type") == "session_created" and event.get("session_id") == fresh["id"],
                )
                self.assertTrue(
                    any(
                        event.get("type") == "session_created" and event.get("session_id") == fresh["id"]
                        for event in events
                    )
                )
            finally:
                self._close_websocket(ws)
        finally:
            server.close()

    def test_sidecar_new_session_without_pin_follows_default(self) -> None:
        root = self._stable_test_dir("sidecar-session-new-default")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        try:
            server.start_background()
            _, create_payload = self._request_json("POST", f"{server.base_url}/sessions", {})
            old_id = create_payload["session"]["id"]

            status, new_payload = self._request_json("POST", f"{server.base_url}/sessions/{old_id}/new", {})
            self.assertEqual(status, 200)
            fresh = new_payload["session"]
            self.assertNotEqual(fresh["id"], old_id)
            self.assertIsNone(fresh.get("provider_override"))
            self.assertIsNone(fresh.get("model_override"))
        finally:
            server.close()

    def test_sidecar_new_session_unknown_id_returns_404(self) -> None:
        root = self._stable_test_dir("sidecar-session-new-404")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        try:
            server.start_background()
            with self.assertRaises(urllib.error.HTTPError) as context:
                self._request_json("POST", f"{server.base_url}/sessions/nope/new", {})
            self.assertEqual(context.exception.code, 404)
            # The same missing-session contract holds for the other session routes.
            with self.assertRaises(urllib.error.HTTPError) as context:
                self._request_json("GET", f"{server.base_url}/sessions/nope")
            self.assertEqual(context.exception.code, 404)
            with self.assertRaises(urllib.error.HTTPError) as context:
                self._request_json("POST", f"{server.base_url}/sessions/nope/compact", {})
            self.assertEqual(context.exception.code, 404)
        finally:
            server.close()

    def test_sidecar_session_context_usage_uses_pinned_model_window(self) -> None:
        root = self._stable_test_dir("sidecar-session-context-usage-pin")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        try:
            server.start_background()
            _, create_payload = self._request_json("POST", f"{server.base_url}/sessions", {})
            session_id = create_payload["session"]["id"]
            # The workspace default (fake-model) has a 64k window; the pin target
            # (fake-model-mini) has 128k. The session payload must report the
            # pinned model's window, not the default's.
            self._request_json(
                "POST",
                f"{server.base_url}/sessions/{session_id}/model",
                {"provider_name": "openai", "model": "fake-model-mini"},
            )
            _, payload = self._request_json("GET", f"{server.base_url}/sessions/{session_id}")
            usage = payload["session"].get("context_window_usage")
            self.assertIsNotNone(usage)
            self.assertEqual(usage["max_tokens"], 128_000)
        finally:
            server.close()

    def test_sidecar_session_model_pin_accepts_reasoning_level(self) -> None:
        root = self._stable_test_dir("sidecar-session-model-reasoning")
        server = SidecarServer.from_settings(self._make_settings(root), host="127.0.0.1", port=0)
        try:
            server.start_background()
            _, create_payload = self._request_json("POST", f"{server.base_url}/sessions", {})
            session_id = create_payload["session"]["id"]
            traits = server.runtime.settings.provider_profiles["openai"].model_traits["fake-model-mini"]

            # Pin with a concrete level: stored on the pinned model's traits.
            status, payload = self._request_json(
                "POST",
                f"{server.base_url}/sessions/{session_id}/model",
                {"provider_name": "openai", "model": "fake-model-mini", "reasoning_level": "high"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["reasoning_level"], "high")
            self.assertEqual(traits.reasoning_level, "high")
            config_text = (root / ".open_somnia" / "open_somnia.toml").read_text(encoding="utf-8")
            self.assertIn('reasoning_level = "high"', config_text)

            # Key omitted: the stored level is untouched.
            _, payload = self._request_json(
                "POST",
                f"{server.base_url}/sessions/{session_id}/model",
                {"provider_name": "openai", "model": "fake-model-mini"},
            )
            self.assertEqual(payload["reasoning_level"], "high")
            self.assertEqual(traits.reasoning_level, "high")

            # An explicit null clears the level back to auto.
            _, payload = self._request_json(
                "POST",
                f"{server.base_url}/sessions/{session_id}/model",
                {"provider_name": "openai", "model": "fake-model-mini", "reasoning_level": None},
            )
            self.assertIsNone(payload["reasoning_level"])
            self.assertIsNone(traits.reasoning_level)

            # A level without a pin has no model to attach to: rejected.
            with self.assertRaises(urllib.error.HTTPError) as context:
                self._request_json(
                    "POST",
                    f"{server.base_url}/sessions/{session_id}/model",
                    {"reasoning_level": "high"},
                )
            self.assertEqual(context.exception.code, 400)
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
