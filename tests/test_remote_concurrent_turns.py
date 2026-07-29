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


if __name__ == "__main__":
    unittest.main()
