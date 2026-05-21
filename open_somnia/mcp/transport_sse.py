from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import json
import socket
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any
from urllib.parse import urljoin, urlparse
import urllib.error
import urllib.request
import uuid


@dataclass(slots=True)
class _PendingResponse:
    queue: Queue = field(default_factory=Queue)


class SSETransport:
    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout_seconds: int = 30,
        startup_timeout_seconds: int = 30,
    ):
        self.url = url
        self.headers = headers or {}
        self.timeout_seconds = timeout_seconds
        self.startup_timeout_seconds = startup_timeout_seconds
        self.session_id: str | None = None
        self.endpoint_url: str | None = None
        self._response = None
        self._reader_thread: Thread | None = None
        self._closed = Event()
        self._endpoint_ready = Event()
        self._pending: dict[str, _PendingResponse] = {}
        self._pending_lock = Lock()
        self._reader_error: Exception | None = None

    def start(self) -> None:
        if self._reader_thread is not None and self._reader_thread.is_alive():
            return
        self._closed.clear()
        self._endpoint_ready.clear()
        self._reader_error = None
        request = urllib.request.Request(
            self.url,
            headers={"Accept": "text/event-stream", **self.headers},
            method="GET",
        )
        try:
            self._response = urllib.request.urlopen(request, timeout=self.startup_timeout_seconds)
        except urllib.error.URLError as exc:
            raise RuntimeError(f"SSE MCP connect failed: {exc}") from exc
        self._disable_response_timeout(self._response)
        self._reader_thread = Thread(target=self._read_loop, name="somnia-mcp-sse-reader", daemon=True)
        self._reader_thread.start()
        if not self._endpoint_ready.wait(self.startup_timeout_seconds):
            raise RuntimeError("SSE MCP server did not provide an endpoint")
        if self._reader_error is not None:
            raise RuntimeError(f"SSE MCP reader failed: {self._reader_error}") from self._reader_error

    def request(self, method: str, params: dict[str, Any] | None = None, *, startup: bool = False) -> dict[str, Any]:
        if not self.endpoint_url:
            self.start()
        if self._closed.is_set():
            if self._reader_error is not None:
                raise RuntimeError(f"SSE MCP connection closed: {self._reader_error}") from self._reader_error
            raise RuntimeError("SSE MCP connection closed")
        request_id = uuid.uuid4().hex[:8]
        pending = _PendingResponse()
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            self._post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            timeout = self.startup_timeout_seconds if startup else self.timeout_seconds
            try:
                message = pending.queue.get(timeout=timeout)
            except Empty as exc:
                raise RuntimeError(f"SSE MCP request '{method}' timed out after {timeout}s") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if isinstance(message, Exception):
            raise RuntimeError(f"SSE MCP request failed: {message}") from message
        if "error" in message:
            raise RuntimeError(str(message["error"]))
        return message

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self.endpoint_url:
            self.start()
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _post(self, payload: dict[str, Any]) -> None:
        if not self.endpoint_url:
            raise RuntimeError("SSE MCP endpoint is not initialized")
        parsed = urlparse(self.endpoint_url)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            **self.headers,
        }
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_class(parsed.hostname, parsed.port, timeout=self.timeout_seconds)
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read().decode("utf-8", errors="replace")
            if response.status < 200 or response.status >= 300:
                details = response_body.strip() or response.reason
                raise RuntimeError(f"SSE MCP POST failed: {response.status} {details}")
        except OSError as exc:
            raise RuntimeError(f"SSE MCP POST failed: {exc}") from exc
        finally:
            connection.close()

    def _read_loop(self) -> None:
        event_name = "message"
        data_lines: list[str] = []
        try:
            while not self._closed.is_set() and self._response is not None:
                raw_line = self._response.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    self._handle_event(event_name, "\n".join(data_lines))
                    event_name = "message"
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip() or "message"
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                self._handle_event(event_name, "\n".join(data_lines))
        except Exception as exc:
            self._reader_error = exc
            self._fail_pending(exc)
        finally:
            self._closed.set()

    def _handle_event(self, event_name: str, data: str) -> None:
        if not data:
            return
        if event_name == "endpoint":
            self.endpoint_url = urljoin(self.url, data)
            if "sessionId=" in data:
                self.session_id = data.split("sessionId=", 1)[1].split("&", 1)[0]
            self._endpoint_ready.set()
            return
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            return
        if "method" in message and "id" in message:
            self._post({"jsonrpc": "2.0", "id": message["id"], "result": {}})
            return
        message_id = str(message.get("id", ""))
        with self._pending_lock:
            pending = self._pending.get(message_id)
        if pending is not None:
            pending.queue.put(message)

    def _fail_pending(self, exc: Exception) -> None:
        with self._pending_lock:
            pending_items = list(self._pending.values())
        for pending in pending_items:
            pending.queue.put(exc)

    def close(self) -> None:
        self._closed.set()
        response = self._response
        self._response = None
        if response is not None:
            Thread(target=self._close_response, args=(response,), name="somnia-mcp-sse-close", daemon=True).start()
        thread = self._reader_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.2)
        self._reader_thread = None
        self.session_id = None
        self.endpoint_url = None

    @staticmethod
    def _close_response(response) -> None:
        try:
            response.close()
        except Exception:
            pass

    @staticmethod
    def _disable_response_timeout(response) -> None:
        for path in (
            ("fp", "raw", "_sock"),
            ("fp", "raw", "_fp", "fp", "raw", "_sock"),
        ):
            current = response
            for name in path:
                current = getattr(current, name, None)
                if current is None:
                    break
            if isinstance(current, socket.socket):
                current.settimeout(None)
                return
