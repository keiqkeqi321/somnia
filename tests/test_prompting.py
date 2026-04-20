from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prompt_toolkit.document import Document

from open_somnia.cli.prompting import OpenAgentCompleter, _handle_tab_action, create_prompt_session


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


if __name__ == "__main__":
    unittest.main()
