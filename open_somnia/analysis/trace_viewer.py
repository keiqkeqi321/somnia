from __future__ import annotations

import hashlib
import html
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from open_somnia.storage.common import atomic_write_text


PROVIDER_PAYLOAD_DIRNAME = "provider_payloads"
DEFAULT_REPORT_NAME = "trace-viewer.html"
RUNTIME_NOTICE_MARKER = "<runtime-notice"


@dataclass(slots=True)
class TraceRecord:
    path: Path
    payload: dict[str, Any]
    timestamp: float
    session_id: str
    actor: str
    kind: str
    provider_name: str
    provider_type: str
    model: str
    context_used_tokens: int
    context_max_tokens: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    latency_ms: float | None
    provider_error: str
    message_count: int
    message_content_chars: int
    system_prompt_chars: int
    system_stable_chars: int
    system_dynamic_chars: int
    tool_count: int
    transient_message_count: int
    runtime_notice_count: int
    messages_digest: str
    system_digest: str
    tools_digest: str

    @property
    def cache_hit_ratio(self) -> float | None:
        if self.input_tokens <= 0:
            return None
        return self.cache_read_input_tokens / self.input_tokens

    @property
    def context_usage_ratio(self) -> float | None:
        if self.context_max_tokens <= 0:
            return None
        return self.context_used_tokens / self.context_max_tokens

    @property
    def label(self) -> str:
        return f"{self.session_id or 'unknown'} / {self.kind} / {self.path.name}"


@dataclass(slots=True)
class TraceDiff:
    previous: TraceRecord
    current: TraceRecord
    common_prefix_messages: int
    first_diff_message_index: int | None
    previous_message_count: int
    current_message_count: int
    system_changed: bool
    tools_changed: bool
    model_changed: bool
    cache_risks: list[str]


def provider_payload_dir(logs_dir: Path) -> Path:
    return Path(logs_dir) / PROVIDER_PAYLOAD_DIRNAME


def default_report_path(logs_dir: Path) -> Path:
    return provider_payload_dir(logs_dir) / DEFAULT_REPORT_NAME


