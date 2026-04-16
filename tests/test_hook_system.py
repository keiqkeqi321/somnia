from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from open_somnia.config.models import (
    AgentSettings,
    AppSettings,
    HookMatcherSettings,
    HookSettings,
    ProviderSettings,
    RuntimeSettings,
    StorageSettings,
)
from open_somnia.config.settings import load_settings
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.messages import AssistantTurn
from open_somnia.tools.registry import ToolDefinition


class HookSystemTests(unittest.TestCase):
    def test_load_settings_adds_builtin_notification_hooks_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "openai"

                [providers.openai]
                models = ["gpt-4.1"]
                default_model = "gpt-4.1"
                api_key = "sk-test"
                base_url = "https://api.openai.example/v1"
                """,
            )

            with patch("open_somnia.config.settings.Path.home", return_value=home):
                settings = load_settings(root)
                global_config_exists = (home / ".open_somnia" / "open_somnia.toml").exists()
                builtin_script_exists = (home / ".open_somnia" / "Hooks" / "builtin_notify" / "notify_user.py").exists()

        events = [hook.event for hook in settings.hooks]
        self.assertIn("AssistantResponse", events)
        self.assertIn("UserChoiceRequested", events)
        builtin = next(hook for hook in settings.hooks if hook.event == "AssistantResponse")
        self.assertEqual(Path(builtin.command).resolve(), Path(sys.executable).resolve())
        self.assertEqual(
            [Path(arg).resolve() for arg in builtin.args],
            [(home / ".open_somnia" / "Hooks" / "builtin_notify" / "notify_user.py").resolve()],
        )
        self.assertTrue(global_config_exists)
        self.assertTrue(builtin_script_exists)

    def test_load_settings_reads_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "openai"

                [providers.openai]
                models = ["gpt-4.1"]
                default_model = "gpt-4.1"
                api_key = "sk-test"
                base_url = "https://api.openai.example/v1"

                [[hooks]]
                event = "PreToolUse"
                command = "python"
                args = ["hooks/pre_bash.py"]
                timeout_seconds = 7
                on_error = "continue"

                [hooks.matcher]
                tool_name = "bash"
                actor = "lead"
                """,
            )

            with patch("open_somnia.config.settings.Path.home", return_value=home):
                settings = load_settings(root)

        self.assertEqual(len(settings.hooks), 3)
        hook = next(hook for hook in settings.hooks if hook.event == "PreToolUse")
        self.assertEqual(hook.event, "PreToolUse")
        self.assertEqual(hook.command, "python")
        self.assertEqual(hook.args, ["hooks/pre_bash.py"])
        self.assertEqual(hook.timeout_seconds, 7)
        self.assertEqual(hook.matcher.tool_name, "bash")
        self.assertEqual(hook.matcher.actor, "lead")
        events = [hook.event for hook in settings.hooks]
        self.assertIn("AssistantResponse", events)
        self.assertIn("UserChoiceRequested", events)

    def test_load_settings_keeps_builtin_notification_when_user_configures_same_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "openai"

                [providers.openai]
                models = ["gpt-4.1"]
                default_model = "gpt-4.1"
                api_key = "sk-test"
                base_url = "https://api.openai.example/v1"

                [[hooks]]
                event = "AssistantResponse"
                command = "python"
                args = ["hooks/custom_notify.py"]
                """,
            )

            with patch("open_somnia.config.settings.Path.home", return_value=home):
                settings = load_settings(root)

        assistant_hooks = [hook for hook in settings.hooks if hook.event == "AssistantResponse"]
        self.assertEqual(len(assistant_hooks), 2)
        self.assertEqual(
            [hook.args for hook in assistant_hooks if hook.managed_by != "somnia_builtin_notify"],
            [["hooks/custom_notify.py"]],
        )
        self.assertEqual(len([hook for hook in assistant_hooks if hook.managed_by == "somnia_builtin_notify"]), 1)
        self.assertIn("UserChoiceRequested", [hook.event for hook in settings.hooks])

    def test_load_settings_preserves_disabled_builtin_notification_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_global_config(
                home,
                """
                [[hooks]]
                event = "AssistantResponse"
                command = "python"
                args = ["hooks/notify_user.py"]
                managed_by = "somnia_builtin_notify"
                enabled = false
                """,
            )
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "openai"

                [providers.openai]
                models = ["gpt-4.1"]
                default_model = "gpt-4.1"
                api_key = "sk-test"
                base_url = "https://api.openai.example/v1"
                """,
            )

            with patch("open_somnia.config.settings.Path.home", return_value=home):
                settings = load_settings(root)
                reloaded = load_settings(root)

        assistant_hooks = [hook for hook in settings.hooks if hook.event == "AssistantResponse"]
        self.assertEqual(len(assistant_hooks), 1)
        self.assertEqual(assistant_hooks[0].managed_by, "somnia_builtin_notify")
        self.assertFalse(assistant_hooks[0].enabled)
        self.assertFalse(next(hook for hook in reloaded.hooks if hook.event == "AssistantResponse").enabled)

    def test_session_start_hook_runs_when_creating_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "session_start.json"
            script_path = self._write_script(
                root / "record_payload.py",
                """
                import json
                import pathlib
                import sys

                payload = json.load(sys.stdin)
                pathlib.Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                """,
            )
            settings = self._make_settings(
                root,
                hooks=[
                    HookSettings(
                        event="SessionStart",
                        command=sys.executable,
                        args=[str(script_path), str(output_path)],
                    )
                ],
            )
            runtime = OpenAgentRuntime(settings)

            session = runtime.create_session()
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["event"], "SessionStart")
        self.assertEqual(payload["session_id"], session.id)
        self.assertEqual(payload["actor"], "lead")

    def test_pre_and_post_tool_hooks_can_rewrite_and_observe_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pre_output = root / "pre.json"
            post_output = root / "post.json"
            rewrite_script = self._write_script(
                root / "rewrite_input.py",
                """
                import json
                import pathlib
                import sys

                payload = json.load(sys.stdin)
                pathlib.Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                print(json.dumps({"action": "replace_input", "replacement_input": {"value": "patched"}}, ensure_ascii=False))
                """,
            )
            record_script = self._write_script(
                root / "record_post.py",
                """
                import json
                import pathlib
                import sys

                payload = json.load(sys.stdin)
                pathlib.Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                """,
            )
            settings = self._make_settings(
                root,
                hooks=[
                    HookSettings(
                        event="PreToolUse",
                        command=sys.executable,
                        args=[str(rewrite_script), str(pre_output)],
                        matcher=HookMatcherSettings(tool_name="echo_payload"),
                    ),
                    HookSettings(
                        event="PostToolUse",
                        command=sys.executable,
                        args=[str(record_script), str(post_output)],
                        matcher=HookMatcherSettings(tool_name="echo_payload"),
                    ),
                ],
            )
            runtime = OpenAgentRuntime(settings)
            runtime.execution_mode = "yolo"
            session = runtime.create_session()
            runtime.registry.register(
                ToolDefinition(
                    name="echo_payload",
                    description="Return the payload for testing.",
                    input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
                    handler=lambda ctx, payload: payload["value"],
                )
            )

            result = runtime.invoke_tool(session, "echo_payload", {"value": "original"})
            pre_payload = json.loads(pre_output.read_text(encoding="utf-8"))
            post_payload = json.loads(post_output.read_text(encoding="utf-8"))

        self.assertEqual(result, "patched")
        self.assertEqual(pre_payload["tool_name"], "echo_payload")
        self.assertEqual(pre_payload["tool_input"]["value"], "original")
        self.assertEqual(post_payload["tool_input"]["value"], "patched")
        self.assertEqual(post_payload["tool_result"], "patched")

    def test_pre_tool_hook_can_deny_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            deny_script = self._write_script(
                root / "deny.py",
                """
                import json

                print(json.dumps({"action": "deny", "message": "blocked by test"}, ensure_ascii=False))
                """,
            )
            settings = self._make_settings(
                root,
                hooks=[
                    HookSettings(
                        event="PreToolUse",
                        command=sys.executable,
                        args=[str(deny_script)],
                        matcher=HookMatcherSettings(tool_name="echo_payload"),
                    )
                ],
            )
            runtime = OpenAgentRuntime(settings)
            runtime.execution_mode = "yolo"
            session = runtime.create_session()
            runtime.registry.register(
                ToolDefinition(
                    name="echo_payload",
                    description="Return the payload for testing.",
                    input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
                    handler=lambda ctx, payload: payload["value"],
                )
            )

            result = runtime.invoke_tool(session, "echo_payload", {"value": "original"})

        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["message"], "blocked by test")

    def test_assistant_response_hook_runs_on_non_tool_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "assistant_response.json"
            script_path = self._write_script(
                root / "record_response.py",
                """
                import json
                import pathlib
                import sys

                payload = json.load(sys.stdin)
                pathlib.Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                """,
            )
            settings = self._make_settings(
                root,
                hooks=[
                    HookSettings(
                        event="AssistantResponse",
                        command=sys.executable,
                        args=[str(script_path), str(output_path)],
                    )
                ],
            )
            runtime = OpenAgentRuntime(settings)
            runtime.complete = lambda *args, **kwargs: AssistantTurn(stop_reason="end_turn", text_blocks=["Done."])
            session = runtime.create_session()

            result = runtime.run_turn(session, "Say hi")
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result, "Done.")
        self.assertEqual(payload["event"], "AssistantResponse")
        self.assertEqual(payload["text"], "Done.")
        self.assertEqual(payload["assistant_message"]["role"], "assistant")
        self.assertEqual(payload["assistant_message"]["content"], "Done.")

    def test_assistant_response_hook_handles_unicode_payload_on_windows_codepages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "assistant_unicode.json"
            script_path = self._write_script(
                root / "record_unicode_response.py",
                """
                import json
                import pathlib
                import sys

                payload = json.load(sys.stdin)
                pathlib.Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                """,
            )
            settings = self._make_settings(
                root,
                hooks=[
                    HookSettings(
                        event="AssistantResponse",
                        command=sys.executable,
                        args=[str(script_path), str(output_path)],
                    )
                ],
            )
            runtime = OpenAgentRuntime(settings)
            runtime.complete = lambda *args, **kwargs: AssistantTurn(stop_reason="end_turn", text_blocks=["Hi there! 👋"])
            session = runtime.create_session()

            result = runtime.run_turn(session, "Say hi")
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result, "Hi there! 👋")
        self.assertEqual(payload["text"], "Hi there! 👋")

    def test_user_choice_requested_hook_runs_for_authorization_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "user_choice_auth.json"
            script_path = self._write_script(
                root / "record_user_choice_auth.py",
                """
                import json
                import pathlib
                import sys

                payload = json.load(sys.stdin)
                pathlib.Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                """,
            )
            settings = self._make_settings(
                root,
                hooks=[
                    HookSettings(
                        event="UserChoiceRequested",
                        command=sys.executable,
                        args=[str(script_path), str(output_path)],
                    )
                ],
            )
            runtime = OpenAgentRuntime(settings)
            runtime.execution_mode = "plan"
            runtime.authorization_request_handler = lambda **kwargs: {
                "status": "approved",
                "scope": "once",
                "reason": "Allowed once.",
            }

            result = runtime.request_authorization("bash", "Inspect repo", "command=pwd")
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertIn('"status": "approved"', result)
        self.assertEqual(payload["event"], "UserChoiceRequested")
        self.assertEqual(payload["choice_type"], "authorization")
        self.assertEqual(payload["choice_payload"]["tool_name"], "bash")
        self.assertEqual(payload["options"], ["allow_once", "allow_workspace", "deny"])

    def test_user_choice_requested_hook_runs_for_mode_switch_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "user_choice_mode.json"
            script_path = self._write_script(
                root / "record_user_choice_mode.py",
                """
                import json
                import pathlib
                import sys

                payload = json.load(sys.stdin)
                pathlib.Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                """,
            )
            settings = self._make_settings(
                root,
                hooks=[
                    HookSettings(
                        event="UserChoiceRequested",
                        command=sys.executable,
                        args=[str(script_path), str(output_path)],
                    )
                ],
            )
            runtime = OpenAgentRuntime(settings)
            runtime.execution_mode = "plan"
            runtime.mode_switch_request_handler = lambda **kwargs: {
                "approved": True,
                "active_mode": "accept_edits",
                "reason": "Switched to accept edits.",
            }

            result = runtime.request_mode_switch("accept_edits", "Plan is done")
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertIn('"status": "approved"', result)
        self.assertEqual(payload["event"], "UserChoiceRequested")
        self.assertEqual(payload["choice_type"], "mode_switch")
        self.assertEqual(payload["choice_payload"]["current_mode"], "plan")
        self.assertEqual(payload["choice_payload"]["target_mode"], "accept_edits")
        self.assertEqual(payload["options"], ["accept_edits", "plan"])

    def _make_settings(self, root: Path, *, hooks: list[HookSettings] | None = None) -> AppSettings:
        data_dir = root / ".open_somnia"
        transcripts_dir = data_dir / "transcripts"
        sessions_dir = data_dir / "sessions"
        tasks_dir = data_dir / "tasks"
        inbox_dir = data_dir / "inbox"
        team_dir = data_dir / "team"
        jobs_dir = data_dir / "jobs"
        requests_dir = data_dir / "requests"
        logs_dir = data_dir / "logs"
        for path in [
            data_dir,
            transcripts_dir,
            sessions_dir,
            tasks_dir,
            inbox_dir,
            team_dir,
            jobs_dir,
            requests_dir,
            logs_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)
        return AppSettings(
            workspace_root=root,
            agent=AgentSettings(name="Somnia"),
            provider=ProviderSettings(
                name="openai",
                provider_type="openai",
                model="fake-model",
                api_key="fake",
                base_url="http://localhost",
            ),
            runtime=RuntimeSettings(),
            storage=StorageSettings(
                data_dir=data_dir,
                transcripts_dir=transcripts_dir,
                sessions_dir=sessions_dir,
                tasks_dir=tasks_dir,
                inbox_dir=inbox_dir,
                team_dir=team_dir,
                jobs_dir=jobs_dir,
                requests_dir=requests_dir,
                logs_dir=logs_dir,
            ),
            hooks=list(hooks or []),
        )

    def _write_script(self, path: Path, body: str) -> Path:
        path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
        return path

    def _write_workspace_config(self, root: Path, content: str) -> None:
        config_dir = root / ".open_somnia"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "open_somnia.toml").write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")

    def _write_global_config(self, home: Path, content: str) -> None:
        config_dir = home / ".open_somnia"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "open_somnia.toml").write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")
