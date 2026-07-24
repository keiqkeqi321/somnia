from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from open_somnia.remote.auth import device_challenge_payload
from open_somnia.remote.connector import LocalSidecarBridge, RemoteConnector
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
        thread = Thread(target=server.serve_forever, daemon=True)
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
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            bridge = LocalSidecarBridge(f"http://127.0.0.1:{server.server_port}")

            self.assertEqual(bridge.execute("session.create", {}), {"id": "session-1", "messages": []})
            self.assertEqual(
                bridge.execute("session.load", {"session_id": "session-1"}),
                {"id": "session-1", "messages": [{"role": "assistant", "content": "done"}]},
            )
            self.assertEqual(
                bridge.execute("turn.start", {"session_id": "session-1", "user_input": "hello"}),
                {"turn_id": "turn-1", "session_id": "session-1", "accepted_input": "hello"},
            )
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
        self._send({"error": "not found"}, status=404)

    def do_GET(self) -> None:
        if self.path == "/sessions/session-1":
            self._send(
                {"session": {"id": "session-1", "messages": [{"role": "assistant", "content": "done"}]}},
            )
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


if __name__ == "__main__":
    unittest.main()
