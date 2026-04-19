from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from open_somnia.providers.base import ProviderError
from open_somnia.runtime.messages import normalize_tool_importance


SEMANTIC_JANITOR_TRIGGER_RATIO = 0.60
AUTO_COMPACT_TRIGGER_RATIO = 0.82
DUPLICATE_TOOL_RESULT_MIN_LENGTH = 240
READ_FILE_TRUNCATION_MARKER = "[read_file output truncated at "
READ_FILE_PREFIX_PATTERN = re.compile(r"^\.\.\. \((\d+) lines omitted before line (\d+)\)$")
READ_FILE_SUFFIX_PATTERN = re.compile(r"^\.\.\. \((\d+) more lines(?: after line (\d+))?\)$")


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
    importance: str | None
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


@dataclass(slots=True)
class ReadFilePayloadSpan:
    path: str
    start_line: int
    end_line: int
    visible_lines: list[str]
    prefix_marker: str | None = None
    suffix_marker: str | None = None


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


def _tool_call_lookup(messages: list[dict[str, Any]]) -> dict[str, tuple[str, dict[str, Any], str | None]]:
    lookup: dict[str, tuple[str, dict[str, Any], str | None]] = {}
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
            tool_input = dict(item.get("input") or {})
            importance = normalize_tool_importance(item.get("importance"))
            if importance is None:
                importance = normalize_tool_importance(tool_input.get("importance"))
            lookup[call_id] = (
                str(item.get("name", "")).strip() or "tool",
                tool_input,
                importance,
            )
    return lookup


def _duplicate_tool_result_summary(tool_name: str) -> str:
    label = str(tool_name).strip() or "tool"
    return f"[Duplicate tool result omitted | {label}] Identical output appears later."


def _overlapping_read_file_summary(span: ReadFilePayloadSpan) -> str:
    return (
        f"[Overlapping read_file result omitted | {span.path}:{span.start_line}-{span.end_line}] "
        "Covered by later read(s) of the same file."
    )


def _read_file_overlap_marker(start_line: int, end_line: int) -> str:
    count = max(0, end_line - start_line + 1)
    if count <= 0:
        return ""
    line_label = f"line {start_line}" if start_line == end_line else f"lines {start_line}-{end_line}"
    return (
        f"... ({count} overlapping lines omitted here; covered by later read(s) of the same file, "
        f"{line_label})"
    )


def _normalized_read_file_path(tool_input: dict[str, Any]) -> str:
    path = str(tool_input.get("path", "")).strip().replace("\\", "/")
    return path


def _parse_optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except Exception:
        return None
    if parsed <= 0:
        return None
    return parsed


def _parse_read_file_payload_span(
    item: dict[str, Any],
    lookup: dict[str, tuple[str, dict[str, Any], str | None]],
) -> ReadFilePayloadSpan | None:
    call_id = str(item.get("tool_call_id", "")).strip()
    tool_name, tool_input, _importance = lookup.get(call_id, ("tool", {}, None))
    if tool_name != "read_file":
        return None
    path = _normalized_read_file_path(tool_input)
    if not path:
        return None
    content = str(item.get("content", ""))
    if not content or READ_FILE_TRUNCATION_MARKER in content:
        return None
    if content.startswith("[Duplicate tool result omitted |") or content.startswith("[Semantic Summary |"):
        return None
    if content.startswith("[Context Evicted |") or content.startswith("[Overlapping read_file result omitted |"):
        return None

    start_line = _parse_optional_positive_int(tool_input.get("start_line")) or 1
    end_line = _parse_optional_positive_int(tool_input.get("end_line"))
    limit = _parse_optional_positive_int(tool_input.get("limit"))
    if end_line is None and limit is not None:
        end_line = start_line + limit - 1

    rendered_lines = [] if content == "" else content.split("\n")
    prefix_marker: str | None = None
    suffix_marker: str | None = None
    visible_lines = list(rendered_lines)

    if visible_lines and READ_FILE_PREFIX_PATTERN.match(visible_lines[0]):
        prefix_marker = visible_lines.pop(0)
    if visible_lines and READ_FILE_SUFFIX_PATTERN.match(visible_lines[-1]):
        suffix_marker = visible_lines.pop()

    if end_line is None:
        if not visible_lines:
            return None
        end_line = start_line + len(visible_lines) - 1
    expected_count = max(0, end_line - start_line + 1)
    if len(visible_lines) != expected_count:
        return None
    return ReadFilePayloadSpan(
        path=path,
        start_line=start_line,
        end_line=end_line,
        visible_lines=visible_lines,
        prefix_marker=prefix_marker,
        suffix_marker=suffix_marker,
    )


