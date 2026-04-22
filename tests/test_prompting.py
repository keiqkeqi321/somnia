from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prompt_toolkit.document import Document

from open_somnia.cli.prompting import (
    OpenAgentCompleter,
    _WindowsSafeCursorOutput,
    _handle_tab_action,
    create_prompt_session,
)


class _FakeVtOutput:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.vt100_output = object()
        self.stdout = None

    def fileno(self) -> int:
        return 1

    def encoding(self) -> str:
        return "utf-8"

    def write(self, data: str) -> None:
        self.calls.append(("write", data))

    def write_raw(self, data: str) -> None:
        self.calls.append(("write_raw", data))

    def set_title(self, title: str) -> None:
        self.calls.append(("set_title", title))

    def clear_title(self) -> None:
        self.calls.append(("clear_title", None))

    def flush(self) -> None:
        self.calls.append(("flush", None))

    def erase_screen(self) -> None:
        self.calls.append(("erase_screen", None))

    def enter_alternate_screen(self) -> None:
        self.calls.append(("enter_alternate_screen", None))

    def quit_alternate_screen(self) -> None:
        self.calls.append(("quit_alternate_screen", None))

    def enable_mouse_support(self) -> None:
        self.calls.append(("enable_mouse_support", None))

    def disable_mouse_support(self) -> None:
        self.calls.append(("disable_mouse_support", None))

    def erase_end_of_line(self) -> None:
        self.calls.append(("erase_end_of_line", None))

    def erase_down(self) -> None:
        self.calls.append(("erase_down", None))

    def reset_attributes(self) -> None:
        self.calls.append(("reset_attributes", None))

    def set_attributes(self, attrs, color_depth) -> None:
        self.calls.append(("set_attributes", (attrs, color_depth)))

    def disable_autowrap(self) -> None:
        self.calls.append(("disable_autowrap", None))

    def enable_autowrap(self) -> None:
        self.calls.append(("enable_autowrap", None))

    def cursor_goto(self, row: int = 0, column: int = 0) -> None:
        self.calls.append(("cursor_goto", (row, column)))

    def cursor_up(self, amount: int) -> None:
        self.calls.append(("cursor_up", amount))

    def cursor_down(self, amount: int) -> None:
        self.calls.append(("cursor_down", amount))

    def cursor_forward(self, amount: int) -> None:
        self.calls.append(("cursor_forward", amount))

    def cursor_backward(self, amount: int) -> None:
        self.calls.append(("cursor_backward", amount))

    def hide_cursor(self) -> None:
        self.calls.append(("hide_cursor", None))

    def show_cursor(self) -> None:
        self.calls.append(("show_cursor", None))

    def set_cursor_shape(self, cursor_shape) -> None:
        self.calls.append(("set_cursor_shape", cursor_shape))

    def reset_cursor_shape(self) -> None:
        self.calls.append(("reset_cursor_shape", None))

    def ask_for_cpr(self) -> None:
        self.calls.append(("ask_for_cpr", None))

    @property
    def responds_to_cpr(self) -> bool:
        return False

    def get_size(self):
        return SimpleNamespace(rows=24, columns=80)

    def bell(self) -> None:
        self.calls.append(("bell", None))

    def enable_bracketed_paste(self) -> None:
        self.calls.append(("enable_bracketed_paste", None))

    def disable_bracketed_paste(self) -> None:
        self.calls.append(("disable_bracketed_paste", None))

    def reset_cursor_key_mode(self) -> None:
        self.calls.append(("reset_cursor_key_mode", None))

    def scroll_buffer_to_prompt(self) -> None:
        self.calls.append(("scroll_buffer_to_prompt", None))

    def get_rows_below_cursor_position(self) -> int:
        return 10

    def get_default_color_depth(self):
        return "truecolor"


