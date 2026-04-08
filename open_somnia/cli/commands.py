from __future__ import annotations

import json
import sys
from dataclasses import dataclass

from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.messages import MarkdownStreamRenderer, render_markdown_text, render_message_content, render_text_content
from open_somnia.cli.prompting import choose_session_interactively, format_session_timestamp


ASSISTANT_BULLET = "\u25cf"
USER_BULLET = "\u276f"


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
    return f"{ASSISTANT_BULLET} "


def _user_prefix(*, ansi: bool) -> str:
    if ansi:
        return "\x1b[38;5;45m\u276f\x1b[0m "
    return f"{USER_BULLET} "


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


def _build_session_choices(runtime: OpenAgentRuntime) -> list[SessionChoice]:
    choices: list[SessionChoice] = []
    for session in runtime.list_sessions():
        if not _has_visible_exchange(session):
            continue
        stamp = format_session_timestamp(session.updated_at or session.created_at)
        preview = _session_preview(session)
        label = f"{session.id} | {stamp} | {preview}"
        choices.append(SessionChoice(session_id=session.id, label=label))
    return choices


def _select_session(runtime: OpenAgentRuntime):
    choices = _build_session_choices(runtime)
    if not choices:
        print("No saved sessions. Starting a new chat.")
        return runtime.create_session(), False

    selected_id = choose_session_interactively([(item.session_id, item.label) for item in choices])
    if not selected_id:
        print("Session selection cancelled. Starting a new chat.")
        return runtime.create_session(), False
    return runtime.load_session(selected_id), True


def _select_latest_session(runtime: OpenAgentRuntime):
    choices = _build_session_choices(runtime)
    if not choices:
        print("No saved sessions. Starting a new chat.")
        return runtime.create_session(), False
    return runtime.load_session(choices[0].session_id), True


def cmd_chat(runtime: OpenAgentRuntime, resume: bool = False, continue_session: bool = False) -> int:
    from open_somnia.cli.repl import run_repl

    if resume:
        session, resumed = _select_session(runtime)
    elif continue_session:
        session, resumed = _select_latest_session(runtime)
    else:
        session, resumed = runtime.create_session(), False
    return run_repl(runtime, session, resumed=resumed)


def cmd_run(runtime: OpenAgentRuntime, prompt: str) -> int:
    session = runtime.create_session()
    streamer = ConsoleStreamer()
    result = runtime.run_turn(session, prompt, text_callback=streamer)
    if streamer.has_output:
        streamer.finish()
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
