from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from openagent.providers.base import ProviderError


AUTO_COMPACT_TRIGGER_RATIO = 0.72
MICROCOMPACT_KEEP_RECENT_TOOL_ROUNDS = 2
MICROCOMPACT_OLD_RESULT_PREVIEW_CHARS = 160
MICROCOMPACT_RECENT_RESULT_PREVIEW_CHARS = 96
MICROCOMPACT_RECENT_RESULT_HARD_CAP_CHARS = 4_000
MICROCOMPACT_RECENT_TOOL_BUDGET_CHARS = 12_000


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


def _compact_text(text: Any) -> str:
    return " ".join(str(text).split()).strip()


def _preview_text(text: Any, *, limit: int) -> str:
    compact = _compact_text(text)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _clone_messages_for_payload(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return deepcopy(messages)
    except Exception:
        return json.loads(json.dumps(messages, ensure_ascii=False, default=str))


def _tool_call_name_map(messages: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_call":
                continue
            tool_call_id = str(item.get("id", "")).strip()
            if not tool_call_id:
                continue
            names[tool_call_id] = str(item.get("name", "")).strip() or "tool"
    return names


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


def _tool_result_summary(item: dict[str, Any], tool_call_names: dict[str, str], *, preview_limit: int) -> str:
    tool_call_id = str(item.get("tool_call_id", "")).strip()
    tool_name = tool_call_names.get(tool_call_id, "tool")
    preview = _preview_text(item.get("content", ""), limit=preview_limit)
    if preview:
        return f"[tool:{tool_name}] {preview}"
    return f"[tool:{tool_name}] output compacted"


def _compact_old_tool_results(
    rounds: list[tuple[int, list[dict[str, Any]]]],
    tool_call_names: dict[str, str],
) -> None:
    if len(rounds) <= MICROCOMPACT_KEEP_RECENT_TOOL_ROUNDS:
        return
    recent_start = len(rounds) - MICROCOMPACT_KEEP_RECENT_TOOL_ROUNDS
    for position, (_, tool_results) in enumerate(rounds):
        if position >= recent_start:
            continue
        for item in tool_results:
            if _tool_result_length(item) <= MICROCOMPACT_OLD_RESULT_PREVIEW_CHARS:
                continue
            item["content"] = _tool_result_summary(
                item,
                tool_call_names,
                preview_limit=MICROCOMPACT_OLD_RESULT_PREVIEW_CHARS,
            )


def _shrink_tool_round(
    tool_results: list[dict[str, Any]],
    tool_call_names: dict[str, str],
    *,
    target_chars: int,
    preview_limit: int,
    force_all: bool,
) -> int:
    current_chars = sum(_tool_result_length(item) for item in tool_results)
    if current_chars <= target_chars:
        return current_chars
    candidates = sorted(
        range(len(tool_results)),
        key=lambda index: _tool_result_length(tool_results[index]),
        reverse=True,
    )
    for index in candidates:
        if current_chars <= target_chars:
            break
        item = tool_results[index]
        old_chars = _tool_result_length(item)
        if not force_all and old_chars <= MICROCOMPACT_RECENT_RESULT_HARD_CAP_CHARS:
            continue
        compacted = _tool_result_summary(item, tool_call_names, preview_limit=preview_limit)
        new_chars = len(compacted)
        if new_chars >= old_chars:
            continue
        item["content"] = compacted
        current_chars -= old_chars - new_chars
    return current_chars


def _compact_recent_tool_rounds(
    rounds: list[tuple[int, list[dict[str, Any]]]],
    tool_call_names: dict[str, str],
) -> None:
    if not rounds:
        return
    remaining_budget = MICROCOMPACT_RECENT_TOOL_BUDGET_CHARS
    recent_rounds = rounds[-MICROCOMPACT_KEEP_RECENT_TOOL_ROUNDS :]
    for _, tool_results in reversed(recent_rounds):
        current_chars = sum(_tool_result_length(item) for item in tool_results)
        if current_chars > remaining_budget:
            current_chars = _shrink_tool_round(
                tool_results,
                tool_call_names,
                target_chars=max(remaining_budget, 0),
                preview_limit=MICROCOMPACT_OLD_RESULT_PREVIEW_CHARS,
                force_all=False,
            )
        if current_chars > remaining_budget:
            current_chars = _shrink_tool_round(
                tool_results,
                tool_call_names,
                target_chars=max(remaining_budget, 0),
                preview_limit=MICROCOMPACT_RECENT_RESULT_PREVIEW_CHARS,
                force_all=True,
            )
        remaining_budget = max(0, remaining_budget - min(current_chars, remaining_budget))


def build_payload_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload_messages = _clone_messages_for_payload(messages)
    tool_call_names = _tool_call_name_map(payload_messages)
    rounds = _tool_result_rounds(payload_messages)
    for _, tool_results in rounds:
        for item in tool_results:
            _strip_tool_result_metadata(item)
    _compact_old_tool_results(rounds, tool_call_names)
    _compact_recent_tool_rounds(rounds, tool_call_names)
    return payload_messages


def should_auto_compact(usage: ContextWindowUsage, *, hard_threshold: int) -> bool:
    ratio = usage.usage_ratio
    if ratio is not None and ratio >= AUTO_COMPACT_TRIGGER_RATIO:
        return True
    return usage.used_tokens >= hard_threshold


class CompactManager:
    def __init__(self, provider, transcript_store, model_max_tokens: int):
        self.provider = provider
        self.transcript_store = transcript_store
        self.model_max_tokens = model_max_tokens

    def auto_compact(self, session_id: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.transcript_store.save_snapshot(session_id, messages)
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
                            + json.dumps(messages, ensure_ascii=False, default=str)[:80_000]
                        ),
                    }
                ],
                tools=[],
                max_tokens=min(2_000, self.model_max_tokens),
            )
            summary = "\n".join(summary_turn.text_blocks).strip() or "Conversation compacted."
        except ProviderError as exc:
            summary = f"Conversation compacted without model summary due to error: {exc}"
        return [
            {"role": "user", "content": f"[Compressed. Full transcript saved for session {session_id}]\n{summary}"},
            {"role": "assistant", "content": "Understood. Continuing from compacted context."},
        ]
