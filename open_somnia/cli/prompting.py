from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import time
from typing import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completion, Completer
from prompt_toolkit.filters import has_focus
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.shortcuts.dialogs import input_dialog
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Button, Dialog, Label, RadioList, TextArea

COMMAND_SPECS = [
    ("/model", "Choose the active provider and model"),
    ("/providers", "Add or edit shared provider profiles"),
    ("/undo", "Undo the most recent file change set"),
    ("/compact", "Compact the current session context"),
    ("/skills", "Choose a skill to apply to the next prompt"),
    ("/tasks", "Show persistent tasks"),
    ("/team", "Show teammate roster and states"),
    ("/teamlog", "Show the full message and tool history for a teammate"),
    ("/inbox", "Read the lead inbox"),
    ("/mcp", "Show configured MCP servers and tools"),
    ("/toollog", "Show recent tool logs or expand one by id"),
    ("/bg", "Show background jobs"),
    ("/help", "Show available REPL commands"),
    ("/exit", "Exit chat mode"),
]

IGNORED_DIR_NAMES = {
    ".git",
    ".open_somnia",
    "__pycache__",
    ".venv",
    "node_modules",
}

TOKEN_PATTERN = re.compile(r"(?:^|\s)([@/])([^\s]*)$")
PROMPT_BORDER = "\u2500" * 58
PROMPT_TEXT = "\u276f "
PROMPT_ANSI = "\u276f "
PROMPT_FORMATTED = FormattedText(
    [
        ("#38bdf8 bold", "\u276f "),
    ]
)

SESSION_PICKER_STYLE = Style.from_dict(
    {
        "dialog": "bg:#0b1220",
        "dialog.body": "bg:#111827 fg:#d1d5db",
        "frame.border": "fg:#334155 bg:#111827",
        "frame.label": "fg:#67e8f9 bg:#111827 bold",
        "button": "bg:#1f2937 fg:#cbd5e1",
        "button.arrow": "bg:#1f2937 fg:#64748b",
        "button.focused": "bg:#0f766e fg:#ecfeff bold",
        "button.focused.arrow": "bg:#0f766e fg:#99f6e4 bold",
        "button.text": "",
        "text-area": "bg:#0f172a fg:#e5e7eb",
        "text-area.cursor": "bg:#f8fafc fg:#0f172a",
        "text-area.selection": "bg:#1d4ed8 fg:#eff6ff",
        "text-area.focused": "bg:#111827 fg:#f9fafb",
        "radio-list": "bg:#0f172a fg:#cbd5e1",
        "radio": "bg:#0f172a fg:#94a3b8",
        "radio-selected": "bg:#1e293b fg:#e2e8f0",
        "radio-checked": "fg:#5eead4 bold",
        "radio-number": "fg:#64748b",
        "label": "fg:#cbd5e1",
        "session-picker.help": "fg:#7dd3fc bg:#111827",
        "session-picker.subtitle": "fg:#94a3b8 bg:#111827",
        "shadow": "bg:#020617",
    }
)


@dataclass(slots=True)
class PathCandidate:
    relative_path: str
    basename: str
    kind: str


