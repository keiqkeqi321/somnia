from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any

REQUIRED_ACCEPT_TYPES = ("application/json", "text/event-stream")


class StreamableHTTPTransport:
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

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            **self.headers,
        }
        accept_values: list[str] = []
        raw_accept = headers.get("Accept", "")
        if raw_accept:
            accept_values.extend(
                part.strip() for part in raw_accept.split(",") if part.strip()
            )
        seen = {value.lower() for value in accept_values}
        for required in REQUIRED_ACCEPT_TYPES:
            if required.lower() not in seen:
                accept_values.append(required)
        headers["Accept"] = ", ".join(accept_values)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def request(self, method: str, params: dict[str, Any] | None = None, *, startup: bool = False) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex[:8],
            "method": method,
            "params": params or {},
        }
        timeout = self.startup_timeout_seconds if startup else self.timeout_seconds
        headers = self._request_headers()
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
                if session_id:
                    self.session_id = session_id
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/event-stream" in content_type:
                    message = self._read_sse_response(response, payload["id"])
                else:
                    body = response.read().decode("utf-8")
                    message = json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP MCP request failed: {exc.code} {details}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"HTTP MCP request failed: {exc}") from exc
        if "error" in message:
            raise RuntimeError(str(message["error"]))
        return message

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        headers = self._request_headers()
        request = urllib.request.Request(
            self.url,
            data=json.dumps(
                {"jsonrpc": "2.0", "method": method, "params": params or {}},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds):
                return
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP MCP notify failed: {exc.code} {details}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"HTTP MCP notify failed: {exc}") from exc

    def _read_sse_response(self, response, request_id: str) -> dict[str, Any]:
        data_lines: list[str] = []
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if not data_lines:
                    continue
                payload = "\n".join(data_lines)
                data_lines = []
                try:
                    message = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if str(message.get("id")) == str(request_id):
                    return message
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            message = json.loads("\n".join(data_lines))
            if str(message.get("id")) == str(request_id):
                return message
        raise RuntimeError("HTTP MCP server did not return a matching JSON-RPC response")

    def close(self) -> None:
        if not self.session_id:
            return
        headers = self._request_headers()
        request = urllib.request.Request(self.url, headers=headers, method="DELETE")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds):
                pass
        except Exception:
            pass
        finally:
            self.session_id = None
