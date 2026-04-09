from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import json
import random
import sys
import time
from queue import Empty, Queue
from threading import Event, Lock, Thread

from open_somnia.cli.commands import ConsoleStreamer, _assistant_prefix, _prefix_first_line, print_user_message
from open_somnia.cli.prompting import (
    COMMAND_SPECS,
    PROMPT_BORDER,
    PROMPT_TEXT,
    choose_authorization_interactively,
    choose_item_interactively,
    choose_mode_switch_interactively,
    create_prompt_session,
    fallback_prompt_message,
    prompt_text_interactively,
    styled_prompt_message,
)
from open_somnia.cli.provider_management import collect_provider_profile_interactively, choose_provider_target_interactively
from open_somnia.config.settings import persist_provider_profile
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.compact import ContextWindowUsage
from open_somnia.runtime.execution_mode import (
    DEFAULT_EXECUTION_MODE,
    execution_mode_spec,
    execution_mode_status_text,
    next_execution_mode,
    normalize_execution_mode,
)
from open_somnia.runtime.messages import render_markdown_text, render_message_content, render_text_content
from open_somnia.tools.todo import TODO_CLOSED_STATUSES, TODO_STATUS_MARKERS, TODO_VISIBLE_STATUSES

try:
    from prompt_toolkit.patch_stdout import patch_stdout
except Exception:  # pragma: no cover - prompt_toolkit may be unavailable in fallback mode
    patch_stdout = None


READ_ONLY_COMMAND_PREFIXES = (
    "/scan",
    "/symbols",
    "/providers",
    "/skills",
    "/tasks",
    "/team",
    "/teamlog",
    "/inbox",
    "/mcp",
    "/toollog",
    "/bg",
    "/help",
)
AUTHORIZATION_PROMPT_SENTINEL = "__open_somnia_authorization__"


def _parse_skill_command(query: str) -> tuple[str, str] | None:
    stripped = query.strip()
    if not stripped.startswith("/+"):
        return None
    payload = stripped[2:].strip()
    if not payload:
        return None
    parts = payload.split(maxsplit=1)
    skill_name = parts[0].strip()
    if not skill_name:
        return None
    remainder = parts[1].strip() if len(parts) > 1 else ""
    return skill_name, remainder


def _expand_skill_command(runtime, query: str) -> str:
    parsed = _parse_skill_command(query)
    if parsed is None:
        return query
    skill_name, remainder = parsed
    skill_payload = runtime.skill_loader.load(skill_name)
    if skill_payload.startswith("Error:"):
        return skill_payload
    instruction = f"The user explicitly requested skill '{skill_name}'. Follow it for this task."
    if remainder:
        return f"{skill_payload}\n\n{instruction}\n\n{remainder}"
    return f"{skill_payload}\n\n{instruction}"


def _assistant_tool_calls(content: object) -> list[dict[str, object]]:
    if not isinstance(content, list):
        return []
    calls: list[dict[str, object]] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_call":
            calls.append(item)
    return calls


def _tool_result_map(content: object) -> dict[str, object]:
    if not isinstance(content, list):
        return {}
    results: dict[str, object] = {}
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_result":
            results[str(item.get("tool_call_id", ""))] = item
    return results


def _print_resumed_tool_call(runtime, tool_name: str, payload: dict[str, object], result_payload: object) -> None:
    if isinstance(result_payload, dict):
        output = result_payload.get("raw_output", result_payload.get("content", "(no output)"))
        log_id = str(result_payload.get("log_id", "")).strip() or None
    else:
        output = result_payload
        log_id = None
    print()
    for line in runtime.render_tool_event_lines(tool_name, payload, output, log_id=log_id):
        print(line)
    print()


def _print_resumed_history(session, runtime=None) -> None:
    printed_any = False
    header_printed = False
    index = 0
    messages = list(getattr(session, "messages", []) or [])
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        content = message.get("content")
        if role == "user":
            if isinstance(content, str):
                if content.startswith("<background-results>") or content.startswith("<inbox>"):
                    index += 1
                    continue
                if not header_printed:
                    print("[resumed history]")
                    header_printed = True
                print_user_message(content)
                printed_any = True
            index += 1
            continue
        if role == "assistant":
            text = render_message_content(content, ansi=sys.stdout.isatty()).strip()
            if text:
                if not header_printed:
                    print("[resumed history]")
                    header_printed = True
                print()
                print(_prefix_first_line(text, _assistant_prefix(ansi=sys.stdout.isatty())))
                print()
                printed_any = True
            tool_calls = _assistant_tool_calls(content)
            tool_results = {}
            if index + 1 < len(messages):
                next_message = messages[index + 1]
                if next_message.get("role") == "user":
                    tool_results = _tool_result_map(next_message.get("content"))
                    if tool_results:
                        index += 1
            for tool_call in tool_calls:
                if not header_printed:
                    print("[resumed history]")
                    header_printed = True
                if runtime is None:
                    index += 1
                    continue
                _print_resumed_tool_call(
                    runtime,
                    str(tool_call.get("name", "")),
                    dict(tool_call.get("input", {}) or {}),
                    tool_results.get(str(tool_call.get("id", "")), "(no output)"),
                )
                printed_any = True
        index += 1
    if not printed_any:
        print("[resumed session has no visible chat history]")


