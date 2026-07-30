from __future__ import annotations

import json
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest

import httpx
import uvicorn
from websockets.sync.client import connect

from desktop.backend.server import SidecarServer
from open_somnia.remote.connector import LocalSidecarBridge, RemoteConnector
from open_somnia.remote.identity import DeviceIdentity, pair_device
from open_somnia.remote.relay import create_relay_app
from open_somnia.runtime.messages import AssistantTurn
from tests.remote_tracer_support import remote_tracer_settings, wait_until


class RemoteTracerProtocolIntegrationTests(unittest.TestCase):
    def test_websocket_protocol_streams_a_real_runtime_turn_and_reloads_the_completed_session(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar = SidecarServer.from_settings(remote_tracer_settings(root), host="127.0.0.1", port=0)
            sidecar.runtime.complete = _streaming_complete("Hello remote")
            relay_port = _free_port()
            relay = uvicorn.Server(
                uvicorn.Config(
                    create_relay_app(administrators={"admin": "admin-password"}),
                    host="127.0.0.1",
                    port=relay_port,
                    log_level="error",
                    lifespan="off",
                )
            )
            relay_thread = Thread(target=relay.run, name="test-remote-relay", daemon=True)
            connector_stop = Event()
            connector_errors: list[Exception] = []
            identity: DeviceIdentity | None = None

            def run_connector() -> None:
                try:
                    RemoteConnector(
                        f"ws://127.0.0.1:{relay_port}",
                        identity=identity,
                        project_id="project-1",
                        sidecar=LocalSidecarBridge(sidecar.base_url),
                    ).run(connector_stop)
                except Exception as exc:
                    if not connector_stop.is_set():
                        connector_errors.append(exc)

            connector_thread = Thread(target=run_connector, name="test-remote-connector", daemon=True)
            try:
                sidecar.start_background()
                self.assertTrue(sidecar.wait_until_ready())
                relay_thread.start()
                self.assertTrue(wait_until(lambda: relay.started))
                relay_http_url = f"http://127.0.0.1:{relay_port}"
                with httpx.Client(base_url=relay_http_url) as auth_client:
                    login = auth_client.post(
                        "/api/auth/login",
                        json={"username": "admin", "password": "admin-password"},
                    )
                    self.assertEqual(login.status_code, 200)
                    code = auth_client.post("/api/pairings", json={"name": "Test Device"}).json()["code"]
                    identity = DeviceIdentity.load_or_create(root / "device-identity.json")
                    pair_device(identity, relay_url=relay_http_url, code=code)
                    browser_cookie = f"somnia_access={auth_client.cookies.get('somnia_access')}"
                connector_thread.start()

                with connect(
                    f"ws://127.0.0.1:{relay_port}/ws/client/{identity.device_id}",
                    origin="http://127.0.0.1:4173",
                    additional_headers={"Cookie": browser_cookie},
                ) as browser:
                    created = _request_until_online(
                        browser,
                        request_id="create-1",
                        method="session.create",
                        params={},
                    )
                    session_id = created["id"]

                    browser.send(
                        json.dumps(
                            {
                                "kind": "request",
                                "request_id": "turn-1",
                                "project_id": "project-1",
                                "method": "turn.start",
                                "params": {"session_id": session_id, "user_input": "hello"},
                            }
                        )
                    )
                    messages = _receive_until(
                        browser,
                        lambda message: message.get("kind") == "event"
                        and message.get("event", {}).get("type") == "turn_result",
                    )
                    event_types = [
                        message["event"]["type"]
                        for message in messages
                        if message.get("kind") == "event" and isinstance(message.get("event"), dict)
                    ]
                    self.assertIn("assistant_delta", event_types)
                    self.assertLess(event_types.index("assistant_delta"), event_types.index("turn_result"))

                    loaded = _request(
                        browser,
                        request_id="load-1",
                        method="session.load",
                        params={"session_id": session_id},
                    )
                    self.assertEqual(loaded["messages"][-1]["content"], "Hello remote")
                    streamed = "".join(
                        str(message["event"]["payload"].get("delta", ""))
                        for message in messages
                        if message.get("kind") == "event" and message.get("event", {}).get("type") == "assistant_delta"
                    )
                    self.assertEqual(streamed, "Hello remote")
                self.assertEqual(connector_errors, [])
            finally:
                connector_stop.set()
                sidecar.close()
                relay.should_exit = True
                relay_thread.join(timeout=5.0)
                connector_thread.join(timeout=5.0)


def _request_until_online(browser, *, request_id: str, method: str, params: dict, project_id: str = "project-1") -> dict:
    deadline = time.time() + 5.0
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            return _request(browser, request_id=f"{request_id}-{attempt}", method=method, params=params, project_id=project_id)
        except RuntimeError as exc:
            if "offline" not in str(exc).lower():
                raise
            time.sleep(0.05)
    raise TimeoutError("Connector did not come online.")


def _request(browser, *, request_id: str, method: str, params: dict, project_id: str = "project-1") -> dict:
    browser.send(
        json.dumps(
            {
                "kind": "request",
                "request_id": request_id,
                "project_id": project_id,
                "method": method,
                "params": params,
            }
        )
    )
    for message in _receive_until(
        browser,
        lambda message: message.get("kind") == "response" and message.get("request_id") == request_id,
    ):
        if message.get("kind") == "response" and message.get("request_id") == request_id:
            if message.get("ok") is not True:
                raise RuntimeError(str(message.get("error", "Remote request failed.")))
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Remote result must be an object.")
            return result
    raise TimeoutError(f"No response for {request_id}.")


def _receive_until(browser, predicate, timeout: float = 5.0) -> list[dict]:
    deadline = time.time() + timeout
    messages: list[dict] = []
    while time.time() < deadline:
        raw = browser.recv(timeout=max(0.05, deadline - time.time()))
        message = json.loads(raw)
        if not isinstance(message, dict):
            continue
        messages.append(message)
        if predicate(message):
            return messages
    raise TimeoutError("Expected remote message was not received.")


def _streaming_complete(final_text: str):
    def complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
        if text_callback is not None:
            midpoint = len(final_text) // 2
            text_callback(final_text[:midpoint])
            text_callback(final_text[midpoint:])
        return AssistantTurn(stop_reason="end_turn", text_blocks=[final_text])

    return complete


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
