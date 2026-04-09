from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from open_somnia.providers.base import ProviderError


AUTO_COMPACT_TRIGGER_RATIO = 0.72
MICROCOMPACT_KEEP_RECENT_TOOL_ROUNDS = 3
MICROCOMPACT_OLD_RESULT_PREVIEW_CHARS = 160
MICROCOMPACT_OLD_STRUCTURED_RESULT_PREVIEW_CHARS = 480
MICROCOMPACT_RECENT_RESULT_PREVIEW_CHARS = 96
MICROCOMPACT_RECENT_STRUCTURED_RESULT_PREVIEW_CHARS = 320
MICROCOMPACT_RECENT_RESULT_HARD_CAP_CHARS = 4_000
MICROCOMPACT_RECENT_STRUCTURED_RESULT_HARD_CAP_CHARS = 12_000
MICROCOMPACT_RECENT_TOOL_BUDGET_CHARS = 18_000
MICROCOMPACT_STRUCTURED_TOOLS = frozenset({"read_file", "tree", "project_scan", "find_symbol", "grep"})
MICROCOMPACT_MEDIUM_PRIORITY_TOOLS = frozenset({"grep", "find_symbol"})


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


def _tool_call_meta_map(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    names: dict[str, dict[str, Any]] = {}
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
            names[tool_call_id] = {
                "name": str(item.get("name", "")).strip() or "tool",
                "input": dict(item.get("input", {}) or {}),
            }
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


def _tool_name_and_input(item: dict[str, Any], tool_call_meta: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    tool_call_id = str(item.get("tool_call_id", "")).strip()
    metadata = tool_call_meta.get(tool_call_id, {})
    return str(metadata.get("name", "tool")).strip() or "tool", dict(metadata.get("input", {}) or {})


def _tool_is_structured(tool_name: str) -> bool:
    return tool_name in MICROCOMPACT_STRUCTURED_TOOLS


def _tool_preview_limit(tool_name: str, *, recent: bool) -> int:
    if _tool_is_structured(tool_name):
        return MICROCOMPACT_RECENT_STRUCTURED_RESULT_PREVIEW_CHARS if recent else MICROCOMPACT_OLD_STRUCTURED_RESULT_PREVIEW_CHARS
    return MICROCOMPACT_RECENT_RESULT_PREVIEW_CHARS if recent else MICROCOMPACT_OLD_RESULT_PREVIEW_CHARS


def _tool_hard_cap(tool_name: str) -> int:
    if _tool_is_structured(tool_name):
        return MICROCOMPACT_RECENT_STRUCTURED_RESULT_HARD_CAP_CHARS
    return MICROCOMPACT_RECENT_RESULT_HARD_CAP_CHARS


def _tool_compaction_rank(tool_name: str) -> int:
    if tool_name in {"read_file", "tree", "project_scan"}:
        return 2
    if tool_name in MICROCOMPACT_MEDIUM_PRIORITY_TOOLS:
        return 1
    return 0


def _line_preview_text(text: Any, *, limit: int, max_lines: int) -> str:
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    selected: list[str] = []
    used = 0
    for line in lines:
        if len(selected) >= max_lines:
            break
        compact_line = line.rstrip()
        extra = len(compact_line) + (1 if selected else 0)
        if selected and used + extra > limit:
            break
        if not selected and len(compact_line) > limit:
            selected.append(compact_line[: max(0, limit - 3)].rstrip() + "...")
            used = len(selected[0])
            break
        selected.append(compact_line)
        used += extra
    preview = "\n".join(selected).strip()
    if not preview:
        return ""
    if len(preview) < len(normalized.strip()):
        if not preview.endswith("..."):
            preview = preview.rstrip() + "\n..."
    return preview


def _tool_label(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name == "read_file":
        path = str(tool_input.get("path", "")).strip()
        return f"tool:{tool_name} path={path}" if path else f"tool:{tool_name}"
    if tool_name in {"tree", "project_scan"}:
        path = str(tool_input.get("path", "")).strip()
        return f"tool:{tool_name} path={path or '.'}"
    if tool_name in {"find_symbol", "grep", "glob"}:
        query = str(tool_input.get("query", tool_input.get("pattern", ""))).strip()
        path = str(tool_input.get("path", "")).strip()
        parts = [f"tool:{tool_name}"]
        if query:
            parts.append(f"query={query}")
        if path:
            parts.append(f"path={path}")
        return " ".join(parts)
    return f"tool:{tool_name}"


def _tool_result_summary(item: dict[str, Any], tool_call_meta: dict[str, dict[str, Any]], *, recent: bool) -> str:
    tool_name, tool_input = _tool_name_and_input(item, tool_call_meta)
    preview_limit = _tool_preview_limit(tool_name, recent=recent)
    if _tool_is_structured(tool_name):
        preview = _line_preview_text(
            item.get("content", ""),
            limit=preview_limit,
            max_lines=12 if recent else 18,
        )
    else:
        preview = _preview_text(item.get("content", ""), limit=preview_limit)
    label = _tool_label(tool_name, tool_input)
    if preview:
        if "\n" in preview:
            return f"[{label}]\n{preview}"
        return f"[{label}] {preview}"
    return f"[{label}] output compacted"


def _compact_old_tool_results(
    rounds: list[tuple[int, list[dict[str, Any]]]],
    tool_call_meta: dict[str, dict[str, Any]],
) -> None:
    if len(rounds) <= MICROCOMPACT_KEEP_RECENT_TOOL_ROUNDS:
        return
    recent_start = len(rounds) - MICROCOMPACT_KEEP_RECENT_TOOL_ROUNDS
    for position, (_, tool_results) in enumerate(rounds):
        if position >= recent_start:
            continue
        for item in tool_results:
            tool_name, _ = _tool_name_and_input(item, tool_call_meta)
            if _tool_result_length(item) <= _tool_preview_limit(tool_name, recent=False):
                continue
            item["content"] = _tool_result_summary(item, tool_call_meta, recent=False)


def _shrink_tool_round(
    tool_results: list[dict[str, Any]],
    tool_call_meta: dict[str, dict[str, Any]],
    *,
    target_chars: int,
    recent: bool,
    force_all: bool,
) -> int:
    current_chars = sum(_tool_result_length(item) for item in tool_results)
    if current_chars <= target_chars:
        return current_chars
    candidates = sorted(
        range(len(tool_results)),
        key=lambda index: (
            _tool_compaction_rank(_tool_name_and_input(tool_results[index], tool_call_meta)[0]),
            -_tool_result_length(tool_results[index]),
        ),
    )
    for index in candidates:
        if current_chars <= target_chars:
            break
        item = tool_results[index]
        tool_name, _ = _tool_name_and_input(item, tool_call_meta)
        old_chars = _tool_result_length(item)
        if not force_all and old_chars <= _tool_hard_cap(tool_name):
            continue
        compacted = _tool_result_summary(item, tool_call_meta, recent=recent)
        new_chars = len(compacted)
        if new_chars >= old_chars:
            continue
        item["content"] = compacted
        current_chars -= old_chars - new_chars
    return current_chars


def _compact_recent_tool_rounds(
    rounds: list[tuple[int, list[dict[str, Any]]]],
    tool_call_meta: dict[str, dict[str, Any]],
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
                tool_call_meta,
                target_chars=max(remaining_budget, 0),
                recent=False,
                force_all=False,
            )
        if current_chars > remaining_budget:
            current_chars = _shrink_tool_round(
                tool_results,
                tool_call_meta,
                target_chars=max(remaining_budget, 0),
                recent=True,
                force_all=True,
            )
        remaining_budget = max(0, remaining_budget - min(current_chars, remaining_budget))


def build_payload_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload_messages = _clone_messages_for_payload(messages)
    tool_call_meta = _tool_call_meta_map(payload_messages)
    rounds = _tool_result_rounds(payload_messages)
    for _, tool_results in rounds:
        for item in tool_results:
            _strip_tool_result_metadata(item)
    _compact_old_tool_results(rounds, tool_call_meta)
    _compact_recent_tool_rounds(rounds, tool_call_meta)
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