class OpenAgentCompleter(Completer):
    def __init__(self, workspace_root: Path, skill_names_getter: Callable[[], list[str]] | None = None):
        self.workspace_root = workspace_root
        self.skill_names_getter = skill_names_getter
        self._path_candidates: list[PathCandidate] = []
        self._last_scan_at = 0.0

    def get_completions(self, document, complete_event):
        token = self._current_token(document.text_before_cursor)
        if token is None:
            return
        symbol, query = token
        if symbol == "/":
            yield from self._command_completions(query)
            return
        if symbol == "@":
            yield from self._file_completions(query)

    def _current_token(self, text_before_cursor: str) -> tuple[str, str] | None:
        match = TOKEN_PATTERN.search(text_before_cursor)
        if not match:
            return None
        symbol, query = match.groups()
        if symbol == "/" and not text_before_cursor.lstrip().startswith("/"):
            return None
        return symbol, query

    def _command_completions(self, query: str):
        if query.startswith("+"):
            yield from self._skill_command_completions(query)
            return
        lowered = query.lower()
        for command, description in COMMAND_SPECS:
            command_name = command[1:]
            haystack = f"{command_name} {description}".lower()
            if lowered and lowered not in haystack:
                continue
            yield Completion(
                text=command_name,
                start_position=-len(query),
                display=command,
                display_meta=description,
            )

    def _skill_command_completions(self, query: str):
        getter = self.skill_names_getter
        if getter is None:
            return
        lowered = query[1:].lower()
        for skill_name in getter():
            haystack = skill_name.lower()
            if lowered and lowered not in haystack:
                continue
            yield Completion(
                text=f"+{skill_name}",
                start_position=-len(query),
                display=f"/+{skill_name}",
                display_meta="skill",
            )

    def _file_completions(self, query: str):
        for candidate in self._matching_paths(query):
            insertion = candidate.relative_path
            if candidate.kind == "dir" and not insertion.endswith("/"):
                insertion += "/"
            yield Completion(
                text=insertion,
                start_position=-len(query),
                display=candidate.relative_path + ("/" if candidate.kind == "dir" and not candidate.relative_path.endswith("/") else ""),
                display_meta="folder" if candidate.kind == "dir" else "file",
            )

    def _matching_paths(self, query: str) -> list[PathCandidate]:
        self._refresh_paths()
        lowered = query.lower()
        if not lowered:
            return self._path_candidates[:30]

        def score(item: PathCandidate) -> tuple[int, int, int, str]:
            basename = item.basename.lower()
            path = item.relative_path.lower()
            basename_starts = 0 if basename.startswith(lowered) else 1
            basename_contains = 0 if lowered in basename else 1
            kind_rank = 0 if item.kind == "dir" else 1
            return (basename_starts, basename_contains, kind_rank, item.relative_path)

        matches = [
            candidate
            for candidate in self._path_candidates
            if lowered in candidate.relative_path.lower() or lowered in candidate.basename.lower()
        ]
        return sorted(matches, key=score)[:30]

    def _refresh_paths(self) -> None:
        now = time.time()
        if self._path_candidates and now - self._last_scan_at < 5:
            return
        candidates: list[PathCandidate] = []
        for path in self.workspace_root.rglob("*"):
            relative_parts = path.relative_to(self.workspace_root).parts
            if any(part in IGNORED_DIR_NAMES for part in relative_parts):
                continue
            if path.is_dir():
                kind = "dir"
            elif path.is_file():
                kind = "file"
            else:
                continue
            relative = path.relative_to(self.workspace_root).as_posix()
            candidates.append(PathCandidate(relative_path=relative, basename=path.name, kind=kind))
        self._path_candidates = sorted(
            candidates,
            key=lambda item: (0 if item.kind == "dir" else 1, len(item.relative_path), item.relative_path),
        )
        self._last_scan_at = now


def _history_file(workspace_root: Path) -> Path:
    history_dir = workspace_root / ".open_somnia"
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir / "repl_history.txt"


def _apply_current_completion(buffer) -> bool:
    complete_state = getattr(buffer, "complete_state", None)
    if not complete_state:
        return False
    completion = complete_state.current_completion
    if completion is None and complete_state.completions:
        completion = complete_state.completions[0]
    if completion is None:
        return False
    buffer.apply_completion(completion)
    return True


def _accept_inline_suggestion(buffer) -> bool:
    suggestion = getattr(buffer, "suggestion", None)
    text = getattr(suggestion, "text", "") if suggestion is not None else ""
    if not text:
        return False
    buffer.insert_text(text)
    return True


def _handle_tab_action(buffer) -> bool:
    if _accept_inline_suggestion(buffer):
        return True
    if _apply_current_completion(buffer):
        return True
    start_completion = getattr(buffer, "start_completion", None)
    if start_completion is None:
        return False
    start_completion(select_first=True)
    return _apply_current_completion(buffer)


def create_prompt_session(
    workspace_root: Path,
    *,
    on_interrupt=None,
    is_busy=None,
    on_cycle_mode=None,
    skill_names_getter: Callable[[], list[str]] | None = None,
) -> PromptSession[str]:
    bindings = KeyBindings()

    @bindings.add("enter")
    def _handle_enter(event) -> None:
        buffer = event.current_buffer
        if buffer.complete_state:
            completion = buffer.complete_state.current_completion
            if completion is None and buffer.complete_state.completions:
                completion = buffer.complete_state.completions[0]
            if completion is not None:
                buffer.apply_completion(completion)
                return
        buffer.validate_and_handle()

    @bindings.add("escape")
    def _handle_escape(event) -> None:
        buffer = event.current_buffer
        if buffer.complete_state:
            buffer.cancel_completion()
            return
        if on_interrupt is not None and callable(is_busy) and is_busy() and not buffer.text:
            on_interrupt()
            return

    @bindings.add("up")
    def _handle_up(event) -> None:
        buffer = event.current_buffer
        if buffer.complete_state:
            buffer.complete_previous()
            return
        buffer.auto_up()

    @bindings.add("down")
    def _handle_down(event) -> None:
        buffer = event.current_buffer
        if buffer.complete_state:
            buffer.complete_next()
            return
        buffer.auto_down()

    @bindings.add("tab")
    def _handle_tab(event) -> None:
        buffer = event.current_buffer
        if _handle_tab_action(buffer):
            return
        buffer.insert_text("    ")

    @bindings.add("s-tab")
    def _handle_shift_tab(event) -> None:
        if on_cycle_mode is not None:
            on_cycle_mode()
            return

    @bindings.add("c-c", eager=True)
    def _handle_ctrl_c(event) -> None:
        buffer = event.current_buffer
        if buffer.text:
            buffer.reset()
            return
        event.app.exit(exception=KeyboardInterrupt())

    return PromptSession(
        history=FileHistory(str(_history_file(workspace_root))),
        auto_suggest=AutoSuggestFromHistory(),
        completer=OpenAgentCompleter(workspace_root, skill_names_getter=skill_names_getter),
        complete_while_typing=True,
        reserve_space_for_menu=8,
        complete_style=CompleteStyle.MULTI_COLUMN,
        key_bindings=bindings,
        erase_when_done=True,
    )


