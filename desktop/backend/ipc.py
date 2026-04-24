from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import base64
import hashlib
import json
import struct
from typing import Any


_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def serialize_session(session: Any) -> dict[str, Any]:
    payload_getter = getattr(session, "to_payload", None)
    if callable(payload_getter):
        return deepcopy(payload_getter())
    return {
        "id": getattr(session, "id", None),
        "created_at": getattr(session, "created_at", None),
        "updated_at": getattr(session, "updated_at", None),
        "messages": deepcopy(list(getattr(session, "messages", []) or [])),
        "token_usage": deepcopy(dict(getattr(session, "token_usage", {}) or {})),
        "todo_items": deepcopy(list(getattr(session, "todo_items", []) or [])),
        "rounds_without_todo": int(getattr(session, "rounds_without_todo", 0) or 0),
        "read_file_overlap_state": deepcopy(dict(getattr(session, "read_file_overlap_state", {}) or {})),
        "latest_turn_id": getattr(session, "latest_turn_id", None),
        "last_turn_file_changes": deepcopy(list(getattr(session, "last_turn_file_changes", []) or [])),
        "undo_stack": deepcopy(list(getattr(session, "undo_stack", []) or [])),
    }


def serialize_provider(provider: Any) -> dict[str, Any]:
    return asdict(provider)


def serialize_model(model: Any) -> dict[str, Any]:
    return asdict(model)


def serialize_interaction(interaction: Any) -> dict[str, Any]:
    return {
        "id": str(getattr(interaction, "id", "")).strip(),
        "kind": str(getattr(interaction, "kind", "")).strip(),
        "session_id": getattr(interaction, "session_id", None),
        "turn_id": getattr(interaction, "turn_id", None),
        "payload": deepcopy(dict(getattr(interaction, "payload", {}) or {})),
        "response": deepcopy(getattr(interaction, "response", None)),
    }


def serialize_turn_result(result: Any) -> dict[str, Any]:
    return {
        "text": str(getattr(result, "text", "")),
        "status": str(getattr(result, "status", "")),
        "open_todo_count": int(getattr(result, "open_todo_count", 0) or 0),
        "interrupted": bool(getattr(result, "interrupted", False)),
        "error": getattr(result, "error", None),
        "session": serialize_session(getattr(result, "session", None)) if getattr(result, "session", None) is not None else None,
    }


def serialize_app_event(event: Any) -> dict[str, Any]:
    return {
        "type": str(getattr(event, "type", "")).strip(),
        "session_id": getattr(event, "session_id", None),
        "turn_id": getattr(event, "turn_id", None),
        "payload": deepcopy(dict(getattr(event, "payload", {}) or {})),
        "timestamp": float(getattr(event, "timestamp", 0.0) or 0.0),
    }


def make_sidecar_event(
    event_type: str,
    *,
    payload: dict[str, Any] | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    return {
        "type": str(event_type).strip(),
        "session_id": session_id,
        "turn_id": turn_id,
        "payload": deepcopy(payload or {}),
    }


def websocket_accept_value(key: str) -> str:
    digest = hashlib.sha1((str(key).strip() + _WEBSOCKET_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def build_websocket_frame(opcode: int, payload: bytes = b"") -> bytes:
    length = len(payload)
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    if length < 126:
        header.append(length)
    elif length <= 0xFFFF:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + payload


def build_websocket_text_frame(text: str) -> bytes:
    return build_websocket_frame(0x1, str(text).encode("utf-8"))


def build_websocket_pong_frame(payload: bytes = b"") -> bytes:
    return build_websocket_frame(0xA, payload)


def build_websocket_close_frame(code: int = 1000, reason: str = "") -> bytes:
    payload = struct.pack("!H", int(code)) + str(reason).encode("utf-8")
    return build_websocket_frame(0x8, payload)


def read_websocket_frame(stream) -> tuple[int, bytes] | None:
    header = _read_exact(stream, 2)
    if header is None:
        return None
    first_byte, second_byte = header
    opcode = first_byte & 0x0F
    masked = bool(second_byte & 0x80)
    payload_length = second_byte & 0x7F
    if payload_length == 126:
        extended_length = _read_exact(stream, 2)
        if extended_length is None:
            return None
        payload_length = struct.unpack("!H", extended_length)[0]
    elif payload_length == 127:
        extended_length = _read_exact(stream, 8)
        if extended_length is None:
            return None
        payload_length = struct.unpack("!Q", extended_length)[0]
    mask = _read_exact(stream, 4) if masked else b""
    if masked and mask is None:
        return None
    payload = _read_exact(stream, payload_length)
    if payload is None:
        return None
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def _read_exact(stream, size: int) -> bytes | None:
    remaining = max(0, int(size))
    chunks: list[bytes] = []
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
