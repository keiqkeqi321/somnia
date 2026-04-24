from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

from open_somnia.app_service import AppService
from open_somnia.app_service.events import (
    ASSISTANT_DELTA,
    AUTHORIZATION_REQUESTED,
    MODE_SWITCH_REQUESTED,
    TOOL_FINISHED,
)
from open_somnia.app_service.models import TurnRunResult
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.messages import MarkdownStreamRenderer, render_markdown_text, render_message_content, render_text_content
from open_somnia.cli.prompting import choose_session_interactively, format_session_timestamp


ASSISTANT_BULLET = "\u25cf"
USER_BULLET = "\u276f"


def _supports_output_text(text: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return False
    except LookupError:
        return True
    return True


def _prefix_first_line(text: str, prefix: str) -> str:
    if not text:
        return prefix.rstrip()
    lines = text.splitlines()
    if not lines:
        return prefix.rstrip()
    lines[0] = f"{prefix}{lines[0]}"
    return "\n".join(lines)


def _assistant_prefix(*, ansi: bool) -> str:
    if ansi:
        return "\x1b[37m\u25cf\x1b[0m "
    bullet = ASSISTANT_BULLET if _supports_output_text(ASSISTANT_BULLET) else "*"
    return f"{bullet} "


def _user_prefix(*, ansi: bool) -> str:
    if ansi:
        return "\x1b[38;5;45m\u276f\x1b[0m "
    bullet = USER_BULLET if _supports_output_text(USER_BULLET) else ">"
    return f"{bullet} "


def print_user_message(text: str, *, ansi: bool | None = None) -> None:
    ansi_enabled = sys.stdout.isatty() if ansi is None else ansi
    lines = text.splitlines() or [""]
    first = f"{_user_prefix(ansi=ansi_enabled)}{lines[0]}"
    remainder = [f"  {line}" if line else "  " for line in lines[1:]]
    print()
    print(first)
    for line in remainder:
        print(line)
    print()


class ConsoleStreamer:
    def __init__(
        self,
        start_on_new_line: bool = False,
        line_buffered: bool = False,
        on_first_output=None,
    ) -> None:
        self.has_output = False
        self.start_on_new_line = start_on_new_line
        self.line_buffered = line_buffered
        self.on_first_output = on_first_output
        self._renderer: MarkdownStreamRenderer | None = None
        self._started_printing = False

    def __call__(self, text: str) -> None:
        if not text:
            return
        if self._renderer is None:
            self._renderer = MarkdownStreamRenderer(ansi=sys.stdout.isatty())
        if not self.has_output and self.on_first_output is not None:
            self.on_first_output()
        self._print_rendered(self._renderer.feed(text))
        self.has_output = True

    def finish(self) -> None:
        if not self.has_output:
            return
        if self._renderer is None:
            return
        self._print_rendered(self._renderer.finish())

    def _print_rendered(self, rendered: str) -> None:
        if not rendered:
            return
        if self.start_on_new_line and not self._started_printing:
            print()
        if not self._started_printing:
            rendered = _prefix_first_line(rendered, _assistant_prefix(ansi=sys.stdout.isatty()))
        print(rendered, end="" if rendered.endswith("\n") else "\n", flush=True)
        self._started_printing = True


@dataclass(slots=True)
class SessionChoice:
    session_id: str
    label: str


def _has_visible_exchange(session) -> bool:
    has_user = False
    has_assistant = False
    for message in session.messages:
        role = message.get("role")
        content = message.get("content")
        if role == "user" and isinstance(content, str):
            if content.startswith("<background-results>") or content.startswith("<inbox>"):
                continue
            if content.strip():
                has_user = True
        elif role == "assistant":
            text = render_text_content(content).strip()
            if text:
                has_assistant = True
        if has_user and has_assistant:
            return True
    return False


def _session_preview(session) -> str:
    for message in reversed(session.messages):
        role = message.get("role")
        content = message.get("content")
        if role == "assistant":
            text = render_message_content(content, ansi=False).strip()
        elif role == "user" and isinstance(content, str):
            if content.startswith("<background-results>") or content.startswith("<inbox>"):
                continue
            text = content.strip()
        else:
            continue
        if text:
            return " ".join(text.split())[:80]
    return "[no visible messages]"


def _build_session_choices(runtime) -> list[SessionChoice]:
    choices: list[SessionChoice] = []
    for session in runtime.list_sessions():
        if not _has_visible_exchange(session):
            continue
        stamp = format_session_timestamp(session.updated_at or session.created_at)
        preview = _session_preview(session)
        label = f"{session.id} | {stamp} | {preview}"
        choices.append(SessionChoice(session_id=session.id, label=label))
    return choices


def _select_session(runtime):
    choices = _build_session_choices(runtime)
    if not choices:
        print("No saved sessions. Starting a new chat.")
        return runtime.create_session(), False

    selected_id = choose_session_interactively([(item.session_id, item.label) for item in choices])
    if not selected_id:
        print("Session selection cancelled. Starting a new chat.")
        return runtime.create_session(), False
    return runtime.load_session(selected_id), True


def _select_latest_session(runtime):
    choices = _build_session_choices(runtime)
    if not choices:
        print("No saved sessions. Starting a new chat.")
        return runtime.create_session(), False
    return runtime.load_session(choices[0].session_id), True


def _build_app_service(runtime) -> AppService | None:
    if isinstance(runtime, OpenAgentRuntime):
        return AppService(runtime)
    return None


def _print_service_tool_event(payload: dict[str, Any]) -> None:
    tool_name = str(payload.get("tool_name", "")).strip()
    actor = str(payload.get("actor", "")).strip() or "lead"
    if tool_name == "TodoWrite" or actor != "lead" or not sys.stdout.isatty():
        return
    rendered_lines = payload.get("rendered_lines")
    if not isinstance(rendered_lines, list) or not rendered_lines:
        return
    print()
    for line in rendered_lines:
        print(str(line))
    print()


def _run_service_turn_to_console(runtime: OpenAgentRuntime, service: AppService, session, prompt: str) -> TurnRunResult:
    streamer = ConsoleStreamer()
    handle = service.run_turn(session, prompt)

    while True:
        batch = handle.drain_events(block=not handle.is_done(), timeout=0.05)
        if batch:
            for event in batch:
                payload = getattr(event, "payload", {}) or {}
                if event.type == ASSISTANT_DELTA:
                    streamer(str(payload.get("delta", "")))
                elif event.type == TOOL_FINISHED:
                    _print_service_tool_event(payload)
                elif event.type == AUTHORIZATION_REQUESTED:
                    request_id = str(payload.get("request_id", "")).strip()
                    if request_id:
                        service.resolve_authorization(
                            request_id,
                            scope="deny",
                            approved=False,
                            reason="Interactive approvals are unavailable in this session.",
                        )
                elif event.type == MODE_SWITCH_REQUESTED:
                    request_id = str(payload.get("request_id", "")).strip()
                    if request_id:
                        service.resolve_mode_switch(
                            request_id,
                            approved=False,
                            active_mode=getattr(runtime, "execution_mode", None),
                            reason="Interactive mode switching is unavailable in this session.",
                        )
            continue
        if handle.is_done():
            trailing = handle.drain_events()
            if trailing:
                batch = trailing
                for event in batch:
                    payload = getattr(event, "payload", {}) or {}
                    if event.type == ASSISTANT_DELTA:
                        streamer(str(payload.get("delta", "")))
                    elif event.type == TOOL_FINISHED:
                        _print_service_tool_event(payload)
                continue
            break

    result = handle.result or TurnRunResult(session=session, text="", status="failed", error="Turn failed.")
    result_status = str(getattr(result, "status", "")).strip()
    if result_status == "failed":
        raise RuntimeError(str(getattr(result, "error", "")).strip() or "Turn failed.")
    if streamer.has_output:
        streamer.finish()
        if result_status in {"stopped_with_open_todos", "stopped_after_max_rounds"} and result.text:
            print()
            print(_prefix_first_line(render_markdown_text(result.text, ansi=sys.stdout.isatty()), _assistant_prefix(ansi=sys.stdout.isatty())))
    elif result.text:
        print(_prefix_first_line(render_markdown_text(result.text, ansi=sys.stdout.isatty()), _assistant_prefix(ansi=sys.stdout.isatty())))
    return result


def cmd_chat(runtime: OpenAgentRuntime, resume: bool = False, continue_session: bool = False) -> int:
    from open_somnia.cli.repl import run_repl

    service = _build_app_service(runtime)
    session_api = service or runtime
    if resume:
        session, resumed = _select_session(session_api)
    elif continue_session:
        session, resumed = _select_latest_session(session_api)
    else:
        session, resumed = session_api.create_session(), False
    return run_repl(runtime, session, resumed=resumed, service=service)


def cmd_run(runtime: OpenAgentRuntime, prompt: str) -> int:
    service = _build_app_service(runtime)
    if service is not None:
        session = service.create_session()
        _run_service_turn_to_console(runtime, service, session, prompt)
        return 0

    session = runtime.create_session()
    streamer = ConsoleStreamer()
    result = runtime.run_turn(session, prompt, text_callback=streamer)
    result_status = str(getattr(result, "status", "")).strip()
    if streamer.has_output:
        streamer.finish()
        if result_status in {"stopped_with_open_todos", "stopped_after_max_rounds"} and result:
            print()
            print(_prefix_first_line(render_markdown_text(result, ansi=sys.stdout.isatty()), _assistant_prefix(ansi=sys.stdout.isatty())))
    elif result:
        print(_prefix_first_line(render_markdown_text(result, ansi=sys.stdout.isatty()), _assistant_prefix(ansi=sys.stdout.isatty())))
    return 0


def cmd_tasks_list(runtime: OpenAgentRuntime) -> int:
    tasks = runtime.task_store.list_all()
    if not tasks:
        print("No tasks.")
    else:
        for task in tasks:
            print(json.dumps(task, ensure_ascii=False, indent=2))
    return 0


def cmd_tasks_get(runtime: OpenAgentRuntime, task_id: int) -> int:
    print(json.dumps(runtime.task_store.get(task_id), ensure_ascii=False, indent=2))
    return 0


def cmd_compact(runtime: OpenAgentRuntime) -> int:
    session = runtime.latest_session()
    runtime.compact_session(session)
    print(f"Compacted session {session.id}")
    return 0


def cmd_doctor(runtime: OpenAgentRuntime) -> int:
    print(runtime.doctor())
    return 0
