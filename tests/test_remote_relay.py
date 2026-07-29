from __future__ import annotations

import asyncio
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from open_somnia.remote.relay import RelayHub, _Peer, create_relay_app
from tests.remote_auth_support import BROWSER_ORIGIN, authenticate_connector, login, pair_device


class RemoteRelayTests(unittest.TestCase):
    def test_slow_browser_is_disconnected_for_resync_without_blocking_connector_delivery(self) -> None:
        async def scenario() -> tuple[dict, dict]:
            app = create_relay_app(administrators={"admin": "admin-password"}, client_send_timeout_seconds=0.01)
            hub: RelayHub = app.state.relay_hub
            account = app.state.remote_auth.authenticate_password("admin", "admin-password", source="test")
            tokens = app.state.remote_auth.issue_browser_tokens(account.id)
            slow_socket = _SlowSocket()
            slow_peer = _Peer(
                socket=slow_socket,
                account_id=account.id,
                access_token=tokens.access_token,
                send_timeout_seconds=0.01,
            )
            hub._clients["device-1"] = {"slow": slow_peer}
            hub._connectors["device-1"] = _Peer(
                socket=SimpleNamespace(),
                account_id=account.id,
                send_timeout_seconds=0.01,
            )

            await hub._broadcast_to_clients("device-1", {"kind": "event", "payload": "transient"})
            await asyncio.sleep(0.03)
            return slow_socket.closed, hub._clients.get("device-1", {})

        closed, clients = asyncio.run(scenario())
        self.assertEqual(closed, {"code": 4008, "reason": "Client too slow; resync required."})
        self.assertEqual(clients, {})

    def test_burst_events_are_queued_instead_of_disconnecting_the_client(self) -> None:
        async def scenario() -> tuple[list, dict, dict]:
            app = create_relay_app(administrators={"admin": "admin-password"}, client_send_timeout_seconds=0.5)
            hub: RelayHub = app.state.relay_hub
            account = app.state.remote_auth.authenticate_password("admin", "admin-password", source="test")
            tokens = app.state.remote_auth.issue_browser_tokens(account.id)
            burst_socket = _DelaySocket(0.02)
            burst_peer = _Peer(
                socket=burst_socket,
                account_id=account.id,
                access_token=tokens.access_token,
                send_timeout_seconds=0.5,
            )
            hub._clients["device-1"] = {"burst": burst_peer}
            hub._connectors["device-1"] = _Peer(
                socket=SimpleNamespace(),
                account_id=account.id,
                send_timeout_seconds=0.5,
            )

            for index in range(3):
                await hub._broadcast_to_clients("device-1", {"kind": "event", "seq": index})
            await asyncio.sleep(0.3)
            return burst_socket.received, burst_socket.closed, hub._clients.get("device-1", {})

        received, closed, clients = asyncio.run(scenario())
        self.assertEqual([message["seq"] for message in received], [0, 1, 2])
        self.assertIsNone(closed)
        self.assertTrue(clients)

    def test_health_endpoint_reports_ready_without_storing_state(self) -> None:
        with TestClient(create_relay_app()) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})

    def test_relay_forwards_requests_responses_and_events_without_interpreting_payloads(self) -> None:
        with TestClient(create_relay_app(administrators={"admin": "admin-password"})) as client:
            login(client)
            private_key, device_id = pair_device(client)
            with (
                client.websocket_connect(f"/ws/connector/{device_id}") as connector,
                client.websocket_connect(
                    f"/ws/client/{device_id}", headers={"origin": BROWSER_ORIGIN}
                ) as browser,
            ):
                authenticate_connector(connector, device_id, private_key)
                request = {
                    "kind": "request",
                    "request_id": "request-1",
                    "project_id": "project-1",
                    "method": "turn.start",
                    "params": {"session_id": "session-1", "user_input": "hello"},
                }
                browser.send_json(request)
                self.assertEqual(connector.receive_json(), request)

                response = {
                    "kind": "response",
                    "request_id": "request-1",
                    "ok": True,
                    "result": {"turn_id": "turn-1", "session_id": "session-1"},
                }
                connector.send_json(response)
                self.assertEqual(browser.receive_json(), response)

                event = {
                    "kind": "event",
                    "project_id": "project-1",
                    "event": {
                        "type": "assistant_delta",
                        "session_id": "session-1",
                        "turn_id": "turn-1",
                        "payload": {"delta": "streamed"},
                    },
                }
                connector.send_json(event)
                self.assertEqual(browser.receive_json(), event)

    def test_relay_rejects_content_when_device_is_offline_instead_of_queueing_it(self) -> None:
        with TestClient(create_relay_app(administrators={"admin": "admin-password"})) as client:
            login(client)
            _, device_id = pair_device(client)
            with client.websocket_connect(
                f"/ws/client/{device_id}", headers={"origin": BROWSER_ORIGIN}
            ) as browser:
                browser.send_json(
                    {
                        "kind": "request",
                        "request_id": "request-2",
                        "project_id": "project-1",
                        "method": "turn.start",
                        "params": {"session_id": "session-1", "user_input": "do not retain"},
                    }
                )
                self.assertEqual(
                    browser.receive_json(),
                    {
                        "kind": "response",
                        "request_id": "request-2",
                        "ok": False,
                        "error": "Device is offline.",
                    },
                )

    def test_relay_closes_oversized_content_frames_before_forwarding(self) -> None:
        with TestClient(create_relay_app(administrators={"admin": "admin-password"}, max_message_bytes=1024)) as client:
            login(client)
            private_key, device_id = pair_device(client)
            with (
                client.websocket_connect(f"/ws/connector/{device_id}") as connector,
                client.websocket_connect(f"/ws/client/{device_id}", headers={"origin": BROWSER_ORIGIN}) as browser,
            ):
                authenticate_connector(connector, device_id, private_key)
                browser.send_json({"kind": "request", "request_id": "large", "payload": "x" * 4096})
                with self.assertRaises(WebSocketDisconnect) as disconnected:
                    browser.receive_json()
                self.assertEqual(disconnected.exception.code, 1009)

    def test_metadata_database_does_not_persist_forwarded_conversation_content(self) -> None:
        marker = "conversation-secret-marker-7f4b"
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "relay.db"
            database_url = f"sqlite:///{database_path.as_posix()}"
            with TestClient(create_relay_app(administrators={"admin": "admin-password"}, database_url=database_url)) as client:
                login(client)
                private_key, device_id = pair_device(client)
                with (
                    client.websocket_connect(f"/ws/connector/{device_id}") as connector,
                    client.websocket_connect(f"/ws/client/{device_id}", headers={"origin": BROWSER_ORIGIN}) as browser,
                ):
                    authenticate_connector(connector, device_id, private_key)
                    browser.send_json({"kind": "request", "request_id": "content", "params": {"user_input": marker}})
                    self.assertEqual(connector.receive_json()["params"]["user_input"], marker)
            self.assertNotIn(marker.encode("utf-8"), database_path.read_bytes())

    def test_device_navigation_exposes_online_project_names_without_workspace_paths(self) -> None:
        with TestClient(create_relay_app(administrators={"admin": "admin-password"})) as client:
            login(client)
            private_key, device_id = pair_device(client)
            with client.websocket_connect(f"/ws/connector/{device_id}") as connector:
                authenticate_connector(connector, device_id, private_key)
                connector.send_json(
                    {
                        "kind": "connector_presence",
                        "projects": [
                            {"project_id": "notes", "name": "Personal notes"},
                            {"project_id": "work", "name": "Work"},
                        ],
                    }
                )

                devices = client.get("/api/devices").json()["devices"]

            self.assertEqual(devices[0]["status"], "online")
            self.assertEqual(
                devices[0]["projects"],
                [
                    {"project_id": "notes", "name": "Personal notes"},
                    {"project_id": "work", "name": "Work"},
                ],
            )
            self.assertNotIn("path", str(devices))
            self.assertEqual(client.get("/api/devices").json()["devices"][0]["status"], "reconnecting")


class _SlowSocket:
    def __init__(self) -> None:
        self._never = asyncio.Event()
        self.closed: dict = {}

    async def send_json(self, message: dict) -> None:
        del message
        await self._never.wait()

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = {"code": code, "reason": reason}


class _DelaySocket:
    """Sends succeed after a small delay, simulating a busy-but-alive client."""

    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.received: list[dict] = []
        self.closed: dict | None = None

    async def send_json(self, message: dict) -> None:
        await asyncio.sleep(self.delay_seconds)
        self.received.append(message)

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = {"code": code, "reason": reason}


if __name__ == "__main__":
    unittest.main()
