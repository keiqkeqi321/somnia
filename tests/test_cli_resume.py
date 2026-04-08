from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from open_somnia import __version__
from open_somnia.cli.commands import _build_session_choices, cmd_chat, print_user_message
from open_somnia.cli.main import _default_base_url, _parse_model_ids, build_parser, main
from open_somnia.cli.prompting import PROMPT_BORDER
from open_somnia.config.settings import NoConfiguredProvidersError, NoUsableProvidersError
from open_somnia.cli.repl import _print_resumed_history


class CliResumeTests(unittest.TestCase):
    def test_parser_supports_single_dash_version_flag(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            with self.assertRaises(SystemExit) as exited:
                build_parser().parse_args(["-version"])
        self.assertEqual(exited.exception.code, 0)
        self.assertIn(f"somnia {__version__}", stream.getvalue())

    def test_parser_defaults_to_chat_mode_without_command(self) -> None:
        args = build_parser().parse_args([])
        self.assertIsNone(args.command)
        self.assertFalse(args.resume)

    def test_parser_supports_short_and_single_dash_resume_flags(self) -> None:
        self.assertTrue(build_parser().parse_args(["-r"]).resume)
        self.assertTrue(build_parser().parse_args(["-resume"]).resume)
        self.assertTrue(build_parser().parse_args(["-c"]).continue_session)

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

    def test_parser_supports_continue_after_subcommand(self) -> None:
        args = build_parser().parse_args(["chat", "-c"])

        self.assertTrue(args.continue_session)
        self.assertFalse(args.resume)
        self.assertEqual(args.command, "chat")

    def test_parser_supports_provider_and_model_for_doctor_subcommand(self) -> None:
        args = build_parser().parse_args(["doctor", "--provider", "openai", "--model", "gpt-4.1"])

        self.assertEqual(args.provider, "openai")
        self.assertEqual(args.model, "gpt-4.1")
        self.assertEqual(args.command, "doctor")

    def test_parser_supports_providers_subcommand(self) -> None:
        args = build_parser().parse_args(["providers"])

        self.assertEqual(args.command, "providers")

    def test_parser_accepts_custom_provider_profile_name(self) -> None:
        args = build_parser().parse_args(["--provider", "openrouter", "--model", "stepfun/step-3.5-flash"])

        self.assertEqual(args.provider, "openrouter")
        self.assertEqual(args.model, "stepfun/step-3.5-flash")

    def test_parse_model_ids_accepts_commas_only(self) -> None:
        self.assertEqual(
            _parse_model_ids("gpt-5, gpt-4.1-mini, claude-sonnet-4-5"),
            ["gpt-5", "gpt-4.1-mini", "claude-sonnet-4-5"],
        )
        self.assertEqual(_parse_model_ids("gpt-5\ngpt-4.1-mini"), ["gpt-5\ngpt-4.1-mini"])

    def test_default_base_url_matches_provider_type(self) -> None:
        self.assertEqual(_default_base_url("openai"), "https://api.openai.com/v1")
        self.assertEqual(_default_base_url("anthropic"), "https://api.anthropic.com")

    def test_main_bootstraps_first_provider_when_missing(self) -> None:
        settings = SimpleNamespace()
        runtime = SimpleNamespace(close=lambda: None)

        with patch("open_somnia.cli.main.load_settings", side_effect=[NoConfiguredProvidersError("missing"), settings]), patch(
            "open_somnia.cli.main._can_prompt_interactively", return_value=True
        ), patch(
            "open_somnia.cli.main.collect_provider_profile_interactively",
            return_value=SimpleNamespace(
                provider_name="openrouter",
                provider_type="openai",
                base_url="https://openrouter.ai/api/v1",
                api_key="sk-test",
                models=["gpt-5", "gpt-4.1-mini"],
            ),
        ), patch(
            "open_somnia.cli.main.persist_initial_provider_setup"
        ) as mock_persist, patch(
            "open_somnia.cli.main.OpenAgentRuntime", return_value=runtime
        ), patch(
            "open_somnia.cli.commands.cmd_chat", return_value=0
        ) as mock_chat:
            result = main([])

        self.assertEqual(result, 0)
        mock_persist.assert_called_once_with(
            "openrouter",
            "openai",
            ["gpt-5", "gpt-4.1-mini"],
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
        )
        mock_chat.assert_called_once_with(runtime, resume=False, continue_session=False)

    def test_main_bootstraps_first_provider_when_stale_provider_config_was_cleared(self) -> None:
        settings = SimpleNamespace()
        runtime = SimpleNamespace(close=lambda: None)

        with patch("open_somnia.cli.main.load_settings", side_effect=[NoUsableProvidersError("stale"), settings]), patch(
            "open_somnia.cli.main._can_prompt_interactively", return_value=True
        ), patch(
            "open_somnia.cli.main.collect_provider_profile_interactively",
            return_value=SimpleNamespace(
                provider_name="anthropic",
                provider_type="anthropic",
                base_url="https://api.anthropic.com",
                api_key="sk-ant-test",
                models=["claude-sonnet-4-5", "claude-3-5-haiku-latest"],
            ),
        ), patch(
            "open_somnia.cli.main.persist_initial_provider_setup"
        ) as mock_persist, patch(
            "open_somnia.cli.main.OpenAgentRuntime", return_value=runtime
        ), patch(
            "open_somnia.cli.commands.cmd_chat", return_value=0
        ) as mock_chat:
            result = main([])

        self.assertEqual(result, 0)
        mock_persist.assert_called_once_with(
            "anthropic",
            "anthropic",
            ["claude-sonnet-4-5", "claude-3-5-haiku-latest"],
            api_key="sk-ant-test",
            base_url="https://api.anthropic.com",
        )
        mock_chat.assert_called_once_with(runtime, resume=False, continue_session=False)

    def test_main_reports_missing_provider_in_noninteractive_mode(self) -> None:
        with patch("open_somnia.cli.main.load_settings", side_effect=NoConfiguredProvidersError("missing")), patch(
            "open_somnia.cli.main._can_prompt_interactively", return_value=False
        ), patch("sys.stderr", new_callable=io.StringIO):
            result = main([])

        self.assertEqual(result, 2)

    def test_main_providers_command_saves_selected_profile(self) -> None:
        profile = SimpleNamespace(
            name="openrouter",
            provider_type="openai",
            default_model="gpt-5",
            models=["gpt-5"],
            api_key="sk-old",
            base_url="https://openrouter.ai/api/v1",
        )

        with patch("open_somnia.cli.main._can_prompt_interactively", return_value=True), patch(
            "open_somnia.cli.main.load_settings",
            return_value=SimpleNamespace(provider_profiles={"openrouter": profile}),
        ), patch(
            "open_somnia.cli.main.choose_provider_target_interactively",
            return_value="openrouter",
        ), patch(
            "open_somnia.cli.main.collect_provider_profile_interactively",
            return_value=SimpleNamespace(
                previous_provider_name="openrouter",
                provider_name="openrouter",
                provider_type="openai",
                base_url="https://openrouter.ai/api/v1",
                api_key="sk-new",
                models=["gpt-5", "gpt-4.1-mini"],
            ),
        ), patch(
            "open_somnia.cli.main.persist_provider_profile",
            return_value="C:/Users/test/.open_somnia/open_somnia.toml",
        ) as mock_persist, patch("builtins.print") as mock_print:
            result = main(["providers"])

        self.assertEqual(result, 0)
        mock_persist.assert_called_once_with(
            "openrouter",
            "openai",
            ["gpt-5", "gpt-4.1-mini"],
            api_key="sk-new",
            base_url="https://openrouter.ai/api/v1",
            previous_provider_name="openrouter",
        )
        mock_print.assert_called_once()

    def test_cmd_chat_starts_new_session_by_default(self) -> None:
        runtime = SimpleNamespace(
            create_session=lambda: SimpleNamespace(id="new-session", messages=[]),
        )

        with patch("open_somnia.cli.repl.run_repl", return_value=0) as mock_repl:
            result = cmd_chat(runtime, resume=False)

        self.assertEqual(result, 0)
        self.assertEqual(mock_repl.call_args.args[1].id, "new-session")
        self.assertFalse(mock_repl.call_args.kwargs["resumed"])

    def test_cmd_chat_continue_loads_latest_visible_session(self) -> None:
        latest = SimpleNamespace(
            id="latest",
            updated_at=20.0,
            created_at=20.0,
            messages=[
                {"role": "user", "content": "latest question"},
                {"role": "assistant", "content": [{"type": "text", "text": "latest answer"}]},
            ],
        )
        older = SimpleNamespace(
            id="older",
            updated_at=10.0,
            created_at=10.0,
            messages=[
                {"role": "user", "content": "older question"},
                {"role": "assistant", "content": [{"type": "text", "text": "older answer"}]},
            ],
        )
        runtime = SimpleNamespace(
            list_sessions=lambda: [latest, older],
            load_session=lambda session_id: latest if session_id == "latest" else older,
            create_session=lambda: SimpleNamespace(id="fresh", messages=[]),
        )

        with patch("open_somnia.cli.repl.run_repl", return_value=0) as mock_repl:
            result = cmd_chat(runtime, continue_session=True)

        self.assertEqual(result, 0)
        self.assertEqual(mock_repl.call_args.args[1].id, "latest")
        self.assertTrue(mock_repl.call_args.kwargs["resumed"])

    def test_cmd_chat_continue_falls_back_to_new_session_when_none_available(self) -> None:
        session = SimpleNamespace(id="fresh", messages=[])
        runtime = SimpleNamespace(
            list_sessions=lambda: [SimpleNamespace(id="empty", updated_at=1.0, created_at=1.0, messages=[])],
            load_session=lambda session_id: None,
            create_session=lambda: session,
        )

        with patch("open_somnia.cli.repl.run_repl", return_value=0) as mock_repl:
            result = cmd_chat(runtime, continue_session=True)

        self.assertEqual(result, 0)
        self.assertEqual(mock_repl.call_args.args[1].id, "fresh")
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

        with patch("open_somnia.cli.commands.choose_session_interactively", return_value="session-1"), patch(
            "open_somnia.cli.repl.run_repl", return_value=0
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

        with patch("open_somnia.cli.commands.choose_session_interactively", return_value=None), patch(
            "open_somnia.cli.repl.run_repl", return_value=0
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
