from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from open_somnia.providers.base import ProviderError


SEMANTIC_JANITOR_TRIGGER_RATIO = 0.60
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


@dataclass(slots=True, frozen=True)
class ToolResultLocator:
    message_index: int
    item_index: int


@dataclass(slots=True)
class ToolResultCandidate:
    locator: ToolResultLocator
    tool_call_id: str
    tool_name: str
    tool_input: dict[str, Any]
    content: str
    log_id: str | None
    age: int
    output_length: int
    output_preview: str
    has_error: bool = False


@dataclass(slots=True)
class SemanticCompressionDecision:
    message_index: int
    item_index: int
    state: str
    summary: str | None = None

    @property
    def locator(self) -> ToolResultLocator:
        return ToolResultLocator(self.message_index, self.item_index)


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


def _tool_call_lookup(messages: list[dict[str, Any]]) -> dict[str, tuple[str, dict[str, Any]]]:
    lookup: dict[str, tuple[str, dict[str, Any]]] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_call":
                continue
            call_id = str(item.get("id", "")).strip()
            if not call_id:
                continue
            lookup[call_id] = (
                str(item.get("name", "")).strip() or "tool",
                dict(item.get("input") or {}),
            )
    return lookup


def _compact_text(text: str, *, limit: int = 220) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


def _looks_like_error(text: str) -> bool:
    lowered = str(text).lower()
    return any(
        marker in lowered
        for marker in (
            "traceback",
            "exception",
            "error",
            "failed",
            "failure",
            "assertionerror",
            "syntaxerror",
        )
    )


def extract_tool_result_candidates(
    messages: list[dict[str, Any]],
    *,
    preserve_recent_rounds: int = 2,
    preview_chars: int = 220,
) -> list[ToolResultCandidate]:
    rounds = _tool_result_rounds(messages)
    if not rounds:
        return []
    protected_indexes = {
        message_index for message_index, _ in rounds[-max(0, preserve_recent_rounds) :]
    }
    lookup = _tool_call_lookup(messages)
    candidates: list[ToolResultCandidate] = []
    total_rounds = len(rounds)
    for round_position, (message_index, tool_results) in enumerate(rounds):
        if message_index in protected_indexes:
            continue
        for item_index, item in enumerate(tool_results):
            semantic_state = str(item.get("semantic_state", "")).strip().lower()
            if semantic_state in {"condensed", "evicted"}:
                continue
            call_id = str(item.get("tool_call_id", "")).strip()
            tool_name, tool_input = lookup.get(call_id, ("tool", {}))
            content = str(item.get("content", ""))
            candidates.append(
                ToolResultCandidate(
                    locator=ToolResultLocator(message_index=message_index, item_index=item_index),
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    content=content,
                    log_id=str(item.get("log_id", "")).strip() or None,
                    age=total_rounds - round_position,
                    output_length=len(content),
                    output_preview=_compact_text(content, limit=preview_chars),
                    has_error=_looks_like_error(content),
                )
            )
    return candidates


def apply_semantic_compression(
    payload_messages: list[dict[str, Any]],
    semantic_decisions: list[SemanticCompressionDecision] | None,
) -> list[dict[str, Any]]:
    if not semantic_decisions:
        return payload_messages
    decisions = {decision.locator: decision for decision in semantic_decisions}
    rounds = _tool_result_rounds(payload_messages)
    for message_index, tool_results in rounds:
        for item_index, item in enumerate(tool_results):
            decision = decisions.get(ToolResultLocator(message_index=message_index, item_index=item_index))
            if decision is None:
                continue
            if decision.state == "original":
                continue
            if decision.state in {"condensed", "evicted"} and decision.summary:
                item["content"] = decision.summary
    return payload_messages


def persist_semantic_compression(
    messages: list[dict[str, Any]],
    semantic_decisions: list[SemanticCompressionDecision] | None,
) -> bool:
    if not semantic_decisions:
        return False
    decisions = {decision.locator: decision for decision in semantic_decisions}
    rounds = _tool_result_rounds(messages)
    changed = False
    for message_index, tool_results in rounds:
        for item_index, item in enumerate(tool_results):
            decision = decisions.get(ToolResultLocator(message_index=message_index, item_index=item_index))
            if decision is None:
                continue
            state = str(decision.state).strip().lower()
            if state == "original":
                if item.pop("semantic_state", None) is not None:
                    changed = True
                continue
            if state not in {"condensed", "evicted"}:
                continue
            if decision.summary and item.get("content") != decision.summary:
                item["content"] = decision.summary
                changed = True
            if item.get("semantic_state") != state:
                item["semantic_state"] = state
                changed = True
            if "raw_output" in item:
                item.pop("raw_output", None)
                changed = True
    return changed


def build_payload_messages(
    messages: list[dict[str, Any]],
    semantic_decisions: list[SemanticCompressionDecision] | None = None,
) -> list[dict[str, Any]]:
    payload_messages = _clone_messages_for_payload(messages)
    apply_semantic_compression(payload_messages, semantic_decisions)
    rounds = _tool_result_rounds(payload_messages)
    for _, tool_results in rounds:
        for item in tool_results:
            _strip_tool_result_metadata(item)
    return payload_messages


def should_run_semantic_janitor(usage: ContextWindowUsage) -> bool:
    ratio = usage.usage_ratio
    return ratio is not None and ratio >= SEMANTIC_JANITOR_TRIGGER_RATIO


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
        self.last_usage: dict[str, Any] | None = None

    def _summarize_messages(self, messages: list[dict[str, Any]]) -> str:
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
