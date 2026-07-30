from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
import time
import unittest
from unittest.mock import patch

import httpx
import uvicorn
from websockets.sync.client import connect

from desktop.backend.server import SidecarServer
from open_somnia.remote.connector import LocalSidecarBridge, RemoteConnector
from open_somnia.remote.identity import DeviceIdentity, pair_device
from open_somnia.remote.relay import create_relay_app
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.messages import AssistantTurn
from tests.remote_tracer_support import remote_tracer_settings, wait_until
from tests.test_remote_tracer_e2e import (
    _free_port,
    _receive_until,
    _request,
    _request_until_online,
)

CHUNKS_PER_TURN = 40


def _scripted_complete(barrier: Barrier, calls: list[int]):
    """Stream many small deltas slowly; the first two calls overlap via the barrier."""

    def complete(self, system_prompt, messages, tools, text_callback=None, thinking_callback=None, should_interrupt=None, **kwargs):
        calls.append(1)
        if len(calls) <= 2:
            barrier.wait(timeout=15.0)
        if text_callback is not None:
            for index in range(CHUNKS_PER_TURN):
                text_callback(f"chunk-{index} ")
                time.sleep(0.005)
        return AssistantTurn(stop_reason="end_turn", text_blocks=["done"])

    return complete


def _simple_complete(self, system_prompt, messages, tools, text_callback=None, **kwargs):
    if text_callback is not None:
        text_callback("late ")
        text_callback("joined")
    return AssistantTurn(stop_reason="end_turn", text_blocks=["late joined"])


class _StreamTracker:
    """Mimics the web client's strict in-order consumption of the event stream."""

    def __init__(self) -> None:
        self.sequences: list[int] = []
        self.delta_sessions: set[str] = set()
        self.finished_sessions: set[str] = set()

    def __call__(self, message: dict) -> bool:
        if message.get("kind") != "event" or not isinstance(message.get("event"), dict):
            return False
        sequence = message.get("sequence")
        if isinstance(sequence, int):
            self.sequences.append(sequence)
        event = message["event"]
        session_id = str(event.get("session_id", ""))
        if event.get("type") == "assistant_delta":
            self.delta_sessions.add(session_id)
        if event.get("type") == "turn_result":
            self.finished_sessions.add(session_id)
        return False


