from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from open_somnia import __version__
from open_somnia.cli.commands import _build_session_choices, cmd_chat, cmd_run, print_user_message
from open_somnia.cli.main import _default_base_url, _open_trace_report, _parse_model_ids, build_parser, main
from open_somnia.cli.provider_management import collect_provider_profile_interactively
from open_somnia.cli.prompting import PROMPT_BORDER
from open_somnia.config.settings import NoConfiguredProvidersError, NoUsableProvidersError
from open_somnia.runtime.agent import OpenAgentRuntime
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

    def test_parser_supports_trace_subcommand(self) -> None:
        args = build_parser().parse_args(["trace", "--provider", "openrouter", "--model", "glm-5", "hello"])

        self.assertEqual(args.command, "trace")
        self.assertEqual(args.provider, "openrouter")
        self.assertEqual(args.model, "glm-5")
        self.assertEqual(args.prompt, "hello")

    def test_parser_supports_traceviewer_subcommand(self) -> None:
        args = build_parser().parse_args(["traceviewer", "--session", "session-1"])

        self.assertEqual(args.command, "trace-viewer")
        self.assertEqual(args.session_id, "session-1")

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

    def test_main_trace_enables_provider_payload_debug_and_marks_provider(self) -> None:
        settings = SimpleNamespace(
            provider=SimpleNamespace(name="openrouter", model="glm-5", provider_type="openai"),
            storage=SimpleNamespace(logs_dir=Path("workspace") / ".open_somnia" / "logs"),
        )
        runtime = SimpleNamespace(close=lambda: None)
        output = io.StringIO()
        debug_env_value = None

        with patch.dict(os.environ, {OpenAgentRuntime.DEBUG_PROVIDER_PAYLOAD_ENV: ""}, clear=False), patch(
            "open_somnia.cli.main.load_settings",
            return_value=settings,
        ) as mock_load, patch(
            "open_somnia.cli.main.OpenAgentRuntime",
            return_value=runtime,
        ) as mock_runtime, patch(
            "open_somnia.cli.commands.cmd_run",
            return_value=0,
        ) as mock_run, patch(
            "open_somnia.cli.main._open_trace_report",
            return_value=0,
        ) as mock_open_report, redirect_stdout(output):
            mock_runtime.DEBUG_PROVIDER_PAYLOAD_ENV = OpenAgentRuntime.DEBUG_PROVIDER_PAYLOAD_ENV
            result = main(["--workspace", "workspace", "-trace", "--provider", "openrouter", "--model", "glm-5", "hello"])
            debug_env_value = os.environ.get(OpenAgentRuntime.DEBUG_PROVIDER_PAYLOAD_ENV)

        self.assertEqual(result, 0)
        self.assertEqual(debug_env_value, "1")
        mock_load.assert_called_once_with("workspace", provider_override="openrouter", model_override="glm-5")
        mock_runtime.assert_called_once_with(settings)
        mock_run.assert_called_once_with(runtime, "hello")
        mock_open_report.assert_called_once_with(settings)
        text = output.getvalue()
        self.assertIn("Provider debug tracing enabled: openrouter / glm-5 (openai)", text)
        self.assertIn("Trace payloads:", text)
        self.assertIn("Trace report will open automatically after exit.", text)

    def test_main_rejects_trace_viewer_split_command(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as exited:
                main(["trace", "viewer"])

        self.assertEqual(exited.exception.code, 2)

    def test_main_trace_chat_opens_report_after_exit(self) -> None:
        settings = SimpleNamespace(
            provider=SimpleNamespace(name="openai", model="gpt-5", provider_type="openai"),
            storage=SimpleNamespace(logs_dir=Path("workspace") / ".open_somnia" / "logs"),
        )
        runtime = SimpleNamespace(close=lambda: None)

        with patch.dict(os.environ, {OpenAgentRuntime.DEBUG_PROVIDER_PAYLOAD_ENV: ""}, clear=False), patch(
            "open_somnia.cli.main.load_settings",
            return_value=settings,
        ), patch(
            "open_somnia.cli.main.OpenAgentRuntime",
            return_value=runtime,
        ) as mock_runtime, patch(
            "open_somnia.cli.commands.cmd_chat",
            return_value=0,
        ) as mock_chat, patch(
            "open_somnia.cli.main._open_trace_report",
            return_value=0,
        ) as mock_open_report, redirect_stdout(io.StringIO()):
            mock_runtime.DEBUG_PROVIDER_PAYLOAD_ENV = OpenAgentRuntime.DEBUG_PROVIDER_PAYLOAD_ENV
            result = main(["--workspace", "workspace", "trace"])

        self.assertEqual(result, 0)
        mock_chat.assert_called_once_with(runtime, resume=False, continue_session=False)
        mock_open_report.assert_called_once_with(settings)

    def test_open_trace_report_opens_browser_viewer(self) -> None:
        settings = SimpleNamespace(storage=SimpleNamespace(logs_dir=Path("workspace") / ".open_somnia" / "logs"))

        with patch("open_somnia.cli.commands.cmd_trace_viewer", return_value=0) as mock_trace_viewer, redirect_stdout(io.StringIO()):
            result = _open_trace_report(settings)

        self.assertEqual(result, 0)
        mock_trace_viewer.assert_called_once_with(settings, open_browser=True)

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

    def test_collect_provider_profile_prefills_existing_api_key(self) -> None:
        profile = SimpleNamespace(
            name="openrouter",
            provider_type="openai",
            default_model="gpt-5",
            models=["gpt-5"],
            api_key="sk-old",
            base_url="https://openrouter.ai/api/v1",
        )

        with patch(
            "open_somnia.cli.provider_management.choose_provider_type_interactively",
            return_value="openai",
        ), patch(
            "open_somnia.cli.provider_management.prompt_provider_details_interactively",
            return_value={
                "provider_name": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "",
                "models": "gpt-5",
            },
        ) as mock_prompt, patch(
            "open_somnia.cli.provider_management.choose_item_interactively",
            return_value="save",
        ):
            submission = collect_provider_profile_interactively(
                {"openrouter": profile},
                previous_provider_name="openrouter",
            )

        self.assertIsNotNone(submission)
        assert submission is not None
        self.assertEqual(submission.api_key, "sk-old")
        self.assertEqual(mock_prompt.call_args.kwargs["default_api_key"], "sk-old")

    def test_cmd_chat_starts_new_session_by_default(self) -> None:
        runtime = SimpleNamespace(
            create_session=lambda: SimpleNamespace(id="new-session", messages=[]),
        )

        with patch("open_somnia.cli.repl.run_repl", return_value=0) as mock_repl:
            result = cmd_chat(runtime, resume=False)

        self.assertEqual(result, 0)
        self.assertEqual(mock_repl.call_args.args[1].id, "new-session")
        self.assertFalse(mock_repl.call_args.kwargs["resumed"])

    def test_cmd_chat_passes_service_to_repl_when_app_service_is_available(self) -> None:
        session = SimpleNamespace(id="service-session", messages=[])
        service = SimpleNamespace(create_session=lambda: session)
        runtime = SimpleNamespace()

        with patch("open_somnia.cli.commands._build_app_service", return_value=service), patch(
            "open_somnia.cli.repl.run_repl", return_value=0
        ) as mock_repl:
            result = cmd_chat(runtime, resume=False)

        self.assertEqual(result, 0)
        self.assertIs(mock_repl.call_args.kwargs["service"], service)
        self.assertEqual(mock_repl.call_args.args[1].id, "service-session")

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

    def test_cmd_run_uses_service_turn_pipeline_when_available(self) -> None:
        session = SimpleNamespace(id="service-run", messages=[])

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

        class _FakeHandle:
            def __init__(self) -> None:
                self.turn_id = "turn-1"
                self.result = SimpleNamespace(status="completed", text="Hello")
                self._drained = 0

            def is_done(self) -> bool:
                return self._drained > 0

            def drain_events(self, *, block: bool = False, timeout: float | None = None):
                self._drained += 1
                if self._drained == 1:
                    return [SimpleNamespace(type="assistant_delta", payload={"delta": "Hello"})]
                return []

        service = SimpleNamespace(
            create_session=lambda: session,
            run_turn=lambda current_session, prompt: _FakeHandle(),
        )
        runtime = SimpleNamespace(run_turn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runtime.run_turn should not be used")))

        fake_stdout = _StdoutCapture()
        with patch("open_somnia.cli.commands._build_app_service", return_value=service), patch("sys.stdout", fake_stdout):
            result = cmd_run(runtime, "hello")

        self.assertEqual(result, 0)
        self.assertIn("● Hello", fake_stdout.getvalue())

    def test_cmd_run_flushes_service_stream_before_tool_output(self) -> None:
        session = SimpleNamespace(id="service-run", messages=[])

        class _StdoutCapture:
            encoding = "utf-8"

            def __init__(self) -> None:
                self.parts: list[str] = []

            def write(self, text: str) -> int:
                self.parts.append(text)
                return len(text)

            def flush(self) -> None:
                return None

            def isatty(self) -> bool:
                return True

            def getvalue(self) -> str:
                return "".join(self.parts)

        class _FakeHandle:
            def __init__(self) -> None:
                self.turn_id = "turn-1"
                self.result = SimpleNamespace(status="completed", text="Done.")
                self._drained = 0

            def is_done(self) -> bool:
                return self._drained > 0

            def drain_events(self, *, block: bool = False, timeout: float | None = None):
                self._drained += 1
                if self._drained == 1:
                    return [
                        SimpleNamespace(type="assistant_delta", payload={"delta": "Preparing update."}),
                        SimpleNamespace(
                            type="tool_finished",
                            payload={
                                "actor": "lead",
                                "tool_name": "edit_file",
                                "rendered_lines": ["TOOL: Update(file.py)"],
                            },
                        ),
                    ]
                return []

        service = SimpleNamespace(
            create_session=lambda: session,
            run_turn=lambda current_session, prompt: _FakeHandle(),
        )
        runtime = SimpleNamespace(run_turn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runtime.run_turn should not be used")))

        fake_stdout = _StdoutCapture()
        with patch("open_somnia.cli.commands._build_app_service", return_value=service), patch("sys.stdout", fake_stdout):
            result = cmd_run(runtime, "hello")

        output = fake_stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertLess(output.index("Preparing update."), output.index("TOOL: Update(file.py)"))

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

    def test_session_history_uses_lightweight_summaries_when_available(self) -> None:
        def fail_list_sessions():
            raise AssertionError("list_sessions should not be needed for resume choices")

        runtime = SimpleNamespace(
            list_session_summaries=lambda: [
                {
                    "id": "empty",
                    "updated_at": 10.0,
                    "created_at": 10.0,
                    "has_visible_exchange": False,
                    "preview": "[no visible messages]",
                },
                {
                    "id": "valid",
                    "updated_at": 13.0,
                    "created_at": 13.0,
                    "has_visible_exchange": True,
                    "preview": "history answer",
                },
            ],
            list_sessions=fail_list_sessions,
        )

        choices = _build_session_choices(runtime)

        self.assertEqual([choice.session_id for choice in choices], ["valid"])
        self.assertIn("history answer", choices[0].label)

    def test_resumed_history_uses_chat_output_styles(self) -> None:
        session = SimpleNamespace(
            messages=[
                {"role": "user", "content": "history question"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking_log", "path": "thinking/session.turn.jsonl", "characters": 99},
                        {"type": "tool_call", "id": "call-1", "name": "bash", "input": {"command": "git status"}},
                        {"type": "text", "text": "# Title\n\n- item"},
                        {"type": "thinking_log", "path": "thinking/session.final.jsonl", "characters": 7},
                        {"type": "text", "text": "Final answer."},
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
        self.assertIn("● think 99 chars -> thinking/session.turn.jsonl", rendered)
        self.assertIn("● think 7 chars -> thinking/session.final.jsonl", rendered)
        self.assertIn("● bash(git status)", rendered)
        self.assertIn("All clean", rendered)
        self.assertIn("Log: /toollog log-1", rendered)
        self.assertLess(rendered.index("● think 99 chars"), rendered.index("● bash(git status)"))
        self.assertLess(rendered.index("● bash(git status)"), rendered.index("● Title"))
        self.assertLess(rendered.index("● Title"), rendered.index("● think 7 chars"))
        self.assertLess(rendered.index("● think 7 chars"), rendered.index("Final answer."))
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