@dataclass(slots=True)
class AuthorizationRequest:
    tool_name: str
    reason: str
    argument_summary: str
    execution_mode: str
    completed: Event
    response: dict[str, str] | None = None


@dataclass(slots=True)
class ModeSwitchRequest:
    target_mode: str
    current_mode: str
    reason: str
    completed: Event
    response: dict[str, str] | None = None


@dataclass(slots=True)
class QueueTask:
    id: int
    kind: str
    payload: str
    echo_on_start: bool
    preview: str


class TurnQueueRunner:
    THINKING_PHRASES = (
        "AI is cooking",
        "Processing vibes",
        "Doing robot thoughts",
        "Consulting the void",
        "Loading genius",
    )
    DONE_TEXT = "done"
    THINKING_FRAME_SECONDS = 0.25
    CONTEXT_HEALTHY_STYLE = "fg:#22c55e"
    CONTEXT_WARNING_STYLE = "fg:#84cc16"
    CONTEXT_REDUCING_STYLE = "fg:#f59e0b"
    CONTEXT_CRITICAL_STYLE = "fg:#ef4444"

    def __init__(self, runtime, session, *, stable_prompt: bool = False) -> None:
        self.runtime = runtime
        self.session = session
        self.stable_prompt = stable_prompt
        self._execution_mode = normalize_execution_mode(getattr(runtime, "execution_mode", DEFAULT_EXECUTION_MODE))
        setattr(self.runtime, "execution_mode", self._execution_mode)
        self._queue: Queue[QueueTask | None] = Queue()
        self._lock = Lock()
        self._worker = Thread(target=self._worker_loop, name="open-somnia-chat-worker", daemon=True)
        self._active = False
        self._queued = 0
        self._status = ""
        self._status_changed_at = time.monotonic()
        self._ui_invalidator = None
        self._prompt_interrupter = None
        self._thinking_phrase = self.THINKING_PHRASES[0]
        self._next_query_id = 1
        self._queued_previews: list[tuple[int, str]] = []
        self._interrupt_requested = False
        self._authorization_requests: list[AuthorizationRequest] = []
        self._mode_switch_requests: list[ModeSwitchRequest] = []

    def start(self) -> None:
        self._worker.start()

    def stats(self) -> tuple[bool, int]:
        with self._lock:
            return self._active, self._queued

    def set_ui_invalidator(self, invalidator) -> None:
        self._ui_invalidator = invalidator

    def set_prompt_interrupter(self, interrupter) -> None:
        self._prompt_interrupter = interrupter

    def enqueue(self, query: str) -> tuple[bool, int]:
        return self._enqueue_task("turn", query)

    def enqueue_compact(self) -> tuple[bool, int]:
        return self._enqueue_task("compact", "/compact")

    def _enqueue_task(self, kind: str, payload: str) -> tuple[bool, int]:
        with self._lock:
            was_active = self._active
            queued_before = self._queued
            query_id = self._next_query_id
            self._next_query_id += 1
            self._queued += 1
            show_queue_preview = was_active or queued_before > 0
            preview = self._summarize_preview(kind, payload)
            if show_queue_preview:
                self._queued_previews.append((query_id, preview))
        self._queue.put(
            QueueTask(
                id=query_id,
                kind=kind,
                payload=payload,
                echo_on_start=show_queue_preview and kind == "turn",
                preview=preview,
            )
        )
        self._invalidate_ui()
        return was_active, queued_before

    def has_inflight_work(self) -> bool:
        active, queued = self.stats()
        return active or queued > 0

    def request_authorization(
        self,
        *,
        tool_name: str,
        reason: str,
        argument_summary: str = "",
        execution_mode: str = DEFAULT_EXECUTION_MODE,
    ) -> dict[str, str]:
        request = AuthorizationRequest(
            tool_name=tool_name,
            reason=reason,
            argument_summary=argument_summary,
            execution_mode=execution_mode,
            completed=Event(),
        )
        with self._lock:
            self._authorization_requests.append(request)
        self._invalidate_ui()
        if self._prompt_interrupter is not None:
            try:
                self._prompt_interrupter()
            except Exception:
                pass
        if not request.completed.wait(timeout=300):
            return {"status": "denied", "scope": "deny", "reason": "Authorization request timed out."}
        return request.response or {"status": "denied", "scope": "deny", "reason": "Authorization denied."}

    def drain_authorization_requests(self) -> list[AuthorizationRequest]:
        with self._lock:
            pending = list(self._authorization_requests)
            self._authorization_requests = []
        return pending

    def request_mode_switch(self, *, target_mode: str, reason: str = "", current_mode: str = DEFAULT_EXECUTION_MODE) -> dict[str, str]:
        request = ModeSwitchRequest(
            target_mode=target_mode,
            current_mode=current_mode,
            reason=reason,
            completed=Event(),
        )
        with self._lock:
            self._mode_switch_requests.append(request)
        self._invalidate_ui()
        if self._prompt_interrupter is not None:
            try:
                self._prompt_interrupter()
            except Exception:
                pass
        if not request.completed.wait(timeout=300):
            return {
                "approved": False,
                "active_mode": self._execution_mode,
                "reason": "Mode switch request timed out.",
            }
        return request.response or {"approved": False, "active_mode": self._execution_mode, "reason": "Mode switch denied."}

    def drain_mode_switch_requests(self) -> list[ModeSwitchRequest]:
        with self._lock:
            pending = list(self._mode_switch_requests)
            self._mode_switch_requests = []
        return pending

    def close(self, *, drain: bool) -> int:
        dropped = 0
        if not drain:
            dropped = self._clear_pending()
        self._queue.put(None)
        self._worker.join()
        return dropped

    def request_interrupt(self) -> bool:
        with self._lock:
            if not self._active or self._interrupt_requested:
                return False
            self._interrupt_requested = True
        interrupter = getattr(self.runtime, "interrupt_active_teammates", None)
        if callable(interrupter):
            try:
                interrupter(reason="lead_interrupt")
            except Exception:
                pass
        self._set_status("interrupting")
        return True

    def should_interrupt(self) -> bool:
        with self._lock:
            return self._interrupt_requested

    def _clear_pending(self) -> int:
        dropped = 0
        dropped_ids: set[int] = set()
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                break
            if item is None:
                self._queue.put(None)
                break
            dropped_ids.add(item.id)
            dropped += 1
            self._queue.task_done()
        if dropped:
            with self._lock:
                self._queued = max(0, self._queued - dropped)
                self._queued_previews = [
                    (preview_id, preview)
                    for preview_id, preview in self._queued_previews
                    if preview_id not in dropped_ids
                ]
            self._invalidate_ui()
        return dropped

    def _worker_loop(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                self._queue.task_done()
                return
            with self._lock:
                self._queued = max(0, self._queued - 1)
                self._active = True
                self._thinking_phrase = random.choice(self.THINKING_PHRASES)
                self._queued_previews = [
                    (preview_id, preview)
                    for preview_id, preview in self._queued_previews
                    if preview_id != task.id
                ]
            self._set_status("compacting" if task.kind == "compact" else "thinking")
            try:
                with self._lock:
                    self._interrupt_requested = False
                if task.kind == "compact":
                    self.runtime.compact_session(self.session)
                    print("[manual compact complete]")
                    print()
                else:
                    if task.echo_on_start:
                        print_user_message(task.payload)
                    streamer = ConsoleStreamer(
                        start_on_new_line=True,
                        line_buffered=self.stable_prompt,
                        on_first_output=None,
                    )
                    response = self.runtime.run_turn(
                        self.session,
                        task.payload,
                        text_callback=streamer,
                        should_interrupt=self.should_interrupt,
                    )
                    if streamer.has_output:
                        streamer.finish()
                        print()
                    elif response:
                        print()
                        print(
                            _prefix_first_line(
                                render_markdown_text(response, ansi=sys.stdout.isatty()),
                                _assistant_prefix(ansi=sys.stdout.isatty()),
                            )
                        )
                        print()
                    self.runtime.print_last_turn_file_summary(self.session)
            except TurnInterrupted:
                print()
                print("[interrupted]")
                print()
            except Exception as exc:
                print(f"[turn failed] {exc}")
                print()
            finally:
                with self._lock:
                    self._active = False
                    self._interrupt_requested = False
                self._set_status("done")
                self._queue.task_done()

    def _set_status(self, status: str) -> None:
        with self._lock:
            self._status = status
            self._status_changed_at = time.monotonic()
        self._invalidate_ui()

    def prompt_message(self):
        prompt_line = list(styled_prompt_message())
        mode_line = self._execution_mode_fragments()
        status_line = self._status_line()
        context_line = self.current_context_label()
        todo_lines = self._todo_lines()
        team_lines = self._team_lines()
        queue_lines = self._queue_preview_lines()
        fragments = []
        panel_prefix = ("fg:#64748b", "│ ")
        if self.stable_prompt and status_line:
            style = "fg:#9fb8ab" if status_line == self.DONE_TEXT else "fg:#eab308"
            fragments.extend([panel_prefix, (style, status_line), ("", "\n")])
        if self.stable_prompt:
            for style, line in todo_lines:
                fragments.extend([panel_prefix, (style, line), ("", "\n")])
            for style, line in team_lines:
                fragments.extend([panel_prefix, (style, line), ("", "\n")])
            if context_line:
                fragments.extend([panel_prefix, (self.current_context_style(), context_line), ("", "\n")])
            for index, queue_line in enumerate(queue_lines, start=1):
                fragments.extend([panel_prefix, ("fg:#94a3b8", f"queued {index}: {queue_line}"), ("", "\n")])
        fragments.append(panel_prefix)
        fragments.extend([*mode_line, ("", "\n")])
        fragments.extend([("fg:#64748b", PROMPT_BORDER), ("", "\n")])
        fragments.extend(prompt_line)
        return fragments

    def current_model_label(self) -> str:
        settings = getattr(self.runtime, "settings", None)
        provider = getattr(settings, "provider", None)
        if provider is None:
            return "model: unknown"
        provider_name = getattr(provider, "name", "unknown")
        model_name = getattr(provider, "model", "unknown")
        return f"model: {provider_name} / {model_name}"

    def _format_token_count(self, token_count: int) -> str:
        if token_count >= 1_000_000:
            return f"{token_count / 1_000_000:.2f}M"
        if token_count >= 1_000:
            return f"{token_count / 1_000:.1f}k"
        return str(token_count)

    def current_context_usage(self) -> ContextWindowUsage | None:
        usage_getter = getattr(self.runtime, "context_window_usage", None)
        if not callable(usage_getter):
            return None
        try:
            return usage_getter(self.session)
        except Exception:
            return None

    def current_context_label(self) -> str:
        usage = self.current_context_usage()
        if usage is None:
            return ""
        if usage.max_tokens:
            percent = usage.usage_percent or 0.0
            return (
                f"ctx: {percent:.1f}% "
                f"({self._format_token_count(usage.used_tokens)} / {self._format_token_count(usage.max_tokens)} tokens)"
            )
        return f"ctx: {self._format_token_count(usage.used_tokens)} tokens"

    def current_context_style(self) -> str:
        usage = self.current_context_usage()
        percent = usage.usage_percent if usage is not None else None
        if percent is None:
            return "fg:#7dd3fc"
        if percent <= 40.0:
            return self.CONTEXT_HEALTHY_STYLE
        if percent <= 60.0:
            return self.CONTEXT_WARNING_STYLE
        if percent <= 75.0:
            return self.CONTEXT_REDUCING_STYLE
        return self.CONTEXT_CRITICAL_STYLE

    def current_status_label(self) -> str:
        context_label = self.current_context_label()
        if not context_label:
            return self.current_model_label()
        return f"{self.current_model_label()} | {context_label}"

    def bottom_toolbar(self):
        context_label = self.current_context_label()
        if not context_label:
            return [("fg:#94a3b8", self.current_model_label())]
        return [
            ("fg:#94a3b8", self.current_model_label()),
            ("fg:#64748b", " | "),
            (self.current_context_style(), context_label),
        ]

    def current_execution_mode(self):
        return execution_mode_spec(self._execution_mode)

    def execution_mode_label(self) -> str:
        return execution_mode_status_text(self._execution_mode)

    def execution_mode_ansi_label(self) -> str:
        spec = self.current_execution_mode()
        return f"{spec.ansi_color}{self.execution_mode_label()}\x1b[0m"

    def cycle_execution_mode(self):
        self._execution_mode = next_execution_mode(self._execution_mode)
        setattr(self.runtime, "execution_mode", self._execution_mode)
        self._invalidate_ui()
        return self.current_execution_mode()

    def set_execution_mode(self, mode: str):
        self._execution_mode = normalize_execution_mode(mode)
        setattr(self.runtime, "execution_mode", self._execution_mode)
        self._invalidate_ui()
        return self.current_execution_mode()

    def _status_line(self) -> str:
        with self._lock:
            status = self._status
            changed_at = self._status_changed_at
            thinking_phrase = self._thinking_phrase
        if status == "thinking":
            dots = int((time.monotonic() - changed_at) / self.THINKING_FRAME_SECONDS) % 4
            return thinking_phrase + ("." * dots)
        if status == "compacting":
            dots = int((time.monotonic() - changed_at) / self.THINKING_FRAME_SECONDS) % 4
            return "compacting context" + ("." * dots)
        if status == "interrupting":
            return "interrupting"
        if status == "done":
            return self.DONE_TEXT
        return ""

    def _queue_preview_lines(self) -> list[str]:
        with self._lock:
            return [preview for _, preview in self._queued_previews]

    def _execution_mode_fragments(self):
        spec = self.current_execution_mode()
        return [
            (spec.color, spec.title),
            ("fg:#64748b", "  (Shift+Tab to cycle)"),
        ]

    def _todo_lines(self) -> list[tuple[str, str]]:
        todo_items = [
            item
            for item in list(getattr(self.session, "todo_items", []) or [])
            if str(item.get("status", "pending")).lower() in TODO_VISIBLE_STATUSES
        ]
        if not todo_items:
            return []
        if not any(str(item.get("status", "pending")).lower() not in TODO_CLOSED_STATUSES for item in todo_items):
            return []

        completed = sum(1 for item in todo_items if item.get("status") == "completed")
        lines: list[tuple[str, str]] = [("fg:#5eead4", f"todo ({completed}/{len(todo_items)} completed)")]
        styles = {
            "pending": "fg:#cbd5e1",
            "in_progress": "fg:#fbbf24",
            "completed": "fg:#64748b",
            "cancelled": "fg:#64748b",
        }
        for item in todo_items:
            status = str(item.get("status", "pending")).lower()
            marker = TODO_STATUS_MARKERS.get(status, "•")
            style = styles.get(status, "fg:#cbd5e1")
            text = str(item.get("content", "")).strip()
            if not text:
                continue
            if status == "in_progress":
                active_form = str(item.get("activeForm", "")).strip()
                suffix = f" <- {active_form}" if active_form else ""
            else:
                suffix = ""
            lines.append((style, f"{marker} {text}{suffix}"))
        return lines

    def _team_lines(self) -> list[tuple[str, str]]:
        manager = getattr(self.runtime, "team_manager", None)
        summaries = getattr(manager, "active_member_summaries", None)
        formatter = getattr(manager, "_format_member_summary", None)
        if not callable(summaries) or not callable(formatter):
            return []
        members = summaries()
        if not members:
            return []
        lines: list[tuple[str, str]] = [("fg:#c4b5fd", f"team ({len(members)} active)")]
        for member in members:
            status = str(member.get("status", "")).strip()
            if status == "working":
                style = "fg:#fbbf24"
            elif status == "idle":
                style = "fg:#93c5fd"
            else:
                style = "fg:#cbd5e1"
            lines.append((style, formatter(member)))
        return lines

    def _summarize_preview(self, kind: str, payload: str) -> str:
        if kind == "compact":
            return "/compact"
        single_line = " ".join(payload.split())
        if len(single_line) <= 48:
            return single_line
        return single_line[:45] + "..."

    def _invalidate_ui(self) -> None:
        if self._ui_invalidator is not None:
            try:
                self._ui_invalidator()
            except Exception:
                pass


def _is_read_only_command(command: str) -> bool:
    return any(command == prefix or command.startswith(f"{prefix} ") for prefix in READ_ONLY_COMMAND_PREFIXES)


def _is_exit_command(command: str) -> bool:
    stripped = command.strip()
    return stripped in {"q", "exit", "/exit"}


def _handle_scan_command(runtime, session, command: str) -> None:
    args = command.split()[1:]
    if args and args[0] == "--refresh":
        args = args[1:]
    target_path = " ".join(args).strip() or "."
    output = runtime.invoke_tool(
        session,
        "project_scan",
        {
            "path": target_path,
            "depth": 2,
            "limit": 8,
        },
    )
    print(output)


def _handle_symbols_command(runtime, session, command: str) -> None:
    query = command.split(maxsplit=1)[1].strip() if " " in command else ""
    if not query:
        query = (
            prompt_text_interactively(
                "Find Symbols",
                "Enter one or more symbol substrings. Use `|` to search alternatives in one pass, up to 10 terms.",
            )
            or ""
        ).strip()
    if not query:
        print("[symbol search cancelled]")
        return
    output = runtime.invoke_tool(
        session,
        "find_symbol",
        {
            "query": query,
            "path": ".",
            "limit": 50,
        },
    )
    matches = runtime.parse_symbol_output(output)
    if not matches:
        print(output)
        return
    items = [
        (
            str(index),
            f"{match['name']} | {match['kind']} | {match['path']}:{match['line']}",
        )
        for index, match in enumerate(matches, start=1)
    ]
    selection = choose_item_interactively(
        "Symbols",
        f"Found {len(matches)} match(es) for '{query}'. Choose one to preview the source location.",
        items,
    )
    if not selection:
        print(output)
        return
    match = matches[int(selection) - 1]
    print(runtime.render_symbol_preview(match["path"], int(match["line"])))

def _handle_model_command(runtime) -> None:
    profiles = runtime.configured_provider_profiles()
    if not profiles:
        print("[no configured providers]")
        return
    provider_items = [
        (
            name,
            f"{name} | default={profile.default_model} | models={len(profile.models)}",
        )
        for name, profile in sorted(profiles.items())
    ]
    selected_provider = choose_item_interactively("Choose Provider", "Select the provider to use for subsequent turns.", provider_items)
    if not selected_provider:
        print("[model selection cancelled]")
        return
    profile = profiles[selected_provider]
    model_items = [
        (
            model,
            f"{model}{' (default)' if model == profile.default_model else ''}",
        )
        for model in profile.models
    ]
    selected_model = choose_item_interactively(
        "Choose Model",
        f"Select a configured model under provider '{selected_provider}'.",
        model_items,
    )
    if not selected_model:
        print("[model selection cancelled]")
        return
    print(runtime.switch_provider_model(selected_provider, selected_model))


def _handle_providers_command(runtime) -> None:
    profiles = runtime.configured_provider_profiles()
    selected = choose_provider_target_interactively(profiles)
    if not selected:
        return
    previous_provider_name = None if selected == "__add__" else selected
    submission = collect_provider_profile_interactively(
        profiles,
        previous_provider_name=previous_provider_name,
    )
    if submission is None:
        return

    config_path = persist_provider_profile(
        submission.provider_name,
        submission.provider_type,
        submission.models,
        api_key=submission.api_key,
        base_url=submission.base_url,
        previous_provider_name=submission.previous_provider_name,
    )
    current_provider_name = runtime.settings.provider.name
    current_model = runtime.settings.provider.model
    if submission.previous_provider_name == current_provider_name:
        current_provider_name = submission.provider_name
        if current_model not in submission.models:
            current_model = submission.models[0]
    runtime.reload_provider_configuration(provider_name=current_provider_name, model=current_model)

    if submission.previous_provider_name and submission.previous_provider_name != submission.provider_name:
        print(f"Renamed provider '{submission.previous_provider_name}' to '{submission.provider_name}' in {config_path}.")
    elif submission.previous_provider_name:
        print(f"Updated provider '{submission.provider_name}' in {config_path}.")
    else:
        print(f"Added provider '{submission.provider_name}' in {config_path}.")


def _handle_mcp_command(runtime) -> None:
    registry = getattr(runtime, "mcp_registry", None)
    if registry is None or not callable(getattr(registry, "server_summaries", None)):
        print(runtime.mcp_status())
        return
    summaries = registry.server_summaries()
    if not summaries:
        print("No MCP servers configured.")
        return

    while True:
        items = []
        for summary in summaries:
            status = summary["status"]
            suffix = f"tools={summary['tool_count']}" if status == "connected" else (summary["error"] or status)
            items.append(
                (
                    summary["name"],
                    f"{summary['name']} | {status} | {summary['transport']} | {suffix}",
                )
            )
        selected_server = choose_item_interactively(
            "MCP Servers",
            "Choose an MCP server to inspect its registered tools.",
            items,
        )
        if not selected_server:
            return

        server_summary = next((item for item in summaries if item["name"] == selected_server), None)
        if server_summary is None:
            return
        while True:
            tool_summaries = registry.tool_summaries(selected_server)
            subtitle_lines = [
                f"Server: {selected_server}",
                f"Status: {server_summary['status']}",
                f"Transport: {server_summary['transport']}",
                f"Target: {server_summary['target']}",
            ]
            if server_summary["error"]:
                subtitle_lines.append(f"Error: {server_summary['error']}")
            subtitle_lines.append("Choose a tool to inspect, or go back.")
            tool_items = [("__back__", "Back to MCP servers")]
            tool_items.extend(
                (
                    tool["name"],
                    f"{tool['name']} | {tool['description'] or '(no description)'}",
                )
                for tool in tool_summaries
            )
            selected_tool = choose_item_interactively(
                "MCP Tools",
                "\n".join(subtitle_lines),
                tool_items,
            )
            if not selected_tool or selected_tool == "__back__":
                break

            tool_summary = next((item for item in tool_summaries if item["name"] == selected_tool), None)
            if tool_summary is None:
                continue
            choose_item_interactively(
                "MCP Tool Details",
                (
                    f"Server: {selected_server}\n"
                    f"Tool: {tool_summary['name']}\n"
                    f"Description: {tool_summary['description'] or '(no description)'}\n"
                    f"Input schema:\n{json.dumps(tool_summary['input_schema'], ensure_ascii=False, indent=2)}"
                ),
                [("__back__", "Back to tools list")],
            )


def _handle_undo_command(runtime, session) -> None:
    undo_stack = list(getattr(session, "undo_stack", []) or [])
    if not undo_stack:
        print("Nothing to undo.")
        return
    selection = choose_item_interactively(
        "Confirm Undo",
        "Undo the most recent file change set?",
        [
            ("cancel", "Cancel (default)"),
            ("confirm", "Confirm undo"),
        ],
    )
    if selection != "confirm":
        return
    print(runtime.undo_last_turn(session))


def _handle_skills_command(runtime) -> str | None:
    entries = list(runtime.skill_loader.list_entries())
    if not entries:
        print("No skills.")
        return None
    items = [
        (
            str(entry["name"]),
            f"{entry['name']} [{entry['scope']}] - {entry['description']}",
        )
        for entry in entries
    ]
    selected = choose_item_interactively(
        "Choose Skill",
        "Select a skill to apply to the next prompt.",
        items,
    )
    if not selected:
        return None
    return f"/+{selected} "


def _resolve_authorization_requests(runner: TurnQueueRunner) -> bool:
    pending = runner.drain_authorization_requests()
    if not pending:
        return False
    for request in pending:
        selection = choose_authorization_interactively(
            request.tool_name,
            request.reason,
            argument_summary=request.argument_summary,
            mode_label=execution_mode_spec(request.execution_mode).title,
        )
        if selection == "workspace":
            request.response = {"status": "approved", "scope": "workspace", "reason": "Allowed in this workspace."}
        elif selection == "once":
            request.response = {"status": "approved", "scope": "once", "reason": "Allowed once."}
        else:
            request.response = {"status": "denied", "scope": "deny", "reason": "Not allowed."}
        request.completed.set()
    return True


def _resolve_mode_switch_requests(runner: TurnQueueRunner) -> bool:
    pending = runner.drain_mode_switch_requests()
    if not pending:
        return False
    for request in pending:
        selection = choose_mode_switch_interactively(
            execution_mode_spec(request.target_mode).title,
            execution_mode_spec(request.current_mode).title,
            request.reason,
        )
        if selection == "switch":
            active_mode = runner.set_execution_mode(request.target_mode).key
            request.response = {
                "approved": True,
                "active_mode": active_mode,
                "reason": f"Switched to {execution_mode_spec(active_mode).title}.",
            }
        else:
            request.response = {
                "approved": False,
                "active_mode": runner.current_execution_mode().key,
                "reason": "Stayed in the current mode.",
            }
        request.completed.set()
    return True


def run_repl(runtime, session, resumed: bool = False) -> int:
    runner = TurnQueueRunner(runtime, session, stable_prompt=False)
    runtime.authorization_request_handler = runner.request_authorization
    runtime.mode_switch_request_handler = runner.request_mode_switch
    prompt_session = None
    pending_query_prefix = ""
    try:
        prompt_session = create_prompt_session(
            runtime.settings.workspace_root,
            on_interrupt=runner.request_interrupt,
            is_busy=runner.has_inflight_work,
            on_cycle_mode=runner.cycle_execution_mode,
            skill_names_getter=lambda: runtime.skill_loader.names(),
        )
    except Exception:
        prompt_session = None
    runner.stable_prompt = prompt_session is not None and patch_stdout is not None
    if prompt_session is not None:
        runner.set_ui_invalidator(lambda: prompt_session.app.invalidate() if prompt_session.app else None)
        runner.set_prompt_interrupter(
            lambda: prompt_session.app.exit(result=AUTHORIZATION_PROMPT_SENTINEL) if prompt_session.app else None
        )
    runner.start()
    print(f"[session {session.id}]")
    if resumed:
        _print_resumed_history(session, runtime)
    prompt_context = patch_stdout(raw=True) if prompt_session is not None and patch_stdout is not None else nullcontext()
    try:
        with prompt_context:
            while True:
                if _resolve_mode_switch_requests(runner):
                    continue
                if _resolve_authorization_requests(runner):
                    continue
                try:
                    if prompt_session is not None:
                        prompt_kwargs = {
                            "refresh_interval": 0.1,
                            "bottom_toolbar": runner.bottom_toolbar,
                        }
                        if pending_query_prefix:
                            prompt_kwargs["default"] = pending_query_prefix
                        query = prompt_session.prompt(
                            runner.prompt_message,
                            **prompt_kwargs,
                        )
                    else:
                        prefix = pending_query_prefix
                        if sys.stdout.isatty():
                            query = input(
                                f"{runner.execution_mode_ansi_label()}\n"
                                f"{runner.current_status_label()}\n"
                                f"{fallback_prompt_message()}{prefix}"
                            )
                        else:
                            query = input(
                                f"{runner.execution_mode_label()}\n"
                                f"{runner.current_status_label()}\n"
                                f"{PROMPT_TEXT}{prefix}"
                            )
                        if prefix:
                            query = prefix + query
                    pending_query_prefix = ""
                except (EOFError, KeyboardInterrupt):
                    print()
                    active, queued = runner.stats()
                    if active:
                        if runner.request_interrupt():
                            print("[interrupt requested]")
                        continue
                    if queued:
                        print(f"[waiting for {queued} queued item(s) before exit]")
                        runner.close(drain=True)
                        break
                    runner.close(drain=True)
                    break
                if query == AUTHORIZATION_PROMPT_SENTINEL:
                    _resolve_mode_switch_requests(runner)
                    _resolve_authorization_requests(runner)
                    continue
                stripped = query.strip()
                if not stripped:
                    continue
                if _is_exit_command(stripped):
                    active, queued = runner.stats()
                    if queued:
                        dropped = runner.close(drain=False)
                        if active:
                            print(f"[exiting after current response; dropped {dropped} queued prompt(s)]")
                        elif dropped:
                            print(f"[dropped {dropped} queued prompt(s)]")
                    else:
                        if active:
                            print("[waiting for current response before exit]")
                        runner.close(drain=True)
                    break
                if stripped == "/compact":
                    was_active, queued_before = runner.enqueue_compact()
                    if (was_active or queued_before) and not runner.stable_prompt:
                        ahead = queued_before + (1 if was_active else 0)
                        print(f"[queued compact; {ahead} item(s) ahead]")
                    continue
                if stripped == "/scan" or stripped.startswith("/scan "):
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /scan]")
                        continue
                    _handle_scan_command(runtime, session, stripped)
                    continue
                if stripped == "/symbols" or stripped.startswith("/symbols "):
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /symbols]")
                        continue
                    _handle_symbols_command(runtime, session, stripped)
                    continue
                if stripped == "/skills":
                    skill_prefix = _handle_skills_command(runtime)
                    if skill_prefix is not None:
                        pending_query_prefix = skill_prefix
                        continue
                if stripped == "/undo":
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /undo]")
                        continue
                    _handle_undo_command(runtime, session)
                    continue
                if stripped == "/model":
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /model]")
                        continue
                    _handle_model_command(runtime)
                    continue
                if stripped == "/providers":
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /providers]")
                        continue
                    _handle_providers_command(runtime)
                    continue
                if stripped == "/tasks":
                    tasks = runtime.task_store.list_all()
                    if not tasks:
                        print("No tasks.")
                    else:
                        for task in tasks:
                            print(json.dumps(task, ensure_ascii=False, indent=2))
                    continue
                if stripped == "/team":
                    print(runtime.team_manager.list_all())
                    continue
                if stripped == "/teamlog":
                    active = runtime.team_manager.active_member_summaries()
                    if not active:
                        print("No active teammates. Use /team to inspect the full roster.")
                    else:
                        print("Use /teamlog <name>. Active teammates: " + ", ".join(member["name"] for member in active))
                    continue
                if stripped.startswith("/teamlog "):
                    name = stripped.split(maxsplit=1)[1].strip()
                    print(runtime.render_team_log(name))
                    continue
                if stripped == "/inbox":
                    print(json.dumps(runtime.bus.read_inbox("lead"), indent=2, ensure_ascii=False))
                    continue
                if stripped == "/mcp":
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /mcp]")
                        continue
                    _handle_mcp_command(runtime)
                    continue
                if stripped == "/toollog":
                    print(runtime.recent_tool_logs())
                    continue
                if stripped.startswith("/toollog "):
                    log_id = stripped.split(maxsplit=1)[1].strip()
                    print(runtime.render_tool_log(log_id))
                    continue
                if stripped == "/bg":
                    print(runtime.background_manager.check())
                    continue
                if stripped == "/help":
                    print("\n".join(f"{command} - {description}" for command, description in COMMAND_SPECS))
                    continue
                skill_command = _parse_skill_command(query)
                expanded_query = _expand_skill_command(runtime, query)
                if expanded_query.startswith("Error: Unknown skill '"):
                    print(expanded_query)
                    continue
                if stripped.startswith("/") and skill_command is None and not _is_read_only_command(stripped):
                    print(f"[unknown command] {stripped}")
                    continue
                was_active, queued_before = runner.enqueue(expanded_query)
                if runner.stable_prompt and not was_active and queued_before == 0:
                    print_user_message(query)
                if not runner.stable_prompt and not was_active and queued_before == 0:
                    print_user_message(query)
                if (was_active or queued_before) and not runner.stable_prompt:
                    ahead = queued_before + (1 if was_active else 0)
                    print(f"[queued; {ahead} item(s) ahead]")
    finally:
        runtime.authorization_request_handler = None
        runtime.mode_switch_request_handler = None
    return 0
