from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openagent.cli.commands import ConsoleStreamer, _build_session_choices
from openagent.runtime.messages import render_markdown_text


class MarkdownRenderingTests(unittest.TestCase):
    def test_render_markdown_text_supports_basic_plain_terminal_formatting(self) -> None:
        rendered = render_markdown_text(
            "# Title\n\nParagraph with **bold**, *italic*, and `code`.\n\n> quoted line\n\n- first item\n1. second item\n\n```\nprint('hi')\n```\n\n---",
            ansi=False,
        )

        self.assertIn("Title\n=====", rendered)
        self.assertIn("Paragraph with bold, italic, and `code`.", rendered)
        self.assertIn("│ quoted line", rendered)
        self.assertIn("• first item", rendered)
        self.assertIn("1. second item", rendered)
        self.assertIn("    print('hi')", rendered)
        self.assertIn("────────────────────────────────────────", rendered)
        self.assertNotIn("**bold**", rendered)
        self.assertNotIn("*italic*", rendered)

    def test_console_streamer_renders_markdown_on_finish(self) -> None:
        streamer = ConsoleStreamer()

        class _Stdout(io.StringIO):
            def isatty(self) -> bool:
                return False

        fake_stdout = _Stdout()
        with patch("sys.stdout", fake_stdout):
            streamer("# Title\n\n- item")
            streamer.finish()

        rendered = fake_stdout.getvalue()
        self.assertIn("● Title\n=====", rendered)
        self.assertIn("• item", rendered)
        self.assertNotIn("# Title", rendered)

    def test_console_streamer_flushes_completed_lines_before_finish(self) -> None:
        streamer = ConsoleStreamer()

        class _Stdout(io.StringIO):
            def isatty(self) -> bool:
                return False

        fake_stdout = _Stdout()
        with patch("sys.stdout", fake_stdout):
            streamer("# Title\n")
            rendered_during_stream = fake_stdout.getvalue()
            streamer("- item")
            rendered_before_finish = fake_stdout.getvalue()
            streamer.finish()

        rendered_after_finish = fake_stdout.getvalue()
        self.assertIn("● Title\n=====\n", rendered_during_stream)
        self.assertEqual(rendered_during_stream, rendered_before_finish)
        self.assertIn("• item", rendered_after_finish)

    def test_session_preview_uses_rendered_markdown_text(self) -> None:
        session = SimpleNamespace(
            id="session-1",
            updated_at=1.0,
            created_at=1.0,
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [{"type": "text", "text": "# Title\n\n- item"}]},
            ],
        )
        runtime = SimpleNamespace(list_sessions=lambda: [session])

        choices = _build_session_choices(runtime)

        self.assertEqual(len(choices), 1)
        self.assertIn("Title", choices[0].label)
        self.assertIn("item", choices[0].label)
        self.assertNotIn("# Title", choices[0].label)


if __name__ == "__main__":
    unittest.main()
