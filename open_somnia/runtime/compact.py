from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from open_somnia.providers.base import ProviderError
from open_somnia.runtime.messages import image_source_block_to_reference
from open_somnia.runtime.thinking import strip_thinking_blocks_from_messages


AUTO_COMPACT_TRIGGER_RATIO = 0.82


@dataclass(slots=True)
class ContextWindowUsage:
    used_tokens: int
    max_tokens: int | None = None
    counter_name: str = "estimate"

    @property
    def usage_ratio(self) -> float | None:
        if not self.max_tokens:
            return None
        return self.used_tokens / self.max_tokens

    @property
    def usage_percent(self) -> float | None:
        ratio = self.usage_ratio
        if ratio is None:
            return None
        return ratio * 100.0


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    return estimate_payload_tokens("", messages, [])


def estimate_payload_tokens(system_prompt: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    payload = {
        "system": system_prompt,
        "messages": messages,
        "tools": tools,
    }
    return len(json.dumps(payload, ensure_ascii=False, default=str)) // 4


def _clone_messages_for_payload(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return deepcopy(messages)
    except Exception:
        return json.loads(json.dumps(messages, ensure_ascii=False, default=str))


def _tool_result_rounds(messages: list[dict[str, Any]]) -> list[tuple[int, list[dict[str, Any]]]]:
    rounds: list[tuple[int, list[dict[str, Any]]]] = []
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        tool_results = [
            item
            for item in content
            if isinstance(item, dict) and item.get("type") == "tool_result"
        ]
        if tool_results:
            rounds.append((index, tool_results))
    return rounds


def _tool_result_length(item: dict[str, Any]) -> int:
    return len(str(item.get("content", "")))


def _strip_tool_result_metadata(item: dict[str, Any]) -> None:
    item.pop("raw_output", None)
    item.pop("log_id", None)


def _is_tool_result_message(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(item, dict) and item.get("type") == "tool_result" for item in content)


def _message_contains_image_blocks(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, dict) and str(item.get("type", "")).strip() in {"image_url", "input_image"}
        for item in content
    )


def _stale_user_image_placeholder(item: dict[str, Any]) -> dict[str, Any]:
    return image_source_block_to_reference(item, origin="user_input")


def _strip_stale_user_image_blocks(payload_messages: list[dict[str, Any]]) -> None:
    latest_user_message_index: int | None = None
    latest_user_message_has_images = False
    for index, message in enumerate(payload_messages):
        if message.get("role") != "user" or _is_tool_result_message(message):
            continue
        latest_user_message_index = index
        latest_user_message_has_images = _message_contains_image_blocks(message)
    keep_image_blocks_index = latest_user_message_index if latest_user_message_has_images else None
    for index, message in enumerate(payload_messages):
        if message.get("role") != "user" or _is_tool_result_message(message):
            continue
        if index == keep_image_blocks_index or not _message_contains_image_blocks(message):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        message["content"] = [
            _stale_user_image_placeholder(item)
            if isinstance(item, dict) and str(item.get("type", "")).strip() in {"image_url", "input_image"}
            else item
            for item in content
        ]


def _strip_stale_tool_result_content_blocks(payload_messages: list[dict[str, Any]]) -> None:
    rounds = _tool_result_rounds(payload_messages)
    if len(rounds) <= 1:
        return
    latest_message_index, _latest_tool_results = rounds[-1]
    for message_index, tool_results in rounds:
        if message_index == latest_message_index:
            continue
        for item in tool_results:
            item.pop("content_blocks", None)
            item.pop("tool_result_text", None)


def build_payload_messages(
    messages: list[dict[str, Any]],
    preserve_thinking_blocks: bool = False,
) -> list[dict[str, Any]]:
    payload_messages = strip_thinking_blocks_from_messages(
        _clone_messages_for_payload(messages),
        preserve_thinking_blocks=preserve_thinking_blocks,
    )
    payload_messages = [
        message
        for message in payload_messages
        if not (
            message.get("content") == []
            or (isinstance(message.get("content"), str) and not str(message.get("content")).strip())
        )
    ]
    _strip_stale_user_image_blocks(payload_messages)
    rounds = _tool_result_rounds(payload_messages)
    for _, tool_results in rounds:
        for item in tool_results:
            _strip_tool_result_metadata(item)
    _strip_stale_tool_result_content_blocks(payload_messages)
    return payload_messages


def should_auto_compact(usage: ContextWindowUsage) -> bool:
    ratio = usage.usage_ratio
    return ratio is not None and ratio >= AUTO_COMPACT_TRIGGER_RATIO


class CompactManager:
    def __init__(self, provider, transcript_store, model_max_tokens: int):
        self.provider = provider
        self.transcript_store = transcript_store
        self.model_max_tokens = model_max_tokens
        self.last_usage: dict[str, Any] | None = None

    def _summarize_messages(self, messages: list[dict[str, Any]]) -> str:
        summary_messages = strip_thinking_blocks_from_messages(messages)
        try:
            summary_turn = self.provider.complete(
                system_prompt=(
                    "Compress the conversation for continuity.\n"
                    "Return concise plain text with these exact sections:\n"
                    "Current goal\n"
                    "Confirmed decisions\n"
                    "Open work\n"
                    "Files changed\n"
                    "Constraints\n"
                    "Risks\n"
                    "Focus on concrete state the next turn needs."
                ),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Summarize this conversation so the agent can continue working without the full history.\n"
                            "Keep it compact and implementation-focused.\n\n"
                            "Conversation:\n"
                            + json.dumps(summary_messages, ensure_ascii=False, default=str)[:80_000]
                        ),
                    }
                ],
                tools=[],
                max_tokens=min(2_000, self.model_max_tokens),
            )
            self.last_usage = getattr(summary_turn, "usage", None)
            return "\n".join(summary_turn.text_blocks).strip() or "Conversation compacted."
        except ProviderError as exc:
            self.last_usage = None
            return f"Conversation compacted without model summary due to error: {exc}"

    def auto_compact(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        preserve_from_index: int | None = None,
    ) -> list[dict[str, Any]]:
        self.transcript_store.save_snapshot(session_id, messages)
        if preserve_from_index is not None:
            preserve_from_index = max(0, min(preserve_from_index, len(messages)))
            older_messages = messages[:preserve_from_index]
            preserved_messages = messages[preserve_from_index:]
            if not older_messages:
                return messages
            summary = self._summarize_messages(older_messages)
            return [
                {"role": "user", "content": f"[Compressed earlier history for session {session_id}]\n{summary}"},
                {"role": "assistant", "content": "Understood. Continuing with the preserved active task window."},
                *preserved_messages,
            ]

        summary = self._summarize_messages(messages)
        return [
            {"role": "user", "content": f"[Compressed. Full transcript saved for session {session_id}]\n{summary}"},
            {"role": "assistant", "content": "Understood. Continuing from compacted context."},
        ]
