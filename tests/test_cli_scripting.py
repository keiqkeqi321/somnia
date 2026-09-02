from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from open_somnia.cli.scripting import (
    EXIT_AUTH_FAILED,
    EXIT_CONFIG_ERROR,
    EXIT_MODEL_ERROR,
    EXIT_QUOTA_EXCEEDED,
    EXIT_SESSION_NOT_FOUND,
    EXIT_TIMEOUT,
    EXIT_USAGE_ERROR,
    CliError,
    error_code_for_kind,
    exit_code_for_error_kind,
)
from open_somnia.config.settings import get_config_value, set_config_value
from open_somnia.providers.anthropic_provider import _wrap_anthropic_exception
from open_somnia.providers.base import ProviderError
from open_somnia.providers.openai_provider import _wrap_openai_exception
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.messages import AssistantTurn


class _StatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class ScriptingMappingTests(unittest.TestCase):
    def test_kind_to_exit_code(self) -> None:
        self.assertEqual(exit_code_for_error_kind("quota"), EXIT_QUOTA_EXCEEDED)
        self.assertEqual(exit_code_for_error_kind("auth"), EXIT_AUTH_FAILED)
        self.assertEqual(exit_code_for_error_kind("model"), EXIT_MODEL_ERROR)
        self.assertEqual(exit_code_for_error_kind("timeout"), EXIT_TIMEOUT)
        self.assertEqual(exit_code_for_error_kind("other"), 1)
        self.assertEqual(exit_code_for_error_kind(None), 1)

    def test_kind_to_error_code(self) -> None:
        self.assertEqual(error_code_for_kind("quota"), "quota_exceeded")
        self.assertEqual(error_code_for_kind("auth"), "auth_failed")
        self.assertEqual(error_code_for_kind("model"), "model_error")
        self.assertEqual(error_code_for_kind("timeout"), "timeout")
        self.assertEqual(error_code_for_kind("nonsense"), "internal_error")

    def test_cli_error_carries_code_and_exit(self) -> None:
        err = CliError("boom", code="session_not_found", exit_code=EXIT_SESSION_NOT_FOUND)
        self.assertEqual(err.code, "session_not_found")
        self.assertEqual(err.exit_code, EXIT_SESSION_NOT_FOUND)
        self.assertIsInstance(err, RuntimeError)


class ProviderErrorKindTests(unittest.TestCase):
    def test_anthropic_kind_by_status_code(self) -> None:
        cases = {
            401: "auth",
            403: "auth",
            429: "quota",
            408: "timeout",
            400: "model",
            404: "model",
            500: "other",
        }
        for status_code, expected in cases.items():
            with self.subTest(status_code=status_code):
                wrapped = _wrap_anthropic_exception(_StatusError(status_code))
                self.assertEqual(wrapped.kind, expected)

    def test_openai_kind_by_status_code(self) -> None:
        cases = {
            401: "auth",
            429: "quota",
            404: "model",
            408: "timeout",
            500: "other",
        }
        for status_code, expected in cases.items():
            with self.subTest(status_code=status_code):
                wrapped = _wrap_openai_exception(_StatusError(status_code))
                self.assertEqual(wrapped.kind, expected)

    def test_wrap_preserves_existing_provider_error(self) -> None:
        original = ProviderError("nope", retryable=False, kind="quota")
        self.assertIs(_wrap_anthropic_exception(original), original)
        self.assertIs(_wrap_openai_exception(original), original)

    def test_runtime_retry_reraise_preserves_kind(self) -> None:
        class _FailingProvider:
            def __init__(self, settings):
                self.settings = settings

            def complete(self, **kwargs):
                raise ProviderError("denied", retryable=False, kind="auth")

        from open_somnia.config.models import ProviderSettings

        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.provider = _FailingProvider(
            ProviderSettings(name="x", provider_type="anthropic", model="m", max_tokens=1)
        )
        runtime.settings = SimpleNamespace(
            provider=runtime.provider.settings,
            vision_provider=None,
            vision_model=None,
            provider_profiles={},
        )
        runtime._instantiate_provider = lambda provider_settings: runtime.provider
        runtime._raise_if_interrupted = lambda should_interrupt=None: None
        runtime._wait_before_provider_retry = lambda should_interrupt=None: None

        with self.assertRaises(ProviderError) as caught:
            OpenAgentRuntime.complete(runtime, "system", [{"role": "user", "content": "hi"}], [])
        self.assertEqual(caught.exception.kind, "auth")
        self.assertIn("Provider call failed", str(caught.exception))


class ConfigGetSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="somnia-config-test-")
        self.config_path = Path(self._tmp.name) / "open_somnia.toml"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_set_then_get_roundtrip(self) -> None:
        set_config_value(self.config_path, "agent.name", "demo-agent")
        set_config_value(self.config_path, "runtime.max_rounds", "42")
        set_config_value(self.config_path, "providers.demo.models", '["m1", "m2"]')
        set_config_value(self.config_path, "providers.demo.enabled", "true")

        import tomllib

        raw = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(get_config_value(raw, "agent.name"), "demo-agent")
        self.assertEqual(get_config_value(raw, "runtime.max_rounds"), 42)
        self.assertEqual(get_config_value(raw, "providers.demo.models"), ["m1", "m2"])
        self.assertEqual(get_config_value(raw, "providers.demo.enabled"), True)

    def test_set_overwrites_existing_key(self) -> None:
        set_config_value(self.config_path, "agent.name", "first")
        set_config_value(self.config_path, "agent.name", "second")
        import tomllib

        raw = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(get_config_value(raw, "agent.name"), "second")

    def test_unquoted_string_fallback(self) -> None:
        set_config_value(self.config_path, "agent.name", "not a toml literal!!!")
        import tomllib

        raw = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(get_config_value(raw, "agent.name"), "not a toml literal!!!")

    def test_hooks_section_rejected(self) -> None:
        with self.assertRaises(ValueError):
            set_config_value(self.config_path, "hooks.event", "x")

    def test_sectionless_key_rejected(self) -> None:
        with self.assertRaises(ValueError):
            set_config_value(self.config_path, "nosection", "x")

    def test_get_missing_key_raises(self) -> None:
        with self.assertRaises(KeyError):
            get_config_value({"a": {}}, "a.b.c")


class CollectRunPromptTests(unittest.TestCase):
    def test_stdin_supplies_prompt(self) -> None:
        from open_somnia.cli.commands import _collect_run_prompt

        with patch.object(sys, "stdin", io.StringIO("from stdin")):
            self.assertEqual(_collect_run_prompt(None), "from stdin")

    def test_argument_combined_with_stdin(self) -> None:
        from open_somnia.cli.commands import _collect_run_prompt

        with patch.object(sys, "stdin", io.StringIO("piped context")):
            self.assertEqual(_collect_run_prompt("review this"), "review this\n\npiped context")

    def test_file_supplies_prompt(self) -> None:
        from open_somnia.cli.commands import _collect_run_prompt

        with tempfile.TemporaryDirectory() as tmp:
            prompt_file = Path(tmp) / "prompt.txt"
            prompt_file.write_text("from file", encoding="utf-8")
            with patch.object(sys, "stdin", io.StringIO("")):
                self.assertEqual(_collect_run_prompt(None, file_path=prompt_file), "from file")

    def test_missing_file_is_usage_error(self) -> None:
        from open_somnia.cli.commands import _collect_run_prompt

        with patch.object(sys, "stdin", io.StringIO("")):
            with self.assertRaises(CliError) as caught:
                _collect_run_prompt(None, file_path="no/such/file.txt")
            self.assertEqual(caught.exception.exit_code, EXIT_USAGE_ERROR)

    def test_empty_prompt_is_usage_error(self) -> None:
        from open_somnia.cli.commands import _collect_run_prompt

        with patch.object(sys, "stdin", io.StringIO("")):
            with self.assertRaises(CliError) as caught:
                _collect_run_prompt(None)
            self.assertEqual(caught.exception.exit_code, EXIT_USAGE_ERROR)