def _subtract_interval(start_line: int, end_line: int, covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    remaining: list[tuple[int, int]] = []
    cursor = start_line
    for covered_start, covered_end in covered:
        if covered_end < cursor:
            continue
        if covered_start > end_line:
            break
        if covered_start > cursor:
            remaining.append((cursor, min(end_line, covered_start - 1)))
        cursor = max(cursor, covered_end + 1)
        if cursor > end_line:
            break
    if cursor <= end_line:
        remaining.append((cursor, end_line))
    return remaining


def _merge_interval(intervals: list[tuple[int, int]], new_interval: tuple[int, int]) -> list[tuple[int, int]]:
    start_line, end_line = new_interval
    merged: list[tuple[int, int]] = []
    inserted = False
    for current_start, current_end in intervals:
        if current_end + 1 < start_line:
            merged.append((current_start, current_end))
            continue
        if end_line + 1 < current_start:
            if not inserted:
                merged.append((start_line, end_line))
                inserted = True
            merged.append((current_start, current_end))
            continue
        start_line = min(start_line, current_start)
        end_line = max(end_line, current_end)
    if not inserted:
        merged.append((start_line, end_line))
    return merged


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _serialize_read_file_overlap_coverage(
    coverage: dict[str, list[tuple[int, int]]],
) -> dict[str, list[list[int]]]:
    return {
        path: [[start_line, end_line] for start_line, end_line in intervals]
        for path, intervals in coverage.items()
        if intervals
    }


def _normalize_read_file_overlap_state(
    read_file_overlap_state: dict[str, Any] | None,
) -> tuple[dict[str, list[tuple[int, int]]], set[str]]:
    if not isinstance(read_file_overlap_state, dict):
        return {}, set()

    raw_coverage = read_file_overlap_state.get("coverage")
    if not isinstance(raw_coverage, dict):
        raw_coverage = {
            key: value
            for key, value in read_file_overlap_state.items()
            if key not in {"coverage", "source_tool_call_ids"}
        }

    coverage: dict[str, list[tuple[int, int]]] = {}
    for raw_path, raw_intervals in raw_coverage.items():
        path = str(raw_path).strip().replace("\\", "/")
        if not path or not isinstance(raw_intervals, list):
            continue
        intervals: list[tuple[int, int]] = []
        for raw_interval in raw_intervals:
            if not isinstance(raw_interval, (list, tuple)) or len(raw_interval) < 2:
                continue
            start_line = _parse_optional_positive_int(raw_interval[0])
            end_line = _parse_optional_positive_int(raw_interval[1])
            if start_line is None or end_line is None:
                continue
            if end_line < start_line:
                start_line, end_line = end_line, start_line
            intervals = _merge_interval(intervals, (start_line, end_line))
        if intervals:
            coverage[path] = intervals

    source_tool_call_ids = set(
        _dedupe_preserve_order(list(read_file_overlap_state.get("source_tool_call_ids") or []))
    )
    return coverage, source_tool_call_ids


def _render_pruned_read_file_content(
    span: ReadFilePayloadSpan,
    kept_segments: list[tuple[int, int]],
) -> str:
    lines: list[str] = []
    if span.prefix_marker:
        lines.append(span.prefix_marker)

    cursor = span.start_line
    for segment_start, segment_end in kept_segments:
        if segment_start > cursor:
            gap_marker = _read_file_overlap_marker(cursor, segment_start - 1)
            if gap_marker:
                lines.append(gap_marker)
        offset_start = segment_start - span.start_line
        offset_end = segment_end - span.start_line + 1
        lines.extend(span.visible_lines[offset_start:offset_end])
        cursor = segment_end + 1

    if cursor <= span.end_line:
        gap_marker = _read_file_overlap_marker(cursor, span.end_line)
        if gap_marker:
            lines.append(gap_marker)
    if span.suffix_marker:
        lines.append(span.suffix_marker)
    return "\n".join(lines)


def _latest_round_read_file_overlap_state(
    rounds: list[tuple[int, list[dict[str, Any]]]],
    lookup: dict[str, tuple[str, dict[str, Any], str | None]],
) -> tuple[bool, dict[str, list[tuple[int, int]]], list[str]]:
    if not rounds:
        return False, {}, []
    _message_index, latest_tool_results = rounds[-1]
    coverage: dict[str, list[tuple[int, int]]] = {}
    source_tool_call_ids: list[str] = []
    saw_read_file = False
    for item in latest_tool_results:
        call_id = str(item.get("tool_call_id", "")).strip()
        tool_name, tool_input, _importance = lookup.get(call_id, ("tool", {}, None))
        if tool_name != "read_file":
            continue
        saw_read_file = True
        if call_id:
            source_tool_call_ids.append(call_id)
        span = _parse_read_file_payload_span(item, lookup)
        if span is None:
            continue
        coverage[span.path] = _merge_interval(
            coverage.get(span.path, []),
            (span.start_line, span.end_line),
        )
    return saw_read_file, coverage, _dedupe_preserve_order(source_tool_call_ids)


def extract_latest_read_file_overlap_state(messages: list[dict[str, Any]]) -> dict[str, Any]:
    rounds = _tool_result_rounds(messages)
    if not rounds:
        return {}
    lookup = _tool_call_lookup(messages)
    saw_read_file, coverage, source_tool_call_ids = _latest_round_read_file_overlap_state(rounds, lookup)
    if not saw_read_file:
        return {}
    return {
        "source_tool_call_ids": source_tool_call_ids,
        "coverage": _serialize_read_file_overlap_coverage(coverage),
    }


def _suppress_overlapping_read_file_results(
    payload_messages: list[dict[str, Any]],
    *,
    read_file_overlap_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rounds = _tool_result_rounds(payload_messages)
    if not rounds:
        return payload_messages
    lookup = _tool_call_lookup(payload_messages)
    latest_has_read_file, latest_coverage, latest_source_tool_call_ids = _latest_round_read_file_overlap_state(
        rounds,
        lookup,
    )
    persisted_coverage, persisted_source_tool_call_ids = _normalize_read_file_overlap_state(read_file_overlap_state)

    if latest_has_read_file:
        if latest_coverage:
            active_paths = set(latest_coverage)
            covered_by_path: dict[str, list[tuple[int, int]]] = {}
            source_tool_call_ids: set[str] = set()
        elif (
            latest_source_tool_call_ids
            and set(latest_source_tool_call_ids) == persisted_source_tool_call_ids
            and persisted_coverage
        ):
            active_paths = set(persisted_coverage)
            covered_by_path = {
                path: list(intervals)
                for path, intervals in persisted_coverage.items()
            }
            source_tool_call_ids = persisted_source_tool_call_ids
        else:
            return payload_messages
    else:
        active_paths = set(persisted_coverage)
        covered_by_path = {
            path: list(intervals)
            for path, intervals in persisted_coverage.items()
        }
        source_tool_call_ids = persisted_source_tool_call_ids
    if not active_paths:
        return payload_messages

    for _message_index, tool_results in reversed(rounds):
        for item in reversed(tool_results):
            call_id = str(item.get("tool_call_id", "")).strip()
            span = _parse_read_file_payload_span(item, lookup)
            if span is None:
                continue
            if span.path not in active_paths:
                continue
            covered = covered_by_path.get(span.path, [])
            if call_id not in source_tool_call_ids and covered:
                kept_segments = _subtract_interval(span.start_line, span.end_line, covered)
                if not kept_segments:
                    item["content"] = _overlapping_read_file_summary(span)
                elif kept_segments != [(span.start_line, span.end_line)]:
                    item["content"] = _render_pruned_read_file_content(span, kept_segments)
            covered_by_path[span.path] = _merge_interval(
                covered,
                (span.start_line, span.end_line),
            )
    return payload_messages


def _tool_result_dedup_key(
    item: dict[str, Any],
    lookup: dict[str, tuple[str, dict[str, Any], str | None]],
) -> tuple[str, str, str] | None:
    content = str(item.get("content", ""))
    if len(content) < DUPLICATE_TOOL_RESULT_MIN_LENGTH:
        return None
    call_id = str(item.get("tool_call_id", "")).strip()
    tool_name, tool_input, _importance = lookup.get(call_id, ("tool", {}, None))
    normalized_input = {
        key: value
        for key, value in dict(tool_input or {}).items()
        if key != "importance"
    }
    try:
        input_signature = json.dumps(normalized_input, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        input_signature = json.dumps(str(normalized_input), ensure_ascii=False)
    content_hash = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()
    return (str(tool_name).strip() or "tool", input_signature, content_hash)


def _dedupe_tool_results(payload_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rounds = _tool_result_rounds(payload_messages)
    if not rounds:
        return payload_messages
    lookup = _tool_call_lookup(payload_messages)
    seen_keys: set[tuple[str, str, str]] = set()
    for _message_index, tool_results in reversed(rounds):
        for item in reversed(tool_results):
            key = _tool_result_dedup_key(item, lookup)
            if key is None:
                continue
            if key in seen_keys:
                call_id = str(item.get("tool_call_id", "")).strip()
                tool_name, _tool_input, _importance = lookup.get(call_id, ("tool", {}, None))
                item["content"] = _duplicate_tool_result_summary(tool_name)
                continue
            seen_keys.add(key)
    return payload_messages


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
            tool_name, tool_input, importance = lookup.get(call_id, ("tool", {}, None))
            content = str(item.get("content", ""))
            candidates.append(
                ToolResultCandidate(
                    locator=ToolResultLocator(message_index=message_index, item_index=item_index),
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    importance=importance,
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
    read_file_overlap_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    payload_messages = _clone_messages_for_payload(messages)
    apply_semantic_compression(payload_messages, semantic_decisions)
    rounds = _tool_result_rounds(payload_messages)
    for _, tool_results in rounds:
        for item in tool_results:
            _strip_tool_result_metadata(item)
    _dedupe_tool_results(payload_messages)
    _suppress_overlapping_read_file_results(
        payload_messages,
        read_file_overlap_state=read_file_overlap_state,
    )
    return payload_messages


def should_run_semantic_janitor(
    usage: ContextWindowUsage,
    *,
    trigger_ratio: float = SEMANTIC_JANITOR_TRIGGER_RATIO,
) -> bool:
    ratio = usage.usage_ratio
    return ratio is not None and ratio >= float(trigger_ratio)


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
