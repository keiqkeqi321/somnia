from __future__ import annotations

import io
import json
import os
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
from open_somnia.providers.anthropic_provider import AnthropicProvider
from open_somnia.providers.openai_provider import OpenAIProvider
from open_somnia.runtime.agent import OpenAgentRuntime, TurnInterrupted
from open_somnia.runtime.compact import (
    ContextWindowUsage,
    SemanticCompressionDecision,
    ToolResultCandidate,
    ToolResultLocator,
    build_payload_messages,
)
from open_somnia.runtime.messages import AssistantTurn, ToolCall
from open_somnia.runtime.session import AgentSession


class RuntimeToolOutputTests(unittest.TestCase):
    def _stable_test_dir(self, name: str) -> Path:
        root = Path.cwd() / ".tmp-tests" / f"{name}-{time.time_ns()}"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _candidate(
        self,
        *,
        message_index: int,
        item_index: int,
        tool_name: str,
        content: str,
        tool_input: dict | None = None,
        importance: str | None = None,
        log_id: str = "log-1",
        age: int = 4,
        output_preview: str | None = None,
        has_error: bool = False,
    ) -> ToolResultCandidate:
        return ToolResultCandidate(
            locator=ToolResultLocator(message_index=message_index, item_index=item_index),
            tool_call_id=f"call-{message_index}-{item_index}",
            tool_name=tool_name,
            tool_input=tool_input or {},
            importance=importance,
            content=content,
            log_id=log_id,
            age=age,
            output_length=len(content),
            output_preview=output_preview or content[:220],
            has_error=has_error,
        )

    def _tool_round_messages(self, *contents: str) -> list[dict]:
        messages: list[dict] = []
        for index, content in enumerate(contents, start=1):
            call_id = f"call-{index}"
            messages.append(
                {
                    "role": "assistant",
                    "content": [{"type": "tool_call", "id": call_id, "name": "grep", "input": {"pattern": f"needle-{index}"}}],
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_call_id": call_id,
                            "content": content,
                            "raw_output": content,
                            "log_id": f"log-{index}",
                        }
                    ],
                }
            )
        return messages

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
                {
                    "path": "open_somnia/config/settings.py",
                    "edits": [{"old_text": "a\n", "new_text": "a\nb\n"}],
                },
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

    def test_recent_tool_logs_and_render_tool_log_show_update_for_edit_file(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.tool_log_store = SimpleNamespace(
            list_recent=lambda limit=10: [
                {"id": "edit-log", "category": "TOOL", "actor": "lead", "tool_name": "edit_file"}
            ],
            get=lambda log_id: {
                "id": "edit-log",
                "category": "TOOL",
                "actor": "lead",
                "tool_name": "edit_file",
                "tool_input": {"path": "demo.txt", "edits": [{"old_text": "a", "new_text": "b"}]},
                "output": {"status": "ok", "path": "demo.txt"},
            }
            if log_id == "edit-log"
            else None,
        )
        runtime.settings = SimpleNamespace(workspace_root=Path("D:/workspace"))

        recent = OpenAgentRuntime.recent_tool_logs(runtime, limit=5)
        rendered_log = OpenAgentRuntime.render_tool_log(runtime, "edit-log")

        self.assertIn("-> Update", recent)
        self.assertNotIn("-> edit_file", recent)
        self.assertIn("Tool: Update", rendered_log)
        self.assertNotIn("Tool: edit_file", rendered_log)

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
        runtime.current_working_file_context = lambda: (
            "Active working file cache:\n"
            "- Path: frontend/src/App.tsx\n"
            "- Source: edit_file\n"
            "Cached snapshot:\n1: const App = () => null"
        )

        prompt = OpenAgentRuntime.build_system_prompt(runtime)

        self.assertIn("Execution environment:", prompt)
        self.assertIn("Tool behavior:", prompt)
        self.assertIn("Workspace:", prompt)
        self.assertIn("bash", prompt)
        self.assertIn("Prefer dedicated tools over `bash`", prompt)
        self.assertIn("prefer `project_scan` or a focused `tree`", prompt)
        self.assertIn("Use `find_symbol` to locate classes", prompt)
        self.assertIn("Use `glob` instead of shell file discovery commands", prompt)
        self.assertIn("Use `grep` instead of shell content search commands", prompt)
        self.assertIn("Do not start with broad `glob` patterns such as `**/*`", prompt)
        self.assertIn("Before `read_file` or `edit_file`, confirm the exact path", prompt)
        self.assertIn("always wrap replacements as `edits=[{old_text,new_text}, ...]`", prompt)
        self.assertIn("Use `TodoWrite` to break down meaningful work", prompt)
        self.assertIn("Use `edit_file` with `edits=[...]` for every text replacement", prompt)
        self.assertIn("use the returned updated snippet or active working file cache", prompt)
        self.assertIn("Do not claim a root cause", prompt)
        self.assertIn("If you keep rereading the same file or area", prompt)
        self.assertIn("Active provider: openai", prompt)
        self.assertIn("Active model: kimi-k2.5", prompt)
        self.assertIn("Active working file cache:", prompt)
        self.assertIn("frontend/src/App.tsx", prompt)
        self.assertIn("Current mode: ⏸ plan mode on.", prompt)
        self.assertIn("Return a concrete implementation plan", prompt)
        self.assertIn("request_mode_switch", prompt)
        self.assertIn("Use subagent for isolated subagent work.", prompt)
        self.assertIn("Do not claim to be Claude", prompt)

    def test_build_system_prompt_does_not_include_removed_exploration_memory_sections(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=Path("D:/workspace"),
            agent=SimpleNamespace(system_prompt=None, name="Somnia"),
            provider=SimpleNamespace(name="openai", model="gpt-5"),
        )
        runtime.execution_mode = "accept_edits"
        runtime.skill_loader = SimpleNamespace(descriptions=lambda: "none")

        prompt = OpenAgentRuntime.build_system_prompt(runtime, session=AgentSession(id="session-1"))

        self.assertNotIn("Repository memory:", prompt)
        self.assertNotIn("Session exploration memory:", prompt)

    def test_agent_session_ignores_legacy_exploration_cache_payload(self) -> None:
        restored = AgentSession.from_payload(
            {
                "id": "session-1",
                "messages": [],
                "exploration_cache": {
                    "last_project_scan": {"path": "."},
                },
            }
        )

        self.assertEqual(restored.id, "session-1")
        self.assertFalse(hasattr(restored, "exploration_cache"))

    def test_agent_session_roundtrips_token_usage(self) -> None:
        session = AgentSession(
            id="session-1",
            token_usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

        restored = AgentSession.from_payload(session.to_payload())

        self.assertEqual(restored.token_usage["input_tokens"], 10)
        self.assertEqual(restored.token_usage["output_tokens"], 5)
        self.assertEqual(restored.token_usage["total_tokens"], 15)

    def test_agent_session_roundtrips_read_file_overlap_state(self) -> None:
        session = AgentSession(
            id="session-1",
            read_file_overlap_state={
                "source_tool_call_ids": ["call-2"],
                "coverage": {"demo.txt": [[1, 10]]},
            },
        )

        restored = AgentSession.from_payload(session.to_payload())

        self.assertEqual(restored.read_file_overlap_state["source_tool_call_ids"], ["call-2"])
        self.assertEqual(restored.read_file_overlap_state["coverage"]["demo.txt"], [[1, 10]])

    def test_request_original_context_returns_tool_log_output(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.tool_log_store = SimpleNamespace(
            get=lambda log_id: {
                "tool_name": "bash",
                "output": "full original output",
            }
            if log_id == "log-1"
            else None
        )

        restored = OpenAgentRuntime.request_original_context(runtime, "log-1")
        missing = OpenAgentRuntime.request_original_context(runtime, "missing")

        self.assertIn("[Restored tool output | bash | log log-1]", restored)
        self.assertIn("full original output", restored)
        self.assertIn("No tool log found", missing)

    def test_extract_recent_topic_context_collects_recent_files_symbols_and_keywords(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        messages = [
            {"role": "user", "content": "Please inspect open_somnia/runtime/agent.py and the request_original_context tool."},
            {"role": "assistant", "content": "I will compare context_window_usage with build_payload_messages."},
            {"role": "user", "content": "<background-results>\nignore this\n</background-results>"},
            {"role": "user", "content": "Also check tests/test_compact.py for SemanticCompressionDecision coverage."},
        ]

        topic = OpenAgentRuntime._extract_recent_topic_context(runtime, messages)

        self.assertIn("open_somnia/runtime/agent.py", topic["active_files"])
        self.assertIn("tests/test_compact.py", topic["active_files"])
        self.assertIn("request_original_context", topic["active_symbols"])
        self.assertIn("context_window_usage", topic["active_symbols"])
        self.assertIn("semanticcompressiondecision", {value.lower() for value in topic["keywords"]})

    def test_fallback_context_relevance_decisions_respects_error_pwd_and_relevant_read_file(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        session = AgentSession(
            id="session-1",
            todo_items=[
                {"content": "inspect agent runtime", "status": "in_progress", "activeForm": "inspecting agent runtime"},
                {"content": "old directory walk", "status": "completed", "activeForm": "completed old walk"},
            ],
        )
        topic_context = {
            "active_files": ["open_somnia/runtime/agent.py"],
            "active_symbols": ["request_original_context"],
            "keywords": ["agent", "runtime", "request_original_context"],
        }
        candidates = [
            self._candidate(
                message_index=1,
                item_index=0,
                tool_name="bash",
                content="Traceback: RuntimeError connection failed in open_somnia/runtime/agent.py",
                tool_input={"command": "pytest tests/test_runtime_tool_output.py"},
                log_id="err-log",
                has_error=True,
            ),
            self._candidate(
                message_index=3,
                item_index=0,
                tool_name="pwd",
                content="D:/Project/Git/somnia",
                tool_input={"command": "pwd"},
                log_id="pwd-log",
                age=5,
            ),
            self._candidate(
                message_index=5,
                item_index=0,
                tool_name="read_file",
                content="def request_original_context(self, log_id: str) -> str:\n    ...",
                tool_input={"path": "open_somnia/runtime/agent.py"},
                log_id="read-log",
            ),
        ]

        decisions = OpenAgentRuntime._fallback_context_relevance_decisions(runtime, session, candidates, topic_context)
        by_locator = {(item.message_index, item.item_index): item for item in decisions}

        self.assertEqual(by_locator[(1, 0)].state, "original")
        self.assertEqual(by_locator[(3, 0)].state, "evicted")
        self.assertIn("[Context Evicted | pwd | log pwd-log]", by_locator[(3, 0)].summary)
        self.assertEqual(by_locator[(5, 0)].state, "original")

    def test_fallback_context_relevance_decisions_evicts_stale_read_for_same_path(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        session = AgentSession(id="session-1")
        topic_context = {
            "active_files": ["frontend/src/App.tsx"],
            "active_symbols": ["renderSidebar"],
            "keywords": ["sidebar", "selection"],
        }
        candidates = [
            self._candidate(
                message_index=1,
                item_index=0,
                tool_name="read_file",
                content="old file snapshot",
                tool_input={"path": "frontend/src/App.tsx"},
                log_id="read-old",
                age=6,
            ),
            self._candidate(
                message_index=3,
                item_index=0,
                tool_name="edit_file",
                content='{"status":"ok"}',
                tool_input={
                    "path": "frontend/src/App.tsx",
                    "edits": [{"old_text": "old", "new_text": "new"}],
                },
                log_id="edit-new",
                age=4,
            ),
        ]

        decisions = OpenAgentRuntime._fallback_context_relevance_decisions(runtime, session, candidates, topic_context)
        by_locator = {(item.message_index, item.item_index): item for item in decisions}

        self.assertEqual(by_locator[(1, 0)].state, "evicted")
        self.assertIn("[Context Evicted | read_file | log read-old]", by_locator[(1, 0)].summary)
        self.assertEqual(by_locator[(3, 0)].state, "original")

    def test_run_semantic_janitor_primes_cached_payload(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            provider=SimpleNamespace(name="openai", model="gpt-4.1", context_window_tokens=100_000),
            runtime=SimpleNamespace(janitor_trigger_ratio=0.6),
        )
        runtime.provider = SimpleNamespace(
            count_tokens=lambda system_prompt, messages, tools: 70_000 if "Semantic Summary" not in str(messages) else 55_000,
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 100_000,
        )
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.worker_registry = SimpleNamespace(schemas=lambda: [])
        runtime.execution_mode = "accept_edits"
        runtime._context_usage_cache = {}
        runtime._payload_message_cache = {}
        runtime._context_governance_events = {}
        runtime._count_payload_usage = OpenAgentRuntime._count_payload_usage.__get__(runtime, OpenAgentRuntime)
        runtime._payload_message_cache_key = OpenAgentRuntime._payload_message_cache_key.__get__(runtime, OpenAgentRuntime)
        runtime._context_usage_tools = OpenAgentRuntime._context_usage_tools.__get__(runtime, OpenAgentRuntime)
        runtime._should_run_context_janitor = OpenAgentRuntime._should_run_context_janitor.__get__(runtime, OpenAgentRuntime)
        runtime._note_context_governance = OpenAgentRuntime._note_context_governance.__get__(runtime, OpenAgentRuntime)
        runtime._context_usage_cache_key = OpenAgentRuntime._context_usage_cache_key.__get__(runtime, OpenAgentRuntime)
        runtime.build_system_prompt = lambda actor="lead", role="lead coding agent", session=None: "system"
        runtime._analyze_context_relevance = lambda **kwargs: [
            SemanticCompressionDecision(
                message_index=1,
                item_index=0,
                state="condensed",
                summary="[Semantic Summary | read_file | log log-1] Latest file snapshot already captured.",
            )
        ]
        session = AgentSession(
            id="session-1",
            messages=[
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-1", "name": "read_file", "input": {"path": "demo.txt"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-1", "content": "x" * 1200, "raw_output": "x" * 1200, "log_id": "log-1"}]},
            ],
        )

        message = OpenAgentRuntime.run_semantic_janitor(runtime, session)
        cache_key, cached_payload = runtime._payload_message_cache["session-1"]
        _, cached_usage = runtime._context_usage_cache["session-1"]

        self.assertIn("Janitor reviewed", message)
        self.assertIsInstance(cache_key, tuple)
        self.assertIn("[Semantic Summary | read_file | log log-1]", cached_payload[1]["content"][0]["content"])
        self.assertEqual(cached_usage.used_tokens, 55_000)
        self.assertIn("[Semantic Summary | read_file | log log-1]", session.messages[1]["content"][0]["content"])
        self.assertEqual(session.messages[1]["content"][0]["semantic_state"], "condensed")
        self.assertNotIn("raw_output", session.messages[1]["content"][0])

    def test_run_semantic_janitor_manual_command_runs_above_manual_threshold(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            provider=SimpleNamespace(name="openai", model="gpt-4.1", context_window_tokens=100_000),
            runtime=SimpleNamespace(janitor_trigger_ratio=0.6),
        )
        runtime.provider = SimpleNamespace(
            count_tokens=lambda system_prompt, messages, tools: 30_000 if "Semantic Summary" not in str(messages) else 24_000,
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 100_000,
        )
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.worker_registry = SimpleNamespace(schemas=lambda: [])
        runtime.execution_mode = "accept_edits"
        runtime._context_usage_cache = {}
        runtime._payload_message_cache = {}
        runtime._context_governance_events = {}
        runtime._count_payload_usage = OpenAgentRuntime._count_payload_usage.__get__(runtime, OpenAgentRuntime)
        runtime._payload_message_cache_key = OpenAgentRuntime._payload_message_cache_key.__get__(runtime, OpenAgentRuntime)
        runtime._context_usage_tools = OpenAgentRuntime._context_usage_tools.__get__(runtime, OpenAgentRuntime)
        runtime._note_context_governance = OpenAgentRuntime._note_context_governance.__get__(runtime, OpenAgentRuntime)
        runtime._context_usage_cache_key = OpenAgentRuntime._context_usage_cache_key.__get__(runtime, OpenAgentRuntime)
        runtime.build_system_prompt = lambda actor="lead", role="lead coding agent", session=None: "system"
        runtime._analyze_context_relevance = lambda **kwargs: [
            SemanticCompressionDecision(
                message_index=1,
                item_index=0,
                state="condensed",
                summary="[Semantic Summary | read_file | log log-1] Manual janitor reduced older snapshot.",
            )
        ]
        session = AgentSession(
            id="session-1",
            messages=[
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-1", "name": "read_file", "input": {"path": "demo.txt"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-1", "content": "x" * 1200, "raw_output": "x" * 1200, "log_id": "log-1"}]},
            ],
        )

        message = OpenAgentRuntime.run_semantic_janitor(runtime, session)

        self.assertIn("Janitor reviewed", message)
        self.assertIn("[Semantic Summary | read_file | log log-1]", session.messages[1]["content"][0]["content"])

    def test_run_semantic_janitor_manual_command_skips_below_manual_threshold(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            provider=SimpleNamespace(name="openai", model="gpt-4.1", context_window_tokens=100_000),
            runtime=SimpleNamespace(janitor_trigger_ratio=0.6),
        )
        runtime.provider = SimpleNamespace(
            count_tokens=lambda system_prompt, messages, tools: 19_000,
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 100_000,
        )
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.worker_registry = SimpleNamespace(schemas=lambda: [])
        runtime.execution_mode = "accept_edits"
        runtime._context_usage_cache = {}
        runtime._payload_message_cache = {}
        runtime.build_system_prompt = lambda actor="lead", role="lead coding agent", session=None: "system"
        session = AgentSession(id="session-1", messages=[{"role": "user", "content": "hello"}])

        message = OpenAgentRuntime.run_semantic_janitor(runtime, session)

        self.assertIn("below the manual 20% trigger", message)

    def test_parse_semantic_janitor_response_accepts_valid_json_and_ignores_extra_fields(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        candidates = [
            self._candidate(
                message_index=1,
                item_index=0,
                tool_name="grep",
                content="needle found",
                tool_input={"pattern": "needle"},
                log_id="grep-log",
            ),
            self._candidate(
                message_index=3,
                item_index=0,
                tool_name="pwd",
                content="D:/Project/Git/somnia",
                tool_input={"command": "pwd"},
                log_id="pwd-log",
            ),
        ]

        parsed = OpenAgentRuntime._parse_semantic_janitor_response(
            runtime,
            """```json
[
  {"message_index": 1, "item_index": 0, "state": "condensed", "summary": "Confirmed needle location.", "extra": "ignored"},
  {"message_index": 3, "item_index": 0, "state": "evicted", "why": "old pwd"},
  {"message_index": 999, "item_index": 0, "state": "original"}
]
```""",
            candidates,
        )

        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].state, "condensed")
        self.assertIn("[Semantic Summary | grep | log grep-log]", parsed[0].summary)
        self.assertEqual(parsed[1].state, "evicted")
        self.assertIn("[Context Evicted | pwd | log pwd-log]", parsed[1].summary)

    def test_parse_semantic_janitor_response_rejects_invalid_json_and_missing_fields(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        candidates = [self._candidate(message_index=1, item_index=0, tool_name="grep", content="needle found")]

        with self.assertRaises(Exception):
            OpenAgentRuntime._parse_semantic_janitor_response(runtime, "{not json", candidates)

        with self.assertRaises(Exception):
            OpenAgentRuntime._parse_semantic_janitor_response(
                runtime,
                '[{"message_index": 1, "state": "condensed", "summary": "missing item index"}]',
                candidates,
            )

    def test_evicted_restore_end_to_end_returns_original_tool_output(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.tool_log_store = SimpleNamespace(
            get=lambda log_id: {
                "tool_name": "pwd",
                "output": "D:/Project/Git/somnia",
            }
            if log_id == "pwd-log"
            else None
        )
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": "call-1", "name": "pwd", "input": {"command": "pwd"}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-1",
                        "content": "D:/Project/Git/somnia",
                        "raw_output": "D:/Project/Git/somnia",
                        "log_id": "pwd-log",
                    }
                ],
            },
        ]

        payload = build_payload_messages(
            messages,
            semantic_decisions=[
                SemanticCompressionDecision(
                    message_index=1,
                    item_index=0,
                    state="evicted",
                    summary="[Context Evicted | pwd | log pwd-log] Output removed from payload. Use request_original_context if needed.",
                )
            ],
        )
        restored = OpenAgentRuntime.request_original_context(runtime, "pwd-log")

        self.assertIn("[Context Evicted | pwd | log pwd-log]", payload[1]["content"][0]["content"])
        self.assertIn("[Restored tool output | pwd | log pwd-log]", restored)
        self.assertIn("D:/Project/Git/somnia", restored)

    def test_build_payload_messages_dedupes_large_duplicate_tool_results_and_keeps_latest_copy(self) -> None:
        duplicate_content = "x" * 400
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": "call-1", "name": "read_file", "input": {"path": "demo.txt", "limit": 120}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-1",
                        "content": duplicate_content,
                        "raw_output": duplicate_content,
                        "log_id": "log-1",
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": "call-2", "name": "read_file", "input": {"path": "demo.txt", "limit": 120}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-2",
                        "content": duplicate_content,
                        "raw_output": duplicate_content,
                        "log_id": "log-2",
                    }
                ],
            },
        ]

        payload = build_payload_messages(messages)

        self.assertEqual(
            payload[1]["content"][0]["content"],
            "[Duplicate tool result omitted | read_file] Identical output appears later.",
        )
        self.assertEqual(payload[3]["content"][0]["content"], duplicate_content)
        self.assertNotIn("raw_output", payload[1]["content"][0])
        self.assertNotIn("log_id", payload[1]["content"][0])
        self.assertIn("raw_output", messages[1]["content"][0])
        self.assertEqual(messages[1]["content"][0]["content"], duplicate_content)

    def test_build_payload_messages_omits_older_read_file_result_fully_covered_by_later_range(self) -> None:
        older_content = "\n".join(f"line {index}" for index in range(3, 9))
        newer_content = "\n".join(f"line {index}" for index in range(1, 11))
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_call",
                        "id": "call-1",
                        "name": "read_file",
                        "input": {"path": "demo.txt", "start_line": 3, "end_line": 8},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-1",
                        "content": older_content,
                        "raw_output": older_content,
                        "log_id": "log-1",
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_call",
                        "id": "call-2",
                        "name": "read_file",
                        "input": {"path": "demo.txt", "start_line": 1, "end_line": 10},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-2",
                        "content": newer_content,
                        "raw_output": newer_content,
                        "log_id": "log-2",
                    }
                ],
            },
        ]

        payload = build_payload_messages(messages)

        self.assertEqual(
            payload[1]["content"][0]["content"],
            "[Overlapping read_file result omitted | demo.txt:3-8] Covered by later read(s) of the same file.",
        )
        self.assertEqual(payload[3]["content"][0]["content"], newer_content)
        self.assertEqual(messages[1]["content"][0]["content"], older_content)

    def test_build_payload_messages_prunes_partial_read_file_overlap_and_keeps_unique_lines(self) -> None:
        older_content = (
            "... (2 lines omitted before line 3)\n"
            "line 3\n"
            "line 4\n"
            "line 5\n"
            "line 6\n"
            "line 7\n"
            "line 8\n"
            "... (2 more lines after line 8)"
        )
        newer_content = (
            "... (3 lines omitted before line 4)\n"
            "line 4\n"
            "line 5\n"
            "line 6\n"
            "... (4 more lines after line 6)"
        )
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_call",
                        "id": "call-1",
                        "name": "read_file",
                        "input": {"path": "demo.txt", "start_line": 3, "end_line": 8},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-1",
                        "content": older_content,
                        "raw_output": older_content,
                        "log_id": "log-1",
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_call",
                        "id": "call-2",
                        "name": "read_file",
                        "input": {"path": "demo.txt", "start_line": 4, "end_line": 6},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-2",
                        "content": newer_content,
                        "raw_output": newer_content,
                        "log_id": "log-2",
                    }
                ],
            },
        ]

        payload = build_payload_messages(messages)

        self.assertEqual(
            payload[1]["content"][0]["content"],
            (
                "... (2 lines omitted before line 3)\n"
                "line 3\n"
                "... (3 overlapping lines omitted here; covered by later read(s) of the same file, lines 4-6)\n"
                "line 7\n"
                "line 8\n"
                "... (2 more lines after line 8)"
            ),
        )
        self.assertEqual(payload[3]["content"][0]["content"], newer_content)
        self.assertEqual(messages[1]["content"][0]["content"], older_content)

    def test_build_payload_messages_skips_overlap_pruning_when_latest_round_has_no_read_file(self) -> None:
        older_content = "\n".join(f"line {index}" for index in range(3, 9))
        newer_content = "\n".join(f"line {index}" for index in range(1, 11))
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_call",
                        "id": "call-1",
                        "name": "read_file",
                        "input": {"path": "demo.txt", "start_line": 3, "end_line": 8},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-1",
                        "content": older_content,
                        "raw_output": older_content,
                        "log_id": "log-1",
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_call",
                        "id": "call-2",
                        "name": "read_file",
                        "input": {"path": "demo.txt", "start_line": 1, "end_line": 10},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-2",
                        "content": newer_content,
                        "raw_output": newer_content,
                        "log_id": "log-2",
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": "call-3", "name": "grep", "input": {"path": "demo.txt", "pattern": "needle"}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_call_id": "call-3", "content": "demo.txt:5: needle", "raw_output": "demo.txt:5: needle", "log_id": "log-3"}],
            },
        ]

        payload = build_payload_messages(messages)

        self.assertEqual(payload[1]["content"][0]["content"], older_content)
        self.assertEqual(payload[3]["content"][0]["content"], newer_content)

    def test_build_payload_messages_uses_persisted_read_file_overlap_state_after_trailing_non_read_file_round(self) -> None:
        older_content = "\n".join(f"line {index}" for index in range(3, 9))
        newer_content = "\n".join(f"line {index}" for index in range(1, 11))
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_call",
                        "id": "call-1",
                        "name": "read_file",
                        "input": {"path": "demo.txt", "start_line": 3, "end_line": 8},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-1",
                        "content": older_content,
                        "raw_output": older_content,
                        "log_id": "log-1",
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_call",
                        "id": "call-2",
                        "name": "read_file",
                        "input": {"path": "demo.txt", "start_line": 1, "end_line": 10},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-2",
                        "content": newer_content,
                        "raw_output": newer_content,
                        "log_id": "log-2",
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": "call-3", "name": "TodoWrite", "input": {"items": []}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_call_id": "call-3", "content": "ok", "raw_output": "ok", "log_id": "log-3"}],
            },
        ]

        payload = build_payload_messages(
            messages,
            read_file_overlap_state={
                "source_tool_call_ids": ["call-2"],
                "coverage": {"demo.txt": [[1, 10]]},
            },
        )

        self.assertEqual(
            payload[1]["content"][0]["content"],
            "[Overlapping read_file result omitted | demo.txt:3-8] Covered by later read(s) of the same file.",
        )
        self.assertEqual(payload[3]["content"][0]["content"], newer_content)
        self.assertEqual(payload[5]["content"][0]["content"], "ok")

    def test_messages_for_model_accepts_explicit_read_file_overlap_state_for_transient_payloads(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        older_content = "\n".join(f"line {index}" for index in range(3, 9))
        newer_content = "\n".join(f"line {index}" for index in range(1, 11))
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_call",
                        "id": "call-1",
                        "name": "read_file",
                        "input": {"path": "demo.txt", "start_line": 3, "end_line": 8},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-1",
                        "content": older_content,
                        "raw_output": older_content,
                        "log_id": "log-1",
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_call",
                        "id": "call-2",
                        "name": "read_file",
                        "input": {"path": "demo.txt", "start_line": 1, "end_line": 10},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-2",
                        "content": newer_content,
                        "raw_output": newer_content,
                        "log_id": "log-2",
                    }
                ],
            },
            {"role": "user", "content": OpenAgentRuntime.TODO_REMINDER_TEXT},
        ]

        payload = OpenAgentRuntime._messages_for_model(
            runtime,
            messages,
            session=None,
            read_file_overlap_state={
                "source_tool_call_ids": ["call-2"],
                "coverage": {"demo.txt": [[1, 10]]},
            },
        )

        self.assertEqual(
            payload[1]["content"][0]["content"],
            "[Overlapping read_file result omitted | demo.txt:3-8] Covered by later read(s) of the same file.",
        )
        self.assertEqual(payload[3]["content"][0]["content"], newer_content)
        self.assertEqual(payload[4]["content"], OpenAgentRuntime.TODO_REMINDER_TEXT)

    def test_build_payload_messages_prunes_only_paths_read_in_latest_round(self) -> None:
        older_demo = "\n".join(f"demo {index}" for index in range(3, 9))
        newer_demo = "\n".join(f"demo {index}" for index in range(1, 11))
        older_other = "\n".join(f"other {index}" for index in range(3, 9))
        newer_other = "\n".join(f"other {index}" for index in range(1, 11))
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": "call-1", "name": "read_file", "input": {"path": "demo.txt", "start_line": 3, "end_line": 8}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_call_id": "call-1", "content": older_demo, "raw_output": older_demo, "log_id": "log-1"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": "call-2", "name": "read_file", "input": {"path": "other.txt", "start_line": 3, "end_line": 8}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_call_id": "call-2", "content": older_other, "raw_output": older_other, "log_id": "log-2"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": "call-3", "name": "read_file", "input": {"path": "other.txt", "start_line": 1, "end_line": 10}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_call_id": "call-3", "content": newer_other, "raw_output": newer_other, "log_id": "log-3"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": "call-4", "name": "read_file", "input": {"path": "demo.txt", "start_line": 1, "end_line": 10}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_call_id": "call-4", "content": newer_demo, "raw_output": newer_demo, "log_id": "log-4"}],
            },
        ]

        payload = build_payload_messages(messages)

        self.assertEqual(
            payload[1]["content"][0]["content"],
            "[Overlapping read_file result omitted | demo.txt:3-8] Covered by later read(s) of the same file.",
        )
        self.assertEqual(payload[3]["content"][0]["content"], older_other)
        self.assertEqual(payload[5]["content"][0]["content"], newer_other)
        self.assertEqual(payload[7]["content"][0]["content"], newer_demo)

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
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmpdir:
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

    def test_context_window_usage_does_not_apply_semantic_janitor_side_effects_when_threshold_crossed(self) -> None:
        captured_messages: list[dict] = []
        analyzer_calls: list[int] = []
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            provider=SimpleNamespace(name="openai", model="gpt-4.1", context_window_tokens=100_000),
            runtime=SimpleNamespace(janitor_trigger_ratio=0.6),
        )

        def _count_tokens(system_prompt, messages, tools):
            captured_messages.clear()
            captured_messages.extend(messages)
            return 70_000

        runtime.provider = SimpleNamespace(
            count_tokens=_count_tokens,
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 100_000,
        )
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.worker_registry = SimpleNamespace(schemas=lambda: [])
        runtime.build_system_prompt = lambda actor="lead", role="lead coding agent", session=None: "system"
        runtime.execution_mode = "accept_edits"
        runtime._context_usage_cache = {}
        runtime._payload_message_cache = {}
        runtime._analyze_context_relevance = lambda **kwargs: analyzer_calls.append(len(kwargs["messages"])) or []
        session = AgentSession(
            id="session-1",
            messages=[
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-1", "name": "bash", "input": {"command": "ls -R"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-1", "content": "a" * 1000, "raw_output": "a" * 1000, "log_id": "log-call-1"}]},
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-2", "name": "grep", "input": {"pattern": "needle"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-2", "content": "needle", "raw_output": "needle", "log_id": "log-call-2"}]},
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-3", "name": "read_file", "input": {"path": "main.py"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-3", "content": "print('hello')", "raw_output": "print('hello')", "log_id": "log-call-3"}]},
            ],
        )

        usage = OpenAgentRuntime.context_window_usage(runtime, session)

        self.assertEqual(usage.used_tokens, 70_000)
        self.assertEqual(captured_messages[1]["content"][0]["content"], "a" * 1000)
        self.assertEqual(session.messages[1]["content"][0]["content"], "a" * 1000)
        self.assertNotIn("semantic_state", session.messages[1]["content"][0])
        self.assertIn("raw_output", session.messages[1]["content"][0])
        self.assertEqual(session.messages[1]["content"][0]["log_id"], "log-call-1")
        self.assertEqual(analyzer_calls, [])

    def test_run_turn_runs_auto_janitor_before_agent_loop_and_includes_current_user_message_in_topic(self) -> None:
        analyzer_inputs: list[str] = []
        loop_messages: list[dict] = []
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            provider=SimpleNamespace(name="openai", model="gpt-4.1", context_window_tokens=100_000),
            runtime=SimpleNamespace(janitor_trigger_ratio=0.6),
        )
        runtime.provider = SimpleNamespace(
            count_tokens=lambda system_prompt, messages, tools: 70_000 if "Semantic Summary" not in str(messages) else 55_000,
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 100_000,
        )
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.worker_registry = SimpleNamespace(schemas=lambda: [])
        runtime.build_system_prompt = lambda actor="lead", role="lead coding agent", session=None: "system"
        runtime.execution_mode = "accept_edits"
        runtime._context_usage_cache = {}
        runtime._payload_message_cache = {}
        runtime._recent_context_usage = {}
        runtime._context_governance_events = {}
        runtime._janitor_state = {}
        runtime._count_payload_usage = OpenAgentRuntime._count_payload_usage.__get__(runtime, OpenAgentRuntime)
        runtime._payload_message_cache_key = OpenAgentRuntime._payload_message_cache_key.__get__(runtime, OpenAgentRuntime)
        runtime._context_usage_tools = OpenAgentRuntime._context_usage_tools.__get__(runtime, OpenAgentRuntime)
        runtime._should_run_context_janitor = OpenAgentRuntime._should_run_context_janitor.__get__(runtime, OpenAgentRuntime)
        runtime._note_context_governance = OpenAgentRuntime._note_context_governance.__get__(runtime, OpenAgentRuntime)
        runtime._context_usage_cache_key = OpenAgentRuntime._context_usage_cache_key.__get__(runtime, OpenAgentRuntime)
        runtime._remember_context_usage = OpenAgentRuntime._remember_context_usage.__get__(runtime, OpenAgentRuntime)
        runtime._record_context_janitor_run = OpenAgentRuntime._record_context_janitor_run.__get__(runtime, OpenAgentRuntime)
        runtime._janitor_state_for = OpenAgentRuntime._janitor_state_for.__get__(runtime, OpenAgentRuntime)
        runtime._count_prunable_janitor_candidates = OpenAgentRuntime._count_prunable_janitor_candidates.__get__(runtime, OpenAgentRuntime)
        runtime._janitor_candidates = OpenAgentRuntime._janitor_candidates.__get__(runtime, OpenAgentRuntime)
        runtime._semantic_janitor_trigger_ratio = OpenAgentRuntime._semantic_janitor_trigger_ratio.__get__(runtime, OpenAgentRuntime)
        runtime._janitor_preemptive_compact_ratio = OpenAgentRuntime._janitor_preemptive_compact_ratio.__get__(runtime, OpenAgentRuntime)
        runtime._run_automatic_context_janitor = OpenAgentRuntime._run_automatic_context_janitor.__get__(runtime, OpenAgentRuntime)
        runtime.transcript_store = SimpleNamespace(append=lambda *args, **kwargs: None)

        def _analyze(**kwargs):
            visible_messages = kwargs["messages"]
            analyzer_inputs.append(str(visible_messages[-1]["content"]))
            return [
                SemanticCompressionDecision(
                    message_index=1,
                    item_index=0,
                    state="condensed",
                    summary="[Semantic Summary | bash | log log-call-1] Earlier directory scan already reviewed.",
                )
            ]

        runtime._analyze_context_relevance = _analyze
        runtime._agent_loop = lambda session, **kwargs: loop_messages.extend(session.messages) or "loop-result"
        session = AgentSession(
            id="session-1",
            messages=[
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-1", "name": "bash", "input": {"command": "ls -R"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-1", "content": "a" * 1000, "raw_output": "a" * 1000, "log_id": "log-call-1"}]},
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-2", "name": "grep", "input": {"pattern": "needle"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-2", "content": "needle", "raw_output": "needle", "log_id": "log-call-2"}]},
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-3", "name": "read_file", "input": {"path": "main.py"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-3", "content": "print('hello')", "raw_output": "print('hello')", "log_id": "log-call-3"}]},
                {"role": "assistant", "content": "Ready for the next request."},
            ],
        )

        result = OpenAgentRuntime.run_turn(runtime, session, "please continue in main.py")

        self.assertEqual(result, "loop-result")
        self.assertEqual(analyzer_inputs, ["please continue in main.py"])
        self.assertEqual(session.messages[1]["content"][0]["semantic_state"], "condensed")
        self.assertIn("[Semantic Summary | bash | log log-call-1]", session.messages[1]["content"][0]["content"])
        self.assertEqual(loop_messages[-1]["content"], "please continue in main.py")

    def test_run_turn_skips_topic_shift_detection_below_manual_janitor_threshold(self) -> None:
        detection_calls: list[str] = []
        transcript_entries: list[dict] = []
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            provider=SimpleNamespace(name="openai", model="gpt-4.1", context_window_tokens=100_000),
            runtime=SimpleNamespace(janitor_trigger_ratio=0.6),
        )
        runtime.provider = SimpleNamespace(
            count_tokens=lambda system_prompt, messages, tools: 15_000,
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 100_000,
        )
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.worker_registry = SimpleNamespace(schemas=lambda: [])
        runtime.build_system_prompt = lambda actor="lead", role="lead coding agent", session=None: "system"
        runtime.execution_mode = "accept_edits"
        runtime._context_usage_cache = {}
        runtime._payload_message_cache = {}
        runtime._recent_context_usage = {}
        runtime._context_governance_events = {}
        runtime._janitor_state = {}
        runtime.transcript_store = SimpleNamespace(append=lambda session_id, payload: transcript_entries.append(payload))
        runtime._detect_topic_shift = lambda **kwargs: detection_calls.append(kwargs["latest_user_message"]) or (True, "shift")
        runtime._agent_loop = lambda session, **kwargs: "loop-result"

        session = AgentSession(id="session-1", messages=[{"role": "assistant", "content": "Ready."}])

        result = OpenAgentRuntime.run_turn(runtime, session, "new question")

        self.assertEqual(result, "loop-result")
        self.assertEqual(detection_calls, [])
        self.assertEqual(transcript_entries, [{"role": "user", "content": "new question"}])

    def test_run_turn_topic_shift_detection_can_trigger_janitor_without_polluting_transcript(self) -> None:
        detection_calls: list[str] = []
        loop_messages: list[dict] = []
        transcript_entries: list[dict] = []
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            provider=SimpleNamespace(name="openai", model="gpt-4.1", context_window_tokens=100_000),
            runtime=SimpleNamespace(janitor_trigger_ratio=0.6),
        )
        runtime.provider = SimpleNamespace(
            count_tokens=lambda system_prompt, messages, tools: 30_000 if "Semantic Summary" not in str(messages) else 22_000,
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 100_000,
        )
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.worker_registry = SimpleNamespace(schemas=lambda: [])
        runtime.build_system_prompt = lambda actor="lead", role="lead coding agent", session=None: "system"
        runtime.execution_mode = "accept_edits"
        runtime._context_usage_cache = {}
        runtime._payload_message_cache = {}
        runtime._recent_context_usage = {}
        runtime._context_governance_events = {}
        runtime._janitor_state = {}
        runtime.transcript_store = SimpleNamespace(append=lambda session_id, payload: transcript_entries.append(payload))
        runtime._detect_topic_shift = lambda **kwargs: detection_calls.append(kwargs["latest_user_message"]) or (True, "new topic")
        runtime._agent_loop = lambda session, **kwargs: loop_messages.extend(session.messages) or "loop-result"

        def _analyze(**kwargs):
            return [
                SemanticCompressionDecision(
                    message_index=1,
                    item_index=0,
                    state="condensed",
                    summary="[Semantic Summary | bash | log log-call-1] Earlier directory scan already reviewed.",
                )
            ]

        runtime._analyze_context_relevance = _analyze
        session = AgentSession(
            id="session-1",
            messages=[
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-1", "name": "bash", "input": {"command": "ls -R"}, "importance": "glance"}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-1", "content": "a" * 1000, "raw_output": "a" * 1000, "log_id": "log-call-1"}]},
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-2", "name": "grep", "input": {"pattern": "needle"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-2", "content": "needle", "raw_output": "needle", "log_id": "log-call-2"}]},
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-3", "name": "read_file", "input": {"path": "main.py"}, "importance": "foundation"}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-3", "content": "print('hello')", "raw_output": "print('hello')", "log_id": "log-call-3"}]},
                {"role": "assistant", "content": "Ready for the next request."},
            ],
        )

        result = OpenAgentRuntime.run_turn(runtime, session, "now switch to auth.py")

        self.assertEqual(result, "loop-result")
        self.assertEqual(detection_calls, ["now switch to auth.py"])
        self.assertEqual(session.messages[1]["content"][0]["semantic_state"], "condensed")
        self.assertIn("[Semantic Summary | bash | log log-call-1]", session.messages[1]["content"][0]["content"])
        self.assertEqual(loop_messages[-1]["content"], "now switch to auth.py")
        self.assertEqual(transcript_entries, [{"role": "user", "content": "now switch to auth.py"}])

    def test_topic_shift_candidate_pressure_ignores_foundation_only_candidates(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime._janitor_candidates = OpenAgentRuntime._janitor_candidates.__get__(runtime, OpenAgentRuntime)
        runtime._topic_shift_candidate_pressure = OpenAgentRuntime._topic_shift_candidate_pressure.__get__(runtime, OpenAgentRuntime)
        runtime._tool_importance_review_priority = OpenAgentRuntime._tool_importance_review_priority.__get__(runtime, OpenAgentRuntime)
        runtime.JANITOR_PRUNABLE_OUTPUT_CHARS = OpenAgentRuntime.JANITOR_PRUNABLE_OUTPUT_CHARS

        messages = [
            {"role": "assistant", "content": [{"type": "tool_call", "id": "call-1", "name": "read_file", "input": {"path": "main.py"}, "importance": "foundation"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-1", "content": "x" * 800, "raw_output": "x" * 800, "log_id": "log-1"}]},
            {"role": "assistant", "content": [{"type": "tool_call", "id": "call-2", "name": "grep", "input": {"pattern": "needle"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-2", "content": "recent", "raw_output": "recent", "log_id": "log-2"}]},
            {"role": "assistant", "content": [{"type": "tool_call", "id": "call-3", "name": "grep", "input": {"pattern": "other"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-3", "content": "most recent", "raw_output": "most recent", "log_id": "log-3"}]},
        ]

        pressure = OpenAgentRuntime._topic_shift_candidate_pressure(runtime, messages)

        self.assertEqual(pressure, 0)

    def test_candidate_relevance_score_applies_importance_bonus(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime._tool_candidate_haystack = OpenAgentRuntime._tool_candidate_haystack.__get__(runtime, OpenAgentRuntime)
        runtime._tool_importance_preservation_score = OpenAgentRuntime._tool_importance_preservation_score.__get__(runtime, OpenAgentRuntime)

        foundation_score = OpenAgentRuntime._candidate_relevance_score(
            runtime,
            self._candidate(message_index=1, item_index=0, tool_name="read_file", content="plain content", importance="foundation"),
            active_files=set(),
            active_symbols=set(),
            topic_tokens=set(),
            open_todo_tokens=set(),
            completed_todo_tokens=set(),
        )
        glance_score = OpenAgentRuntime._candidate_relevance_score(
            runtime,
            self._candidate(message_index=1, item_index=0, tool_name="read_file", content="plain content", importance="glance"),
            active_files=set(),
            active_symbols=set(),
            topic_tokens=set(),
            open_todo_tokens=set(),
            completed_todo_tokens=set(),
        )

        self.assertGreater(foundation_score, glance_score)

    def test_context_window_usage_cache_invalidates_after_session_messages_change(self) -> None:
        provider_calls: list[int] = []
        analyzer_calls: list[int] = []
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            provider=SimpleNamespace(name="openai", model="gpt-4.1", context_window_tokens=100_000),
            runtime=SimpleNamespace(janitor_trigger_ratio=0.6),
        )

        def _count_tokens(system_prompt, messages, tools):
            provider_calls.append(len(messages))
            return 70_000

        runtime.provider = SimpleNamespace(
            count_tokens=_count_tokens,
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 100_000,
        )
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.worker_registry = SimpleNamespace(schemas=lambda: [])
        runtime.build_system_prompt = lambda actor="lead", role="lead coding agent", session=None: "system"
        runtime.execution_mode = "accept_edits"
        runtime._context_usage_cache = {}
        runtime._payload_message_cache = {}

        def _analyze(**kwargs):
            analyzer_calls.append(len(kwargs["messages"]))
            return []

        runtime._analyze_context_relevance = _analyze
        session = AgentSession(id="session-1", messages=[{"role": "user", "content": "hello"}])

        first = OpenAgentRuntime.context_window_usage(runtime, session)
        second = OpenAgentRuntime.context_window_usage(runtime, session)
        session.messages.append({"role": "assistant", "content": "new reply"})
        third = OpenAgentRuntime.context_window_usage(runtime, session)

        self.assertEqual(first.used_tokens, 70_000)
        self.assertEqual(second.used_tokens, 70_000)
        self.assertEqual(third.used_tokens, 70_000)
        self.assertEqual(len(analyzer_calls), 0)
        self.assertEqual(analyzer_calls, [])
        self.assertEqual(len(provider_calls), 2)
        self.assertEqual(provider_calls, [1, 2])

    def test_recent_context_window_usage_returns_cached_snapshot_without_recount(self) -> None:
        provider_calls: list[int] = []
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            provider=SimpleNamespace(name="openai", model="gpt-4.1", context_window_tokens=100_000),
            runtime=SimpleNamespace(janitor_trigger_ratio=0.6),
        )

        def _count_tokens(system_prompt, messages, tools):
            provider_calls.append(len(messages))
            return 30_000

        runtime.provider = SimpleNamespace(
            count_tokens=_count_tokens,
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 100_000,
        )
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.worker_registry = SimpleNamespace(schemas=lambda: [])
        runtime.build_system_prompt = lambda actor="lead", role="lead coding agent", session=None: "system"
        runtime.execution_mode = "accept_edits"
        session = AgentSession(id="session-1", messages=[{"role": "user", "content": "hello"}])

        usage = OpenAgentRuntime.context_window_usage(runtime, session)
        recent = OpenAgentRuntime.recent_context_window_usage(runtime, session)

        self.assertEqual(usage.used_tokens, 30_000)
        self.assertIs(recent, usage)
        self.assertEqual(provider_calls, [1])

    def test_context_janitor_uses_cooldown_until_usage_grows_meaningfully(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.JANITOR_REARM_RATIO = OpenAgentRuntime.JANITOR_REARM_RATIO
        runtime.JANITOR_FORCE_RATIO = OpenAgentRuntime.JANITOR_FORCE_RATIO
        runtime.JANITOR_MIN_TOKEN_DELTA = OpenAgentRuntime.JANITOR_MIN_TOKEN_DELTA
        runtime.JANITOR_MIN_MESSAGE_DELTA = OpenAgentRuntime.JANITOR_MIN_MESSAGE_DELTA
        runtime._janitor_state = {}
        runtime._janitor_state_for = OpenAgentRuntime._janitor_state_for.__get__(runtime, OpenAgentRuntime)
        runtime._record_context_janitor_run = OpenAgentRuntime._record_context_janitor_run.__get__(runtime, OpenAgentRuntime)
        runtime._should_run_context_janitor = OpenAgentRuntime._should_run_context_janitor.__get__(runtime, OpenAgentRuntime)
        session = AgentSession(id="session-1", messages=[{"role": "user", "content": "hello"}] * 10)

        first = OpenAgentRuntime._should_run_context_janitor(
            runtime,
            ContextWindowUsage(used_tokens=60_000, max_tokens=100_000),
            session=session,
            message_count=10,
        )
        OpenAgentRuntime._record_context_janitor_run(
            runtime,
            session,
            ContextWindowUsage(used_tokens=60_000, max_tokens=100_000),
            ContextWindowUsage(used_tokens=54_000, max_tokens=100_000),
            message_count=10,
            automatic=True,
        )
        second = OpenAgentRuntime._should_run_context_janitor(
            runtime,
            ContextWindowUsage(used_tokens=61_000, max_tokens=100_000),
            session=session,
            message_count=12,
        )
        rearm = OpenAgentRuntime._should_run_context_janitor(
            runtime,
            ContextWindowUsage(used_tokens=43_000, max_tokens=100_000),
            session=session,
            message_count=12,
        )
        third = OpenAgentRuntime._should_run_context_janitor(
            runtime,
            ContextWindowUsage(used_tokens=61_000, max_tokens=100_000),
            session=session,
            message_count=13,
        )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertFalse(rearm)
        self.assertTrue(third)

    def test_context_janitor_skips_when_prunable_candidates_are_exhausted(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime._janitor_state = {}
        runtime._janitor_state_for = OpenAgentRuntime._janitor_state_for.__get__(runtime, OpenAgentRuntime)
        runtime._should_run_context_janitor = OpenAgentRuntime._should_run_context_janitor.__get__(runtime, OpenAgentRuntime)
        runtime._count_prunable_janitor_candidates = OpenAgentRuntime._count_prunable_janitor_candidates.__get__(runtime, OpenAgentRuntime)
        runtime._janitor_candidates = OpenAgentRuntime._janitor_candidates.__get__(runtime, OpenAgentRuntime)
        session = AgentSession(id="session-1", messages=self._tool_round_messages("a" * 400, "b" * 400))

        should_run = OpenAgentRuntime._should_run_context_janitor(
            runtime,
            ContextWindowUsage(used_tokens=60_000, max_tokens=100_000),
            session=session,
            message_count=len(session.messages),
            messages=session.messages,
        )

        self.assertFalse(should_run)
        self.assertTrue(runtime._janitor_state["session-1"]["saturated"])

    def test_context_janitor_skips_when_close_to_auto_compact_threshold(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime._janitor_state = {}
        runtime._janitor_state_for = OpenAgentRuntime._janitor_state_for.__get__(runtime, OpenAgentRuntime)
        runtime._should_run_context_janitor = OpenAgentRuntime._should_run_context_janitor.__get__(runtime, OpenAgentRuntime)
        runtime._count_prunable_janitor_candidates = OpenAgentRuntime._count_prunable_janitor_candidates.__get__(runtime, OpenAgentRuntime)
        runtime._janitor_candidates = OpenAgentRuntime._janitor_candidates.__get__(runtime, OpenAgentRuntime)
        runtime._semantic_janitor_trigger_ratio = OpenAgentRuntime._semantic_janitor_trigger_ratio.__get__(runtime, OpenAgentRuntime)
        runtime._janitor_preemptive_compact_ratio = OpenAgentRuntime._janitor_preemptive_compact_ratio.__get__(runtime, OpenAgentRuntime)
        session = AgentSession(id="session-1", messages=self._tool_round_messages("a" * 400, "b" * 400, "c" * 400, "d" * 400, "e" * 400))

        should_run = OpenAgentRuntime._should_run_context_janitor(
            runtime,
            ContextWindowUsage(used_tokens=80_000, max_tokens=100_000),
            session=session,
            message_count=len(session.messages),
            messages=session.messages,
        )

        self.assertFalse(should_run)

    def test_context_janitor_skips_when_usage_delta_since_last_run_is_too_small(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.JANITOR_REARM_RATIO = OpenAgentRuntime.JANITOR_REARM_RATIO
        runtime.JANITOR_FORCE_RATIO = OpenAgentRuntime.JANITOR_FORCE_RATIO
        runtime.JANITOR_MIN_TOKEN_DELTA = OpenAgentRuntime.JANITOR_MIN_TOKEN_DELTA
        runtime.JANITOR_MIN_MESSAGE_DELTA = OpenAgentRuntime.JANITOR_MIN_MESSAGE_DELTA
        runtime.JANITOR_MIN_USAGE_DELTA_RATIO = OpenAgentRuntime.JANITOR_MIN_USAGE_DELTA_RATIO
        runtime.JANITOR_MIN_USAGE_DELTA_TOKENS = OpenAgentRuntime.JANITOR_MIN_USAGE_DELTA_TOKENS
        runtime._janitor_state = {}
        runtime._janitor_state_for = OpenAgentRuntime._janitor_state_for.__get__(runtime, OpenAgentRuntime)
        runtime._should_run_context_janitor = OpenAgentRuntime._should_run_context_janitor.__get__(runtime, OpenAgentRuntime)
        runtime._count_prunable_janitor_candidates = OpenAgentRuntime._count_prunable_janitor_candidates.__get__(runtime, OpenAgentRuntime)
        runtime._janitor_candidates = OpenAgentRuntime._janitor_candidates.__get__(runtime, OpenAgentRuntime)
        runtime._semantic_janitor_trigger_ratio = OpenAgentRuntime._semantic_janitor_trigger_ratio.__get__(runtime, OpenAgentRuntime)
        runtime._janitor_preemptive_compact_ratio = OpenAgentRuntime._janitor_preemptive_compact_ratio.__get__(runtime, OpenAgentRuntime)
        session = AgentSession(id="session-1", messages=self._tool_round_messages("a" * 400, "b" * 400, "c" * 400, "d" * 400, "e" * 400))
        runtime._janitor_state["session-1"] = {
            "armed": True,
            "last_run_used_tokens": 50_000,
            "last_run_message_count": len(session.messages),
            "last_run_ratio": 0.50,
            "last_reduction_ratio": 0.20,
            "saturated": False,
            "auto_low_yield_streak": 0,
            "disabled": False,
        }

        should_run = OpenAgentRuntime._should_run_context_janitor(
            runtime,
            ContextWindowUsage(used_tokens=50_500, max_tokens=100_000),
            session=session,
            message_count=len(session.messages),
            messages=session.messages,
        )

        self.assertFalse(should_run)

    def test_context_janitor_single_low_yield_auto_run_disables_future_auto_janitor(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.JANITOR_LOW_YIELD_RATIO = OpenAgentRuntime.JANITOR_LOW_YIELD_RATIO
        runtime.JANITOR_LOW_YIELD_MAX_AUTO_RUNS = OpenAgentRuntime.JANITOR_LOW_YIELD_MAX_AUTO_RUNS
        runtime._janitor_state = {}
        runtime._janitor_state_for = OpenAgentRuntime._janitor_state_for.__get__(runtime, OpenAgentRuntime)
        runtime._record_context_janitor_run = OpenAgentRuntime._record_context_janitor_run.__get__(runtime, OpenAgentRuntime)
        session = AgentSession(id="session-1")

        OpenAgentRuntime._record_context_janitor_run(
            runtime,
            session,
            ContextWindowUsage(used_tokens=100_000, max_tokens=100_000),
            ContextWindowUsage(used_tokens=95_000, max_tokens=100_000),
            message_count=10,
            automatic=True,
        )

        self.assertEqual(runtime._janitor_state["session-1"]["auto_low_yield_streak"], 1)
        self.assertTrue(runtime._janitor_state["session-1"]["disabled"])

    def test_manual_janitor_run_does_not_count_toward_auto_low_yield_fuse(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.JANITOR_LOW_YIELD_RATIO = OpenAgentRuntime.JANITOR_LOW_YIELD_RATIO
        runtime.JANITOR_LOW_YIELD_MAX_AUTO_RUNS = OpenAgentRuntime.JANITOR_LOW_YIELD_MAX_AUTO_RUNS
        runtime._janitor_state = {}
        runtime._janitor_state_for = OpenAgentRuntime._janitor_state_for.__get__(runtime, OpenAgentRuntime)
        runtime._record_context_janitor_run = OpenAgentRuntime._record_context_janitor_run.__get__(runtime, OpenAgentRuntime)
        session = AgentSession(id="session-1")

        OpenAgentRuntime._record_context_janitor_run(
            runtime,
            session,
            ContextWindowUsage(used_tokens=100_000, max_tokens=100_000),
            ContextWindowUsage(used_tokens=99_000, max_tokens=100_000),
            message_count=10,
            automatic=False,
        )

        self.assertEqual(runtime._janitor_state["session-1"]["auto_low_yield_streak"], 0)
        self.assertFalse(runtime._janitor_state["session-1"]["disabled"])

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
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmpdir:
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

    def test_dump_provider_payload_if_enabled_writes_hidden_debug_artifact(self) -> None:
        root = self._stable_test_dir("provider-payload")
        data_dir = root / ".open_somnia"
        logs_dir = data_dir / "logs"
        transcripts_dir = data_dir / "transcripts"
        sessions_dir = data_dir / "sessions"
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            storage=SimpleNamespace(logs_dir=logs_dir, transcripts_dir=transcripts_dir, sessions_dir=sessions_dir),
            provider=SimpleNamespace(
                name="openai",
                provider_type="openai",
                model="gpt-4.1",
                base_url="https://api.example.test/v1",
                max_tokens=4096,
                context_window_tokens=100_000,
            ),
        )
        runtime.provider = SimpleNamespace(
            count_tokens=lambda system_prompt, messages, tools: 12_345,
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 100_000,
            debug_request_payload=lambda system_prompt, messages, tools, max_tokens, stream=False: {
                "url": "https://api.example.test/v1/chat/completions",
                "body": {"model": "gpt-4.1", "stream": stream},
            },
        )
        runtime._provider_payload_dump_enabled = OpenAgentRuntime._provider_payload_dump_enabled.__get__(runtime, OpenAgentRuntime)
        runtime._dump_provider_payload_if_enabled = OpenAgentRuntime._dump_provider_payload_if_enabled.__get__(runtime, OpenAgentRuntime)
        runtime._serialize_provider_response = OpenAgentRuntime._serialize_provider_response.__get__(runtime, OpenAgentRuntime)
        runtime._record_provider_payload_result = OpenAgentRuntime._record_provider_payload_result.__get__(runtime, OpenAgentRuntime)
        runtime._count_payload_usage = OpenAgentRuntime._count_payload_usage.__get__(runtime, OpenAgentRuntime)
        runtime.transcript_store = SimpleNamespace(transcript_path=lambda session_id: transcripts_dir / f"{session_id}.jsonl")
        session = AgentSession(id="session-1", messages=[{"role": "user", "content": "hello"}])

        with patch.dict(os.environ, {OpenAgentRuntime.DEBUG_PROVIDER_PAYLOAD_ENV: "1"}, clear=False):
            dump_path = OpenAgentRuntime._dump_provider_payload_if_enabled(
                runtime,
                session=session,
                system_prompt="system",
                payload_messages=[{"role": "user", "content": "hello"}],
                tools=[],
                max_tokens=4096,
                actor="lead",
                stream=True,
            )
            OpenAgentRuntime._record_provider_payload_result(
                runtime,
                dump_path,
                turn=AssistantTurn(
                    stop_reason="end_turn",
                    text_blocks=["hello world"],
                    usage={"input_tokens": 10, "output_tokens": 5},
                    raw_response={"id": "resp-1"},
                ),
                latency_ms=23.5,
            )

        dump_files = list((logs_dir / "provider_payloads").glob("*.json"))
        self.assertEqual(len(dump_files), 1)
        dumped = json.loads(dump_files[0].read_text(encoding="utf-8"))
        self.assertEqual(dumped["session_id"], "session-1")
        self.assertEqual(dumped["kind"], "turn")
        self.assertEqual(dumped["provider"]["model"], "gpt-4.1")
        self.assertEqual(dumped["context_usage"]["used_tokens"], 12_345)
        self.assertEqual(dumped["provider_request"]["body"]["stream"], True)
        self.assertEqual(dumped["provider_response"]["stop_reason"], "end_turn")
        self.assertEqual(dumped["response_text"], "hello world")
        self.assertEqual(dumped["latency_ms"], 23.5)
        self.assertTrue(dumped["transcript_path"].endswith("session-1.jsonl"))

    def test_record_provider_payload_result_writes_error_details(self) -> None:
        root = self._stable_test_dir("provider-payload-error")
        data_dir = root / ".open_somnia"
        logs_dir = data_dir / "logs"
        transcripts_dir = data_dir / "transcripts"
        sessions_dir = data_dir / "sessions"
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            storage=SimpleNamespace(logs_dir=logs_dir, transcripts_dir=transcripts_dir, sessions_dir=sessions_dir),
            provider=SimpleNamespace(
                name="openai",
                provider_type="openai",
                model="gpt-4.1",
                base_url="https://api.example.test/v1",
                max_tokens=4096,
                context_window_tokens=100_000,
            ),
        )
        runtime.provider = SimpleNamespace(
            count_tokens=lambda system_prompt, messages, tools: 12_345,
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 100_000,
            debug_request_payload=lambda system_prompt, messages, tools, max_tokens, stream=False: {
                "url": "https://api.example.test/v1/chat/completions",
                "body": {"model": "gpt-4.1", "stream": stream},
            },
        )
        runtime._provider_payload_dump_enabled = OpenAgentRuntime._provider_payload_dump_enabled.__get__(runtime, OpenAgentRuntime)
        runtime._dump_provider_payload_if_enabled = OpenAgentRuntime._dump_provider_payload_if_enabled.__get__(runtime, OpenAgentRuntime)
        runtime._serialize_provider_response = OpenAgentRuntime._serialize_provider_response.__get__(runtime, OpenAgentRuntime)
        runtime._record_provider_payload_result = OpenAgentRuntime._record_provider_payload_result.__get__(runtime, OpenAgentRuntime)
        runtime._count_payload_usage = OpenAgentRuntime._count_payload_usage.__get__(runtime, OpenAgentRuntime)
        runtime.transcript_store = SimpleNamespace(transcript_path=lambda session_id: transcripts_dir / f"{session_id}.jsonl")
        session = AgentSession(id="session-1", messages=[{"role": "user", "content": "hello"}])

        with patch.dict(os.environ, {OpenAgentRuntime.DEBUG_PROVIDER_PAYLOAD_ENV: "1"}, clear=False):
            dump_path = OpenAgentRuntime._dump_provider_payload_if_enabled(
                runtime,
                session=session,
                system_prompt="system",
                payload_messages=[{"role": "user", "content": "hello"}],
                tools=[],
                max_tokens=4096,
                actor="lead",
                stream=False,
            )
            OpenAgentRuntime._record_provider_payload_result(
                runtime,
                dump_path,
                error=RuntimeError("boom"),
                latency_ms=12.0,
            )

        dump_files = list((logs_dir / "provider_payloads").glob("*.json"))
        self.assertEqual(len(dump_files), 1)
        dumped = json.loads(dump_files[0].read_text(encoding="utf-8"))
        self.assertEqual(dumped["provider_error"]["type"], "RuntimeError")
        self.assertEqual(dumped["provider_error"]["message"], "boom")
        self.assertEqual(dumped["latency_ms"], 12.0)

    def test_analyze_context_relevance_dumps_janitor_provider_payload_when_enabled(self) -> None:
        root = self._stable_test_dir("janitor-provider-payload")
        data_dir = root / ".open_somnia"
        logs_dir = data_dir / "logs"
        transcripts_dir = data_dir / "transcripts"
        sessions_dir = data_dir / "sessions"
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            storage=SimpleNamespace(logs_dir=logs_dir, transcripts_dir=transcripts_dir, sessions_dir=sessions_dir),
            provider=SimpleNamespace(
                name="openai",
                provider_type="openai",
                model="gpt-4.1",
                base_url="https://api.example.test/v1",
                max_tokens=4096,
                context_window_tokens=100_000,
            ),
        )
        runtime.provider = SimpleNamespace(
            count_tokens=lambda system_prompt, messages, tools: 12_345,
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 100_000,
            debug_request_payload=lambda system_prompt, messages, tools, max_tokens, stream=False: {
                "url": "https://api.example.test/v1/chat/completions",
                "body": {"model": "gpt-4.1", "stream": stream, "messages": messages},
            },
            complete=lambda **kwargs: AssistantTurn(
                stop_reason="end_turn",
                text_blocks=['[{"message_index":1,"item_index":0,"state":"condensed","summary":"condensed"}]'],
            ),
        )
        runtime._provider_payload_dump_enabled = OpenAgentRuntime._provider_payload_dump_enabled.__get__(runtime, OpenAgentRuntime)
        runtime._dump_provider_payload_if_enabled = OpenAgentRuntime._dump_provider_payload_if_enabled.__get__(runtime, OpenAgentRuntime)
        runtime._serialize_provider_response = OpenAgentRuntime._serialize_provider_response.__get__(runtime, OpenAgentRuntime)
        runtime._record_provider_payload_result = OpenAgentRuntime._record_provider_payload_result.__get__(runtime, OpenAgentRuntime)
        runtime._count_payload_usage = OpenAgentRuntime._count_payload_usage.__get__(runtime, OpenAgentRuntime)
        runtime._selected_janitor_candidates = OpenAgentRuntime._selected_janitor_candidates.__get__(runtime, OpenAgentRuntime)
        runtime._janitor_candidates = OpenAgentRuntime._janitor_candidates.__get__(runtime, OpenAgentRuntime)
        runtime._extract_recent_topic_context = OpenAgentRuntime._extract_recent_topic_context.__get__(runtime, OpenAgentRuntime)
        runtime._todo_hint_context = lambda session: {"open_items": [], "completed_items": [], "open_tokens": set(), "completed_tokens": set()}
        runtime._fallback_context_relevance_decisions = OpenAgentRuntime._fallback_context_relevance_decisions.__get__(runtime, OpenAgentRuntime)
        runtime._build_semantic_janitor_prompt = OpenAgentRuntime._build_semantic_janitor_prompt.__get__(runtime, OpenAgentRuntime)
        runtime._parse_semantic_janitor_response = OpenAgentRuntime._parse_semantic_janitor_response.__get__(runtime, OpenAgentRuntime)
        runtime._strip_json_fence = OpenAgentRuntime._strip_json_fence.__get__(runtime, OpenAgentRuntime)
        runtime._render_condensed_context = OpenAgentRuntime._render_condensed_context.__get__(runtime, OpenAgentRuntime)
        runtime._render_evicted_context = OpenAgentRuntime._render_evicted_context.__get__(runtime, OpenAgentRuntime)
        runtime._context_compact_text = OpenAgentRuntime._context_compact_text.__get__(runtime, OpenAgentRuntime)
        runtime._candidate_target_path = OpenAgentRuntime._candidate_target_path.__get__(runtime, OpenAgentRuntime)
        runtime._candidate_relevance_score = OpenAgentRuntime._candidate_relevance_score.__get__(runtime, OpenAgentRuntime)
        runtime._extract_topic_tokens = OpenAgentRuntime._extract_topic_tokens.__get__(runtime, OpenAgentRuntime)
        runtime._is_visible_conversation_message = OpenAgentRuntime._is_visible_conversation_message.__get__(runtime, OpenAgentRuntime)
        runtime.transcript_store = SimpleNamespace(transcript_path=lambda session_id: transcripts_dir / f"{session_id}.jsonl")
        session = AgentSession(
            id="session-1",
            messages=[
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-1", "name": "read_file", "input": {"path": "demo.txt"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-1", "content": "x" * 1200, "raw_output": "x" * 1200, "log_id": "log-1"}]},
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-2", "name": "grep", "input": {"pattern": "demo"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-2", "content": "demo hit", "raw_output": "demo hit", "log_id": "log-2"}]},
                {"role": "assistant", "content": [{"type": "tool_call", "id": "call-3", "name": "read_file", "input": {"path": "main.py"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-3", "content": "print('hello')", "raw_output": "print('hello')", "log_id": "log-3"}]},
                {"role": "assistant", "content": "please keep demo.txt context"},
            ],
        )

        with patch.dict(os.environ, {OpenAgentRuntime.DEBUG_PROVIDER_PAYLOAD_ENV: "1"}, clear=False):
            decisions = OpenAgentRuntime._analyze_context_relevance(
                runtime,
                session=session,
                messages=session.messages,
                system_prompt="ignored",
                tools=[],
            )

        self.assertEqual(len(decisions), 1)
        dump_files = list((logs_dir / "provider_payloads").glob("*.json"))
        self.assertEqual(len(dump_files), 1)
        dumped = json.loads(dump_files[0].read_text(encoding="utf-8"))
        self.assertEqual(dumped["kind"], "janitor")
        self.assertEqual(dumped["actor"], "janitor")
        self.assertEqual(dumped["stream"], False)
        self.assertEqual(dumped["provider_request"]["body"]["stream"], False)
        self.assertEqual(dumped["provider_response"]["stop_reason"], "end_turn")
        self.assertEqual(
            dumped["response_text"],
            '[{"message_index":1,"item_index":0,"state":"condensed","summary":"condensed"}]',
        )
        self.assertIsNone(dumped["provider_error"])
        self.assertIsInstance(dumped["latency_ms"], float)

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
        runtime.PROVIDER_RETRY_DELAY_SECONDS = 0
        attempts: list[str] = []

        class _Provider:
            def complete(self, **kwargs):
                attempts.append("called")
                raise ProviderError("temporary timeout", retryable=True)

        runtime.provider = _Provider()

        with self.assertRaisesRegex(RuntimeError, "temporary timeout"):
            OpenAgentRuntime.complete(runtime, "system", [], [], text_callback=None)

        self.assertEqual(attempts, ["called", "called", "called"])

    def test_complete_waits_between_retryable_provider_attempts(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(provider=SimpleNamespace(max_tokens=1024))
        attempts: list[str] = []
        waits: list[str] = []

        class _Provider:
            def complete(self, **kwargs):
                attempts.append("called")
                raise ProviderError("temporary timeout", retryable=True)

        runtime.provider = _Provider()
        runtime._wait_before_provider_retry = lambda should_interrupt=None: waits.append("wait")

        with self.assertRaisesRegex(RuntimeError, "temporary timeout"):
            OpenAgentRuntime.complete(runtime, "system", [], [], text_callback=None)

        self.assertEqual(attempts, ["called", "called", "called"])
        self.assertEqual(waits, ["wait", "wait"])

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

    def test_openai_provider_marks_forbidden_like_502_as_non_retryable(self) -> None:
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
        forbidden_body = (
            '{"error":{"message":"Upstream access forbidden, please contact administrator",'
            '"type":"upstream_error"}}'
        ).encode("utf-8")
        http_error = urllib.error.HTTPError(
            url="https://example.com/v1/chat/completions",
            code=502,
            msg="Bad Gateway",
            hdrs=None,
            fp=io.BytesIO(forbidden_body),
        )

        with patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(ProviderError) as context:
                provider.complete("system", [], [], max_tokens=1024)

        self.assertFalse(context.exception.retryable)
        self.assertIn("Upstream access forbidden", str(context.exception))

    def test_openai_provider_wraps_generic_exception_as_retryable_provider_error(self) -> None:
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

        with patch("urllib.request.urlopen", side_effect=RuntimeError("temporary upstream network error")):
            with self.assertRaises(ProviderError) as context:
                provider.complete("system", [], [], max_tokens=1024)

        self.assertTrue(context.exception.retryable)
        self.assertIn("OpenAI request failed", str(context.exception))
        self.assertIn("temporary upstream network error", str(context.exception))

    def test_anthropic_provider_wraps_transient_exception_as_retryable_provider_error(self) -> None:
        provider = AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="glm-5",
                api_key="test-key",
                base_url="https://example.com/anthropic",
                timeout_seconds=30,
            )
        )

        class TemporaryNetworkError(Exception):
            status_code = 502

        provider.client = SimpleNamespace(
            messages=SimpleNamespace(
                create=lambda **kwargs: (_ for _ in ()).throw(TemporaryNetworkError("网络错误，错误id：req-1"))
            )
        )

        with self.assertRaises(ProviderError) as context:
            provider.complete("system", [{"role": "user", "content": "hello"}], [], max_tokens=1024)

        self.assertTrue(context.exception.retryable)
        self.assertIn("Anthropic request failed", str(context.exception))
        self.assertIn("网络错误", str(context.exception))

    def test_complete_retries_wrapped_anthropic_provider_exception(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(provider=SimpleNamespace(max_tokens=1024))
        runtime.PROVIDER_RETRY_DELAY_SECONDS = 0
        attempts: list[str] = []

        class _AnthropicLikeProvider:
            def complete(self, **kwargs):
                attempts.append("called")
                raise ProviderError("Anthropic request failed: 网络错误", retryable=True)

        runtime.provider = _AnthropicLikeProvider()

        with self.assertRaisesRegex(RuntimeError, "Anthropic request failed: 网络错误"):
            OpenAgentRuntime.complete(runtime, "system", [], [], text_callback=None)

        self.assertEqual(attempts, ["called", "called", "called"])

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
            runtime=SimpleNamespace(max_agent_rounds=4, janitor_trigger_ratio=0.6, max_tool_output_chars=5000),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.compact_manager = SimpleNamespace(auto_compact=lambda session_id, messages, preserve_from_index=None: messages)
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
            runtime=SimpleNamespace(max_agent_rounds=4, janitor_trigger_ratio=0.6, max_tool_output_chars=5000),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.compact_manager = SimpleNamespace(auto_compact=lambda session_id, messages, preserve_from_index=None: messages)
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

    def test_agent_loop_auto_compact_preserves_last_conversation_and_active_task_window(self) -> None:
        captured: dict[str, object] = {}
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(max_agent_rounds=4, janitor_trigger_ratio=0.6, max_tool_output_chars=5000),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.todo_manager = SimpleNamespace(has_open_items=lambda session: False)
        runtime.session_manager = SimpleNamespace(save=lambda session: None)
        runtime.transcript_store = SimpleNamespace(append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: None
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=90_000, max_tokens=100_000)
        runtime.compact_manager = SimpleNamespace(
            auto_compact=lambda session_id, messages, preserve_from_index=None: captured.update(
                {
                    "session_id": session_id,
                    "preserve_from_index": preserve_from_index,
                    "messages_before": list(messages),
                }
            )
            or [
                {"role": "user", "content": "[compressed older history]"},
                {"role": "assistant", "content": "continuing"},
                *messages[preserve_from_index or 0 :],
            ]
        )
        runtime.complete = lambda *args, **kwargs: AssistantTurn(stop_reason="end_turn", text_blocks=["Done."])
        runtime.registry = SimpleNamespace(schemas=lambda: [])

        session = AgentSession(
            id="session-1",
            messages=[
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "previous question"},
                {"role": "assistant", "content": "previous answer"},
            ],
        )

        result = OpenAgentRuntime.run_turn(runtime, session, "current request")

        self.assertEqual(result, "Done.")
        self.assertEqual(captured["session_id"], "session-1")
        self.assertEqual(captured["preserve_from_index"], 2)
        self.assertEqual(session.messages[0]["content"], "[compressed older history]")
        self.assertEqual(session.messages[2]["content"], "previous question")
        self.assertEqual(session.messages[3]["content"], "previous answer")
        self.assertEqual(session.messages[4]["content"], "current request")
        self.assertEqual(session.messages[5]["content"], "Done.")

    def test_agent_loop_todo_reminder_persists_while_items_remain_open_and_stops_after_completion(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(max_agent_rounds=5, janitor_trigger_ratio=0.6, max_tool_output_chars=5000),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.compact_manager = SimpleNamespace(auto_compact=lambda session_id, messages, preserve_from_index=None: messages)
        runtime.todo_manager = SimpleNamespace(
            has_open_items=lambda session: any(item.get("status") in {"pending", "in_progress"} for item in getattr(session, "todo_items", []))
        )
        runtime.session_manager = SimpleNamespace(save=lambda session: None)
        runtime.transcript_store = SimpleNamespace(append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: None
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)

        class _Registry:
            def schemas(self):
                return []

            def execute(self, ctx, name, payload):
                if name == "TodoWrite":
                    ctx.session.todo_items = list(payload["items"])
                return "ok"

        payloads: list[list[dict]] = []
        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCall(
                            "call-1",
                            "TodoWrite",
                            {
                                "items": [
                                    {"content": "Step 1", "status": "completed", "activeForm": "Completing step 1"},
                                    {"content": "Step 2", "status": "in_progress", "activeForm": "Completing step 2"},
                                ]
                            },
                        )
                    ],
                ),
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[ToolCall("call-2", "bash", {"command": "git status"})],
                ),
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCall(
                            "call-3",
                            "TodoWrite",
                            {
                                "items": [
                                    {"content": "Step 1", "status": "completed", "activeForm": "Completing step 1"},
                                    {"content": "Step 2", "status": "completed", "activeForm": "Completing step 2"},
                                ]
                            },
                        )
                    ],
                ),
                AssistantTurn(
                    stop_reason="end_turn",
                    text_blocks=["Done."],
                ),
            ]
        )

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            payloads.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return next(turns)

        runtime.complete = fake_complete
        runtime.registry = _Registry()

        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(runtime, session, "inspect")
        reminder = OpenAgentRuntime.TODO_REMINDER_TEXT
        reminder_counts = [json.dumps(payload, ensure_ascii=False).count(reminder) for payload in payloads]

        self.assertEqual(result, "Done.")
        self.assertEqual(reminder_counts, [0, 1, 1, 0])
        self.assertNotIn(reminder, json.dumps(session.messages, ensure_ascii=False))

    def test_agent_loop_todo_reminder_is_injected_every_round_while_items_remain_open(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(max_agent_rounds=5, janitor_trigger_ratio=0.6, max_tool_output_chars=5000),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.compact_manager = SimpleNamespace(auto_compact=lambda session_id, messages, preserve_from_index=None: messages)
        runtime.todo_manager = SimpleNamespace(
            has_open_items=lambda session: any(item.get("status") in {"pending", "in_progress"} for item in getattr(session, "todo_items", []))
        )
        runtime.session_manager = SimpleNamespace(save=lambda session: None)
        runtime.transcript_store = SimpleNamespace(append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: None
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)

        class _Registry:
            def schemas(self):
                return []

            def execute(self, ctx, name, payload):
                return "ok"

        payloads: list[list[dict]] = []
        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[ToolCall("call-1", "bash", {"command": "pwd"})],
                ),
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[ToolCall("call-2", "bash", {"command": "git status"})],
                ),
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[ToolCall("call-3", "bash", {"command": "ls"})],
                ),
                AssistantTurn(
                    stop_reason="end_turn",
                    text_blocks=["Done."],
                ),
            ]
        )

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            payloads.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return next(turns)

        runtime.complete = fake_complete
        runtime.registry = _Registry()

        session = AgentSession(
            id="session-1",
            todo_items=[{"content": "Step 2", "status": "in_progress", "activeForm": "Completing step 2"}],
        )

        result = OpenAgentRuntime.run_turn(runtime, session, "inspect")
        reminder = OpenAgentRuntime.TODO_REMINDER_TEXT
        reminder_counts = [json.dumps(payload, ensure_ascii=False).count(reminder) for payload in payloads]

        self.assertEqual(result, "Done.")
        self.assertEqual(reminder_counts, [1, 1, 1, 1])
        self.assertNotIn(reminder, json.dumps(session.messages, ensure_ascii=False))

    def test_agent_loop_returns_explicit_status_when_max_rounds_end_with_open_todos(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(max_agent_rounds=2, janitor_trigger_ratio=0.6, max_tool_output_chars=5000),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.compact_manager = SimpleNamespace(auto_compact=lambda session_id, messages, preserve_from_index=None: messages)
        runtime.todo_manager = SimpleNamespace(
            has_open_items=lambda session: any(item.get("status") in {"pending", "in_progress"} for item in getattr(session, "todo_items", []))
        )
        runtime.session_manager = SimpleNamespace(save=lambda session: None)
        runtime.transcript_store = SimpleNamespace(append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: None
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)

        class _Registry:
            def schemas(self):
                return []

            def execute(self, ctx, name, payload):
                return "ok"

        payloads: list[list[dict]] = []
        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[ToolCall("call-1", "bash", {"command": "pwd"})],
                ),
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[ToolCall("call-2", "bash", {"command": "git status"})],
                ),
            ]
        )

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            payloads.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return next(turns)

        runtime.complete = fake_complete
        runtime.registry = _Registry()

        session = AgentSession(
            id="session-1",
            todo_items=[{"content": "Step 1", "status": "in_progress", "activeForm": "Doing step 1"}],
        )

        result = OpenAgentRuntime.run_turn(runtime, session, "inspect")

        self.assertEqual(
            result,
            "Stopped after max rounds with open todo items remaining (1 open). Continue the session to resume unfinished work.",
        )
        self.assertEqual(getattr(result, "status", None), "stopped_with_open_todos")
        self.assertEqual(getattr(result, "open_todo_count", None), 1)
        reminder = OpenAgentRuntime.TODO_REMINDER_TEXT
        reminder_counts = [json.dumps(payload, ensure_ascii=False).count(reminder) for payload in payloads]
        self.assertEqual(reminder_counts, [1, 1])

    def test_agent_loop_returns_explicit_status_when_max_rounds_end_without_open_todos(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(max_agent_rounds=1, janitor_trigger_ratio=0.6, max_tool_output_chars=5000),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.compact_manager = SimpleNamespace(auto_compact=lambda session_id, messages, preserve_from_index=None: messages)
        runtime.todo_manager = SimpleNamespace(has_open_items=lambda session: False)
        runtime.session_manager = SimpleNamespace(save=lambda session: None)
        runtime.transcript_store = SimpleNamespace(append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: None
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)

        class _Registry:
            def schemas(self):
                return []

            def execute(self, ctx, name, payload):
                return "ok"

        runtime.complete = lambda *args, **kwargs: AssistantTurn(
            stop_reason="tool_use",
            tool_calls=[ToolCall("call-1", "bash", {"command": "pwd"})],
        )
        runtime.registry = _Registry()

        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(runtime, session, "inspect")

        self.assertEqual(result, "Stopped after max rounds.")
        self.assertEqual(getattr(result, "status", None), "stopped_after_max_rounds")
        self.assertEqual(getattr(result, "open_todo_count", None), 0)

    def test_agent_loop_injects_repair_hint_once_and_keeps_compact_error_afterward(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(max_agent_rounds=5, janitor_trigger_ratio=0.6, max_tool_output_chars=5000),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.compact_manager = SimpleNamespace(auto_compact=lambda session_id, messages, preserve_from_index=None: messages)
        runtime.todo_manager = SimpleNamespace(has_open_items=lambda session: False)
        runtime.session_manager = SimpleNamespace(save=lambda session: None)
        runtime.transcript_store = SimpleNamespace(append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: "log-1"
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)

        class _Registry:
            def schemas(self):
                return []

            def execute(self, ctx, name, payload):
                if name == "write_file":
                    return {
                        "status": "error",
                        "error_type": "missing_required_params",
                        "tool_name": "write_file",
                        "message": "Missing required parameter(s) for 'write_file': content.",
                        "missing_params": ["content"],
                        "repair_hint": {"required": ["path", "content"]},
                    }
                return "ok"

        payloads: list[list[dict]] = []
        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[ToolCall("call-1", "write_file", {"path": "demo.txt"})],
                ),
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[ToolCall("call-2", "bash", {"command": "pwd"})],
                ),
                AssistantTurn(
                    stop_reason="end_turn",
                    text_blocks=["Done."],
                ),
            ]
        )

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            payloads.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return next(turns)

        runtime.complete = fake_complete
        runtime.registry = _Registry()

        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(runtime, session, "write the file")

        self.assertEqual(result, "Done.")
        self.assertEqual(len(payloads), 3)

        round_two_payload = json.dumps(payloads[1], ensure_ascii=False)
        round_three_payload = json.dumps(payloads[2], ensure_ascii=False)
        persisted_tool_error = session.messages[2]["content"][0]["content"]
        session_dump = json.dumps(session.messages, ensure_ascii=False)

        self.assertIn("<tool-repair-hints>", round_two_payload)
        self.assertIn("repair_hint", round_two_payload)
        self.assertIn("path", round_two_payload)
        self.assertIn("content", round_two_payload)
        self.assertNotIn("<tool-repair-hints>", round_three_payload)
        self.assertIn("missing_required_params", round_three_payload)
        self.assertIn("missing_required_params", persisted_tool_error)
        self.assertNotIn("repair_hint", persisted_tool_error)
        self.assertIn("missing_required_params", session_dump)
        self.assertNotIn("repair_hint", session_dump)
        self.assertNotIn("<tool-repair-hints>", session_dump)

    def test_agent_loop_accumulates_token_usage_sum(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(max_agent_rounds=4, janitor_trigger_ratio=0.6, max_tool_output_chars=5000),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.compact_manager = SimpleNamespace(auto_compact=lambda session_id, messages, preserve_from_index=None: messages, last_usage=None)
        runtime.todo_manager = SimpleNamespace(has_open_items=lambda session: False)
        runtime.session_manager = SimpleNamespace(save=lambda session: None)
        runtime.transcript_store = SimpleNamespace(append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: None
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.complete = lambda *args, **kwargs: AssistantTurn(
            stop_reason="end_turn",
            text_blocks=["Done."],
            usage={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150, "source": "provider"},
        )

        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(runtime, session, "hello")

        self.assertEqual(result, "Done.")
        self.assertEqual(session.token_usage["input_tokens"], 120)
        self.assertEqual(session.token_usage["output_tokens"], 30)
        self.assertEqual(session.token_usage["total_tokens"], 150)


if __name__ == "__main__":
    unittest.main()