class CliEndToEndTests(unittest.TestCase):
    """Drive main() with an isolated HOME and workspace."""

    def setUp(self) -> None:
        self._home_tmp = tempfile.TemporaryDirectory(prefix="somnia-cli-home-", ignore_cleanup_errors=True)
        self._ws_tmp = tempfile.TemporaryDirectory(prefix="somnia-cli-ws-", ignore_cleanup_errors=True)
        self.home = Path(self._home_tmp.name)
        self.workspace = Path(self._ws_tmp.name)
        config_dir = self.home / ".open_somnia"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "open_somnia.toml").write_text(
            "\n".join(
                [
                    "[providers]",
                    'default = "demo"',
                    "",
                    "[providers.demo]",
                    'provider_type = "anthropic"',
                    'models = ["demo-model"]',
                    'default_model = "demo-model"',
                    'api_key = "sk-test-fake"',
                    'base_url = "http://127.0.0.1:9"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self._home_patch = patch("open_somnia.config.settings.Path.home", return_value=self.home)
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)
        self.addCleanup(self._home_tmp.cleanup)
        self.addCleanup(self._ws_tmp.cleanup)

    def _run_main(self, argv: list[str]) -> tuple[int, str, str]:
        from open_somnia.cli.main import main

        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO("")
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            patch.object(sys, "stdin", stdin),
        ):
            code = main(["--workspace", str(self.workspace)] + argv)
        return code, out.getvalue(), err.getvalue()

    def test_sessions_list_json(self) -> None:
        from open_somnia.config.settings import load_settings
        from open_somnia.storage.sessions import SessionStore

        settings = load_settings(str(self.workspace), allow_missing_provider=True)
        store = SessionStore(settings.storage.sessions_dir)
        session = store.create()
        session["messages"] = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        store.save(session)

        code, out, _ = self._run_main(["sessions", "list", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        ids = [item["id"] for item in payload["sessions"]]
        self.assertIn(session["id"], ids)

    def _create_session_with_messages(self, messages: list[dict]) -> str:
        from open_somnia.config.settings import load_settings
        from open_somnia.storage.sessions import SessionStore

        settings = load_settings(str(self.workspace), allow_missing_provider=True)
        store = SessionStore(settings.storage.sessions_dir)
        session = store.create()
        session["messages"] = messages
        store.save(session)
        return session["id"]

    def test_sessions_fork_json(self) -> None:
        source_id = self._create_session_with_messages(
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
            ]
        )

        code, out, _ = self._run_main(["sessions", "fork", source_id, "--at", "2", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["forked_from"], source_id)
        self.assertEqual(payload["message_count"], 2)
        self.assertNotEqual(payload["session_id"], source_id)

        from open_somnia.config.settings import load_settings
        from open_somnia.runtime.session import SessionManager
        from open_somnia.storage.sessions import SessionStore
        from open_somnia.storage.transcripts import TranscriptStore

        settings = load_settings(str(self.workspace), allow_missing_provider=True)
        manager = SessionManager(
            SessionStore(settings.storage.sessions_dir),
            TranscriptStore(settings.storage.transcripts_dir),
        )
        forked = manager.load(payload["session_id"])
        self.assertEqual(
            forked.messages,
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
            ],
        )
        self.assertEqual(forked.forked_from, source_id)
        self.assertEqual(len(manager.load(source_id).messages), 4)

    def test_sessions_fork_plain_output(self) -> None:
        source_id = self._create_session_with_messages([{"role": "user", "content": "q1"}])
        code, out, _ = self._run_main(["sessions", "fork", source_id, "--at", "1"])
        self.assertEqual(code, 0)
        self.assertIn(f"from {source_id} @ 1", out)

    def test_sessions_fork_unknown_session_exits_6(self) -> None:
        code, _, err = self._run_main(["sessions", "fork", "nosuchid", "--at", "1", "--json"])
        self.assertEqual(code, EXIT_SESSION_NOT_FOUND)
        payload = json.loads(err)
        self.assertEqual(payload["error"]["code"], "session_not_found")

    def test_sessions_fork_out_of_range_exits_64(self) -> None:
        source_id = self._create_session_with_messages([{"role": "user", "content": "q1"}])
        code, _, err = self._run_main(["sessions", "fork", source_id, "--at", "5", "--json"])
        self.assertEqual(code, EXIT_USAGE_ERROR)
        payload = json.loads(err)
        self.assertEqual(payload["error"]["code"], "usage_error")

    def test_providers_list_json_masks_api_key(self) -> None:
        code, out, _ = self._run_main(["providers", "list", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        demo = next(item for item in payload["providers"] if item["name"] == "demo")
        self.assertTrue(demo["api_key_configured"])
        self.assertTrue(demo["is_default"])
        self.assertNotIn("sk-test-fake", out)

    def test_config_get_unknown_key_exits_7_with_json_error(self) -> None:
        code, _, err = self._run_main(["config", "get", "no.such.key", "--json"])
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        payload = json.loads(err)
        self.assertEqual(payload["error"]["code"], "config_error")

    def test_config_set_via_cli(self) -> None:
        code, _, _ = self._run_main(["config", "set", "agent.name", "cli-set"])
        self.assertEqual(code, 0)
        code, out, _ = self._run_main(["config", "get", "agent.name"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "cli-set")

    def test_run_unknown_session_exits_6(self) -> None:
        code, _, err = self._run_main(["run", "--session", "nosuchid", "hi", "--json"])
        self.assertEqual(code, EXIT_SESSION_NOT_FOUND)
        payload = json.loads(err)
        self.assertEqual(payload["error"]["code"], "session_not_found")

    def test_run_json_envelope(self) -> None:
        from open_somnia.config.models import ProviderSettings

        class _EchoProvider:
            def __init__(self, settings=None):
                self.settings = settings or ProviderSettings(
                    name="demo", provider_type="anthropic", model="demo-model", max_tokens=128
                )

            def complete(self, *args, **kwargs):
                return AssistantTurn(stop_reason="end_turn", text_blocks=["echo reply"])

        with patch.object(OpenAgentRuntime, "_instantiate_provider", return_value=_EchoProvider()):
            code, out, err = self._run_main(["run", "hi", "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["text"], "echo reply")
        self.assertEqual(payload["provider"], "demo")
        self.assertEqual(payload["model"], "demo-model")
        self.assertTrue(payload["session_id"])
        self.assertIsInstance(payload["duration_ms"], int)
        self.assertEqual(
            set(payload["usage"]),
            {
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            },
        )

    def test_run_provider_quota_failure_exits_2(self) -> None:
        from open_somnia.config.models import ProviderSettings

        class _QuotaProvider:
            def __init__(self, settings=None):
                self.settings = settings or ProviderSettings(
                    name="demo", provider_type="anthropic", model="demo-model", max_tokens=128
                )

            def complete(self, *args, **kwargs):
                raise ProviderError("quota exhausted", retryable=False, kind="quota")

        with patch.object(OpenAgentRuntime, "_instantiate_provider", return_value=_QuotaProvider()):
            code, _, err = self._run_main(["run", "hi", "--json"])
        self.assertEqual(code, EXIT_QUOTA_EXCEEDED)
        payload = json.loads(err)
        self.assertEqual(payload["error"]["code"], "quota_exceeded")

    def test_usage_error_exits_64(self) -> None:
        from open_somnia.cli.main import main

        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["run", "--definitely-not-a-flag"])
        self.assertEqual(caught.exception.code, EXIT_USAGE_ERROR)

    def test_doctor_json_healthy(self) -> None:
        code, out, _ = self._run_main(["doctor", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["provider"], "demo")
        self.assertTrue(payload["api_key_configured"])

    def test_run_requires_some_prompt(self) -> None:
        code, _, err = self._run_main(["run", "--json"])
        self.assertEqual(code, EXIT_USAGE_ERROR)
        payload = json.loads(err)
        self.assertEqual(payload["error"]["code"], "usage_error")

    def test_run_plain_output_has_no_prefix(self) -> None:
        from open_somnia.config.models import ProviderSettings

        class _EchoProvider:
            def __init__(self, settings=None):
                self.settings = settings or ProviderSettings(
                    name="demo", provider_type="anthropic", model="demo-model", max_tokens=128
                )

            def complete(self, *args, **kwargs):
                return AssistantTurn(stop_reason="end_turn", text_blocks=["plain reply"])

        with patch.object(OpenAgentRuntime, "_instantiate_provider", return_value=_EchoProvider()):
            code, out, err = self._run_main(["run", "hi", "--plain"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "plain reply\n")
        self.assertNotIn("\x1b[", out)

    def test_run_stdin_prompt_end_to_end(self) -> None:
        from open_somnia.config.models import ProviderSettings

        seen: list[str] = []

        class _RecordingProvider:
            def __init__(self, settings=None):
                self.settings = settings or ProviderSettings(
                    name="demo", provider_type="anthropic", model="demo-model", max_tokens=128
                )

            def complete(self, *args, **kwargs):
                messages = args[1] if len(args) > 1 else kwargs.get("messages", [])
                for message in reversed(messages):
                    if message.get("role") == "user":
                        content = message.get("content")
                        if isinstance(content, str):
                            seen.append(content)
                        break
                return AssistantTurn(stop_reason="end_turn", text_blocks=["ok"])

        from open_somnia.cli.main import main

        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(OpenAgentRuntime, "_instantiate_provider", return_value=_RecordingProvider()),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            patch.object(sys, "stdin", io.StringIO("piped body")),
        ):
            code = main(["--workspace", str(self.workspace), "run", "argument part", "--plain"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(seen, "provider was not called")
        self.assertIn("argument part", seen[-1])
        self.assertIn("piped body", seen[-1])


if __name__ == "__main__":
    unittest.main()
