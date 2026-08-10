from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
import threading
import time
import unittest
import uuid

from open_somnia.mcp.transport_sse import SSETransport


class _SSETestServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _SSEHandler)
        self.session_id = uuid.uuid4().hex
        self.events: Queue[dict] = Queue()
        self.stop_event = threading.Event()


class _SSEHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:
        if self.path != "/sse":
            self.send_error(404)
            return
        server: _SSETestServer = self.server  # type: ignore[assignment]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(f"event: endpoint\ndata: /sse?sessionId={server.session_id}\n\n".encode("utf-8"))
        self.wfile.flush()
        while not server.stop_event.is_set():
            try:
                event = server.events.get(timeout=0.05)
            except Empty:
                continue
            self.wfile.write(b"event: message\n")
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
            self.wfile.flush()

    def do_POST(self) -> None:
        server: _SSETestServer = self.server  # type: ignore[assignment]
        if self.path != f"/sse?sessionId={server.session_id}":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        request = json.loads(body.decode("utf-8"))
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        if "id" in request:
            server.events.put(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {"method": request.get("method"), "ok": True},
                }
            )

    def log_message(self, format: str, *args) -> None:
        return


class SSETransportTests(unittest.TestCase):
    def test_request_round_trip_uses_sse_endpoint(self) -> None:
        server = _SSETestServer()
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
        thread.start()
        transport = SSETransport(
            url=f"http://127.0.0.1:{server.server_port}/sse",
            timeout_seconds=2,
            startup_timeout_seconds=2,
        )
        try:
            response = transport.request("tools/list", {}, startup=True)
        finally:
            transport.close()
            server.stop_event.set()
            server.shutdown()
            server.server_close()

        self.assertEqual(transport.session_id, None)
        self.assertEqual(response["result"]["method"], "tools/list")
        self.assertTrue(response["result"]["ok"])

    def test_close_does_not_wait_on_open_sse_response(self) -> None:
        server = _SSETestServer()
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
        thread.start()
        transport = SSETransport(
            url=f"http://127.0.0.1:{server.server_port}/sse",
            timeout_seconds=2,
            startup_timeout_seconds=2,
        )
        try:
            transport.start()
            started = time.monotonic()
            transport.close()
            elapsed = time.monotonic() - started
        finally:
            server.stop_event.set()
            server.shutdown()
            server.server_close()

        self.assertLess(elapsed, 1.0)

    def test_startup_timeout_does_not_become_idle_read_timeout(self) -> None:
        server = _SSETestServer()
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
        thread.start()
        transport = SSETransport(
            url=f"http://127.0.0.1:{server.server_port}/sse",
            timeout_seconds=2,
            startup_timeout_seconds=0.3,
        )
        try:
            transport.start()
            time.sleep(0.5)
            self.assertIsNotNone(transport._reader_thread)
            self.assertTrue(transport._reader_thread.is_alive())
            response = transport.request("tools/list", {})
        finally:
            transport.close()
            server.stop_event.set()
            server.shutdown()
            server.server_close()

        self.assertEqual(response["result"]["method"], "tools/list")

    def test_post_http_error_fails_without_waiting_for_sse_response(self) -> None:
        server = _SSETestServer()
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
        thread.start()
        transport = SSETransport(
            url=f"http://127.0.0.1:{server.server_port}/sse",
            timeout_seconds=2,
            startup_timeout_seconds=2,
        )
        try:
            transport.start()
            transport.endpoint_url = f"http://127.0.0.1:{server.server_port}/sse?sessionId=missing"
            started = time.monotonic()
            with self.assertRaises(RuntimeError) as exc:
                transport.request("tools/list", {})
            elapsed = time.monotonic() - started
        finally:
            transport.close()
            server.stop_event.set()
            server.shutdown()
            server.server_close()

        self.assertIn("SSE MCP POST failed: 404", str(exc.exception))
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
