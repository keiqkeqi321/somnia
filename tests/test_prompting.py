from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit.document import Document

from open_somnia.cli.prompting import OpenAgentCompleter, _handle_tab_action


class PromptingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
