from __future__ import annotations

import io
import tempfile
import time
import urllib.error
import unittest
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from open_somnia.config.models import ModelTraits, ProviderProfileSettings, ProviderSettings
from open_somnia.providers.base import ProviderError
from open_somnia.providers.openai_provider import OpenAIProvider
from open_somnia.runtime.agent import OpenAgentRuntime, TurnInterrupted
from open_somnia.runtime.compact import ContextWindowUsage
from open_somnia.runtime.messages import AssistantTurn, ToolCall
from open_somnia.runtime.session import AgentSession


class RuntimeToolOutputTests(unittest.TestCase):
    def test_todowrite_is_logged_but_not_printed(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.tool_log_store = SimpleNamespace(write=lambda **kwargs: {"id": "todo-log"})

        class _Stdout(io.StringIO):
            def isatty(self) -> bool:
                return True

        fake_stdout = _Stdout()
        with patch("sys.stdout", fake_stdout):
            log_id = OpenAgentRuntime.print_tool_event(runtime, "lead", "TodoWrite", {"items": []}, "ok")

        self.assertEqual(log_id, "todo-log")
        self.assertEqual(fake_stdout.getvalue(), "")

    def test_teammate_tool_event_is_logged_but_not_printed(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.tool_log_store = SimpleNamespace(write=lambda **kwargs: {"id": "team-log"})

        class _Stdout(io.StringIO):
            def isatty(self) -> bool:
                return True

        fake_stdout = _Stdout()
        with patch("sys.stdout", fake_stdout):
            log_id = OpenAgentRuntime.print_tool_event(runtime, "Analyst", "grep", {"pattern": "fold"}, "ok")

        self.assertEqual(log_id, "team-log")
        self.assertEqual(fake_stdout.getvalue(), "")

    def test_file_edit_tool_event_uses_compact_diffstat_output(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.tool_log_store = SimpleNamespace(write=lambda **kwargs: {"id": "edit-log"})
        runtime._supports_ansi_output = lambda: False

        class _Stdout(io.StringIO):
            def isatty(self) -> bool:
                return True

        fake_stdout = _Stdout()
        with patch("sys.stdout", fake_stdout):
            log_id = OpenAgentRuntime.print_tool_event(
                runtime,
                "lead",
                "edit_file",
                {"path": "open_somnia/config/settings.py", "old_text": "a\n", "new_text": "a\nb\n"},
                {
                    "status": "ok",
                    "path": "open_somnia/config/settings.py",
                    "absolute_path": "D:/workspace/open_somnia/config/settings.py",
                    "added_lines": 1,
                    "removed_lines": 0,
                },
            )

        rendered = fake_stdout.getvalue()
        self.assertEqual(log_id, "edit-log")
        self.assertIn("Update(open_somnia/config/settings.py)", rendered)
        self.assertIn("Added 1 lines", rendered)
        self.assertIn("@@ -1 +1,2 @@", rendered)
        self.assertIn("+b", rendered)
        self.assertNotIn("TOOL lead", rendered)

    def test_failed_tool_event_uses_red_dot_style_without_box_frame(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.tool_log_store = SimpleNamespace(write=lambda **kwargs: {"id": "tool-log"})
        runtime._supports_ansi_output = lambda: False

        class _Stdout(io.StringIO):
            def isatty(self) -> bool:
                return True

        fake_stdout = _Stdout()
        with patch("sys.stdout", fake_stdout):
            log_id = OpenAgentRuntime.print_tool_event(
                runtime,
                "lead",
                "bash",
                {"command": "git status"},
                "error: command failed",
            )

        rendered = fake_stdout.getvalue()
        self.assertEqual(log_id, "tool-log")
        self.assertIn("Bash(git status)", rendered)
        self.assertIn("error: command failed", rendered)
        self.assertNotIn("TOOL lead", rendered)

    def test_bash_tool_event_uses_compact_heading_and_result_preview(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.tool_log_store = SimpleNamespace(write=lambda **kwargs: {"id": "bash-log"})
        runtime._supports_ansi_output = lambda: False

        class _Stdout(io.StringIO):
            def isatty(self) -> bool:
                return True

        fake_stdout = _Stdout()
        with patch("sys.stdout", fake_stdout):
            log_id = OpenAgentRuntime.print_tool_event(
                runtime,
                "lead",
                "bash",
                {
                    "command": 'cd "D:\\Project\\Git\\learn-claude-code-new\\OpenAgent" && python -c "print(\\\'All files compile OK\\\')"',
                },
                "All files compile OK",
            )

        rendered = fake_stdout.getvalue()
        self.assertEqual(log_id, "bash-log")
        self.assertIn('Bash(cd "D:\\Project\\Git\\learn-claude-code-new\\OpenAgent" && python -c', rendered)
        self.assertIn("All files compile OK", rendered)

    def test_long_bash_result_is_truncated_and_shows_toollog_hint(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.tool_log_store = SimpleNamespace(
            write=lambda **kwargs: {"id": "bash-long"},
            root=Path("D:/workspace/.open_somnia/logs/tool_logs"),
        )
        runtime._supports_ansi_output = lambda: False
        runtime.settings = SimpleNamespace(workspace_root=Path("D:/workspace"))

        class _Stdout(io.StringIO):
            def isatty(self) -> bool:
                return True

        fake_stdout = _Stdout()
        long_output = "0123456789" * 10
        with patch("sys.stdout", fake_stdout):
            OpenAgentRuntime.print_tool_event(
                runtime,
                "lead",
                "bash",
                {"command": "python -c \"print('x')\""},
                long_output,
            )

        rendered = fake_stdout.getvalue()
        self.assertIn("Log: /toollog bash-long", rendered)
        self.assertIn("...", rendered)
        self.assertNotIn(long_output, rendered)

    def test_print_last_turn_file_summary_shows_undo_hint(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime._supports_ansi_output = lambda: False
        session = AgentSession(
            id="session-1",
            last_turn_file_changes=[
                {
                    "path": "greet.py",
                    "absolute_path": "D:/workspace/greet.py",
                    "added_lines": 6,
                    "removed_lines": 0,
                }
            ],
        )

        class _Stdout(io.StringIO):
            def isatty(self) -> bool:
                return True

        fake_stdout = _Stdout()
        with patch("sys.stdout", fake_stdout):
            printed = OpenAgentRuntime.print_last_turn_file_summary(runtime, session)

        rendered = fake_stdout.getvalue()
        self.assertTrue(printed)
        self.assertIn("Changed files", rendered)
        self.assertIn("Undo by: /undo", rendered)
        self.assertIn("greet.py +6 -0", rendered)

    def test_clickable_file_label_uses_hyperlink_and_blue_text_when_ansi_enabled(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime._supports_ansi_output = lambda: True

        rendered = OpenAgentRuntime._format_clickable_file_label(runtime, "greet.py", "D:/workspace/greet.py")

        self.assertIn("greet.py", rendered)
        self.assertIn("\x1b]8;;file:///", rendered)
        self.assertIn("\x1b[38;5;39m", rendered)

    def test_build_system_prompt_includes_environment_guidance(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=Path("D:/workspace"),
            agent=SimpleNamespace(system_prompt=None, name="OpenAgent"),
            provider=SimpleNamespace(name="openai", model="kimi-k2.5"),
        )
        runtime.execution_mode = "plan"
        runtime.skill_loader = SimpleNamespace(descriptions=lambda: "none")

        prompt = OpenAgentRuntime.build_system_prompt(runtime)

        self.assertIn("Execution environment:", prompt)
        self.assertIn("Tool behavior:", prompt)
        self.assertIn("Workspace:", prompt)
        self.assertIn("bash", prompt)
        self.assertIn("Prefer dedicated tools over `bash`", prompt)
        self.assertIn("Use `glob` instead of shell file discovery commands", prompt)
        self.assertIn("Use `grep` instead of shell content search commands", prompt)
        self.assertIn("Do not start with broad `glob` patterns such as `**/*`", prompt)
        self.assertIn("Before `read_file` or `edit_file`, confirm the exact path", prompt)
        self.assertIn("Use `TodoWrite` to break down meaningful work", prompt)
        self.assertIn("Active provider: openai", prompt)
        self.assertIn("Active model: kimi-k2.5", prompt)
        self.assertIn("Current mode: ⏸ plan mode on.", prompt)
        self.assertIn("Return a concrete implementation plan", prompt)
        self.assertIn("request_mode_switch", prompt)
        self.assertIn("Use subagent for isolated subagent work.", prompt)
        self.assertIn("Do not claim to be Claude", prompt)

    def test_authorize_tool_call_blocks_non_edit_tools_in_accept_edits_mode(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "accept_edits"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}

        blocked = OpenAgentRuntime.authorize_tool_call(runtime, "bash", {"command": "git status"})
        allowed = OpenAgentRuntime.authorize_tool_call(runtime, "write_file", {"path": "demo.txt", "content": "ok"})

        self.assertIn("requires explicit user approval", blocked)
        self.assertNotIn("! Yolo", blocked)
        self.assertIsNone(allowed)

    def test_authorize_tool_call_blocks_file_edits_in_plan_mode(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "plan"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}

        blocked = OpenAgentRuntime.authorize_tool_call(runtime, "edit_file", {"path": "demo.txt"})

        self.assertIn("workspace files are read-only", blocked)
        self.assertIn("request_mode_switch", blocked)
        self.assertIn("one-off edit", blocked)
        self.assertNotIn("! Yolo", blocked)

    def test_authorize_tool_call_allows_subagent_in_accept_edits_mode(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "accept_edits"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}

        allowed = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "subagent",
            {"prompt": "Inspect the repo", "agent_type": "general-purpose"},
        )

        self.assertIsNone(allowed)

    def test_authorize_tool_call_allows_task_mutations_in_accept_edits_mode(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "accept_edits"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}

        created = OpenAgentRuntime.authorize_tool_call(runtime, "task_create", {"subject": "Analyze folding system"})
        updated = OpenAgentRuntime.authorize_tool_call(runtime, "task_update", {"task_id": 1, "status": "in_progress"})
        claimed = OpenAgentRuntime.authorize_tool_call(runtime, "claim_task", {"task_id": 1})

        self.assertIsNone(created)
        self.assertIsNone(updated)
        self.assertIsNone(claimed)

    def test_authorize_tool_call_allows_team_collaboration_tools_in_accept_edits_mode(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "accept_edits"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}

        spawned = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "spawn_teammate",
            {"name": "Analyst", "role": "算法分析师", "prompt": "Analyze the folding system"},
        )
        messaged = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "send_message",
            {"to": "Analyst", "content": "Focus on crease generation"},
        )
        inbox = OpenAgentRuntime.authorize_tool_call(runtime, "read_inbox", {})
        broadcast = OpenAgentRuntime.authorize_tool_call(runtime, "broadcast", {"content": "Status check"})
        shutdown = OpenAgentRuntime.authorize_tool_call(runtime, "shutdown_request", {"teammate": "Analyst"})
        approval = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "plan_approval",
            {"request_id": "req-1", "approve": True, "feedback": "Looks good"},
        )

        self.assertIsNone(spawned)
        self.assertIsNone(messaged)
        self.assertIsNone(inbox)
        self.assertIsNone(broadcast)
        self.assertIsNone(shutdown)
        self.assertIsNone(approval)

    def test_authorize_tool_call_blocks_task_mutations_in_plan_mode(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "plan"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}

        blocked = OpenAgentRuntime.authorize_tool_call(runtime, "task_create", {"subject": "Analyze folding system"})

        self.assertIn("persistent task mutations are not allowed", blocked)
        self.assertIn("request_mode_switch", blocked)

    def test_authorize_tool_call_blocks_team_collaboration_tools_in_plan_mode(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "plan"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}

        blocked = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "spawn_teammate",
            {"name": "Analyst", "role": "算法分析师", "prompt": "Analyze the folding system"},
        )

        self.assertIn("agent-team collaboration tools are not allowed", blocked)
        self.assertIn("request_mode_switch", blocked)

    def test_authorize_tool_call_blocks_explore_subagent_in_plan_mode(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "plan"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}

        blocked = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "subagent",
            {"prompt": "Inspect the repo", "agent_type": "Explore"},
        )

        self.assertIn("requires explicit user approval", blocked)

    def test_authorize_tool_call_blocks_general_purpose_subagent_in_plan_mode(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "plan"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}

        blocked = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "subagent",
            {"prompt": "Patch a file", "agent_type": "general-purpose"},
        )

        self.assertIn("agent_type='general-purpose'", blocked)
        self.assertIn("Use agent_type='Explore'", blocked)
        self.assertIn("request_mode_switch", blocked)
        self.assertIn("one-off subagent run", blocked)

    def test_authorize_tool_call_allows_subagent_internal_tools(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "accept_edits"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}

        allowed = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "bash",
            {"command": "Get-ChildItem -Recurse -Filter *.py -File"},
            ctx=SimpleNamespace(actor="subagent"),
        )

        self.assertIsNone(allowed)

    def test_request_authorization_grants_once_and_is_consumed(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "accept_edits"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}
        runtime.authorization_request_handler = lambda **kwargs: {
            "status": "approved",
            "scope": "once",
            "reason": "Allowed once.",
        }

        result = OpenAgentRuntime.request_authorization(runtime, "bash", "Need one shell command")

        self.assertIn('"status": "approved"', result)
        self.assertIsNone(OpenAgentRuntime.authorize_tool_call(runtime, "bash", {"command": "git status"}))
        self.assertIn(
            "requires explicit user approval",
            OpenAgentRuntime.authorize_tool_call(runtime, "bash", {"command": "git status"}),
        )

    def test_request_authorization_grants_workspace_scope(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "plan"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}
        runtime.authorization_request_handler = lambda **kwargs: {
            "status": "approved",
            "scope": "workspace",
            "reason": "Allowed in this workspace.",
        }

        result = OpenAgentRuntime.request_authorization(runtime, "edit_file", "Need to patch a file")

        self.assertIn('"scope": "workspace"', result)
        self.assertIsNone(OpenAgentRuntime.authorize_tool_call(runtime, "edit_file", {"path": "demo.txt"}))

    def test_workspace_authorization_is_persisted_under_openagent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / ".open_somnia"
            runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
            runtime.settings = SimpleNamespace(storage=SimpleNamespace(data_dir=data_dir))
            runtime.execution_mode = "plan"
            runtime._workspace_authorized_tools = set()
            runtime._once_authorized_tools = {}
            runtime.authorization_request_handler = lambda **kwargs: {
                "status": "approved",
                "scope": "workspace",
                "reason": "Allowed in this workspace.",
            }

            result = OpenAgentRuntime.request_authorization(runtime, "edit_file", "Need to patch a file")

            self.assertIn('"scope": "workspace"', result)
            permissions_path = data_dir / "permissions.json"
            self.assertTrue(permissions_path.exists())
            self.assertIn('"authorized_tools"', permissions_path.read_text(encoding="utf-8"))
            self.assertIn('"edit_file"', permissions_path.read_text(encoding="utf-8"))

            resumed_runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
            resumed_runtime.settings = SimpleNamespace(storage=SimpleNamespace(data_dir=data_dir))

            loaded = OpenAgentRuntime._load_workspace_authorizations(resumed_runtime)

            self.assertEqual(loaded, {"edit_file"})

    def test_request_mode_switch_rejects_yolo_target(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "plan"

        result = OpenAgentRuntime.request_mode_switch(runtime, "yolo", "Need full autonomy")

        self.assertIn("target_mode must be one of", result)

    def test_request_mode_switch_updates_runtime_mode_when_approved(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "plan"
        runtime.mode_switch_request_handler = lambda **kwargs: {
            "approved": True,
            "active_mode": "accept_edits",
            "reason": "Switched to accept edits.",
        }

        result = OpenAgentRuntime.request_mode_switch(runtime, "accept_edits", "Plan is done")

        self.assertIn('"status": "approved"', result)
        self.assertEqual(runtime.execution_mode, "accept_edits")

    def test_request_mode_switch_downgrades_without_prompt(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "accept_edits"
        runtime.mode_switch_request_handler = lambda **kwargs: self.fail("downgrade should not require prompting")

        result = OpenAgentRuntime.request_mode_switch(runtime, "plan", "Implementation is complete")

        self.assertIn('"status": "approved"', result)
        self.assertIn('"current_mode": "plan"', result)
        self.assertEqual(runtime.execution_mode, "plan")

    def test_build_system_prompt_drops_plan_guidance_after_mode_switch(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=Path("D:/workspace"),
            agent=SimpleNamespace(system_prompt=None, name="OpenAgent"),
            provider=SimpleNamespace(name="openai", model="kimi-k2.5"),
        )
        runtime.skill_loader = SimpleNamespace(descriptions=lambda: "none")
        runtime.execution_mode = "plan"
        runtime.mode_switch_request_handler = lambda **kwargs: {
            "approved": True,
            "active_mode": "accept_edits",
            "reason": "Switched to accept edits.",
        }

        plan_prompt = OpenAgentRuntime.build_system_prompt(runtime)
        OpenAgentRuntime.request_mode_switch(runtime, "accept_edits", "Plan is done")
        edit_prompt = OpenAgentRuntime.build_system_prompt(runtime)

        self.assertIn("Return a concrete implementation plan", plan_prompt)
        self.assertIn("plan mode on.", plan_prompt)
        self.assertNotIn("Return a concrete implementation plan", edit_prompt)
        self.assertIn("accept edits on.", edit_prompt)
        self.assertIn("write_file and edit_file", edit_prompt)
        self.assertNotIn("! Yolo", edit_prompt)

    def test_switch_provider_model_updates_runtime_and_compact_manager(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=Path("D:/workspace"),
            raw_config={"providers": {}},
            provider=ProviderSettings(name="anthropic", provider_type="anthropic", model="glm-5", max_tokens=8000),
            provider_profiles={
                "openai": ProviderProfileSettings(
                    name="openai",
                    provider_type="openai",
                    models=["gpt-4.1", "gpt-4.1-mini"],
                    model_traits={
                        "gpt-4.1": ModelTraits(context_window_tokens=1_047_576),
                        "gpt-4.1-mini": ModelTraits(context_window_tokens=262_144),
                    },
                    default_model="gpt-4.1",
                    api_key="",
                    base_url="https://api.openai.com/v1",
                    max_tokens=4096,
                    timeout_seconds=60,
                )
            },
        )
        runtime.compact_manager = SimpleNamespace(provider=None, model_max_tokens=0)
        runtime.provider = "old-provider"
        runtime._instantiate_provider = lambda provider_settings: {
            "provider": provider_settings.name,
            "model": provider_settings.model,
        }

        with patch("open_somnia.runtime.agent.persist_provider_selection") as mock_persist:
            message = OpenAgentRuntime.switch_provider_model(runtime, "openai", "gpt-4.1-mini")

        self.assertIn("gpt-4.1-mini", message)
        self.assertIn("saved it to .open_somnia/open_somnia.toml", message)
        self.assertEqual(runtime.settings.provider.name, "openai")
        self.assertEqual(runtime.settings.provider.provider_type, "openai")
        self.assertEqual(runtime.settings.provider.model, "gpt-4.1-mini")
        self.assertEqual(runtime.settings.provider.context_window_tokens, 262_144)
        self.assertEqual(runtime.provider, {"provider": "openai", "model": "gpt-4.1-mini"})
        self.assertEqual(runtime.compact_manager.provider, {"provider": "openai", "model": "gpt-4.1-mini"})
        self.assertEqual(runtime.compact_manager.model_max_tokens, 4096)
        self.assertEqual(runtime.settings.provider_profiles["openai"].default_model, "gpt-4.1-mini")
        mock_persist.assert_called_once_with(runtime.settings, "openai", "gpt-4.1-mini")

    def test_context_window_usage_prefers_provider_counter(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(provider=SimpleNamespace(name="anthropic", model="glm-5", context_window_tokens=200_000))
        runtime.provider = SimpleNamespace(
            count_tokens=lambda system_prompt, messages, tools: 50_000,
            token_counter_name=lambda: "anthropic_native",
            context_window_tokens=lambda: 200_000,
        )
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.worker_registry = SimpleNamespace(schemas=lambda: [])
        runtime.build_system_prompt = lambda actor="lead", role="lead coding agent": "system"
        runtime.execution_mode = "accept_edits"
        session = AgentSession(id="session-1", messages=[{"role": "user", "content": "hello"}])

        usage = OpenAgentRuntime.context_window_usage(runtime, session)

        self.assertIsInstance(usage, ContextWindowUsage)
        self.assertEqual(usage.used_tokens, 50_000)
        self.assertEqual(usage.max_tokens, 200_000)
        self.assertEqual(usage.counter_name, "anthropic_native")
        self.assertEqual(usage.usage_percent, 25.0)

    def test_context_window_usage_falls_back_to_payload_estimate(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(provider=SimpleNamespace(name="openai", model="gpt-4.1", context_window_tokens=128_000))
        runtime.provider = SimpleNamespace(
            count_tokens=lambda system_prompt, messages, tools: (_ for _ in ()).throw(RuntimeError("count failed")),
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 128_000,
        )
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.worker_registry = SimpleNamespace(schemas=lambda: [])
        runtime.build_system_prompt = lambda actor="lead", role="lead coding agent": "system"
        runtime.execution_mode = "accept_edits"
        session = AgentSession(id="session-1", messages=[{"role": "user", "content": "hello world"}])

        usage = OpenAgentRuntime.context_window_usage(runtime, session)

        self.assertGreater(usage.used_tokens, 0)
        self.assertEqual(usage.max_tokens, 128_000)
        self.assertEqual(usage.counter_name, "estimate")

    def test_context_window_usage_falls_back_when_provider_returns_zero_for_non_empty_payload(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(provider=SimpleNamespace(name="glm", model="glm-5.1", context_window_tokens=128_000))
        runtime.provider = SimpleNamespace(
            count_tokens=lambda system_prompt, messages, tools: 0,
            token_counter_name=lambda: "anthropic_native",
            context_window_tokens=lambda: 128_000,
        )
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.worker_registry = SimpleNamespace(schemas=lambda: [])
        runtime.build_system_prompt = lambda actor="lead", role="lead coding agent": "system"
        runtime.execution_mode = "accept_edits"
        session = AgentSession(id="session-1", messages=[{"role": "user", "content": "hello world"}])

        usage = OpenAgentRuntime.context_window_usage(runtime, session)

        self.assertGreater(usage.used_tokens, 0)
        self.assertEqual(usage.max_tokens, 128_000)
        self.assertEqual(usage.counter_name, "estimate")

    def test_instantiate_provider_uses_provider_type_instead_of_profile_name(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)

        with patch("open_somnia.runtime.agent.OpenAIProvider", return_value="openai-adapter") as mock_openai, patch(
            "open_somnia.runtime.agent.AnthropicProvider", return_value="anthropic-adapter"
        ) as mock_anthropic:
            provider = OpenAgentRuntime._instantiate_provider(
                runtime,
                ProviderSettings(
                    name="openrouter",
                    provider_type="openai",
                    model="stepfun/step-3.5-flash",
                    api_key="sk-test",
                    base_url="https://openrouter.ai/api/v1",
                ),
            )

        self.assertEqual(provider, "openai-adapter")
        mock_openai.assert_called_once()
        mock_anthropic.assert_not_called()

    def test_undo_last_turn_restores_previous_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "greet.py"
            target.write_text("new\n", encoding="utf-8")
            runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
            runtime.settings = SimpleNamespace(workspace_root=root)
            runtime.session_manager = SimpleNamespace(save=lambda session: None)
            session = AgentSession(
                id="session-1",
                undo_stack=[
                    {
                        "turn_id": "turn-1",
                        "files": [
                            {
                                "path": "greet.py",
                                "absolute_path": str(target),
                                "existed_before": True,
                                "previous_content": "old\n",
                            }
                        ],
                    }
                ],
                last_turn_file_changes=[{"path": "greet.py", "added_lines": 1, "removed_lines": 1}],
            )

            message = OpenAgentRuntime.undo_last_turn(runtime, session)

            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(session.undo_stack, [])
            self.assertEqual(session.last_turn_file_changes, [])
            self.assertIn("Undid 1 file change", message)

    def test_undo_last_turn_normalizes_workspace_root_before_boundary_check(self) -> None:
        class _FakeResolvedPath:
            def __init__(self, value: str) -> None:
                self.value = value

            def resolve(self):
                return self

            def __truediv__(self, relative: str):
                return _FakeJoinedPath(self.value, relative)

            def is_relative_to(self, other) -> bool:
                base = getattr(other, "value", getattr(other, "raw_value", str(other))).rstrip("/\\")
                candidate = self.value.rstrip("/\\")
                return candidate == base or candidate.startswith(base + "\\")

            def exists(self) -> bool:
                return True

            def unlink(self) -> None:
                return None

        class _FakeJoinedPath:
            def __init__(self, base_value: str, relative: str) -> None:
                self.base_value = base_value.rstrip("/\\")
                self.relative = relative

            def resolve(self):
                return _FakeResolvedPath(f"{self.base_value}\\{self.relative}")

        class _FakeWorkspaceRoot:
            def __init__(self, raw_value: str, resolved_value: str) -> None:
                self.raw_value = raw_value
                self.resolved_value = resolved_value

            def resolve(self):
                return _FakeResolvedPath(self.resolved_value)

            def __truediv__(self, relative: str):
                return _FakeJoinedPath(self.resolved_value, relative)

            def __str__(self) -> str:
                return self.raw_value

        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=_FakeWorkspaceRoot(
                raw_value=r"C:\Users\KEQIKE~1\AppData\Local\Temp\tmpabcd",
                resolved_value=r"C:\Users\keqikeqi321\AppData\Local\Temp\tmpabcd",
            )
        )
        runtime.session_manager = SimpleNamespace(save=lambda session: None)
        session = AgentSession(
            id="session-1",
            undo_stack=[
                {
                    "turn_id": "turn-1",
                    "files": [
                        {
                            "path": "greet.py",
                            "absolute_path": r"C:\Users\keqikeqi321\AppData\Local\Temp\tmpabcd\greet.py",
                            "existed_before": True,
                            "previous_content": "old\n",
                        }
                    ],
                }
            ],
        )

        with patch("open_somnia.runtime.agent.atomic_write_text", return_value=None) as mock_write:
            message = OpenAgentRuntime.undo_last_turn(runtime, session)

        self.assertIn("Undid 1 file change", message)
        mock_write.assert_called_once()

    def test_complete_does_not_retry_turn_interrupt(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(provider=SimpleNamespace(max_tokens=1024))
        attempts: list[str] = []

        class _Provider:
            def complete(self, **kwargs):
                attempts.append("called")
                raise TurnInterrupted("Interrupted by user.")

        runtime.provider = _Provider()

        with self.assertRaises(TurnInterrupted):
            OpenAgentRuntime.complete(runtime, "system", [], [], text_callback=None)

        self.assertEqual(attempts, ["called"])

    def test_complete_does_not_retry_non_retryable_provider_error(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(provider=SimpleNamespace(max_tokens=1024))
        attempts: list[str] = []

        class _Provider:
            def complete(self, **kwargs):
                attempts.append("called")
                raise ProviderError("provider overloaded", retryable=False)

        runtime.provider = _Provider()

        with self.assertRaisesRegex(RuntimeError, "provider overloaded"):
            OpenAgentRuntime.complete(runtime, "system", [], [], text_callback=None)

        self.assertEqual(attempts, ["called"])

    def test_complete_retries_retryable_provider_error(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(provider=SimpleNamespace(max_tokens=1024))
        attempts: list[str] = []

        class _Provider:
            def complete(self, **kwargs):
                attempts.append("called")
                raise ProviderError("temporary timeout", retryable=True)

        runtime.provider = _Provider()

        with self.assertRaisesRegex(RuntimeError, "temporary timeout"):
            OpenAgentRuntime.complete(runtime, "system", [], [], text_callback=None)

        self.assertEqual(attempts, ["called", "called", "called"])

    def test_openai_provider_marks_overload_error_as_non_retryable(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="qwen3.5-plus",
                api_key="test-key",
                base_url="https://example.com/v1",
                timeout_seconds=30,
            )
        )
        overload_body = (
            '{"error":{"code":"1305","message":"该模型当前访问量过大，请您稍后再试"},'
            '"request_id":"req-1"}'
        ).encode("utf-8")
        http_error = urllib.error.HTTPError(
            url="https://example.com/v1/chat/completions",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(overload_body),
        )

        with patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(ProviderError) as context:
                provider.complete("system", [], [], max_tokens=1024)

        self.assertFalse(context.exception.retryable)
        self.assertIn("1305", str(context.exception))

    def test_complete_interrupts_promptly_while_provider_call_is_blocked(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(provider=SimpleNamespace(max_tokens=1024))
        started = Event()
        release = Event()
        interrupt_requested = Event()
        result: dict[str, object] = {}

        class _Provider:
            def complete(self, **kwargs):
                started.set()
                release.wait(timeout=2)
                return AssistantTurn(stop_reason="end_turn", text_blocks=["late"])

        runtime.provider = _Provider()

        def run_complete() -> None:
            try:
                result["value"] = OpenAgentRuntime.complete(
                    runtime,
                    "system",
                    [],
                    [],
                    text_callback=None,
                    should_interrupt=interrupt_requested.is_set,
                )
            except Exception as exc:
                result["value"] = exc

        worker = Thread(target=run_complete)
        started_at = time.monotonic()
        worker.start()
        self.assertTrue(started.wait(timeout=1))

        interrupt_requested.set()
        worker.join(timeout=0.5)
        release.set()

        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started_at, 1.0)
        self.assertIsInstance(result.get("value"), TurnInterrupted)

    def test_complete_blocks_late_stream_output_after_interrupt(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(provider=SimpleNamespace(max_tokens=1024))
        started = Event()
        release = Event()
        callback_attempted = Event()
        interrupt_requested = Event()
        streamed: list[str] = []
        result: dict[str, object] = {}

        class _Provider:
            def complete(self, **kwargs):
                started.set()
                release.wait(timeout=2)
                callback = kwargs.get("text_callback")
                if callback is not None:
                    try:
                        callback("late output")
                    finally:
                        callback_attempted.set()
                return AssistantTurn(stop_reason="end_turn", text_blocks=["late output"])

        runtime.provider = _Provider()

        def run_complete() -> None:
            try:
                result["value"] = OpenAgentRuntime.complete(
                    runtime,
                    "system",
                    [],
                    [],
                    text_callback=streamed.append,
                    should_interrupt=interrupt_requested.is_set,
                )
            except Exception as exc:
                result["value"] = exc

        worker = Thread(target=run_complete)
        worker.start()
        self.assertTrue(started.wait(timeout=1))

        interrupt_requested.set()
        worker.join(timeout=0.5)
        release.set()
        self.assertTrue(callback_attempted.wait(timeout=1))

        self.assertFalse(worker.is_alive())
        self.assertEqual(streamed, [])
        self.assertIsInstance(result.get("value"), TurnInterrupted)

    def test_agent_loop_stops_turn_after_request_authorization_and_replans(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(max_agent_rounds=4, token_threshold=999999, max_tool_output_chars=5000),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.compact_manager = SimpleNamespace(auto_compact=lambda session_id, messages: messages)
        runtime.todo_manager = SimpleNamespace(has_open_items=lambda session: False)
        runtime.session_manager = SimpleNamespace(save=lambda session: None)
        runtime.transcript_store = SimpleNamespace(append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: None
        runtime.build_system_prompt = lambda: "system"
        runtime._capture_turn_file_changes = lambda session: None

        executed_tools: list[str] = []

        class _Registry:
            def schemas(self):
                return []

            def execute(self, ctx, name, payload):
                executed_tools.append(name)
                if name == "request_authorization":
                    return '{"status":"approved","scope":"once"}'
                if name == "bash":
                    return "git status output"
                return f"ran {name}"

        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["Need approval first."],
                    tool_calls=[
                        ToolCall("call-1", "request_authorization", {"tool_name": "bash", "reason": "inspect repo"}),
                        ToolCall("call-2", "bash", {"command": "git status"}),
                    ],
                ),
                AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["Now running the command."],
                    tool_calls=[ToolCall("call-3", "bash", {"command": "git status"})],
                ),
                AssistantTurn(
                    stop_reason="end_turn",
                    text_blocks=["Done."],
                ),
            ]
        )
        runtime.complete = lambda *args, **kwargs: next(turns)
        runtime.registry = _Registry()

        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(runtime, session, "check repo")

        self.assertEqual(result, "Done.")
        self.assertEqual(executed_tools, ["request_authorization", "bash"])
        assistant_with_auth = session.messages[1]
        self.assertEqual(assistant_with_auth["role"], "assistant")
        self.assertIsInstance(assistant_with_auth["content"], list)
        tool_calls_after_auth = [item for item in assistant_with_auth["content"] if item.get("type") == "tool_call"]
        self.assertEqual([item["name"] for item in tool_calls_after_auth], ["request_authorization"])
        assistant_with_bash = session.messages[3]
        tool_calls_after_bash = [item for item in assistant_with_bash["content"] if item.get("type") == "tool_call"]
        self.assertEqual([item["name"] for item in tool_calls_after_bash], ["bash"])

    def test_agent_loop_flushes_streamed_text_before_tool_execution(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(max_agent_rounds=4, token_threshold=999999, max_tool_output_chars=5000),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.compact_manager = SimpleNamespace(auto_compact=lambda session_id, messages: messages)
        runtime.todo_manager = SimpleNamespace(has_open_items=lambda session: False)
        runtime.session_manager = SimpleNamespace(save=lambda session: None)
        runtime.transcript_store = SimpleNamespace(append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: None
        runtime.build_system_prompt = lambda: "system"
        runtime._capture_turn_file_changes = lambda session: None

        order: list[tuple[str, str]] = []

        class _Registry:
            def schemas(self):
                return []

            def execute(self, ctx, name, payload):
                order.append(("tool", name))
                return "ok"

        class _Streamer:
            def __call__(self, text: str):
                order.append(("text", text))

            def finish(self):
                order.append(("flush", ""))

        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["I will inspect the workspace."],
                    tool_calls=[ToolCall("call-1", "bash", {"command": "pwd"})],
                ),
                AssistantTurn(
                    stop_reason="end_turn",
                    text_blocks=["Done."],
                ),
            ]
        )

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            turn = next(turns)
            if turn.text_blocks and text_callback is not None:
                text_callback(turn.text_blocks[0])
            return turn

        runtime.complete = fake_complete
        runtime.registry = _Registry()
        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(runtime, session, "inspect", text_callback=_Streamer())

        self.assertEqual(result, "Done.")
        self.assertLess(order.index(("text", "I will inspect the workspace.")), order.index(("flush", "")))
        self.assertLess(order.index(("flush", "")), order.index(("tool", "bash")))


if __name__ == "__main__":
    unittest.main()
