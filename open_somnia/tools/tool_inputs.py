from __future__ import annotations

import re
from typing import Any


TOOL_INTENT_MAX_CHARS = 160
RESERVED_TOOL_INPUT_KEYS = frozenset({"intent", "importance"})


def normalize_tool_intent(value: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    if len(text) <= TOOL_INTENT_MAX_CHARS:
        return text
    return f"{text[: TOOL_INTENT_MAX_CHARS - 3].rstrip()}..."


def normalize_tool_input_for_history(payload: dict[str, Any] | None) -> dict[str, Any]:
    normalized = dict(payload or {})
    if "intent" in normalized:
        intent = normalize_tool_intent(normalized.get("intent"))
        if intent is None:
            normalized.pop("intent", None)
        else:
            normalized["intent"] = intent
    return normalized


def strip_reserved_tool_input_for_execution(payload: dict[str, Any] | None) -> dict[str, Any]:
    sanitized = dict(payload or {})
    for key in RESERVED_TOOL_INPUT_KEYS:
        sanitized.pop(key, None)
    return sanitized


def strip_reserved_tool_input_for_execution_in_place(payload: dict[str, Any]) -> dict[str, Any]:
    for key in RESERVED_TOOL_INPUT_KEYS:
        payload.pop(key, None)
    return payload