def styled_prompt_message():
    return PROMPT_FORMATTED


def fallback_prompt_message() -> str:
    return PROMPT_ANSI


def choose_item_interactively(title: str, subtitle: str, items: list[tuple[str, str]]) -> str | None:
    if not items:
        return None

    try:
        radio_list = RadioList(
            values=items,
            default=items[0][0],
            select_on_focus=True,
            show_scrollbar=True,
            show_numbers=True,
            open_character="[",
            close_character="]",
            select_character="x",
            container_style="class:radio-list",
            default_style="class:radio",
            selected_style="class:radio-selected",
            checked_style="class:radio-checked",
            number_style="class:radio-number",
        )

        def ok_handler() -> None:
            get_app().exit(result=radio_list.current_value)

        def cancel_handler() -> None:
            get_app().exit(result=None)

        dialog = Dialog(
            title=title,
            body=HSplit(
                [
                    Label(
                        text=subtitle,
                        style="class:session-picker.subtitle",
                        dont_extend_height=True,
                    ),
                    radio_list,
                    Label(
                        text="Move: Up/Down or j/k | Scroll: PgUp/PgDn | Switch focus: Tab | Buttons: OK (Enter), Cancel (Esc)",
                        style="class:session-picker.help",
                        dont_extend_height=True,
                    ),
                ],
                padding=1,
            ),
            buttons=[
                Button(text="OK (Enter)", handler=ok_handler, width=16),
                Button(text="Cancel (Esc)", handler=cancel_handler, width=18),
            ],
            with_background=True,
        )

        bindings = KeyBindings()
        bindings.add("tab")(focus_next)
        bindings.add("s-tab")(focus_previous)

        @bindings.add("enter", eager=True)
        def _confirm(event) -> None:
            ok_handler()

        @bindings.add("escape", eager=True)
        def _cancel(event) -> None:
            cancel_handler()

        app: Application[str | None] = Application(
            layout=Layout(dialog),
            key_bindings=merge_key_bindings([load_key_bindings(), bindings]),
            mouse_support=True,
            full_screen=True,
            style=SESSION_PICKER_STYLE,
            erase_when_done=True,
        )
        return app.run()
    except Exception:
        print(title)
        print(subtitle)
        print("Move: enter the number shown below. Leave blank to cancel.")
        for index, (_, label) in enumerate(items, start=1):
            print(f"{index}. {label}")
        while True:
            choice = input("Selection (blank to cancel): ").strip()
            if not choice:
                return None
            if choice.isdigit():
                selected = int(choice)
                if 1 <= selected <= len(items):
                    return items[selected - 1][0]
            print("Invalid selection.")


def choose_session_interactively(items: list[tuple[str, str]]) -> str | None:
    return choose_item_interactively("Resume Session", "Choose a previous session to resume.", items)


def prompt_text_interactively(
    title: str,
    subtitle: str,
    *,
    default: str = "",
    password: bool = False,
) -> str | None:
    try:
        app = input_dialog(
            title=title,
            text=subtitle,
            ok_text="OK (Enter)",
            cancel_text="Cancel (Esc)",
            style=SESSION_PICKER_STYLE,
            default=default,
            password=password,
        )
        app.erase_when_done = True
        return app.run()
    except Exception:
        print(title)
        print(subtitle)
        value = input("Input (blank to cancel): ")
        if not value.strip():
            return None
        return value