class RemoteConcurrentTurnsIntegrationTests(unittest.TestCase):
    """Two conversations streaming at once through the Relay must both complete,
    and a later single conversation must keep streaming afterwards."""

    def test_two_concurrent_turns_both_stream_and_later_turn_still_works(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar = SidecarServer.from_settings(remote_tracer_settings(root), host="127.0.0.1", port=0)
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
            barrier = Barrier(2)
            calls: list[int] = []
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

                with (
                    patch.object(OpenAgentRuntime, "complete", _scripted_complete(barrier, calls)),
                    connect(
                        f"ws://127.0.0.1:{relay_port}/ws/client/{identity.device_id}",
                        origin="http://127.0.0.1:4173",
                        additional_headers={"Cookie": browser_cookie},
                    ) as browser,
                ):
                    session_a = _request_until_online(browser, request_id="create-a", method="session.create", params={})["id"]
                    session_b = _request(browser, request_id="create-b", method="session.create", params={})["id"]

                    for request_id, session_id in (("turn-a", session_a), ("turn-b", session_b)):
                        browser.send(
                            json.dumps(
                                {
                                    "kind": "request",
                                    "request_id": request_id,
                                    "project_id": "project-1",
                                    "method": "turn.start",
                                    "params": {"session_id": session_id, "user_input": "hello"},
                                }
                            )
                        )

                    tracker = _StreamTracker()
                    _receive_until(
                        browser,
                        lambda message: tracker(message) or len(tracker.finished_sessions) == 2,
                        timeout=30.0,
                    )

                    # Every streamed sequence must arrive exactly once, with no gaps:
                    # a lost sequence is what wedges the strict-ordering web client.
                    # (The first few events are consumed by the _request helper reads,
                    # so only the received tail can be checked for contiguity.)
                    first = tracker.sequences[0]
                    self.assertEqual(tracker.sequences, list(range(first, first + len(tracker.sequences))))
                    self.assertIn(session_a, tracker.delta_sessions)
                    self.assertIn(session_b, tracker.delta_sessions)

                    # A single conversation started afterwards must still stream.
                    session_c = _request(browser, request_id="create-c", method="session.create", params={})["id"]
                    browser.send(
                        json.dumps(
                            {
                                "kind": "request",
                                "request_id": "turn-c",
                                "project_id": "project-1",
                                "method": "turn.start",
                                "params": {"session_id": session_c, "user_input": "hello again"},
                            }
                        )
                    )
                    messages = _receive_until(
                        browser,
                        lambda message: message.get("kind") == "event"
                        and message.get("event", {}).get("type") == "turn_result"
                        and message.get("event", {}).get("session_id") == session_c,
                        timeout=30.0,
                    )
                    self.assertTrue(
                        any(
                            message.get("kind") == "event"
                            and message.get("event", {}).get("type") == "assistant_delta"
                            and message.get("event", {}).get("session_id") == session_c
                            for message in messages
                        )
                    )
                self.assertEqual(connector_errors, [])
            finally:
                connector_stop.set()
                sidecar.close()
                relay.should_exit = True
                relay_thread.join(timeout=5.0)
                connector_thread.join(timeout=5.0)


class RemoteLateProjectJoinIntegrationTests(unittest.TestCase):
    """A Project whose sidecar starts late must join the running Connector
    without any Relay reconnect or manual re-enable."""

    def test_late_project_joins_the_running_connector(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar_one = SidecarServer.from_settings(remote_tracer_settings(root / "one"), host="127.0.0.1", port=0)
            relay_port = _free_port()
            late_port = _free_port()
            relay_app = create_relay_app(administrators={"admin": "admin-password"})
            relay = uvicorn.Server(
                uvicorn.Config(
                    relay_app,
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
            connector: RemoteConnector | None = None

            def run_connector() -> None:
                try:
                    connector.run(connector_stop)
                except Exception as exc:
                    if not connector_stop.is_set():
                        connector_errors.append(exc)

            sidecar_two: SidecarServer | None = None
            try:
                sidecar_one.start_background()
                self.assertTrue(sidecar_one.wait_until_ready())
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

                connector = RemoteConnector(
                    f"ws://127.0.0.1:{relay_port}",
                    identity=identity,
                    project_id="project-1",
                    sidecar=LocalSidecarBridge(sidecar_one.base_url),
                    project_names={"project-1": "First"},
                )
                connector_thread = Thread(target=run_connector, name="test-remote-connector", daemon=True)
                connector_thread.start()

                with (
                    patch.object(OpenAgentRuntime, "complete", _simple_complete),
                    connect(
                        f"ws://127.0.0.1:{relay_port}/ws/client/{identity.device_id}",
                        origin="http://127.0.0.1:4173",
                        additional_headers={"Cookie": browser_cookie},
                    ) as browser,
                ):
                    session_one = _request_until_online(browser, request_id="create-1", method="session.create", params={})["id"]

                    # Register the late Project while its sidecar is still down:
                    # the pump retries in the background instead of dying.
                    late_base_url = f"http://127.0.0.1:{late_port}"
                    connector.update_projects(
                        {
                            "project-1": LocalSidecarBridge(sidecar_one.base_url),
                            "project-2": LocalSidecarBridge(late_base_url),
                        },
                        {"project-1": "First", "project-2": "Late"},
                    )

                    # The Relay already sees both Projects via the updated presence.
                    self.assertTrue(
                        wait_until(
                            lambda: {p["project_id"] for p in relay_app.state.relay_hub._project_metadata.get(identity.device_id, [])}
                            == {"project-1", "project-2"}
                        )
                    )

                    # Requests to the late Project fail while its sidecar is down,
                    # but project-1 keeps working on the same Relay connection.
                    browser.send(
                        json.dumps(
                            {
                                "kind": "request",
                                "request_id": "early-2",
                                "project_id": "project-2",
                                "method": "session.list",
                                "params": {},
                            }
                        )
                    )
                    early = _receive_until(
                        browser,
                        lambda message: message.get("kind") == "response" and message.get("request_id") == "early-2",
                    )
                    self.assertFalse(early[-1]["ok"])

                    # The late sidecar starts; its pump connects on the next retry.
                    sidecar_two = SidecarServer.from_settings(
                        remote_tracer_settings(root / "two"), host="127.0.0.1", port=late_port
                    )
                    sidecar_two.start_background()
                    self.assertTrue(sidecar_two.wait_until_ready())
                    # Wait until the Connector's pump has actually subscribed to
                    # the late sidecar's event stream before driving a turn.
                    self.assertTrue(
                        wait_until(lambda: sidecar_two is not None and len(sidecar_two._clients) > 0, timeout=10.0),
                        "Connector pump did not join the late sidecar.",
                    )

                    session_two = _request_until_online(
                        browser, request_id="create-2", method="session.create", params={}, project_id="project-2"
                    )["id"]
                    browser.send(
                        json.dumps(
                            {
                                "kind": "request",
                                "request_id": "turn-2",
                                "project_id": "project-2",
                                "method": "turn.start",
                                "params": {"session_id": session_two, "user_input": "hello"},
                            }
                        )
                    )
                    messages = _receive_until(
                        browser,
                        lambda message: message.get("kind") == "event"
                        and message.get("project_id") == "project-2"
                        and message.get("event", {}).get("type") == "turn_result",
                        timeout=20.0,
                    )
                    self.assertTrue(
                        any(
                            message.get("kind") == "event"
                            and message.get("project_id") == "project-2"
                            and message.get("event", {}).get("type") == "assistant_delta"
                            for message in messages
                        )
                    )

                    # project-1 still streams on the same connection afterwards.
                    browser.send(
                        json.dumps(
                            {
                                "kind": "request",
                                "request_id": "turn-1",
                                "project_id": "project-1",
                                "method": "turn.start",
                                "params": {"session_id": session_one, "user_input": "hello again"},
                            }
                        )
                    )
                    _receive_until(
                        browser,
                        lambda message: message.get("kind") == "event"
                        and message.get("project_id") == "project-1"
                        and message.get("event", {}).get("type") == "turn_result",
                        timeout=20.0,
                    )
                self.assertEqual(connector_errors, [])
            finally:
                connector_stop.set()
                sidecar_one.close()
                if sidecar_two is not None:
                    sidecar_two.close()
                relay.should_exit = True
                relay_thread.join(timeout=5.0)


if __name__ == "__main__":
    unittest.main()