class PromptingTests(unittest.TestCase):
    def _capture_prompt_session(self, **kwargs):
        class _FakePromptSession:
            def __init__(self, *args, **session_kwargs):
                self.args = args
                self.kwargs = session_kwargs

        with patch("open_somnia.cli.prompting.PromptSession", _FakePromptSession):
            return create_prompt_session(Path("."), **kwargs)

    def _escape_handler(self, prompt_session):
        bindings = prompt_session.kwargs["key_bindings"]
        for binding in bindings.bindings:
            if binding.keys and getattr(binding.keys[0], "value", "") == "escape":
                return binding.handler
        self.fail("escape binding not found")

    def test_tab_accepts_inline_history_suggestion(self) -> None:
        inserted: list[str] = []
        buffer = SimpleNamespace(
            suggestion=SimpleNamespace(text="git"),
            complete_state=None,
            insert_text=lambda text: inserted.append(text),
        )

        handled = _handle_tab_action(buffer)

        self.assertTrue(handled)
        self.assertEqual(inserted, ["git"])

    def test_tab_applies_current_completion(self) -> None:
        applied: list[str] = []
        completion = SimpleNamespace(text="compact")
        buffer = SimpleNamespace(
            suggestion=None,
            complete_state=SimpleNamespace(current_completion=completion, completions=[completion]),
            apply_completion=lambda item: applied.append(item.text),
        )

        handled = _handle_tab_action(buffer)

        self.assertTrue(handled)
        self.assertEqual(applied, ["compact"])

    def test_tab_starts_completion_and_applies_first_result(self) -> None:
        applied: list[str] = []
        completion = SimpleNamespace(text="model")
        buffer = SimpleNamespace(
            suggestion=None,
            complete_state=None,
        )

        def start_completion(*, select_first: bool) -> None:
            self.assertTrue(select_first)
            buffer.complete_state = SimpleNamespace(current_completion=completion, completions=[completion])

        buffer.start_completion = start_completion
        buffer.apply_completion = lambda item: applied.append(item.text)

        handled = _handle_tab_action(buffer)

        self.assertTrue(handled)
        self.assertEqual(applied, ["model"])

    def test_command_completion_shows_skill_suggestions_only_for_plus_prefix(self) -> None:
        completer = OpenAgentCompleter(
            Path("."),
            skill_names_getter=lambda: ["unity", "review"],
        )

        slash_only = list(completer.get_completions(Document(text="/", cursor_position=1), None))
        plus_prefixed = list(completer.get_completions(Document(text="/+u", cursor_position=3), None))

        self.assertTrue(any(item.display_text == "/model" for item in slash_only))
        self.assertFalse(any(item.display_text == "/+unity" for item in slash_only))
        self.assertEqual([item.display_text for item in plus_prefixed], ["/+unity"])

    def test_escape_falls_back_to_interrupt_when_busy_escape_does_not_promote(self) -> None:
        events: list[str] = []
        prompt_session = self._capture_prompt_session(
            on_interrupt=lambda: events.append("interrupt"),
            on_busy_escape=lambda: events.append("promote") or False,
            is_busy=lambda: True,
        )

        self._escape_handler(prompt_session)(
            SimpleNamespace(current_buffer=SimpleNamespace(complete_state=None, text=""))
        )

        self.assertEqual(events, ["promote", "interrupt"])

    def test_escape_skips_interrupt_when_busy_escape_promotes_next_message(self) -> None:
        events: list[str] = []
        prompt_session = self._capture_prompt_session(
            on_interrupt=lambda: events.append("interrupt"),
            on_busy_escape=lambda: events.append("promote") or True,
            is_busy=lambda: True,
        )

        self._escape_handler(prompt_session)(
            SimpleNamespace(current_buffer=SimpleNamespace(complete_state=None, text=""))
        )

        self.assertEqual(events, ["promote"])

    def test_escape_does_not_fall_back_to_interrupt_when_queue_state_is_already_armed(self) -> None:
        events: list[str] = []
        prompt_session = self._capture_prompt_session(
            on_interrupt=lambda: events.append("interrupt"),
            on_busy_escape=lambda: events.append("promote") or True,
            is_busy=lambda: True,
        )
        handler = self._escape_handler(prompt_session)

        handler(SimpleNamespace(current_buffer=SimpleNamespace(complete_state=None, text="")))
        handler(SimpleNamespace(current_buffer=SimpleNamespace(complete_state=None, text="")))

        self.assertEqual(events, ["promote", "promote"])

    def test_windows_safe_output_reanchors_horizontal_moves_using_display_width(self) -> None:
        output = _FakeVtOutput()
        wrapped = _WindowsSafeCursorOutput(output)

        wrapped.write("请A接")
        wrapped.cursor_backward(3)
        wrapped.cursor_forward(2)

        self.assertEqual(
            output.calls,
            [
                ("write", "请A接"),
                ("write_raw", "\r"),
                ("cursor_forward", 2),
                ("write_raw", "\r"),
                ("cursor_forward", 4),
            ],
        )

    def test_create_prompt_session_wraps_windows_vt_output(self) -> None:
        fake_output = _FakeVtOutput()

        class _FakePromptSession:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.app = SimpleNamespace(output=fake_output, renderer=SimpleNamespace(output=fake_output))

        with patch("open_somnia.cli.prompting.PromptSession", _FakePromptSession):
            with patch("open_somnia.cli.prompting.sys.platform", "win32"):
                prompt_session = create_prompt_session(Path("."))

        self.assertIsInstance(prompt_session.app.output, _WindowsSafeCursorOutput)
        self.assertIs(prompt_session.app.output, prompt_session.app.renderer.output)


if __name__ == "__main__":
    unittest.main()
