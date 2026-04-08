from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2025-11-25"
SERVER_NAME = "somnia-minimal-stdio"
SERVER_VERSION = "0.1.0"


def _write_message(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def _read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in {b"\r\n", b"\n"}:
            break
        key, _, value = line.decode("utf-8", errors="replace").partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def _jsonrpc_result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _jsonrpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "echo",
            "description": "Echo the provided arguments back as formatted JSON text.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Optional primary message to echo."},
                },
                "additionalProperties": True,
            },
        },
        {
            "name": "server_info",
            "description": "Return basic runtime information for stdio MCP connectivity checks.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    ]


def _tool_result_text(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def _handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "echo":
        return _tool_result_text(json.dumps(arguments, ensure_ascii=False, indent=2, sort_keys=True))
    if name == "server_info":
        info = {
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "platform": platform.platform(),
            "cwd": str(Path.cwd()),
            "pid": os.getpid(),
        }
        return _tool_result_text(json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True))
    return _tool_result_text(f"Unknown tool: {name}", is_error=True)


def _handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = str(message.get("method", "")).strip()
    msg_id = message.get("id")
    params = message.get("params", {})

    if msg_id is None:
        return None

    if method == "initialize":
        requested_version = str(params.get("protocolVersion") or PROTOCOL_VERSION)
        return _jsonrpc_result(
            msg_id,
            {
                "protocolVersion": requested_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "tools/list":
        return _jsonrpc_result(msg_id, {"tools": _tool_definitions()})
    if method == "tools/call":
        name = str(params.get("name", "")).strip()
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        return _jsonrpc_result(msg_id, _handle_tool_call(name, arguments))
    return _jsonrpc_error(msg_id, -32601, f"Method not found: {method}")


def main() -> int:
    while True:
        message = _read_message()
        if message is None:
            return 0
        response = _handle_request(message)
        if response is not None:
            _write_message(response)


if __name__ == "__main__":
    raise SystemExit(main())
