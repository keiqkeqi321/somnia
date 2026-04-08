from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openagent.cli.commands import _build_session_choices, cmd_chat, print_user_message
from openagent.cli.main import build_parser
from openagent.cli.prompting import PROMPT_BORDER
from openagent.cli.repl import _print_resumed_history


class CliResumeTests(unittest.TestCase):
    def test_parser_defaults_to_chat_mode_without_command(self) -> None:
        args = build_parser().parse_args([])
        self.assertIsNone(args.command)
        self.assertFalse(args.resume)

    def test_parser_supports_short_and_single_dash_resume_flags(self) -> None:
        self.assertTrue(build_parser().parse_args(["-r"]).resume)
        self.assertTrue(build_parser().parse_args(["-resume"]).resume)

    def test_parser_supports_provider_and_model_overrides(self) -> None:
        args = build_parser().parse_args(["--provider", "openai", "--model", "gpt-5", "run", "hello"])

        self.assertEqual(args.provider, "openai")
        self.assertEqual(args.model, "gpt-5")
        self.assertEqual(args.command, "run")
        self.assertEqual(args.prompt, "hello")

    def test_parser_supports_provider_and_model_after_subcommand(self) -> None:
        args = build_parser().parse_args(["chat", "--provider", "anthropic", "--model", "glm-5"])

        self.assertEqual(args.provider, "anthropic")
        self.assertEqual(args.model, "glm-5")
        self.assertEqual(args.command, "chat")

    def test_parser_supports_provider_and_model_for_doctor_subcommand(self) -> None:
        args = build_parser().parse_args(["doctor", "--provider", "openai", "--model", "gpt-4.1"])

        self.assertEqual(args.provider, "openai")
        self.assertEqual(args.model, "gpt-4.1")
        self.assertEqual(args.command, "doctor")

    def test_parser_accepts_custom_provider_profile_name(self) -> None:
        args = build_parser().parse_args(["--provider", "openrouter", "--model", "stepfun/step-3.5-flash"])

        self.assertEqual(args.provider, "openrouter")
        self.assertEqual(args.model, "stepfun/step-3.5-flash")

    def test_cmd_chat_starts_new_session_by_default(self) -> None:
        runtime = SimpleNamespace(
            create_session=lambda: SimpleNamespace(id="new-session", messages=[]),
        )

        with patch("openagent.cli.repl.run_repl", return_value=0) as mock_repl:
            result = cmd_chat(runtime, resume=False)

        self.assertEqual(result, 0)
        self.assertEqual(mock_repl.call_args.args[1].id, "new-session")
        self.assertFalse(mock_repl.call_args.kwargs["resumed"])

    def test_cmd_chat_resume_loads_selected_session(self) -> None:
        session = SimpleNamespace(
            id="session-1",
            updated_at=1.0,
            created_at=1.0,
            messages=[
                {"role": "user", "content": "history question"},
                {"role": "assistant", "content": [{"type": "text", "text": "history answer"}]},
            ],
        )
        runtime = SimpleNamespace(
            list_sessions=lambda: [session],
            load_session=lambda session_id: session if session_id == "session-1" else None,
            create_session=lambda: SimpleNamespace(id="fresh", messages=[]),
        )

        with patch("openagent.cli.commands.choose_session_interactively", return_value="session-1"), patch(
            "openagent.cli.repl.run_repl", return_value=0
        ) as mock_repl:
            result = cmd_chat(runtime, resume=True)

        self.assertEqual(result, 0)
        self.assertEqual(mock_repl.call_args.args[1].id, "session-1")
        self.assertTrue(mock_repl.call_args.kwargs["resumed"])

    def test_cmd_chat_resume_cancellation_falls_back_to_new_session(self) -> None:
        session = SimpleNamespace(id="fresh", messages=[])
        runtime = SimpleNamespace(
            list_sessions=lambda: [SimpleNamespace(id="old", updated_at=1.0, created_at=1.0, messages=[])],
            load_session=lambda session_id: None,
            create_session=lambda: session,
        )

        with patch("openagent.cli.commands.choose_session_interactively", return_value=None), patch(
            "openagent.cli.repl.run_repl", return_value=0
        ) as mock_repl:
            result = cmd_chat(runtime, resume=True)

        self.assertEqual(result, 0)
        self.assertEqual(mock_repl.call_args.args[1].id, "fresh")
        self.assertFalse(mock_repl.call_args.kwargs["resumed"])

    def test_session_history_ignores_empty_or_incomplete_sessions(self) -> None:
        empty = SimpleNamespace(id="empty", updated_at=10.0, created_at=10.0, messages=[])
        only_user = SimpleNamespace(
            id="only-user",
            updated_at=11.0,
            created_at=11.0,
            messages=[{"role": "user", "content": "hello"}],
        )
        only_assistant = SimpleNamespace(
            id="only-assistant",
            updated_at=12.0,
            created_at=12.0,
            messages=[{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}],
        )
        valid = SimpleNamespace(
            id="valid",
            updated_at=13.0,
            created_at=13.0,
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
            ],
        )
        runtime = SimpleNamespace(list_sessions=lambda: [empty, only_user, only_assistant, valid])

        choices = _build_session_choices(runtime)

        self.assertEqual([choice.session_id for choice in choices], ["valid"])

    def test_resumed_history_uses_chat_output_styles(self) -> None:
        session = SimpleNamespace(
            messages=[
                {"role": "user", "content": "history question"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "# Title\n\n- item"},
                        {"type": "tool_call", "id": "call-1", "name": "bash", "input": {"command": "git status"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_call_id": "call-1",
                            "content": "All clean",
                            "raw_output": "All clean",
                            "log_id": "log-1",
                        },
                    ],
                },
            ]
        )

        runtime = SimpleNamespace(
            render_tool_event_lines=lambda tool_name, payload, output, log_id=None: [
                f"● {tool_name}({payload.get('command', '')})",
                f"  ⎿  {output if isinstance(output, str) else output.get('message', output)}",
                f"     Log: /toollog {log_id}" if log_id else "     Log: (none)",
            ]
        )

        class _StdoutCapture:
            def __init__(self) -> None:
                self.parts: list[str] = []

            def write(self, text: str) -> int:
                self.parts.append(text)
                return len(text)

            def flush(self) -> None:
                return None

            def isatty(self) -> bool:
                return False

            def getvalue(self) -> str:
                return "".join(self.parts)

        fake_stdout = _StdoutCapture()
        with patch("sys.stdout", fake_stdout):
            _print_resumed_history(session, runtime)

        rendered = fake_stdout.getvalue()
        self.assertIn("[resumed history]", rendered)
        self.assertIn("❯ history question", rendered)
        self.assertIn("● Title\n=====", rendered)
        self.assertIn("• item", rendered)
        self.assertIn("● bash(git status)", rendered)
        self.assertIn("All clean", rendered)
        self.assertIn("Log: /toollog log-1", rendered)
        self.assertNotIn("You:", rendered)
        self.assertNotIn("Assistant:", rendered)
        self.assertNotIn(PROMPT_BORDER, rendered)

    def test_print_user_message_has_no_bottom_rule(self) -> None:
        class _StdoutCapture:
            def __init__(self) -> None:
                self.parts: list[str] = []

            def write(self, text: str) -> int:
                self.parts.append(text)
                return len(text)

            def flush(self) -> None:
                return None

            def isatty(self) -> bool:
                return False

            def getvalue(self) -> str:
                return "".join(self.parts)

        fake_stdout = _StdoutCapture()
        with patch("sys.stdout", fake_stdout):
            print_user_message("hello")

        rendered = fake_stdout.getvalue()
        self.assertIn("❯ hello", rendered)
        self.assertNotIn(PROMPT_BORDER, rendered)


if __name__ == "__main__":
    unittest.main()