def load_trace_records(payload_dir: Path, *, session_id: str | None = None, limit: int | None = None) -> list[TraceRecord]:
    records: list[TraceRecord] = []
    for path in sorted(Path(payload_dir).glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        record = trace_record_from_payload(path, payload)
        if session_id and record.session_id != session_id:
            continue
        records.append(record)
    records.sort(key=lambda item: (item.timestamp, item.path.name))
    if limit is not None and limit > 0:
        records = records[-limit:]
    return records


def trace_record_from_payload(path: Path, payload: dict[str, Any]) -> TraceRecord:
    provider = _dict_value(payload.get("provider"))
    context_usage = _dict_value(payload.get("context_usage"))
    payload_summary = _dict_value(payload.get("payload_summary"))
    message_summary = _dict_value(payload.get("message_summary"))
    usage = _usage_from_payload(payload)
    messages = _list_value(payload.get("messages"))
    tools = _list_value(payload.get("tools"))
    system_sections = _list_value(payload.get("system_prompt_sections"))
    stable_chars, dynamic_chars = _system_section_chars(system_sections)
    provider_error = payload.get("provider_error")
    if isinstance(provider_error, dict):
        error_text = str(provider_error.get("message") or provider_error.get("type") or "").strip()
    else:
        error_text = str(provider_error or "").strip()

    return TraceRecord(
        path=path,
        payload=payload,
        timestamp=_float_value(payload.get("timestamp")),
        session_id=str(payload.get("session_id") or "").strip(),
        actor=str(payload.get("actor") or "").strip() or "lead",
        kind=str(payload.get("kind") or payload_summary.get("kind") or "").strip() or "turn",
        provider_name=str(provider.get("name") or "").strip(),
        provider_type=str(provider.get("type") or "").strip(),
        model=str(provider.get("model") or "").strip(),
        context_used_tokens=_int_value(context_usage.get("used_tokens")),
        context_max_tokens=_int_value(context_usage.get("max_tokens")),
        input_tokens=_int_value(_first_present(usage, ("input_tokens", "prompt_tokens"))),
        output_tokens=_int_value(_first_present(usage, ("output_tokens", "completion_tokens"))),
        total_tokens=_int_value(usage.get("total_tokens")),
        cache_read_input_tokens=_int_value(usage.get("cache_read_input_tokens")),
        cache_creation_input_tokens=_int_value(usage.get("cache_creation_input_tokens")),
        latency_ms=_optional_float(payload.get("latency_ms")),
        provider_error=error_text,
        message_count=_int_value(payload_summary.get("message_count"), fallback=_int_value(message_summary.get("total"), fallback=len(messages))),
        message_content_chars=_int_value(payload_summary.get("message_content_chars"), fallback=_int_value(message_summary.get("content_chars"))),
        system_prompt_chars=_int_value(payload_summary.get("system_prompt_chars")),
        system_stable_chars=stable_chars,
        system_dynamic_chars=dynamic_chars,
        tool_count=_int_value(payload_summary.get("tool_count"), fallback=len(tools)),
        transient_message_count=_count_transient_messages(messages),
        runtime_notice_count=_count_runtime_notices(messages),
        messages_digest=_digest_json(messages),
        system_digest=_digest_json(system_sections or payload.get("system_prompt") or ""),
        tools_digest=_digest_json(_tool_identity(tools)),
    )


def build_trace_diffs(records: list[TraceRecord]) -> list[TraceDiff]:
    diffs: list[TraceDiff] = []
    previous_by_session: dict[str, TraceRecord] = {}
    for record in records:
        session_key = record.session_id or "__unknown__"
        previous = previous_by_session.get(session_key)
        if previous is not None:
            diffs.append(compare_trace_records(previous, record))
        previous_by_session[session_key] = record
    return diffs


def compare_trace_records(previous: TraceRecord, current: TraceRecord) -> TraceDiff:
    previous_messages = _list_value(previous.payload.get("messages"))
    current_messages = _list_value(current.payload.get("messages"))
    common_prefix = 0
    for left, right in zip(previous_messages, current_messages):
        if _canonical_json(left) != _canonical_json(right):
            break
        common_prefix += 1
    if common_prefix == len(previous_messages) == len(current_messages):
        first_diff_index: int | None = None
    else:
        first_diff_index = common_prefix

    system_changed = previous.system_digest != current.system_digest
    tools_changed = previous.tools_digest != current.tools_digest
    model_changed = (previous.provider_type, previous.model) != (current.provider_type, current.model)
    cache_risks: list[str] = []
    if system_changed:
        cache_risks.append("system changed")
    if tools_changed:
        cache_risks.append("tools changed")
    if model_changed:
        cache_risks.append("model changed")
    if first_diff_index is not None:
        prior_len = max(1, min(len(previous_messages), len(current_messages)))
        if first_diff_index == 0:
            cache_risks.append("message prefix changed at start")
        elif first_diff_index < math.ceil(prior_len * 0.5):
            cache_risks.append("early message prefix changed")
    if current.transient_message_count or current.runtime_notice_count:
        cache_risks.append("transient/runtime notice present")
    if current.kind != "turn":
        cache_risks.append(f"non-turn payload: {current.kind}")
    return TraceDiff(
        previous=previous,
        current=current,
        common_prefix_messages=common_prefix,
        first_diff_message_index=first_diff_index,
        previous_message_count=len(previous_messages),
        current_message_count=len(current_messages),
        system_changed=system_changed,
        tools_changed=tools_changed,
        model_changed=model_changed,
        cache_risks=cache_risks,
    )


def build_trace_viewer_report(
    logs_dir: Path,
    *,
    output_path: Path | None = None,
    session_id: str | None = None,
    limit: int | None = None,
) -> Path:
    logs_dir = Path(logs_dir)
    payload_dir = provider_payload_dir(logs_dir)
    records = load_trace_records(payload_dir, session_id=session_id, limit=limit)
    diffs = build_trace_diffs(records)
    html_text = render_trace_viewer(records, diffs, payload_dir=payload_dir, session_id=session_id, limit=limit)
    target = Path(output_path) if output_path is not None else default_report_path(logs_dir)
    atomic_write_text(target, html_text)
    return target


def render_trace_viewer(
    records: list[TraceRecord],
    diffs: list[TraceDiff] | None = None,
    *,
    payload_dir: Path | None = None,
    session_id: str | None = None,
    limit: int | None = None,
) -> str:
    diffs = diffs if diffs is not None else build_trace_diffs(records)
    total_input = sum(record.input_tokens for record in records)
    total_output = sum(record.output_tokens for record in records)
    total_cache_read = sum(record.cache_read_input_tokens for record in records)
    total_cache_creation = sum(record.cache_creation_input_tokens for record in records)
    sessions = {record.session_id for record in records if record.session_id}
    hit_ratio = total_cache_read / total_input if total_input else None
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    filter_note = []
    if session_id:
        filter_note.append(f"session={session_id}")
    if limit:
        filter_note.append(f"limit={limit}")
    subtitle = " · ".join(filter_note) if filter_note else "all provider payload dumps"

    rows = "\n".join(_render_trace_row(record, index) for index, record in enumerate(records, start=1))
    details = "\n".join(_render_trace_detail(record, index) for index, record in enumerate(records, start=1))
    diff_rows = "\n".join(_render_diff_row(diff, index) for index, diff in enumerate(diffs, start=1))
    if not rows:
        rows = "<tr><td colspan=\"12\" class=\"empty\">No provider payload dumps found.</td></tr>"
    if not details:
        details = "<section class=\"empty-panel\">No traces available. Enable SOMNIA_DEBUG_PROVIDER_PAYLOADS=1 and run a turn first.</section>"
    if not diff_rows:
        diff_rows = "<tr><td colspan=\"8\" class=\"empty\">Need at least two payloads in the same session to diff prefixes.</td></tr>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Somnia Trace Viewer</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1d232b;
      --muted: #667085;
      --line: #d9dee7;
      --accent: #0f766e;
      --accent-weak: #d7f4ee;
      --warn: #b45309;
      --warn-weak: #fff3d6;
      --bad: #b42318;
      --bad-weak: #ffe4e0;
      --good: #027a48;
      --good-weak: #dcfae6;
      --code: #111827;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      padding: 28px 32px 18px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
    }}
    h1 {{ margin: 0 0 6px; font-size: 28px; letter-spacing: 0; }}
    h2 {{ margin: 28px 0 12px; font-size: 18px; letter-spacing: 0; }}
    h3 {{ margin: 0 0 10px; font-size: 15px; letter-spacing: 0; }}
    main {{ padding: 22px 32px 40px; }}
    .muted {{ color: var(--muted); }}
    .toolbar {{
      display: flex;
      gap: 10px;
      align-items: center;
      margin: 18px 0 4px;
      max-width: 760px;
    }}
    input[type="search"] {{
      width: 100%;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px;
      margin-top: 18px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }}
    .card strong {{ display: block; font-size: 22px; margin-top: 4px; }}
    .table-wrap {{
      overflow-x: auto;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    table {{ width: 100%; border-collapse: collapse; min-width: 980px; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: top; text-align: left; }}
    th {{ position: sticky; top: 0; background: #f9fafb; color: #475467; font-size: 12px; text-transform: uppercase; }}
    tr:last-child td {{ border-bottom: 0; }}
    code, pre {{
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
    }}
    pre {{
      margin: 8px 0 0;
      max-height: 360px;
      overflow: auto;
      padding: 10px;
      color: var(--code);
      background: #f3f4f6;
      border: 1px solid var(--line);
      border-radius: 6px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 2px 8px;
      border-radius: 999px;
      background: #eef2f6;
      color: #344054;
      font-size: 12px;
      white-space: nowrap;
    }}
    .good {{ background: var(--good-weak); color: var(--good); }}
    .warn {{ background: var(--warn-weak); color: var(--warn); }}
    .bad {{ background: var(--bad-weak); color: var(--bad); }}
    .accent {{ background: var(--accent-weak); color: var(--accent); }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 5px; }}
    details.trace {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      margin-bottom: 10px;
      overflow: hidden;
    }}
    details.trace > summary {{
      cursor: pointer;
      padding: 12px 14px;
      font-weight: 650;
      border-bottom: 1px solid transparent;
    }}
    details.trace[open] > summary {{ border-bottom-color: var(--line); }}
    .detail-body {{ padding: 14px; display: grid; grid-template-columns: minmax(0, 1fr); gap: 14px; }}
    .two-col {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }}
    .empty, .empty-panel {{ color: var(--muted); text-align: center; padding: 24px; }}
    .hidden {{ display: none; }}
    @media (max-width: 760px) {{
      header, main {{ padding-left: 16px; padding-right: 16px; }}
      h1 {{ font-size: 24px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Somnia Trace Viewer</h1>
    <div class="muted">Generated {html.escape(generated_at)} from {html.escape(str(payload_dir or 'provider payloads'))} · {html.escape(subtitle)}</div>
    <div class="toolbar">
      <input id="filter" type="search" placeholder="Filter by session, model, provider, kind, risk, filename, or message preview" aria-label="Filter traces">
    </div>
    <div class="grid">
      {_metric_card("Traces", str(len(records)))}
      {_metric_card("Sessions", str(len(sessions)))}
      {_metric_card("Cache hit", _format_percent(hit_ratio))}
      {_metric_card("Input tokens", _format_int(total_input))}
      {_metric_card("Cache read", _format_int(total_cache_read))}
      {_metric_card("Cache create", _format_int(total_cache_creation))}
      {_metric_card("Output tokens", _format_int(total_output))}
    </div>
  </header>
  <main>
    <h2>Requests</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th><th>Time</th><th>Session</th><th>Kind</th><th>Provider</th><th>Model</th>
            <th>Messages</th><th>System</th><th>Tools</th><th>Cache</th><th>Context</th><th>Status</th>
          </tr>
        </thead>
        <tbody id="trace-rows">{rows}</tbody>
      </table>
    </div>

    <h2>Adjacent Prefix Diffs</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th><th>Session</th><th>From</th><th>To</th><th>Common prefix</th><th>First diff</th><th>Changed</th><th>Cache risk</th>
          </tr>
        </thead>
        <tbody id="diff-rows">{diff_rows}</tbody>
      </table>
    </div>

    <h2>Trace Details</h2>
    <section id="trace-details">{details}</section>
  </main>
  <script>
    const filter = document.getElementById('filter');
    function applyFilter() {{
      const q = (filter.value || '').trim().toLowerCase();
      for (const row of document.querySelectorAll('[data-search]')) {{
        row.classList.toggle('hidden', q.length > 0 && !row.dataset.search.includes(q));
      }}
    }}
    filter.addEventListener('input', applyFilter);
  </script>
</body>
</html>
"""


def _render_trace_row(record: TraceRecord, index: int) -> str:
    search = _search_text(
        record.session_id,
        record.actor,
        record.kind,
        record.provider_name,
        record.provider_type,
        record.model,
        record.path.name,
        _message_preview(record),
        record.provider_error,
    )
    cache_class = _ratio_class(record.cache_hit_ratio)
    status = "error" if record.provider_error else "ok"
    status_class = "bad" if record.provider_error else "good"
    transient = ""
    if record.transient_message_count or record.runtime_notice_count:
        transient = f" <span class=\"pill warn\">transient {record.transient_message_count} / notice {record.runtime_notice_count}</span>"
    return f"""<tr data-search="{html.escape(search)}">
  <td>{index}</td>
  <td>{html.escape(_format_time(record.timestamp))}</td>
  <td><code>{html.escape(record.session_id or "-")}</code><div class="muted">{html.escape(record.actor)}</div></td>
  <td><span class="pill accent">{html.escape(record.kind)}</span></td>
  <td>{html.escape(record.provider_name or "-")}<div class="muted">{html.escape(record.provider_type or "-")}</div></td>
  <td><code>{html.escape(record.model or "-")}</code></td>
  <td>{record.message_count}<div class="muted">{_format_int(record.message_content_chars)} chars</div>{transient}</td>
  <td>{_format_int(record.system_prompt_chars)} chars<div class="muted">stable {_format_int(record.system_stable_chars)} / dynamic {_format_int(record.system_dynamic_chars)}</div></td>
  <td>{record.tool_count}</td>
  <td><span class="pill {cache_class}">{_format_percent(record.cache_hit_ratio)}</span><div class="muted">read {_format_int(record.cache_read_input_tokens)} · create {_format_int(record.cache_creation_input_tokens)}</div></td>
  <td>{_format_int(record.context_used_tokens)} / {_format_int(record.context_max_tokens)}<div class="muted">{_format_percent(record.context_usage_ratio)}</div></td>
  <td><span class="pill {status_class}">{status}</span><div class="muted">{_format_latency(record.latency_ms)}</div></td>
</tr>"""


def _render_diff_row(diff: TraceDiff, index: int) -> str:
    changed: list[str] = []
    if diff.system_changed:
        changed.append("system")
    if diff.tools_changed:
        changed.append("tools")
    if diff.model_changed:
        changed.append("model")
    if diff.first_diff_message_index is not None:
        changed.append("messages")
    risk_html = _chips(diff.cache_risks, default="low risk", default_class="good")
    search = _search_text(diff.current.session_id, diff.previous.path.name, diff.current.path.name, *diff.cache_risks, *changed)
    first_diff = "-" if diff.first_diff_message_index is None else str(diff.first_diff_message_index)
    return f"""<tr data-search="{html.escape(search)}">
  <td>{index}</td>
  <td><code>{html.escape(diff.current.session_id or "-")}</code></td>
  <td>{html.escape(diff.previous.path.name)}</td>
  <td>{html.escape(diff.current.path.name)}</td>
  <td>{diff.common_prefix_messages} / {diff.previous_message_count} -> {diff.current_message_count}</td>
  <td>{html.escape(first_diff)}</td>
  <td>{_chips(changed, default="none", default_class="good")}</td>
  <td>{risk_html}</td>
</tr>"""


def _render_trace_detail(record: TraceRecord, index: int) -> str:
    messages = _list_value(record.payload.get("messages"))
    provider_request = record.payload.get("provider_request")
    provider_response = record.payload.get("provider_response")
    system_sections = record.payload.get("system_prompt_sections")
    search = _search_text(
        record.session_id,
        record.kind,
        record.provider_name,
        record.provider_type,
        record.model,
        record.path.name,
        _message_preview(record),
    )
    message_rows = "\n".join(_render_message_preview(message, idx) for idx, message in enumerate(messages))
    if not message_rows:
        message_rows = "<div class=\"muted\">No messages captured.</div>"
    return f"""<details class="trace" data-search="{html.escape(search)}">
  <summary>#{index} {html.escape(record.path.name)} · {html.escape(record.session_id or "-")} · {html.escape(record.kind)} · cache {_format_percent(record.cache_hit_ratio)}</summary>
  <div class="detail-body">
    <div class="two-col">
      <div>
        <h3>Messages</h3>
        {message_rows}
      </div>
      <div>
        <h3>Payload Metrics</h3>
        <div class="chips">
          <span class="pill">input {_format_int(record.input_tokens)}</span>
          <span class="pill">output {_format_int(record.output_tokens)}</span>
          <span class="pill accent">cache read {_format_int(record.cache_read_input_tokens)}</span>
          <span class="pill accent">cache create {_format_int(record.cache_creation_input_tokens)}</span>
          <span class="pill">latency {_format_latency(record.latency_ms)}</span>
        </div>
        <pre>{html.escape(json.dumps(_summary_payload(record), ensure_ascii=False, indent=2, default=str))}</pre>
      </div>
    </div>
    <details>
      <summary>System prompt sections</summary>
      <pre>{html.escape(json.dumps(system_sections, ensure_ascii=False, indent=2, default=str))}</pre>
    </details>
    <details>
      <summary>Provider request</summary>
      <pre>{html.escape(json.dumps(provider_request, ensure_ascii=False, indent=2, default=str))}</pre>
    </details>
    <details>
      <summary>Provider response</summary>
      <pre>{html.escape(json.dumps(provider_response, ensure_ascii=False, indent=2, default=str))}</pre>
    </details>
  </div>
</details>"""


def _render_message_preview(message: Any, index: int) -> str:
    if not isinstance(message, dict):
        return f"<pre>#{index} {html.escape(json.dumps(message, ensure_ascii=False, default=str))}</pre>"
    role = str(message.get("role") or "unknown")
    content_text = _message_content_text(message)
    flags: list[str] = []
    if message.get("transient") is True:
        flags.append("transient")
    if RUNTIME_NOTICE_MARKER in content_text:
        flags.append("runtime-notice")
    chip_html = _chips(flags, default="", default_class="accent")
    preview = content_text[:1200]
    return f"""<div class="card">
  <h3>#{index} {html.escape(role)} {chip_html}</h3>
  <pre>{html.escape(preview)}</pre>
</div>"""


def _summary_payload(record: TraceRecord) -> dict[str, Any]:
    return {
        "timestamp": record.timestamp,
        "time": _format_time(record.timestamp),
        "session_id": record.session_id,
        "actor": record.actor,
        "kind": record.kind,
        "provider": {
            "name": record.provider_name,
            "type": record.provider_type,
            "model": record.model,
        },
        "messages": {
            "count": record.message_count,
            "content_chars": record.message_content_chars,
            "transient_count": record.transient_message_count,
            "runtime_notice_count": record.runtime_notice_count,
        },
        "system_prompt": {
            "chars": record.system_prompt_chars,
            "stable_chars": record.system_stable_chars,
            "dynamic_chars": record.system_dynamic_chars,
        },
        "tools": {
            "count": record.tool_count,
            "summary": record.payload.get("tool_schema_summary"),
        },
        "usage": {
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "total_tokens": record.total_tokens,
            "cache_read_input_tokens": record.cache_read_input_tokens,
            "cache_creation_input_tokens": record.cache_creation_input_tokens,
            "cache_hit_ratio": record.cache_hit_ratio,
        },
        "context_usage": record.payload.get("context_usage"),
        "provider_error": record.payload.get("provider_error"),
    }


def _metric_card(label: str, value: str) -> str:
    return f"<div class=\"card\"><span class=\"muted\">{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>"


def _chips(values: list[str], *, default: str, default_class: str) -> str:
    if not values:
        if not default:
            return ""
        return f"<span class=\"pill {default_class}\">{html.escape(default)}</span>"
    return "<span class=\"chips\">" + "".join(f"<span class=\"pill warn\">{html.escape(value)}</span>" for value in values) + "</span>"


def _usage_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    provider_response = _dict_value(payload.get("provider_response"))
    usage = provider_response.get("usage")
    if isinstance(usage, dict):
        return usage
    provider_request = _dict_value(payload.get("provider_request"))
    request_usage = provider_request.get("usage")
    if isinstance(request_usage, dict):
        return request_usage
    return {}


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int_value(value: Any, *, fallback: int = 0) -> int:
    try:
        if value is None or value == "":
            return fallback
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _float_value(value: Any, *, fallback: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _optional_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _system_section_chars(sections: list[Any]) -> tuple[int, int]:
    stable = 0
    dynamic = 0
    for section in sections:
        if not isinstance(section, dict):
            continue
        chars = _int_value(section.get("chars"), fallback=len(str(section.get("content") or "")))
        if section.get("dynamic") is True:
            dynamic += chars
        else:
            stable += chars
    return stable, dynamic


def _count_transient_messages(messages: list[Any]) -> int:
    return sum(1 for message in messages if isinstance(message, dict) and message.get("transient") is True)


def _count_runtime_notices(messages: list[Any]) -> int:
    return sum(1 for message in messages if isinstance(message, dict) and RUNTIME_NOTICE_MARKER in _message_content_text(message))


def _message_content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, default=str))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False, default=str)


def _message_preview(record: TraceRecord) -> str:
    messages = _list_value(record.payload.get("messages"))
    previews: list[str] = []
    for message in messages[-4:]:
        if isinstance(message, dict):
            previews.append(f"{message.get('role', 'unknown')}: {_message_content_text(message)[:160]}")
    return "\n".join(previews)


def _tool_identity(tools: list[Any]) -> list[Any]:
    identities: list[Any] = []
    for tool in tools:
        if not isinstance(tool, dict):
            identities.append(tool)
            continue
        identities.append(
            {
                "name": tool.get("name"),
                "input_schema": tool.get("input_schema"),
                "description": tool.get("description"),
            }
        )
    return identities


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _format_time(timestamp: float) -> str:
    if timestamp <= 0:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _format_int(value: int) -> str:
    return f"{value:,}"


def _format_percent(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def _format_latency(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f} ms"


def _ratio_class(value: float | None) -> str:
    if value is None:
        return ""
    if value >= 0.5:
        return "good"
    if value > 0:
        return "warn"
    return "bad"


def _search_text(*values: Any) -> str:
    return " ".join(str(value or "") for value in values).lower()