def prompt_provider_details_interactively(
    *,
    provider_type: str,
    default_provider_name: str,
    default_base_url: str,
    default_models: str = "",
    api_key_hint: str = "",
) -> dict[str, str] | None:
    title = "Provider Details"
    field_style = "bg:#1f2937 fg:#f9fafb"
    subtitle_lines = [
        f"Compatibility mode: {provider_type}\n"
        "Models must be comma-separated.\n"
        "Example: gpt-5, gpt-4.1-mini"
    ]
    if api_key_hint:
        subtitle_lines.append(api_key_hint)
    subtitle = "\n".join(subtitle_lines)
    try:
        provider_name_field = TextArea(text=default_provider_name, multiline=False, style=field_style)
        base_url_field = TextArea(text=default_base_url, multiline=False, style=field_style)
        api_key_field = TextArea(text="", multiline=False, password=True, style=field_style)
        models_field = TextArea(text=default_models, multiline=False, style=field_style)

        def ok_handler() -> None:
            get_app().exit(
                result={
                    "provider_name": provider_name_field.text.strip(),
                    "base_url": base_url_field.text.strip(),
                    "api_key": api_key_field.text.strip(),
                    "models": models_field.text.strip(),
                }
            )

        def cancel_handler() -> None:
            get_app().exit(result=None)

        dialog = Dialog(
            title=title,
            body=HSplit(
                [
                    Label(text=subtitle, style="class:session-picker.subtitle", dont_extend_height=True),
                    Label(text="Provider Name", dont_extend_height=True),
                    provider_name_field,
                    Label(text="Base URL", dont_extend_height=True),
                    base_url_field,
                    Label(text="API Key", dont_extend_height=True),
                    api_key_field,
                    Label(text="Models (comma-separated)", dont_extend_height=True),
                    models_field,
                    Label(
                        text="Switch focus: Tab/Shift+Tab or Up/Down | Enter: next field, then trigger focused button | Cancel: Esc",
                        style="class:session-picker.help",
                        dont_extend_height=True,
                    ),
                ],
                padding=1,
            ),
            buttons=[
                Button(text="Save", handler=ok_handler, width=12),
                Button(text="Cancel (Esc)", handler=cancel_handler, width=18),
            ],
            with_background=True,
        )

        bindings = KeyBindings()
        bindings.add("tab")(focus_next)
        bindings.add("s-tab")(focus_previous)
        bindings.add("down")(focus_next)
        bindings.add("up")(focus_previous)

        @bindings.add(
            "enter",
            filter=has_focus(provider_name_field.buffer)
            | has_focus(base_url_field.buffer)
            | has_focus(api_key_field.buffer)
            | has_focus(models_field.buffer),
            eager=True,
        )
        def _move_focus_forward_on_enter(event) -> None:
            focus_next(event)

        @bindings.add("escape", eager=True)
        def _cancel(event) -> None:
            cancel_handler()

        app: Application[dict[str, str] | None] = Application(
            layout=Layout(dialog),
            key_bindings=merge_key_bindings([load_key_bindings(), bindings]),
            mouse_support=True,
            full_screen=True,
            style=SESSION_PICKER_STYLE,
            erase_when_done=True,
        )
        return app.run()
    except Exception:
        print(title)
        print(subtitle)
        provider_name_prompt = f"Provider Name [{default_provider_name}]: " if default_provider_name else "Provider Name (blank to cancel): "
        provider_name = input(provider_name_prompt).strip() or default_provider_name.strip()
        if not provider_name:
            return None
        base_url_prompt = f"Base URL [{default_base_url}]: " if default_base_url else "Base URL (blank to cancel): "
        base_url = input(base_url_prompt).strip() or default_base_url.strip()
        if not base_url:
            return None
        api_key = input("API Key (blank to keep existing / cancel if none): ").strip()
        models_prompt = "Models, comma-separated"
        if default_models:
            models_prompt += f" [{default_models}]"
        models = input(f"{models_prompt}: ").strip() or default_models.strip()
        if not models:
            return None
        return {
            "provider_name": provider_name,
            "base_url": base_url,
            "api_key": api_key,
            "models": models,
        }


def choose_authorization_interactively(
    tool_name: str,
    reason: str,
    *,
    argument_summary: str = "",
    mode_label: str = "",
) -> str | None:
    details = [
        "A tool needs your approval before the agent can continue.",
        f"Tool: {tool_name}",
    ]
    if mode_label:
        details.append(f"Mode: {mode_label}")
    if reason:
        details.append(f"Reason: {reason}")
    if argument_summary:
        details.append(f"Request: {argument_summary}")
    details.append("Choose how broadly to allow it.")
    return choose_item_interactively(
        "Authorize Tool",
        "\n".join(details),
        [
            ("once", "Allow once"),
            ("workspace", "Allow in this workspace"),
            ("deny", "Do not allow"),
        ],
    )


def choose_mode_switch_interactively(target_mode_label: str, current_mode_label: str, reason: str = "") -> str | None:
    details = [
        "The agent wants to switch execution mode before continuing.",
        f"Current mode: {current_mode_label}",
        f"Requested mode: {target_mode_label}",
    ]
    if reason:
        details.append(f"Reason: {reason}")
    return choose_item_interactively(
        "Switch Execution Mode",
        "\n".join(details),
        [
            ("switch", f"Switch to {target_mode_label}"),
            ("stay", f"Stay in {current_mode_label}"),
        ],
    )


def format_session_timestamp(timestamp: float | None) -> str:
    if not timestamp:
        return "unknown time"
    try:
        return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "unknown time"
