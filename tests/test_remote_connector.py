from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import unittest

from open_somnia.remote.connector import LocalSidecarBridge


class RemoteConnectorTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
