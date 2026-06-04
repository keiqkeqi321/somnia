from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import time
from typing import Any


THINKING_BLOCK_TYPES = {"thinking", "redacted_thinking"}
THINKING_LOG_BLOCK_TYPE = "thinking_log"
THINKING_CONTEXT_BLOCK_TYPES = {*THINKING_BLOCK_TYPES, THINKING_LOG_BLOCK_TYPE}
THINKING_LOG_MAX_CHARS = 16_000


def is_thinking_block(value: Any) -> bool:
    return isinstance(value, dict) and str(value.get("type", "")).strip() in THINKING_BLOCK_TYPES


def is_thinking_context_block(value: Any) -> bool:
    return isinstance(value, dict) and str(value.get("type", "")).strip() in THINKING_CONTEXT_BLOCK_TYPES


def strip_thinking_blocks_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stripped: list[dict[str, Any]] = []
    for message in messages:
        clone = deepcopy(message)
        content = clone.get("content")
        if isinstance(content, list):
            clone["content"] = [item for item in content if not is_thinking_context_block(item)]
        stripped.append(clone)
    return stripped


def extract_thinking_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [dict(item) for item in content if is_thinking_block(item)]


def strip_thinking_blocks_from_message(message: dict[str, Any]) -> dict[str, Any]:
    return strip_thinking_blocks_from_messages([message])[0]


def make_thinking_log_block(
    *,
    turn_id: str,
    path: str,
    characters: int,
    block_count: int,
    duration_ms: float | None = None,
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": THINKING_LOG_BLOCK_TYPE,
        "turn_id": turn_id,
        "path": path,
        "characters": max(0, int(characters)),
        "block_count": max(0, int(block_count)),
    }
    if duration_ms is not None:
        block["duration_ms"] = max(0.0, float(duration_ms))
    return block


class ThinkingLogWriter:
    def __init__(self, root: Path, session_id: str, turn_id: str):
        self.root = root / "thinking"
        self.session_id = session_id
        self.turn_id = turn_id
        self.path = self.root / f"{session_id}.{turn_id}.jsonl"
        self.started_at = time.monotonic()
        self.characters = 0
        self.block_count = 0
        self.truncated_characters = 0
        self._text_parts: list[str] = []
        self._opened = False

    def append_block(self, block: dict[str, Any]) -> None:
        if not is_thinking_block(block):
            return
        text = str(block.get("thinking", "") or block.get("data", "") or "")
        self._append_text(text)

    def append_delta(self, delta: str, *, block_type: str = "thinking_delta") -> None:
        self._append_text(str(delta or ""))

    def _append_text(self, text: str) -> None:
        if not text:
            return
        self.characters += len(text)
        self.block_count += 1
        current_length = sum(len(part) for part in self._text_parts)
        remaining = THINKING_LOG_MAX_CHARS - current_length
        if remaining <= 0:
            self.truncated_characters += len(text)
            return
        self._text_parts.append(text[:remaining])
        if len(text) > remaining:
            self.truncated_characters += len(text) - remaining

    def flush(self) -> None:
        text = "".join(self._text_parts)
        if not text and self.characters <= 0:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "type": "thinking",
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "timestamp": time.time(),
            "thinking": text,
            "characters": self.characters,
            "truncated_characters": self.truncated_characters,
        }
        with self.path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str))
            handle.write("\n")
        self._opened = True

    @property
    def has_content(self) -> bool:
        return self.characters > 0 or self._opened or self.path.exists()

    def marker(self) -> dict[str, Any]:
        self.flush()
        return make_thinking_log_block(
            turn_id=self.turn_id,
            path=str(self.path),
            characters=self.characters,
            block_count=self.block_count,
            duration_ms=(time.monotonic() - self.started_at) * 1000,
        )
