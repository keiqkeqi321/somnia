from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from open_somnia.remote.auth import device_challenge_payload
from open_somnia.remote.connector import LocalSidecarBridge
from open_somnia.remote.identity import DeviceIdentity, pair_device


class RemoteConnectorTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
