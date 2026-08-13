from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from open_somnia.remote.auth import device_challenge_payload
from open_somnia.remote.connector import ConnectorReplaced, LocalSidecarBridge, RemoteConnector
from open_somnia.remote.identity import DeviceIdentity, pair_device


class RemoteConnectorTests(unittest.TestCase):
    def test_events_are_ordered_and_replayed_within_the_bounded_stream_window(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            connector = RemoteConnectorForTest(identity, replay_limit=2)
            first = connector.publish_sidecar_event({"type": "first", "payload": {}})
            connector.publish_sidecar_event({"type": "second", "payload": {}})
            connector.publish_sidecar_event({"type": "third", "payload": {}})

            sent: list[dict] = []
            connector.handle_relay_message(
                json.dumps(
                    {
                        "kind": "stream_resume",
                        "project_id": "project-1",
                        "stream_epoch": first["stream_epoch"],
                        "after_sequence": 1,
                    }
                ),
                sent.append,
            )

            self.assertEqual(sent[0]["kind"], "stream_replay")
            self.assertEqual([event["sequence"] for event in sent[0]["events"]], [2, 3])

    def test_pump_reconnect_resync_resets_the_epoch_and_pushes_a_snapshot(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            connector = RemoteConnectorForTest(identity, replay_limit=4)
            lost_epoch = connector.publish_sidecar_event({"type": "turn_result", "payload": {}})["stream_epoch"]
            sent: list[dict] = []
            connector._active_send = sent.append
            try:
                connector.resync_project_stream("project-1")
            finally:
                connector._active_send = None

            self.assertEqual(sent[0]["kind"], "stream_snapshot")
            self.assertEqual(sent[0]["project_id"], "project-1")
            self.assertEqual(sent[0]["sequence"], 0)
            self.assertNotEqual(sent[0]["stream_epoch"], lost_epoch)
            self.assertEqual(sent[0]["snapshot"], {"sessions": [{"id": "session-1"}], "runtime": {"status": "ready"}})
            # Live events continue on the new epoch from sequence 1, so clients
            # detect the epoch change and fall back to a snapshot resume.
            following = connector.publish_sidecar_event({"type": "next", "payload": {}})
            self.assertEqual(following["stream_epoch"], sent[0]["stream_epoch"])
            self.assertEqual(following["sequence"], 1)

    def test_pump_reconnect_resync_still_resets_the_epoch_while_the_relay_is_down(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            connector = RemoteConnectorForTest(identity, replay_limit=4)
            lost_epoch = connector.publish_sidecar_event({"type": "turn_result", "payload": {}})["stream_epoch"]

            connector.resync_project_stream("project-1")

            following = connector.publish_sidecar_event({"type": "next", "payload": {}})
            self.assertNotEqual(following["stream_epoch"], lost_epoch)
            self.assertEqual(following["sequence"], 1)

    def test_replay_window_miss_returns_an_authoritative_snapshot(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            connector = RemoteConnectorForTest(identity, replay_limit=1)
            connector.publish_sidecar_event({"type": "only", "payload": {}})
            sent: list[dict] = []

            connector.handle_relay_message(
                json.dumps(
                    {
                        "kind": "stream_resume",
                        "project_id": "project-1",
                        "stream_epoch": "old-epoch",
                        "after_sequence": 0,
                    }
                ),
                sent.append,
            )

            self.assertEqual(sent[0]["kind"], "stream_snapshot")
            self.assertEqual(sent[0]["snapshot"], {"sessions": [{"id": "session-1"}], "runtime": {"status": "ready"}})

    def test_fresh_client_resume_replays_the_full_epoch_when_the_ring_covers_it(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            connector = RemoteConnectorForTest(identity, replay_limit=10)
            connector.publish_sidecar_event({"type": "first", "payload": {}})
            connector.publish_sidecar_event({"type": "second", "payload": {}})
            sent: list[dict] = []

            connector.handle_relay_message(
                json.dumps(
                    {
                        "kind": "stream_resume",
                        "project_id": "project-1",
                        "stream_epoch": None,
                        "after_sequence": 0,
                    }
                ),
                sent.append,
            )

            self.assertEqual(sent[0]["kind"], "stream_replay")
            self.assertEqual([event["sequence"] for event in sent[0]["events"]], [1, 2])

    def test_fresh_client_resume_falls_back_to_snapshot_once_the_ring_overflowed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            connector = RemoteConnectorForTest(identity, replay_limit=1)
            connector.publish_sidecar_event({"type": "first", "payload": {}})
            connector.publish_sidecar_event({"type": "second", "payload": {}})
            sent: list[dict] = []

            connector.handle_relay_message(
                json.dumps(
                    {
                        "kind": "stream_resume",
                        "project_id": "project-1",
                        "stream_epoch": None,
                        "after_sequence": 0,
                    }
                ),
                sent.append,
            )

            self.assertEqual(sent[0]["kind"], "stream_snapshot")

    def test_snapshot_resume_keeps_recording_events_while_the_snapshot_is_built(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            connector = RemoteConnectorForTest(identity, replay_limit=10)
            connector.publish_sidecar_event({"type": "before", "payload": {}})

            class ReentrantSidecar(CountingSidecar):
                def execute(self, method: str, params: dict) -> dict:
                    if method == "stream.snapshot":
                        # Recording live events must not deadlock behind the snapshot build.
                        connector.publish_sidecar_event({"type": "during", "payload": {}})
                    return super().execute(method, params)

            connector._projects["project-1"].sidecar = ReentrantSidecar()
            sent: list[dict] = []
            done = Event()

            def run() -> None:
                connector.handle_relay_message(
                    json.dumps(
                        {
                            "kind": "stream_resume",
                            "project_id": "project-1",
                            "stream_epoch": "old-epoch",
                            "after_sequence": 0,
                        }
                    ),
                    sent.append,
                )
                done.set()

            thread = Thread(target=run, name="test-snapshot-resume", daemon=True)
            thread.start()

            self.assertTrue(done.wait(timeout=5.0), "Snapshot resume deadlocked while recording live events.")
            self.assertEqual(sent[0]["kind"], "stream_snapshot")
            # The snapshot only claims coverage up to the pre-fetch sequence (1);
            # the event recorded mid-fetch arrives as a live frame and must not
            # be skipped by the client.
            self.assertEqual(sent[0]["sequence"], 1)

    def test_stream_replay_echoes_the_requesting_client_for_relay_targeting(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            connector = RemoteConnectorForTest(identity, replay_limit=10)
            connector.publish_sidecar_event({"type": "first", "payload": {}})
            sent: list[dict] = []

            connector.handle_relay_message(
                json.dumps(
                    {
                        "kind": "stream_resume",
                        "project_id": "project-1",
                        "stream_epoch": None,
                        "after_sequence": 0,
                        "client_id": "browser-1",
                    }
                ),
                sent.append,
            )

            self.assertEqual(sent[0]["kind"], "stream_replay")
            self.assertEqual(sent[0]["client_id"], "browser-1")

    def test_stream_snapshot_echoes_the_requesting_client_for_relay_targeting(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            connector = RemoteConnectorForTest(identity, replay_limit=1)
            connector.publish_sidecar_event({"type": "only", "payload": {}})
            sent: list[dict] = []

            connector.handle_relay_message(
                json.dumps(
                    {
                        "kind": "stream_resume",
                        "project_id": "project-1",
                        "stream_epoch": "old-epoch",
                        "after_sequence": 0,
                        "client_id": "browser-1",
                    }
                ),
                sent.append,
            )

            self.assertEqual(sent[0]["kind"], "stream_snapshot")
            self.assertEqual(sent[0]["client_id"], "browser-1")

    def test_update_projects_diffs_the_served_set_without_a_relay_connection(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            connector = RemoteConnectorForTest(identity, replay_limit=2)
            other = CountingSidecar()

            connector.update_projects(
                {"project-1": connector.project_sidecar("project-1"), "project-2": other},
                {"project-1": "Main", "project-2": "Extra"},
            )

            self.assertEqual(set(connector._projects), {"project-1", "project-2"})
            self.assertEqual(connector._project_names["project-2"], "Extra")
            self.assertEqual(connector._pumps, {})

            presence = connector.presence_message()
            self.assertEqual(
                presence["projects"],
                [{"project_id": "project-1", "name": "Main"}, {"project_id": "project-2", "name": "Extra"}],
            )

            connector.update_projects({"project-1": connector.project_sidecar("project-1")}, {"project-1": "Main"})

            self.assertEqual(set(connector._projects), {"project-1"})
            self.assertEqual(connector.presence_message()["projects"], [{"project_id": "project-1", "name": "Main"}])

    def test_update_projects_requires_the_primary_project(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            connector = RemoteConnectorForTest(identity, replay_limit=2)

            with self.assertRaises(ValueError):
                connector.update_projects({"project-2": CountingSidecar()})

    def test_retried_request_id_returns_the_original_result_without_reexecuting(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            sidecar = CountingSidecar()
            connector = RemoteConnector(
                "wss://relay.example.com",
                identity=identity,
                project_id="project-1",
                sidecar=sidecar,
            )
            request = json.dumps(
                {
                    "kind": "request",
                    "request_id": "same-request",
                    "project_id": "project-1",
                    "method": "turn.start",
                    "params": {"session_id": "session-1", "user_input": "hello"},
                }
            )
            responses: list[dict] = []
            connector.handle_relay_message(request, responses.append)
            connector.handle_relay_message(request, responses.append)

            self.assertEqual(sidecar.turn_calls, 1)
            self.assertEqual(responses[0], responses[1])

    def test_connector_routes_each_registered_project_to_its_own_sidecar(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            first = CountingSidecar()
            second = CountingSidecar()
            connector = RemoteConnector(
                "wss://relay.example.com",
                identity=identity,
                project_id="project-1",
                sidecar=first,
                sidecars={"project-2": second},
            )
            sent: list[dict] = []
            connector.handle_relay_message(json.dumps({"kind": "request", "request_id": "one", "project_id": "project-1", "method": "turn.start", "params": {"session_id": "one", "user_input": "hello"}}), sent.append)
            connector.handle_relay_message(json.dumps({"kind": "request", "request_id": "two", "project_id": "project-2", "method": "turn.start", "params": {"session_id": "two", "user_input": "hello"}}), sent.append)

            self.assertEqual(first.turn_calls, 1)
            self.assertEqual(second.turn_calls, 1)
            self.assertEqual([response["project_id"] for response in sent], ["project-1", "project-2"])

    def test_presence_announces_project_identity_and_name_without_local_paths(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            identity.complete_pairing(device_id="device-1", device_name="Workstation", relay_url="https://relay.example.com")
            connector = RemoteConnector(
                "wss://relay.example.com",
                identity=identity,
                project_id="project-1",
                sidecar=CountingSidecar(),
                project_names={"project-1": "Personal notes"},
            )

            self.assertEqual(
                connector.presence_message(),
                {"kind": "connector_presence", "projects": [{"project_id": "project-1", "name": "Personal notes"}]},
            )

    def test_device_identity_is_generated_paired_and_reloaded_from_local_storage(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "device-identity.json"
            identity = DeviceIdentity.load_or_create(path)
            public_key = identity.public_key_bytes()
            identity.complete_pairing(
                device_id="device-1",
                device_name="Workstation",
                relay_url="https://relay.example.com",
            )

            loaded = DeviceIdentity.load(path)
            signature = loaded.sign_challenge("nonce-1")

            self.assertEqual(loaded.device_id, "device-1")
            self.assertEqual(loaded.device_name, "Workstation")
            self.assertEqual(loaded.relay_url, "https://relay.example.com")
            self.assertEqual(loaded.public_key_bytes(), public_key)
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature,
                device_challenge_payload("device-1", "nonce-1"),
            )

    def test_pair_device_claims_code_over_http_and_persists_returned_identity(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _PairingStubHandler)
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as temp_dir:
                identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
                result = pair_device(
                    identity,
                    relay_url=f"http://127.0.0.1:{server.server_port}",
                    code="ABCDEFG234",
                )

                self.assertEqual(result.device_id, "paired-device")
                self.assertEqual(DeviceIdentity.load(identity.path).device_name, "Laptop")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_bridge_maps_tracer_requests_over_loopback_http(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SidecarStubHandler)
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
        thread.start()
        try:
            bridge = LocalSidecarBridge(f"http://127.0.0.1:{server.server_port}")

            self.assertEqual(bridge.execute("session.create", {}), {"id": "session-1", "messages": []})
            self.assertEqual(bridge.execute("session.list", {}), {"sessions": [{"id": "session-1", "messages": []}]})
            self.assertEqual(
                bridge.execute("session.load", {"session_id": "session-1"}),
                {"id": "session-1", "messages": [{"role": "assistant", "content": "done"}]},
            )
            self.assertEqual(
                bridge.execute("turn.start", {"session_id": "session-1", "user_input": "hello"}),
                {"turn_id": "turn-1", "session_id": "session-1", "accepted_input": "hello"},
            )
            self.assertEqual(
                bridge.execute("turn.interrupt", {"turn_id": "turn-1"}),
                {"turn_id": "turn-1", "interrupted": True},
            )
            self.assertEqual(
                bridge.execute(
                    "turn.inject",
                    {"turn_id": "turn-1", "injection_id": "inject-1", "user_input": "continue"},
                ),
                {"turn_id": "turn-1", "injection_id": "inject-1", "queued": True},
            )
            self.assertEqual(
                bridge.execute("session.delete", {"session_id": "session-1"}),
                {"session_id": "session-1", "deleted": True},
            )
            self.assertEqual(
                bridge.execute("session.compact", {"session_id": "session-1"}),
                {"message": "Context compacted.", "session": {"id": "session-1", "messages": []}},
            )
            self.assertEqual(
                bridge.execute("session.janitor", {"session_id": "session-1"}),
                {"message": "Janitor complete.", "session": {"id": "session-1", "messages": []}},
            )
            self.assertEqual(
                bridge.execute("session.new", {"session_id": "session-1"}),
                {"session": {"id": "session-2", "messages": []}, "previous_session_id": "session-1"},
            )
            self.assertEqual(bridge.execute("tool_log.list", {"limit": 5}), {"tool_logs": [{"id": "log-1"}]})
            self.assertEqual(bridge.execute("tool_log.get", {"log_id": "log-1"}), {"id": "log-1", "rendered": "done"})
            self.assertEqual(bridge.execute("thinking_log.get", {"path": "safe/log.jsonl"}), {"path": "safe/log.jsonl", "text": "reasoning"})
            self.assertEqual(bridge.execute("team.members", {"session_id": "session-1"}), {"members": [{"name": "Scout"}]})
            self.assertEqual(bridge.execute("team.members", {}), {"members": [{"name": "Scout"}]})
            self.assertEqual(bridge.execute("team.log", {"name": "Scout", "session_id": "session-1"}), {"name": "Scout", "rendered": "working"})
            self.assertEqual(bridge.execute("team.log", {"name": "Scout"}), {"name": "Scout", "rendered": "working"})
            self.assertEqual(bridge.execute("task.list", {"session_id": "session-1"}), {"tasks": [{"id": 1, "subject": "Ship"}]})
            self.assertEqual(bridge.execute("task.list", {}), {"tasks": [{"id": 1, "subject": "Ship"}]})
            self.assertEqual(
                bridge.execute("workspace.paths", {"query": "src", "limit": 30}),
                {"paths": [{"path": "src", "basename": "src", "kind": "dir"}]},
            )
            self.assertEqual(
                bridge.execute(
                    "workspace.image.stage",
                    {"name": "paste.png", "media_type": "image/png", "data_url": "data:image/png;base64,cG5n"},
                ),
                {"path": ".open_somnia/clipboard-images/paste.png", "absolute_path": "C:/workspace/paste.png", "media_type": "image/png"},
            )
            self.assertEqual(bridge.execute("workspace.image", {"path": "safe/pixel.png"})["data_url"], "data:image/png;base64,cG5n")
            self.assertEqual(bridge.execute("provider.list", {}), {"providers": [{"name": "openai", "models": ["gpt-test"]}]})
            self.assertEqual(bridge.execute("provider.presets", {}), {"presets": [{"name": "openai", "provider_type": "openai"}]})
            self.assertEqual(
                bridge.execute("provider.debug_model", {"provider": "openai", "model": "gpt-test"}),
                {"provider": "openai", "model": "gpt-test", "ok": True, "message": "Connection OK."},
            )
            self.assertEqual(
                bridge.execute("settings.config.get", {}),
                {"active_scope": "project", "sections": {"provider": "[providers]", "hooks": "[hooks]"}},
            )
            self.assertEqual(
                bridge.execute("settings.config.save", {"scope": "project", "section": "hooks", "content": "[hooks]"}),
                {"saved": True, "scope": "project", "section": "hooks"},
            )
            self.assertEqual(bridge.execute("mcp.list", {}), {"servers": [{"name": "docs", "enabled": True}]})
            self.assertEqual(
                bridge.execute("mcp.debug", {"name": "docs"}),
                {"server": {"name": "docs"}, "tool_count": 2},
            )
            self.assertEqual(
                bridge.execute("mcp.set_enabled", {"name": "docs", "enabled": False}),
                {"server": {"name": "docs"}, "enabled": False, "tool_count": 0},
            )
            self.assertEqual(bridge.execute("runtime.status", {}), {"status": "ready", "provider": "openai", "model": "gpt-test"})
            self.assertEqual(bridge.execute("model.list", {"provider": "openai"}), {"models": [{"id": "gpt-test", "provider": "openai"}]})
            self.assertEqual(
                bridge.execute("provider.switch", {"provider": "openai", "model": "gpt-test"}),
                {"message": "Provider switched.", "provider": "openai", "model": "gpt-test"},
            )
            self.assertEqual(
                bridge.execute("vision.set", {"provider": "openai", "model": "vision-test"}),
                {"message": "Vision model updated.", "vision_provider": "openai", "vision_model": "vision-test"},
            )
            self.assertEqual(
                bridge.execute("reasoning.set", {"level": "high"}),
                {"message": "Reasoning level updated.", "reasoning_level": "high"},
            )
            self.assertEqual(bridge.execute("interaction.list", {}), {"interactions": [{"id": "interaction-1", "session_id": "session-1", "kind": "authorization"}]})
            self.assertEqual(
                bridge.execute(
                    "interaction.resolve_authorization",
                    {"interaction_id": "interaction-1", "scope": "workspace", "approved": True, "reason": "ok"},
                ),
                {"resolved": True, "scope": "workspace", "approved": True},
            )
            self.assertEqual(
                bridge.execute(
                    "interaction.resolve_mode_switch",
                    {"interaction_id": "interaction-1", "approved": True, "active_mode": "yolo", "reason": ""},
                ),
                {"resolved": True, "approved": True, "active_mode": "yolo"},
            )
            self.assertEqual(bridge.execute("execution.mode", {"mode": "shortcuts"}), {"message": "Execution mode set.", "execution_mode": "shortcuts"})
            self.assertEqual(bridge.execute("execution.mode", {"mode": "yolo"}), {"message": "Execution mode set.", "execution_mode": "yolo"})
            with self.assertRaisesRegex(ValueError, "Unsupported remote execution mode"):
                bridge.execute("execution.mode", {"mode": "bogus"})
            with self.assertRaisesRegex(ValueError, "enabled must be a boolean"):
                bridge.execute("mcp.set_enabled", {"name": "docs", "enabled": "yes"})
            with self.assertRaisesRegex(ValueError, "scope is required"):
                bridge.execute("interaction.resolve_authorization", {"interaction_id": "interaction-1"})
            with self.assertRaisesRegex(ValueError, "Unsupported remote method"):
                bridge.execute("permission.persist", {"scope": "workspace"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_bridge_rejects_non_loopback_sidecars(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            LocalSidecarBridge("https://runtime.example.com")

    def test_identity_rejects_plaintext_remote_relay(self) -> None:
        with TemporaryDirectory() as temp_dir:
            identity = DeviceIdentity.load_or_create(Path(temp_dir) / "identity.json")
            with self.assertRaisesRegex(ValueError, "HTTPS remotely"):
                identity.complete_pairing(
                    device_id="device-1",
                    device_name="Workstation",
                    relay_url="http://relay.example.com",
                )


class _SidecarStubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        body = self._read_body()
        if self.path == "/sessions":
            self._send({"session": {"id": "session-1", "messages": []}}, status=201)
            return
        if self.path == "/turns":
            self._send(
                {
                    "turn_id": "turn-1",
                    "session_id": body.get("session_id"),
                    "accepted_input": body.get("user_input"),
                },
                status=202,
            )
            return
        if self.path == "/turns/turn-1/interrupt":
            self._send({"turn_id": "turn-1", "interrupted": True})
            return
        if self.path == "/turns/turn-1/loop-injections":
            self._send({"turn_id": "turn-1", "injection_id": body.get("injection_id"), "queued": True}, status=202)
            return
        if self.path == "/sessions/session-1/compact":
            self._send({"message": "Context compacted.", "session": {"id": "session-1", "messages": []}})
            return
        if self.path == "/sessions/session-1/janitor":
            self._send({"message": "Janitor complete.", "session": {"id": "session-1", "messages": []}})
            return
        if self.path == "/sessions/session-1/new":
            self._send({"session": {"id": "session-2", "messages": []}, "previous_session_id": "session-1"})
            return
        if self.path == "/workspace/images":
            self._send({"path": ".open_somnia/clipboard-images/paste.png", "absolute_path": "C:/workspace/paste.png", "media_type": body.get("media_type")})
            return
        if self.path == "/providers/switch":
            self._send({"message": "Provider switched.", "provider": body.get("provider_name"), "model": body.get("model")})
            return
        if self.path == "/providers/debug-model":
            self._send({"provider": body.get("provider_name"), "model": body.get("model"), "ok": True, "message": "Connection OK."})
            return
        if self.path == "/settings/config":
            self._send({"saved": True, "scope": body.get("scope"), "section": body.get("section")})
            return
        if self.path == "/mcp/servers/docs/debug":
            self._send({"server": {"name": "docs"}, "tool_count": 2})
            return
        if self.path == "/mcp/servers/docs/enabled":
            self._send({"server": {"name": "docs"}, "enabled": body.get("enabled"), "tool_count": 0})
            return
        if self.path == "/interactions/interaction-1/authorization":
            self._send({"resolved": True, "scope": body.get("scope"), "approved": body.get("approved")})
            return
        if self.path == "/interactions/interaction-1/mode-switch":
            self._send({"resolved": True, "approved": body.get("approved"), "active_mode": body.get("active_mode")})
            return
        if self.path == "/vision-model":
            self._send({"message": "Vision model updated.", "vision_provider": body.get("vision_provider"), "vision_model": body.get("vision_model")})
            return
        if self.path == "/reasoning":
            self._send({"message": "Reasoning level updated.", "reasoning_level": body.get("reasoning_level")})
            return
        if self.path == "/execution-mode":
            self._send({"message": "Execution mode set.", "execution_mode": body.get("mode")})
            return
        self._send({"error": "not found"}, status=404)

    def do_GET(self) -> None:
        if self.path == "/sessions":
            self._send({"sessions": [{"id": "session-1", "messages": []}]})
            return
        if self.path == "/sessions/session-1":
            self._send(
                {"session": {"id": "session-1", "messages": [{"role": "assistant", "content": "done"}]}},
            )
            return
        if self.path == "/tool-logs?limit=5":
            self._send({"tool_logs": [{"id": "log-1"}]})
            return
        if self.path == "/tool-logs/log-1":
            self._send({"tool_log": {"id": "log-1", "rendered": "done"}})
            return
        if self.path == "/thinking-log?path=safe%2Flog.jsonl":
            self._send({"thinking_log": {"path": "safe/log.jsonl", "text": "reasoning"}})
            return
        if self.path == "/team/active?session_id=session-1":
            self._send({"members": [{"name": "Scout"}]})
            return
        if self.path == "/team/active":
            self._send({"members": [{"name": "Scout"}]})
            return
        if self.path == "/team/log?name=Scout&session_id=session-1":
            self._send({"team_log": {"name": "Scout", "rendered": "working"}})
            return
        if self.path == "/team/log?name=Scout":
            self._send({"team_log": {"name": "Scout", "rendered": "working"}})
            return
        if self.path == "/tasks?session_id=session-1":
            self._send({"tasks": [{"id": 1, "subject": "Ship"}]})
            return
        if self.path == "/tasks":
            self._send({"tasks": [{"id": 1, "subject": "Ship"}]})
            return
        if self.path == "/provider-presets":
            self._send({"presets": [{"name": "openai", "provider_type": "openai"}]})
            return
        if self.path == "/settings/config":
            self._send({"active_scope": "project", "sections": {"provider": "[providers]", "hooks": "[hooks]"}})
            return
        if self.path == "/mcp/servers":
            self._send({"servers": [{"name": "docs", "enabled": True}]})
            return
        if self.path == "/workspace/paths?q=src&limit=30":
            self._send({"paths": [{"path": "src", "basename": "src", "kind": "dir"}]})
            return
        if self.path == "/providers":
            self._send({"providers": [{"name": "openai", "models": ["gpt-test"]}]})
            return
        if self.path == "/runtime/status":
            self._send({"status": "ready", "provider": "openai", "model": "gpt-test"})
            return
        if self.path == "/models?provider=openai":
            self._send({"models": [{"id": "gpt-test", "provider": "openai"}]})
            return
        if self.path == "/interactions":
            self._send({"interactions": [{"id": "interaction-1", "session_id": "session-1", "kind": "authorization"}]})
            return
        if self.path == "/workspace/images?path=safe%2Fpixel.png":
            self._send_bytes(b"png", "image/png")
            return
        self._send({"error": "not found"}, status=404)

    def do_DELETE(self) -> None:
        if self.path == "/sessions/session-1":
            self._send({"session_id": "session-1", "deleted": True})
            return
        self._send({"error": "not found"}, status=404)

    def log_message(self, format: str, *args) -> None:
        del format, args

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length)) if length else {}

    def _send(self, payload: dict, *, status: int = 200) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_bytes(self, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _PairingStubHandler(_SidecarStubHandler):
    def do_POST(self) -> None:
        body = self._read_body()
        if (
            self.path != "/api/pairings/claim"
            or body.get("code") != "ABCDEFG234"
            or not isinstance(body.get("public_key"), str)
        ):
            self._send({"error": "invalid pairing claim"}, status=400)
            return
        self._send({"device_id": "paired-device", "name": "Laptop"}, status=201)


class CountingSidecar:
    def __init__(self) -> None:
        self.turn_calls = 0

    def execute(self, method: str, params: dict) -> dict:
        if method == "turn.start":
            self.turn_calls += 1
            return {"turn_id": "turn-1", "session_id": params["session_id"]}
        if method == "stream.snapshot":
            return {"sessions": [{"id": "session-1"}], "runtime": {"status": "ready"}}
        raise AssertionError(method)


class RemoteConnectorForTest(RemoteConnector):
    def __init__(self, identity: DeviceIdentity, *, replay_limit: int) -> None:
        super().__init__(
            "wss://relay.example.com",
            identity=identity,
            project_id="project-1",
            sidecar=CountingSidecar(),
            replay_limit=replay_limit,
        )


class ScriptedConnector(RemoteConnector):
    """Connector double whose run() plays a scripted sequence of outcomes.

    Each outcome is an Exception to raise, the string "close" (Relay closed the
    connection, e.g. a deploy restart), or "block" (stay connected until stopped).
    """

    def __init__(self, identity: DeviceIdentity, outcomes: list[object]) -> None:
        super().__init__(
            "wss://relay.example.com",
            identity=identity,
            project_id="project-1",
            sidecar=CountingSidecar(),
        )
        self.outcomes = list(outcomes)
        self._script_len = len(outcomes)
        self.attempts = 0

    def outcomes_exhausted(self) -> bool:
        """True once the scripted outcomes ran and the final blocking attempt started."""
        return self.attempts > self._script_len

    def run(self, stop_event: Event | None = None, *, on_connect=None) -> int | None:
        self.attempts += 1
        if on_connect is not None:
            on_connect()
        outcome = self.outcomes.pop(0) if self.outcomes else "block"
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "close":
            return 1012
        stop_event.wait(30.0)
        return None


class RemoteConnectorRunForeverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        identity = DeviceIdentity.load_or_create(Path(self._temp.name) / "identity.json")
        identity.complete_pairing(device_id="device-1", device_name="Device", relay_url="https://relay.example.com")
        self.identity = identity
        initial_patcher = patch("open_somnia.remote.connector.RECONNECT_INITIAL_DELAY_SECONDS", 0.01)
        initial_patcher.start()
        self.addCleanup(initial_patcher.stop)
        max_patcher = patch("open_somnia.remote.connector.RECONNECT_MAX_DELAY_SECONDS", 0.02)
        max_patcher.start()
        self.addCleanup(max_patcher.stop)

    def _run_until_stopped(self, connector: ScriptedConnector, retries: list[str], connects: list[int]) -> Event:
        stop = Event()
        thread = Thread(
            target=connector.run_forever,
            kwargs={
                "stop_event": stop,
                "on_retry": lambda reason, delay: retries.append(reason),
                "on_connect": lambda: connects.append(1),
            },
            daemon=True,
        )
        thread.start()
        self.assertTrue(_wait_until(lambda: not thread.is_alive() or connector.outcomes_exhausted()))
        stop.set()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        return stop

    def test_transient_failures_are_retried_with_backoff_until_stopped(self) -> None:
        connector = ScriptedConnector(self.identity, [OSError("connection refused"), OSError("connection refused")])
        retries: list[str] = []
        connects: list[int] = []
        self._run_until_stopped(connector, retries, connects)
        self.assertEqual(connector.attempts, 3)
        self.assertEqual(retries, ["connection refused", "connection refused"])
        self.assertEqual(len(connects), 3)

    def test_relay_close_is_retried(self) -> None:
        connector = ScriptedConnector(self.identity, ["close"])
        retries: list[str] = []
        connects: list[int] = []
        self._run_until_stopped(connector, retries, connects)
        self.assertEqual(connector.attempts, 2)
        self.assertEqual(len(retries), 1)
        self.assertIn("Relay closed the connection", retries[0])

    def test_permanent_close_is_not_retried(self) -> None:
        connector = ScriptedConnector(
            self.identity,
            [ConnectionClosedError(Close(4403, "Device authentication failed."), None)],
        )
        with self.assertRaises(ConnectionClosedError):
            connector.run_forever()
        self.assertEqual(connector.attempts, 1)

    def test_replaced_connector_is_not_retried(self) -> None:
        connector = ScriptedConnector(self.identity, [ConnectorReplaced("Another Connector took over this Device on the Relay.")])
        with self.assertRaises(ConnectorReplaced):
            connector.run_forever()
        self.assertEqual(connector.attempts, 1)

    def test_replaced_close_exception_is_not_retried(self) -> None:
        # 1012 + "Connector replaced" can arrive as a ConnectionClosed exception
        # instead of the clean close-code path; it must still be permanent.
        connector = ScriptedConnector(
            self.identity,
            [ConnectionClosedError(Close(1012, "Connector replaced."), None)],
        )
        with self.assertRaises(ConnectionClosedError):
            connector.run_forever()
        self.assertEqual(connector.attempts, 1)

    def test_service_restart_close_exception_is_retried(self) -> None:
        connector = ScriptedConnector(
            self.identity,
            [ConnectionClosedError(Close(1012, "Service restart"), None)],
        )
        retries: list[str] = []
        connects: list[int] = []
        self._run_until_stopped(connector, retries, connects)
        self.assertEqual(connector.attempts, 2)
        self.assertEqual(len(retries), 1)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


if __name__ == "__main__":
    unittest.main()
