from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.error
import unittest
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from open_somnia.config.models import ModelTraits, ProviderProfileSettings, ProviderSettings
from open_somnia.config.settings import _build_provider_profile, _load_provider_profiles, _materialize_provider, load_settings
from open_somnia.collaboration.bus import MessageBus
from open_somnia.mcp.registry import _render_mcp_result
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
from open_somnia.runtime.messages import (
    MODEL_IMAGE_INLINE_MAX_BYTES_WITHOUT_PILLOW,
    active_tool_result_content_blocks,
    AssistantTurn,
    consume_ephemeral_image_blocks,
    ToolCall,
    encode_embedded_user_message,
    IMAGE_REFERENCE_BLOCK_TYPE,
    make_tool_result_item,
    make_user_multimodal_message,
    prepare_image_bytes_for_model,
)
from open_somnia.runtime.events import ToolExecutionContext
from open_somnia.runtime.session import AgentSession
from open_somnia.runtime.thinking import THINKING_LOG_MAX_CHARS, ThinkingLogWriter
from open_somnia.collaboration.protocols import RequestTracker
from open_somnia.storage.inbox import InboxStore
from open_somnia.tools.registry import ToolDefinition, ToolRegistry
from open_somnia.tools.filesystem import read_image
from open_somnia.tools.team import register_team_tools


class RuntimeToolOutputTests(unittest.TestCase):
    _TINY_PNG_BYTES = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2ioAAAAASUVORK5CYII="
    )

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

    def test_thinking_log_writer_flushes_merged_capped_record(self) -> None:
        root = self._stable_test_dir("thinking-log-writer")
        writer = ThinkingLogWriter(root, "session", "turn")

        writer.append_delta("one ")
        writer.append_delta("two ")
        writer.append_delta("three")
        marker = writer.marker()

        lines = writer.path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[0])
        self.assertEqual(len(lines), 1)
        self.assertEqual(payload["thinking"], "one two three")
        self.assertEqual(marker["characters"], len("one two three"))
        self.assertEqual(marker["block_count"], 3)

        capped = ThinkingLogWriter(root, "session", "capped")
        capped.append_delta("x" * (THINKING_LOG_MAX_CHARS + 25))
        capped.marker()
        capped_payload = json.loads(capped.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(len(capped_payload["thinking"]), THINKING_LOG_MAX_CHARS)
        self.assertEqual(capped_payload["truncated_characters"], 25)

    def test_attach_thinking_log_marker_preserves_signed_thinking_blocks(self) -> None:
        root = self._stable_test_dir("thinking-log-marker")
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        writer = ThinkingLogWriter(root, "session", "turn")
        assistant_message = {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "private reasoning", "signature": "sig-1"},
                {"type": "text", "text": "I need a tool."},
                {"type": "tool_call", "id": "call-1", "name": "bash", "input": {"command": "pwd"}},
            ],
        }

        message = OpenAgentRuntime._attach_thinking_log_marker(runtime, assistant_message, thinking_log=writer)
        content = message["content"]

        self.assertEqual(content[0]["type"], "thinking_log")
        self.assertEqual(
            content[1],
            {"type": "thinking", "thinking": "private reasoning", "signature": "sig-1"},
        )
        self.assertEqual(content[2], {"type": "text", "text": "I need a tool."})
        self.assertEqual(content[3]["type"], "tool_call")

    def test_provider_profile_ignores_legacy_vision_fields(self) -> None:
        profile = _build_provider_profile(
            "openai",
            {
                "provider_type": "openai",
                "models": ["kimi-k2-thinking", "doubao-1-5-vision-pro-32k"],
                "default_model": "kimi-k2-thinking",
                "vision_provider": "openai",
                "vision_model": "doubao-1-5-vision-pro-32k",
                "api_key": "fake",
                "base_url": "http://localhost",
            },
            {},
        )

        settings = _materialize_provider(profile)

        self.assertEqual(profile.models, ["kimi-k2-thinking", "doubao-1-5-vision-pro-32k"])
        self.assertEqual(settings.model, "kimi-k2-thinking")
        self.assertFalse(hasattr(profile, "vision_provider"))
        self.assertFalse(hasattr(settings, "vision_provider"))

    def test_routing_rejects_unconfigured_vision_model(self) -> None:
        raw = {
            "providers": {
                "default": "openai",
                "openai": {
                    "provider_type": "openai",
                    "models": ["strong-text-model"],
                    "default_model": "strong-text-model",
                    "api_key": "fake",
                },
            },
            "routing": {
                "vision_provider": "openai",
                "vision_model": "vision-only",
            },
        }

        with self.assertRaisesRegex(ValueError, "Vision model 'vision-only' is not configured"):
            profiles, _ = _load_provider_profiles(raw)
            from open_somnia.config.settings import _load_vision_route

            _load_vision_route(raw, profiles)

    def test_workspace_config_overrides_global_vision_model(self) -> None:
        root = self._stable_test_dir("vision-model-config-override")
        global_config = root / "global.toml"
        workspace_config = root / ".open_somnia" / "open_somnia.toml"
        workspace_config.parent.mkdir(parents=True, exist_ok=True)
        global_config.write_text(
            "\n".join(
                [
                    "[providers]",
                    'default = "openai"',
                    "",
                    "[providers.openai]",
                    'provider_type = "openai"',
                    'models = ["strong-text-model", "global-vision", "workspace-vision"]',
                    'default_model = "strong-text-model"',
                    'api_key = "fake"',
                    'base_url = "http://localhost"',
                    "",
                    "[routing]",
                    'vision_provider = "openai"',
                    'vision_model = "global-vision"',
                ]
            ),
            encoding="utf-8",
        )
        workspace_config.write_text(
            "\n".join(
                [
                    "[routing]",
                    'vision_provider = "openai"',
                    'vision_model = "workspace-vision"',
                ]
            ),
            encoding="utf-8",
        )

        with patch("open_somnia.config.settings.global_config_path", return_value=global_config):
            settings = load_settings(root)

        self.assertEqual(settings.provider.model, "strong-text-model")
        self.assertEqual(settings.vision_provider, "openai")
        self.assertEqual(settings.vision_model, "workspace-vision")

    def test_complete_uses_vision_model_only_for_image_payloads(self) -> None:
        calls: list[tuple[str, int]] = []

        class _Provider:
            def __init__(self, settings):
                self.settings = settings

            def complete(self, **kwargs):
                calls.append((self.settings.model, kwargs["max_tokens"]))
                return AssistantTurn(stop_reason="end_turn", text_blocks=[self.settings.model])

        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.provider = _Provider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="strong-text-model",
                max_tokens=321,
            )
        )
        runtime.settings = SimpleNamespace(
            provider=runtime.provider.settings,
            vision_provider="vision",
            vision_model="vision-model",
            provider_profiles={
                "openai": ProviderProfileSettings(
                    name="openai",
                    provider_type="openai",
                    models=["strong-text-model"],
                    default_model="strong-text-model",
                    max_tokens=654,
                ),
                "vision": ProviderProfileSettings(
                    name="vision",
                    provider_type="openai",
                    models=["vision-model"],
                    default_model="vision-model",
                    max_tokens=654,
                )
            },
        )
        runtime._instantiate_provider = lambda provider_settings: _Provider(provider_settings)
        runtime._raise_if_interrupted = lambda should_interrupt=None: None
        runtime._wait_before_provider_retry = lambda should_interrupt=None: None

        text_turn = OpenAgentRuntime.complete(runtime, "system", [{"role": "user", "content": "hello"}], [])
        image_turn = OpenAgentRuntime.complete(
            runtime,
            "system",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {"type": "input_image", "absolute_path": "D:/workspace/image.png", "media_type": "image/png"},
                    ],
                }
            ],
            [],
        )

        self.assertEqual(text_turn.text_blocks, ["strong-text-model"])
        self.assertEqual(image_turn.text_blocks, ["vision-model"])
        self.assertEqual(calls, [("strong-text-model", 321), ("vision-model", 654)])

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

    def test_file_edit_tool_event_shows_full_diff_without_ellipsis(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.tool_log_store = SimpleNamespace(write=lambda **kwargs: {"id": "edit-log"})
        runtime._supports_ansi_output = lambda: False

        class _Stdout(io.StringIO):
            def isatty(self) -> bool:
                return True

        old_text = "\n".join(f"old {index}" for index in range(12)) + "\n"
        new_text = "\n".join(f"new {index}" for index in range(12)) + "\n"
        fake_stdout = _Stdout()
        with patch("sys.stdout", fake_stdout):
            OpenAgentRuntime.print_tool_event(
                runtime,
                "lead",
                "edit_file",
                {
                    "path": "demo.txt",
                    "edits": [{"old_text": old_text, "new_text": new_text}],
                },
                {
                    "status": "ok",
                    "path": "demo.txt",
                    "absolute_path": "D:/workspace/demo.txt",
                    "added_lines": 12,
                    "removed_lines": 12,
                },
            )

        rendered = fake_stdout.getvalue()
        self.assertIn("-old 11", rendered)
        self.assertIn("+new 11", rendered)
        self.assertNotIn("      ...", rendered)

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
        runtime.mcp_registry = SimpleNamespace(all_servers=[], server_tools={})
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
        self.assertIn("Follow project instructions first", prompt)
        self.assertIn("Prefer MCP and project-specific tools over generic filesystem/search tools", prompt)
        self.assertIn("Treat generic workspace tools as fallbacks for overlapping work", prompt)
        self.assertIn("Avoid broad repository sweeps", prompt)
        self.assertIn("establish the exact path through the most specific available evidence", prompt)
        self.assertIn("always wrap replacements as `edits=[{old_text,new_text}, ...]`", prompt)
        self.assertNotIn("Use `grep` instead of shell content search commands", prompt)
        self.assertNotIn("Use `project_scan` or a focused `tree`", prompt)
        self.assertIn("Use `TodoWrite` to break down meaningful work", prompt)
        self.assertIn("Problem solving workflow:", prompt)
        self.assertIn("understand local evidence, plan the smallest coherent change", prompt)
        self.assertIn("Do not treat edits as complete until the user-visible goal is verified", prompt)
        self.assertIn("Use `edit_file` with `edits=[...]` for every text replacement", prompt)
        self.assertIn("use the returned updated snippet before rereading", prompt)
        self.assertIn("Do not claim a root cause", prompt)
        self.assertIn("If you keep rereading the same file or area", prompt)
        self.assertIn("Active provider: openai", prompt)
        self.assertIn("Active model: kimi-k2.5", prompt)
        self.assertNotIn("Active working file cache:", prompt)
        self.assertNotIn("frontend/src/App.tsx", prompt)
        self.assertIn("Current mode: ⏸ plan mode on.", prompt)
        self.assertIn("Return a concrete implementation plan", prompt)
        self.assertIn("request_mode_switch", prompt)
        self.assertIn("Use subagent for isolated subagent work.", prompt)
        self.assertIn("Do not claim to be Claude", prompt)

    def test_build_system_prompt_sections_are_structured_for_debug_payloads(self) -> None:
        root = self._stable_test_dir("prompt-sections")
        (root / "AGENTS.md").write_text("Use repo guidance.\n", encoding="utf-8")
        nested = root / "app"
        nested.mkdir(exist_ok=True)
        (nested / "AGENTS.md").write_text("Use app guidance.\n", encoding="utf-8")
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=root,
            agent=SimpleNamespace(system_prompt=None, name="Somnia"),
            provider=SimpleNamespace(name="openai", model="gpt-5"),
        )
        runtime.mcp_registry = SimpleNamespace(all_servers=[], server_tools={})
        runtime.execution_mode = "accept_edits"
        runtime.skill_loader = SimpleNamespace(prompt_index=lambda: "short skill index", descriptions=lambda: "long skill description")
        runtime.current_working_file_context = lambda: "Active working file cache:\n- Path: app.py"
        runtime.current_working_file_path = lambda: "app/main.py"

        sections = OpenAgentRuntime.build_system_prompt_sections(runtime)

        self.assertEqual([section["id"] for section in sections], ["core", "runtime", "skills", "mcp", "repo"])
        self.assertEqual(sections[0]["title"], "A. Core System Prompt")
        self.assertEqual(sections[1]["title"], "B. Runtime Injection")
        self.assertNotIn("Active working file cache:", sections[1]["content"])
        self.assertIn("Available skills:", sections[2]["content"])
        self.assertIn("short skill index", sections[2]["content"])
        self.assertNotIn("long skill description", sections[2]["content"])
        self.assertIn("MCP tools are provided through the tool schema", sections[3]["content"])
        self.assertNotIn("gitnexus", sections[3]["content"].lower())
        self.assertNotIn("playwright", sections[3]["content"].lower())
        self.assertIn("Use repo guidance.", sections[4]["content"])
        self.assertIn("Use app guidance.", sections[4]["content"])

    def test_tool_schemas_for_model_are_sorted_for_stable_provider_prefixes(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        registry = ToolRegistry()
        registry.register(ToolDefinition("z_tool", "z", {"type": "object", "properties": {}}, lambda ctx, payload: "z"))
        registry.register(ToolDefinition("a_tool", "a", {"type": "object", "properties": {}}, lambda ctx, payload: "a"))
        runtime.registry = registry
        runtime.worker_registry = ToolRegistry()

        schemas = OpenAgentRuntime._tool_schemas_for_model(runtime, "lead")

        self.assertEqual([schema["name"] for schema in schemas], ["a_tool", "z_tool"])
        self.assertIn("importance", schemas[0]["input_schema"]["properties"])

    def test_complete_prepares_anthropic_system_prompt_before_provider_call(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            provider=SimpleNamespace(provider_type="anthropic", max_tokens=1024),
        )
        seen: dict[str, object] = {}

        class FakeProvider:
            settings = SimpleNamespace(provider_type="anthropic", max_tokens=1024)

            def complete(self, **kwargs):
                seen["system_prompt"] = kwargs["system_prompt"]
                return AssistantTurn(stop_reason="end_turn", text_blocks=["done"], tool_calls=[])

        runtime.provider = FakeProvider()
        runtime._provider_for_messages = lambda messages: None
        runtime._raise_if_interrupted = lambda should_interrupt=None: None

        turn = OpenAgentRuntime.complete(
            runtime,
            "## A. Core System Prompt\nStable.\n\n## B. Runtime Injection\nDynamic.",
            [{"role": "user", "content": "hello"}],
            [],
        )

        self.assertEqual(turn.text_blocks, ["done"])
        self.assertIsInstance(seen["system_prompt"], list)
        self.assertEqual(seen["system_prompt"][0]["dynamic"], False)
        self.assertEqual(seen["system_prompt"][1]["dynamic"], True)

    def test_complete_keeps_openai_system_prompt_as_string(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            provider=SimpleNamespace(provider_type="openai", max_tokens=1024),
        )
        seen: dict[str, object] = {}

        class FakeProvider:
            settings = SimpleNamespace(provider_type="openai", max_tokens=1024)

            def complete(self, **kwargs):
                seen["system_prompt"] = kwargs["system_prompt"]
                return AssistantTurn(stop_reason="end_turn", text_blocks=["done"], tool_calls=[])

        runtime.provider = FakeProvider()
        runtime._provider_for_messages = lambda messages: None
        runtime._raise_if_interrupted = lambda should_interrupt=None: None

        OpenAgentRuntime.complete(
            runtime,
            "## A. Core System Prompt\nStable.\n\n## B. Runtime Injection\nDynamic.",
            [{"role": "user", "content": "hello"}],
            [],
        )

        self.assertIsInstance(seen["system_prompt"], str)

    def test_build_system_prompt_includes_gitnexus_guidance_only_when_available(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=Path("D:/workspace"),
            agent=SimpleNamespace(system_prompt=None, name="Somnia"),
            provider=SimpleNamespace(name="openai", model="gpt-5"),
        )
        runtime.execution_mode = "accept_edits"
        runtime.skill_loader = SimpleNamespace(descriptions=lambda: "none")
        runtime.current_working_file_context = lambda: ""
        runtime.mcp_registry = SimpleNamespace(
            all_servers=[SimpleNamespace(name="gitnexus", enabled=True)],
            server_tools={"gitnexus": ["query", "impact"]},
        )

        prompt = OpenAgentRuntime.build_system_prompt(runtime)

        self.assertIn("GitNexus integration:", prompt)
        self.assertIn("GitNexus is available through MCP-backed code intelligence tools", prompt)
        self.assertIn("treat those requirements as binding", prompt)
        self.assertIn("retry with a narrower target, file_path, or route", prompt)
        self.assertIn("require normal Somnia authorization", prompt)

    def test_build_system_prompt_omits_gitnexus_guidance_when_server_is_disabled(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=Path("D:/workspace"),
            agent=SimpleNamespace(system_prompt=None, name="Somnia"),
            provider=SimpleNamespace(name="openai", model="gpt-5"),
        )
        runtime.execution_mode = "accept_edits"
        runtime.skill_loader = SimpleNamespace(descriptions=lambda: "none")
        runtime.current_working_file_context = lambda: ""
        runtime.mcp_registry = SimpleNamespace(
            all_servers=[SimpleNamespace(name="gitnexus", enabled=False)],
            server_tools={"gitnexus": ["query", "impact"]},
        )

        prompt = OpenAgentRuntime.build_system_prompt(runtime)

        self.assertNotIn("GitNexus integration:", prompt)
        self.assertNotIn("GitNexus is available through MCP-backed code intelligence tools", prompt)

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

    def test_build_system_prompt_includes_project_instructions_from_agents_md(self) -> None:
        root = self._stable_test_dir("project-instructions-agents")
        (root / "AGENTS.md").write_text("Use project tests.\n", encoding="utf-8")
        (root / "CLAUDE.md").write_text("Use claude tests.\n", encoding="utf-8")
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=root,
            agent=SimpleNamespace(system_prompt=None, name="Somnia"),
            provider=SimpleNamespace(name="openai", model="gpt-5"),
        )
        runtime.execution_mode = "accept_edits"
        runtime.skill_loader = SimpleNamespace(descriptions=lambda: "none")

        prompt = OpenAgentRuntime.build_system_prompt(runtime)

        self.assertIn("Project instructions:", prompt)
        self.assertIn("repository-owner operating rules", prompt)
        self.assertIn("MUST, NEVER, and required workflow", prompt)
        self.assertIn("binding instructions, not suggestions", prompt)
        self.assertIn('source="AGENTS.md"', prompt)
        self.assertIn("Use project tests.", prompt)
        self.assertNotIn("Use claude tests.", prompt)

    def test_build_system_prompt_prioritizes_project_specific_tools_over_general_fallbacks(self) -> None:
        root = self._stable_test_dir("project-tool-priority")
        (root / "AGENTS.md").write_text("Use indexed tools before grep.\n", encoding="utf-8")
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=root,
            agent=SimpleNamespace(system_prompt=None, name="Somnia"),
            provider=SimpleNamespace(name="openai", model="gpt-5"),
        )
        runtime.execution_mode = "accept_edits"
        runtime.skill_loader = SimpleNamespace(descriptions=lambda: "none")

        prompt = OpenAgentRuntime.build_system_prompt(runtime)

        project_tool_priority = "Follow project instructions first"
        generic_fallback_guidance = "Treat generic workspace tools as fallbacks for overlapping work"
        self.assertIn(project_tool_priority, prompt)
        self.assertIn(generic_fallback_guidance, prompt)
        self.assertLess(prompt.index(project_tool_priority), prompt.index(generic_fallback_guidance))
        self.assertIn("Prefer MCP and project-specific tools over generic filesystem/search tools", prompt)
        self.assertNotIn("Use `grep` instead of shell content search commands", prompt)

    def test_build_system_prompt_uses_claude_md_when_agents_md_is_missing(self) -> None:
        root = self._stable_test_dir("project-instructions-claude")
        (root / "CLAUDE.md").write_text("Use claude fallback.\n", encoding="utf-8")
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=root,
            agent=SimpleNamespace(system_prompt=None, name="Somnia"),
            provider=SimpleNamespace(name="openai", model="gpt-5"),
        )
        runtime.execution_mode = "accept_edits"
        runtime.skill_loader = SimpleNamespace(descriptions=lambda: "none")

        prompt = OpenAgentRuntime.build_system_prompt(runtime)

        self.assertIn('source="CLAUDE.md"', prompt)
        self.assertIn("Use claude fallback.", prompt)

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

    def test_agent_session_ignores_legacy_read_file_overlap_state_payload(self) -> None:
        restored = AgentSession.from_payload(
            {
                "id": "session-1",
                "messages": [],
                "read_file_overlap_state": {
                    "source_tool_call_ids": ["call-2"],
                    "coverage": {"demo.txt": [[1, 10]]},
                },
            }
        )

        self.assertEqual(restored.id, "session-1")
        self.assertFalse(hasattr(restored, "read_file_overlap_state"))

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

    def test_build_payload_messages_preserves_large_duplicate_tool_results(self) -> None:
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

        self.assertEqual(payload[1]["content"][0]["content"], duplicate_content)
        self.assertEqual(payload[3]["content"][0]["content"], duplicate_content)
        self.assertNotIn("raw_output", payload[1]["content"][0])
        self.assertNotIn("log_id", payload[1]["content"][0])
        self.assertIn("raw_output", messages[1]["content"][0])
        self.assertEqual(messages[1]["content"][0]["content"], duplicate_content)

    def test_build_payload_messages_preserves_overlapping_read_file_results(self) -> None:
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

        self.assertEqual(payload[1]["content"][0]["content"], older_content)
        self.assertEqual(payload[3]["content"][0]["content"], newer_content)
        self.assertEqual(messages[1]["content"][0]["content"], older_content)

    def test_authorize_tool_call_blocks_non_edit_tools_in_accept_edits_mode(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "accept_edits"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}

        blocked = OpenAgentRuntime.authorize_tool_call(runtime, "bash", {"command": "git status"})
        allowed = OpenAgentRuntime.authorize_tool_call(runtime, "write_file", {"path": "demo.txt", "content": "ok"})
        read_image_allowed = OpenAgentRuntime.authorize_tool_call(runtime, "read_image", {"path": "scripts/image.png"})

        self.assertIn("requires explicit user approval", blocked)
        self.assertNotIn("! Yolo", blocked)
        self.assertIsNone(allowed)
        self.assertIsNone(read_image_allowed)

    def test_authorize_tool_call_allows_read_only_gitnexus_mcp_tools_by_default(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "plan"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}

        impact_allowed = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "mcp__gitnexus__impact",
            {"target": "authorize_tool_call", "direction": "upstream"},
        )
        detect_changes_allowed = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "mcp__gitnexus__detect_changes",
            {"scope": "all"},
        )
        rename_blocked = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "mcp__gitnexus__rename",
            {"symbol_name": "OldName", "new_name": "NewName", "dry_run": False},
        )
        group_sync_blocked = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "mcp__gitnexus__group_sync",
            {"name": "workspace"},
        )
        blocked_other_mcp = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "mcp__filesystem__read_file",
            {"path": "demo.txt"},
        )

        self.assertIsNone(impact_allowed)
        self.assertIsNone(detect_changes_allowed)
        self.assertIn("requires broader tool access", rename_blocked)
        self.assertIn("requires broader tool access", group_sync_blocked)
        self.assertIn("requires broader tool access", blocked_other_mcp)

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

        created = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "task_create_batch",
            {"tasks": [{"subject": "Analyze folding system"}]},
        )
        updated = OpenAgentRuntime.authorize_tool_call(runtime, "task_update", {"task_id": 1, "status": "in_progress"})
        claimed = OpenAgentRuntime.authorize_tool_call(runtime, "claim_task", {"task_id": 1})

        self.assertIsNone(created)
        self.assertIsNone(updated)
        self.assertIsNone(claimed)

    def test_authorize_tool_call_always_allows_submit_plan(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}

        for mode in ("shortcuts", "plan", "accept_edits"):
            runtime.execution_mode = mode
            with self.subTest(mode=mode):
                result = OpenAgentRuntime.authorize_tool_call(
                    runtime,
                    "submit_plan",
                    {"plan": "Request approval for the next implementation step."},
                )
                self.assertIsNone(result)

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

        blocked = OpenAgentRuntime.authorize_tool_call(
            runtime,
            "task_create_batch",
            {"tasks": [{"subject": "Analyze folding system"}]},
        )

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

    def test_request_authorization_returns_cached_once_without_prompting_user(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "accept_edits"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {"bash": 1}
        runtime.authorization_request_handler = lambda **kwargs: self.fail("cached authorization should not prompt")

        result = json.loads(OpenAgentRuntime.request_authorization(runtime, "bash", "Need one shell command"))

        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["scope"], "once")
        self.assertTrue(result["cached"])
        self.assertEqual(runtime._once_authorized_tools, {"bash": 1})

    def test_request_authorization_returns_approved_in_yolo_without_prompting_user(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "yolo"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}
        runtime.authorization_request_handler = lambda **kwargs: self.fail("yolo authorization should not prompt")

        result = json.loads(OpenAgentRuntime.request_authorization(runtime, "bash", "Need one shell command"))

        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["scope"], "mode")
        self.assertTrue(result["cached"])

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

    def test_worker_request_authorization_returns_cached_workspace_without_lead_request(self) -> None:
        root = self._stable_test_dir("worker-auth-cached-workspace")
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "accept_edits"
        runtime._workspace_authorized_tools = {"bash"}
        runtime._once_authorized_tools = {}
        runtime._worker_authorized_tools = set()
        runtime._worker_once_authorized_tools = {}
        runtime.bus = MessageBus(InboxStore(root / "inbox"))
        runtime.request_tracker = RequestTracker(root / "requests")
        runtime.team_manager = SimpleNamespace(_member_session_id=lambda actor: "session-1")

        worker_registry = ToolRegistry()
        OpenAgentRuntime._register_worker_local_tools(runtime, worker_registry)
        output = worker_registry.execute(
            ToolExecutionContext(runtime=runtime, session=None, actor="Worker", trace_id="worker"),
            "request_authorization",
            {"tool_name": "bash", "reason": "Need to simulate work"},
        )

        payload = json.loads(output)
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["scope"], "workspace")
        self.assertTrue(payload["cached"])
        self.assertEqual(runtime.bus.read_inbox("lead", session_id="session-1"), [])

    def test_worker_request_authorization_in_yolo_still_waits_for_lead_approval(self) -> None:
        root = self._stable_test_dir("worker-auth-yolo")
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "yolo"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}
        runtime._worker_authorized_tools = set()
        runtime._worker_once_authorized_tools = {}
        runtime.bus = MessageBus(InboxStore(root / "inbox"))
        runtime.request_tracker = RequestTracker(root / "requests")
        runtime.team_manager = SimpleNamespace(_member_session_id=lambda actor: "session-1")

        worker_registry = ToolRegistry()
        OpenAgentRuntime._register_worker_local_tools(runtime, worker_registry)
        result: dict[str, str] = {}
        done = Event()

        def run_worker_request() -> None:
            result["output"] = worker_registry.execute(
                ToolExecutionContext(runtime=runtime, session=None, actor="Worker", trace_id="worker"),
                "request_authorization",
                {"tool_name": "bash", "reason": "Need to simulate work"},
            )
            done.set()

        thread = Thread(target=run_worker_request)
        thread.start()
        deadline = time.monotonic() + 2
        lead_messages: list[dict] = []
        while time.monotonic() < deadline:
            lead_messages = runtime.bus.read_inbox("lead", session_id="session-1")
            if lead_messages:
                break
            time.sleep(0.02)

        self.assertTrue(lead_messages)
        self.assertFalse(done.is_set())

        lead_registry = ToolRegistry()
        register_team_tools(
            lead_registry,
            SimpleNamespace(member_names=lambda session_id=None: ["Worker"]),
            runtime.bus,
            runtime.request_tracker,
        )
        approval_output = lead_registry.execute(
            ToolExecutionContext(runtime=runtime, session=SimpleNamespace(id="session-1"), actor="lead", trace_id="lead"),
            "authorization_approval",
            {"request_id": lead_messages[0]["request_id"], "approve": True, "scope": "once"},
        )
        thread.join(timeout=2)

        self.assertIn("Authorization approved", approval_output)
        self.assertTrue(done.is_set())
        payload = json.loads(result["output"])
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["scope"], "once")

    def test_worker_request_authorization_waits_for_lead_approval_and_grants_once(self) -> None:
        root = self._stable_test_dir("worker-auth-lead-approval")
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.execution_mode = "accept_edits"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}
        runtime._worker_authorized_tools = set()
        runtime._worker_once_authorized_tools = {}
        runtime.authorization_request_handler = lambda **kwargs: self.fail("lead already has workspace permission")
        runtime.bus = MessageBus(InboxStore(root / "inbox"))
        runtime.request_tracker = RequestTracker(root / "requests")
        runtime.team_manager = SimpleNamespace(_member_session_id=lambda actor: "session-1")

        worker_registry = ToolRegistry()
        OpenAgentRuntime._register_worker_local_tools(runtime, worker_registry)
        result: dict[str, str] = {}
        done = Event()

        def run_worker_request() -> None:
            result["output"] = worker_registry.execute(
                ToolExecutionContext(runtime=runtime, session=None, actor="Worker", trace_id="worker"),
                "request_authorization",
                {"tool_name": "edit_file", "reason": "Need to update a file"},
            )
            done.set()

        thread = Thread(target=run_worker_request)
        thread.start()
        deadline = time.monotonic() + 2
        lead_messages: list[dict] = []
        while time.monotonic() < deadline:
            lead_messages = runtime.bus.read_inbox("lead", session_id="session-1")
            if lead_messages:
                break
            time.sleep(0.02)
        self.assertTrue(lead_messages)
        self.assertFalse(done.is_set())

        lead_registry = ToolRegistry()
        register_team_tools(
            lead_registry,
            SimpleNamespace(member_names=lambda session_id=None: ["Worker"]),
            runtime.bus,
            runtime.request_tracker,
        )
        request_id = lead_messages[0]["request_id"]
        approval_output = lead_registry.execute(
            ToolExecutionContext(runtime=runtime, session=SimpleNamespace(id="session-1"), actor="lead", trace_id="lead"),
            "authorization_approval",
            {"request_id": request_id, "approve": True, "scope": "once"},
        )
        thread.join(timeout=2)

        self.assertIn("Authorization approved", approval_output)
        self.assertTrue(done.is_set())
        payload = json.loads(result["output"])
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["scope"], "once")
        self.assertEqual(lead_messages[0]["type"], "authorization_request")
        self.assertEqual(runtime.bus.read_inbox("Worker", session_id="session-1")[0]["type"], "authorization_response")
        self.assertEqual(runtime._worker_once_authorized_tools, {"Worker\0edit_file": 1})
        self.assertIsNone(
            OpenAgentRuntime.authorize_tool_call(
                runtime,
                "edit_file",
                {"path": "demo.txt", "content": "updated"},
                ctx=SimpleNamespace(actor="Worker"),
            )
        )
        self.assertEqual(runtime._worker_once_authorized_tools, {})

    def test_authorization_approval_waits_for_lead_to_get_user_permission_first(self) -> None:
        root = self._stable_test_dir("worker-auth-lead-needs-user")
        requests: list[dict] = []
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(storage=SimpleNamespace(data_dir=root / ".open_somnia"))
        runtime.execution_mode = "accept_edits"
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}
        runtime._worker_authorized_tools = set()
        runtime._worker_once_authorized_tools = {}
        runtime.authorization_request_handler = lambda **kwargs: requests.append(dict(kwargs)) or {
            "status": "approved",
            "scope": "once",
            "reason": "Allowed once.",
        }
        runtime.bus = MessageBus(InboxStore(root / "inbox"))
        runtime.request_tracker = RequestTracker(root / "requests")
        runtime.team_manager = SimpleNamespace(_member_session_id=lambda actor: "session-1")

        worker_registry = ToolRegistry()
        OpenAgentRuntime._register_worker_local_tools(runtime, worker_registry)
        result: dict[str, str] = {}
        done = Event()
        thread = Thread(
            target=lambda: (
                result.setdefault(
                    "output",
                    worker_registry.execute(
                        ToolExecutionContext(runtime=runtime, session=None, actor="Worker", trace_id="worker"),
                        "request_authorization",
                        {"tool_name": "bash", "reason": "Need to simulate work"},
                    ),
                ),
                done.set(),
            )
        )
        thread.start()
        deadline = time.monotonic() + 2
        lead_messages: list[dict] = []
        while time.monotonic() < deadline:
            lead_messages = runtime.bus.read_inbox("lead", session_id="session-1")
            if lead_messages:
                break
            time.sleep(0.02)
        self.assertTrue(lead_messages)
        request_id = lead_messages[0]["request_id"]

        lead_registry = ToolRegistry()
        register_team_tools(
            lead_registry,
            SimpleNamespace(member_names=lambda session_id=None: ["Worker"]),
            runtime.bus,
            runtime.request_tracker,
        )
        blocked_output = lead_registry.execute(
            ToolExecutionContext(runtime=runtime, session=SimpleNamespace(id="session-1"), actor="lead", trace_id="lead"),
            "authorization_approval",
            {"request_id": request_id, "approve": True, "scope": "once"},
        )

        self.assertIn("lead is not authorized", blocked_output)
        self.assertFalse(done.is_set())
        self.assertEqual(runtime.request_tracker.get_authorization_request(request_id)["status"], "pending")

        OpenAgentRuntime.request_authorization(runtime, "bash", "Lead approves Worker request")
        approved_output = lead_registry.execute(
            ToolExecutionContext(runtime=runtime, session=SimpleNamespace(id="session-1"), actor="lead", trace_id="lead"),
            "authorization_approval",
            {"request_id": request_id, "approve": True, "scope": "once"},
        )
        thread.join(timeout=2)

        self.assertIn("Authorization approved", approved_output)
        self.assertTrue(done.is_set())
        payload = json.loads(result["output"])
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["scope"], "once")
        self.assertEqual(requests[0]["tool_name"], "bash")
        self.assertIn("Lead approves Worker request", requests[0]["reason"])

    def test_workspace_authorization_is_persisted_under_openagent_directory(self) -> None:
        root = self._stable_test_dir("workspace-auth")
        data_dir = root / ".open_somnia"
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

    def test_builtin_authorization_file_allows_external_mcp_tools(self) -> None:
        root = self._stable_test_dir("builtin-auth")
        builtin_path = root / "permissions.json"
        builtin_path.write_text(
            json.dumps({"allow": ["mcp__external__read_resource", "  mcp__external__query  ", ""]}),
            encoding="utf-8",
        )
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.BUILTIN_PERMISSIONS_FILE = builtin_path
        runtime.execution_mode = "plan"
        runtime._builtin_authorized_tools = OpenAgentRuntime._load_builtin_authorizations(runtime)
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}
        runtime.authorization_request_handler = lambda **kwargs: self.fail("builtin authorization should be cached")

        self.assertEqual(runtime._builtin_authorized_tools, {"mcp__external__read_resource", "mcp__external__query"})
        self.assertIsNone(
            OpenAgentRuntime.authorize_tool_call(runtime, "mcp__external__read_resource", {"uri": "demo://resource"})
        )
        self.assertIn(
            "requires broader tool access",
            OpenAgentRuntime.authorize_tool_call(runtime, "mcp__external__write_resource", {"uri": "demo://resource"}),
        )
        cached = json.loads(OpenAgentRuntime.request_authorization(runtime, "mcp__external__query", "Read external MCP data"))
        self.assertEqual(cached["status"], "approved")
        self.assertEqual(cached["scope"], "builtin")
        self.assertTrue(cached["cached"])

    def test_workspace_authorization_persistence_excludes_builtin_authorizations(self) -> None:
        root = self._stable_test_dir("workspace-auth-excludes-builtin")
        data_dir = root / ".open_somnia"
        builtin_path = root / "permissions.json"
        builtin_path.write_text(json.dumps({"allow": ["mcp__external__read_resource"]}), encoding="utf-8")
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.BUILTIN_PERMISSIONS_FILE = builtin_path
        runtime.settings = SimpleNamespace(storage=SimpleNamespace(data_dir=data_dir))
        runtime.execution_mode = "plan"
        runtime._builtin_authorized_tools = OpenAgentRuntime._load_builtin_authorizations(runtime)
        runtime._workspace_authorized_tools = set()
        runtime._once_authorized_tools = {}
        runtime.authorization_request_handler = lambda **kwargs: {
            "status": "approved",
            "scope": "workspace",
            "reason": "Allowed in this workspace.",
        }

        OpenAgentRuntime.request_authorization(runtime, "edit_file", "Need to patch a file")

        saved = json.loads((data_dir / "permissions.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, {"authorized_tools": ["edit_file"]})

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

    def test_set_vision_model_updates_runtime_and_persists(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=Path("D:/workspace"),
            raw_config={"providers": {}},
            provider=ProviderSettings(
                name="openai",
                provider_type="openai",
                model="text-model",
                max_tokens=8000,
            ),
            vision_provider=None,
            vision_model=None,
            provider_profiles={
                "openai": ProviderProfileSettings(
                    name="openai",
                    provider_type="openai",
                    models=["text-model", "vision-model"],
                    default_model="text-model",
                    api_key="",
                    base_url="https://api.openai.com/v1",
                    max_tokens=4096,
                    timeout_seconds=60,
                )
            },
        )
        runtime.compact_manager = SimpleNamespace(provider=None, model_max_tokens=0)
        runtime.provider = "old-provider"

        reloaded = SimpleNamespace(
            vision_provider="openai",
            vision_model="vision-model",
            raw_config={"routing": {"vision_provider": "openai", "vision_model": "vision-model"}},
        )
        with patch("open_somnia.runtime.agent.persist_vision_model") as mock_persist, patch(
            "open_somnia.runtime.agent.load_settings", return_value=reloaded
        ):
            message = OpenAgentRuntime.set_vision_model(runtime, "openai", "vision-model")

        self.assertIn("vision-model", message)
        self.assertEqual(runtime.settings.vision_provider, "openai")
        self.assertEqual(runtime.settings.vision_model, "vision-model")
        mock_persist.assert_called_once_with(runtime.settings, "openai", "vision-model", scope="project")

    def test_set_reasoning_level_updates_runtime_and_compact_manager(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=Path("D:/workspace"),
            raw_config={"providers": {}},
            provider=ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-5",
                max_tokens=8000,
                reasoning_level="medium",
            ),
            provider_profiles={
                "anthropic": ProviderProfileSettings(
                    name="anthropic",
                    provider_type="anthropic",
                    models=["claude-sonnet-4-5"],
                    model_traits={"claude-sonnet-4-5": ModelTraits(reasoning_level="medium")},
                    default_model="claude-sonnet-4-5",
                    api_key="",
                    base_url="https://api.anthropic.com",
                    max_tokens=8000,
                    timeout_seconds=60,
                )
            },
        )
        runtime.compact_manager = SimpleNamespace(provider=None, model_max_tokens=0)
        runtime.provider = "old-provider"
        runtime._instantiate_provider = lambda provider_settings: {
            "provider": provider_settings.name,
            "model": provider_settings.model,
            "reasoning_level": provider_settings.reasoning_level,
        }

        with patch("open_somnia.runtime.agent.persist_provider_reasoning_level") as mock_persist:
            message = OpenAgentRuntime.set_reasoning_level(runtime, "high")

        self.assertIn("high", message)
        self.assertIn("saved it to .open_somnia/open_somnia.toml", message)
        self.assertEqual(runtime.settings.provider.reasoning_level, "high")
        self.assertEqual(runtime.settings.provider.model, "claude-sonnet-4-5")
        self.assertEqual(runtime.provider, {"provider": "anthropic", "model": "claude-sonnet-4-5", "reasoning_level": "high"})
        self.assertEqual(
            runtime.compact_manager.provider,
            {"provider": "anthropic", "model": "claude-sonnet-4-5", "reasoning_level": "high"},
        )
        self.assertEqual(runtime.compact_manager.model_max_tokens, 8000)
        self.assertEqual(runtime.settings.provider_profiles["anthropic"].model_traits["claude-sonnet-4-5"].reasoning_level, "high")
        self.assertIsNone(runtime.settings.provider_profiles["anthropic"].reasoning_level)
        mock_persist.assert_called_once_with(runtime.settings, "anthropic", "claude-sonnet-4-5", "high")

    def test_set_reasoning_level_auto_clears_runtime_and_compact_manager_state(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            workspace_root=Path("D:/workspace"),
            raw_config={"providers": {"anthropic": {"reasoning_level": "high"}}},
            provider=ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-5",
                max_tokens=8000,
                reasoning_level="high",
            ),
            provider_profiles={
                "anthropic": ProviderProfileSettings(
                    name="anthropic",
                    provider_type="anthropic",
                    models=["claude-sonnet-4-5"],
                    model_traits={"claude-sonnet-4-5": ModelTraits(reasoning_level="high")},
                    default_model="claude-sonnet-4-5",
                    api_key="",
                    base_url="https://api.anthropic.com",
                    max_tokens=8000,
                    timeout_seconds=60,
                )
            },
        )
        runtime.compact_manager = SimpleNamespace(provider=None, model_max_tokens=0)
        runtime.provider = "old-provider"
        runtime._instantiate_provider = lambda provider_settings: {
            "provider": provider_settings.name,
            "model": provider_settings.model,
            "reasoning_level": provider_settings.reasoning_level,
        }

        with patch("open_somnia.runtime.agent.persist_provider_reasoning_level") as mock_persist:
            message = OpenAgentRuntime.set_reasoning_level(runtime, None)

        self.assertIn("to 'auto'", message)
        self.assertEqual(runtime.settings.provider.reasoning_level, None)
        self.assertEqual(runtime.provider, {"provider": "anthropic", "model": "claude-sonnet-4-5", "reasoning_level": None})
        self.assertEqual(
            runtime.compact_manager.provider,
            {"provider": "anthropic", "model": "claude-sonnet-4-5", "reasoning_level": None},
        )
        self.assertEqual(runtime.compact_manager.model_max_tokens, 8000)
        self.assertIsNone(runtime.settings.provider_profiles["anthropic"].model_traits["claude-sonnet-4-5"].reasoning_level)
        self.assertEqual(runtime.settings.provider_profiles["anthropic"].reasoning_level, None)
        mock_persist.assert_called_once_with(runtime.settings, "anthropic", "claude-sonnet-4-5", None)

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
        transcript_root = self._stable_test_dir("visible-progress-thinking") / "transcripts"
        runtime.transcript_store = SimpleNamespace(root=transcript_root, append=lambda *args, **kwargs: None)

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

    def test_run_turn_accepts_embedded_user_multimodal_message(self) -> None:
        detection_calls: list[str] = []
        transcript_entries: list[dict] = []
        loop_messages: list[dict] = []
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.transcript_store = SimpleNamespace(append=lambda session_id, payload: transcript_entries.append(payload))
        runtime._run_topic_shift_assist = lambda session, latest_user_message: detection_calls.append(latest_user_message)
        runtime._run_automatic_context_janitor = lambda session: None
        runtime._agent_loop = lambda session, **kwargs: loop_messages.extend(session.messages) or "loop-result"

        session = AgentSession(id="session-1", messages=[{"role": "assistant", "content": "Ready."}])
        encoded_message = encode_embedded_user_message(
            make_user_multimodal_message(
                "look at this image",
                [
                    {
                        "type": "input_image",
                        "path": "images/tiny.png",
                        "absolute_path": str(Path.cwd() / "images" / "tiny.png"),
                        "media_type": "image/png",
                    }
                ],
            )
        )

        result = OpenAgentRuntime.run_turn(runtime, session, encoded_message)

        self.assertEqual(result, "loop-result")
        self.assertEqual(detection_calls, ["look at this image"])
        self.assertEqual(transcript_entries[0]["role"], "user")
        self.assertEqual(transcript_entries[0]["content"][0], {"type": "text", "text": "look at this image"})
        self.assertEqual(transcript_entries[0]["content"][1]["type"], IMAGE_REFERENCE_BLOCK_TYPE)
        self.assertEqual(transcript_entries[0]["content"][1]["path"], "images/tiny.png")
        self.assertEqual(loop_messages[-1]["content"][1]["type"], "input_image")

    def test_read_image_tool_returns_structured_tool_result_content(self) -> None:
        image_root = self._stable_test_dir("read-image-tool")
        image_path = image_root / "tiny.png"
        image_path.write_bytes(self._TINY_PNG_BYTES)
        ctx = SimpleNamespace(runtime=SimpleNamespace(settings=SimpleNamespace(workspace_root=Path.cwd())))

        result = read_image(ctx, {"path": image_path.relative_to(Path.cwd()).as_posix()})

        self.assertIsInstance(result, dict)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["media_type"], "image/png")
        self.assertEqual(result["tool_result_text"], result["message"])
        self.assertEqual(result["tool_result_content"][0]["type"], "text")
        self.assertEqual(result["tool_result_content"][1]["type"], "input_image")
        self.assertIn("Visual data attached for the next model turn only", result["tool_result_text"])

    def test_mcp_image_content_becomes_structured_tool_result_content(self) -> None:
        rendered = _render_mcp_result(
            {
                "content": [
                    {"type": "text", "text": "Screenshot captured."},
                    {
                        "type": "image",
                        "data": base64.b64encode(self._TINY_PNG_BYTES).decode("ascii"),
                        "mimeType": "image/png",
                    },
                ]
            }
        )

        self.assertIsInstance(rendered, dict)
        self.assertEqual(rendered["status"], "ok")
        self.assertIn("Screenshot captured.", rendered["tool_result_text"])
        self.assertEqual(rendered["tool_result_content"][0]["type"], "text")
        self.assertEqual(rendered["tool_result_content"][1]["type"], "image_url")

        result_item = make_tool_result_item(
            "call-1",
            rendered,
            rendered_output=json.dumps(rendered, ensure_ascii=False),
        )

        blocks = active_tool_result_content_blocks(result_item)
        self.assertEqual(blocks[1]["type"], "image_url")
        self.assertTrue(blocks[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_mcp_local_image_link_becomes_image_reference_content(self) -> None:
        rendered = _render_mcp_result(
            {
                "content": [
                    {
                        "type": "text",
                        "text": "### Result\n- [Screenshot of viewport](./feishu-login-qr.png)",
                    }
                ]
            },
            cwd=Path.cwd(),
        )

        self.assertIsInstance(rendered, dict)
        self.assertEqual(rendered["status"], "ok")
        self.assertIn("feishu-login-qr.png", rendered["tool_result_text"])
        self.assertEqual(rendered["tool_result_content"][1]["type"], IMAGE_REFERENCE_BLOCK_TYPE)
        self.assertEqual(rendered["tool_result_content"][1]["path"], "./feishu-login-qr.png")
        self.assertEqual(rendered["tool_result_content"][1]["media_type"], "image/png")

        result_item = make_tool_result_item(
            "call-1",
            rendered,
            rendered_output=json.dumps(rendered, ensure_ascii=False),
        )

        self.assertEqual(result_item["content_blocks"][1]["type"], IMAGE_REFERENCE_BLOCK_TYPE)

    def test_mcp_remote_image_link_uses_remote_hint_instead_of_read_image(self) -> None:
        rendered = _render_mcp_result(
            {
                "content": [
                    {
                        "type": "text",
                        "text": "### Result\n- [Screenshot of viewport](./captcha.png)",
                    }
                ]
            },
            transport="http",
        )

        self.assertIsInstance(rendered, dict)
        self.assertEqual(rendered["status"], "ok")
        self.assertNotIn('read_image(path="./captcha.png")', rendered["tool_result_text"])
        self.assertIn("hosted by the MCP server", rendered["tool_result_text"])

    def test_consume_ephemeral_image_blocks_rewrites_user_image_inputs_to_references(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look at this"},
                    {
                        "type": "input_image",
                        "path": "one.png",
                        "absolute_path": "D:/workspace/one.png",
                        "media_type": "image/png",
                    },
                ],
            }
        ]

        changed = consume_ephemeral_image_blocks(messages)

        self.assertTrue(changed)
        self.assertEqual(messages[0]["content"][1]["type"], IMAGE_REFERENCE_BLOCK_TYPE)
        self.assertEqual(messages[0]["content"][1]["path"], "one.png")

    def test_consume_ephemeral_image_blocks_rewrites_tool_results_to_reference_text(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-1",
                        "content": "Loaded image one.png (image/png) for model inspection.",
                        "tool_result_text": "Loaded image one.png (image/png) for model inspection.",
                        "content_blocks": [
                            {"type": "text", "text": "Loaded image one.png (image/png) for model inspection."},
                            {
                                "type": "input_image",
                                "path": "one.png",
                                "absolute_path": "D:/workspace/one.png",
                                "media_type": "image/png",
                            },
                        ],
                    }
                ],
            }
        ]

        changed = consume_ephemeral_image_blocks(messages)

        self.assertTrue(changed)
        result = messages[0]["content"][0]
        self.assertEqual(result["content_blocks"][1]["type"], IMAGE_REFERENCE_BLOCK_TYPE)
        self.assertEqual(result["tool_result_text"], result["content"])
        self.assertIn("Visual data omitted from active context", result["content"])

    def test_prepare_image_bytes_for_model_rejects_large_input_without_pillow(self) -> None:
        image_root = self._stable_test_dir("read-image-limit")
        image_path = image_root / "large.png"
        image_path.write_bytes(b"x" * (MODEL_IMAGE_INLINE_MAX_BYTES_WITHOUT_PILLOW + 1))

        with (
            patch("open_somnia.runtime.messages.Image", None),
            patch("open_somnia.runtime.messages.ImageOps", None),
        ):
            with self.assertRaisesRegex(ValueError, "too large"):
                prepare_image_bytes_for_model(image_path)

    def test_prepare_image_bytes_for_model_allows_medium_input_without_pillow(self) -> None:
        image_root = self._stable_test_dir("read-image-medium")
        image_path = image_root / "medium.png"
        image_path.write_bytes(b"x" * 360_000)

        with (
            patch("open_somnia.runtime.messages.Image", None),
            patch("open_somnia.runtime.messages.ImageOps", None),
        ):
            media_type, prepared_bytes = prepare_image_bytes_for_model(image_path)

        self.assertEqual(media_type, "image/png")
        self.assertEqual(len(prepared_bytes), 360_000)

    def test_build_payload_messages_strips_image_blocks_from_older_tool_rounds(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": "call-1", "name": "read_image", "input": {"path": "one.png"}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-1",
                        "content": "Loaded image one.png (image/png) for model inspection.",
                        "tool_result_text": "Loaded image one.png (image/png) for model inspection.",
                        "content_blocks": [
                            {"type": "text", "text": "Tool read_image loaded one.png."},
                            {"type": "input_image", "path": "one.png", "absolute_path": "D:/workspace/one.png", "media_type": "image/png"},
                        ],
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": "call-2", "name": "read_image", "input": {"path": "two.png"}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-2",
                        "content": "Loaded image two.png (image/png) for model inspection.",
                        "tool_result_text": "Loaded image two.png (image/png) for model inspection.",
                        "content_blocks": [
                            {"type": "text", "text": "Tool read_image loaded two.png."},
                            {"type": "input_image", "path": "two.png", "absolute_path": "D:/workspace/two.png", "media_type": "image/png"},
                        ],
                    }
                ],
            },
        ]

        payload_messages = build_payload_messages(messages)

        first_result = payload_messages[1]["content"][0]
        second_result = payload_messages[3]["content"][0]
        self.assertNotIn("content_blocks", first_result)
        self.assertNotIn("tool_result_text", first_result)
        self.assertIn("content_blocks", second_result)

    def test_build_payload_messages_keeps_only_latest_user_image_blocks_active(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first image"},
                    {"type": "input_image", "path": "one.png", "absolute_path": "D:/workspace/one.png", "media_type": "image/png"},
                ],
            },
            {
                "role": "assistant",
                "content": "Noted.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "second image"},
                    {"type": "input_image", "path": "two.png", "absolute_path": "D:/workspace/two.png", "media_type": "image/png"},
                ],
            },
        ]

        payload_messages = build_payload_messages(messages)

        self.assertEqual(messages[0]["content"][1]["type"], "input_image")
        self.assertEqual(payload_messages[0]["content"][1]["type"], IMAGE_REFERENCE_BLOCK_TYPE)
        self.assertEqual(payload_messages[2]["content"][1]["type"], "input_image")

    def test_build_payload_messages_strips_all_user_image_blocks_when_latest_user_message_has_no_image(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first image"},
                    {"type": "input_image", "path": "one.png", "absolute_path": "D:/workspace/one.png", "media_type": "image/png"},
                ],
            },
            {
                "role": "assistant",
                "content": "Noted.",
            },
            {
                "role": "user",
                "content": "Now answer using text only.",
            },
        ]

        payload_messages = build_payload_messages(messages)

        self.assertEqual(payload_messages[0]["content"][1]["type"], IMAGE_REFERENCE_BLOCK_TYPE)
        self.assertEqual(payload_messages[0]["content"][1]["path"], "one.png")

    def test_build_payload_messages_keeps_current_turn_user_image_blocks_after_tool_results(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look at this"},
                    {"type": "input_image", "path": "one.png", "absolute_path": "D:/workspace/one.png", "media_type": "image/png"},
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": "call-1", "name": "bash", "input": {"command": "echo ok"}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-1",
                        "content": "ok",
                    }
                ],
            },
        ]

        payload_messages = build_payload_messages(messages)

        self.assertEqual(payload_messages[0]["content"][1]["type"], "input_image")

    def test_active_tool_result_content_blocks_ignores_compacted_image_results(self) -> None:
        blocks = active_tool_result_content_blocks(
            {
                "type": "tool_result",
                "tool_call_id": "call-1",
                "content": '[Image reference | one.png (image/png)] Visual data omitted from active context. Re-read with read_image(path="one.png") if needed.',
                "tool_result_text": "Loaded image one.png (image/png) for model inspection.",
                "content_blocks": [
                    {"type": "text", "text": "Tool read_image loaded one.png."},
                    {"type": "input_image", "path": "one.png", "absolute_path": "D:/workspace/one.png", "media_type": "image/png"},
                ],
            }
        )

        self.assertEqual(blocks, [])

    def test_active_tool_result_content_blocks_ignores_consumed_image_references(self) -> None:
        blocks = active_tool_result_content_blocks(
            {
                "type": "tool_result",
                "tool_call_id": "call-1",
                "content": '[Image reference | one.png (image/png)] Visual data omitted from active context. Re-read with read_image(path="one.png") if needed.',
                "tool_result_text": '[Image reference | one.png (image/png)] Visual data omitted from active context. Re-read with read_image(path="one.png") if needed.',
                "content_blocks": [
                    {
                        "type": IMAGE_REFERENCE_BLOCK_TYPE,
                        "path": "one.png",
                        "absolute_path": "D:/workspace/one.png",
                        "media_type": "image/png",
                    }
                ],
            }
        )

        self.assertEqual(blocks, [])

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
        root = self._stable_test_dir("undo-last-turn")
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
        runtime.build_system_prompt_sections = lambda actor="lead", role="lead coding agent", session=None: [
            {"id": "core", "title": "A. Core System Prompt", "dynamic": False, "chars": 6, "lines": 1, "content": "system"}
        ]
        runtime.transcript_store = SimpleNamespace(transcript_path=lambda session_id: transcripts_dir / f"{session_id}.jsonl")
        session = AgentSession(id="session-1", messages=[{"role": "user", "content": "hello"}])

        with patch.dict(os.environ, {OpenAgentRuntime.DEBUG_PROVIDER_PAYLOAD_ENV: "1"}, clear=False):
            dump_path = OpenAgentRuntime._dump_provider_payload_if_enabled(
                runtime,
                session=session,
                system_prompt="system",
                payload_messages=[{"role": "user", "content": "hello"}],
                tools=[
                    {"name": "read_file"},
                    {"name": "load_skill"},
                    {"name": "mcp__gitnexus__query"},
                    {"name": "TodoWrite"},
                ],
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
        self.assertNotIn("system_prompt", dumped)
        self.assertNotIn("system_prompt_section_summary", dumped)
        self.assertEqual(dumped["system_prompt_sections"][0]["id"], "core")
        self.assertEqual(dumped["system_prompt_sections"][0]["chars"], 6)
        self.assertEqual(dumped["message_summary"]["total"], 1)
        self.assertEqual(dumped["message_summary"]["roles"]["user"], 1)
        self.assertEqual(dumped["tool_schema_summary"]["total"], 4)
        self.assertEqual(dumped["tool_schema_summary"]["groups"]["filesystem"], 1)
        self.assertEqual(dumped["tool_schema_summary"]["groups"]["skill"], 1)
        self.assertEqual(dumped["tool_schema_summary"]["groups"]["mcp:gitnexus"], 1)
        self.assertEqual(dumped["tool_schema_summary"]["groups"]["task"], 1)
        self.assertEqual(dumped["payload_summary"]["kind"], "turn")
        self.assertEqual(dumped["payload_summary"]["system_prompt_chars"], 6)
        self.assertEqual(dumped["payload_summary"]["message_count"], 1)
        self.assertEqual(dumped["payload_summary"]["tool_count"], 4)
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

    def test_openai_provider_reports_empty_choices_as_non_retryable_response_error(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-5.5",
                api_key="test-key",
                base_url="https://example.com/v1",
                timeout_seconds=30,
            )
        )

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b'{"choices":[]}'

        with patch("urllib.request.urlopen", return_value=_Response()):
            with self.assertRaises(ProviderError) as context:
                provider.complete("system", [{"role": "user", "content": "hello"}], [], max_tokens=1024)

        self.assertFalse(context.exception.retryable)
        self.assertIn("did not include any choices", str(context.exception))

    def test_openai_provider_streaming_skips_empty_choice_events(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-5.5",
                api_key="test-key",
                base_url="https://example.com/v1",
                timeout_seconds=30,
            )
        )

        class _StreamingResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def __iter__(self):
                return iter(
                    [
                        b'data: {"choices":[]}\n\n',
                        b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}\n\n',
                        b'data: [DONE]\n\n',
                    ]
                )

        chunks: list[str] = []
        with patch("urllib.request.urlopen", return_value=_StreamingResponse()):
            turn = provider.complete(
                "system",
                [{"role": "user", "content": "hello"}],
                [],
                max_tokens=1024,
                text_callback=chunks.append,
            )

        self.assertEqual(turn.text_blocks, ["hello"])
        self.assertEqual(chunks, ["hello"])

    def test_openai_provider_streams_compatible_reasoning_content(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="deepseek",
                provider_type="openai",
                model="deepseek-reasoner",
                api_key="test-key",
                base_url="https://api.deepseek.com/v1",
                timeout_seconds=30,
            )
        )

        class _StreamingResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def __iter__(self):
                return iter(
                    [
                        b'data: {"choices":[{"delta":{"reasoning_content":"think "},"finish_reason":null}]}\n\n',
                        b'data: {"choices":[{"delta":{"reasoning_content":"first"},"finish_reason":null}]}\n\n',
                        b'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n',
                        b"data: [DONE]\n\n",
                    ]
                )

        text_chunks: list[str] = []
        thinking_events: list[dict] = []
        with patch("urllib.request.urlopen", return_value=_StreamingResponse()):
            turn = provider.complete(
                "system",
                [{"role": "user", "content": "hello"}],
                [],
                max_tokens=1024,
                text_callback=text_chunks.append,
                thinking_callback=thinking_events.append,
            )

        self.assertEqual(text_chunks, ["answer"])
        self.assertEqual([event["delta"] for event in thinking_events], ["think ", "first"])
        self.assertEqual(turn.text_blocks, ["answer"])
        self.assertEqual(turn.content_blocks[0], {"type": "thinking", "thinking": "think first"})
        self.assertEqual(turn.content_blocks[1], {"type": "text", "text": "answer"})

    def test_openai_provider_streams_compatible_think_tags_outside_answer(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="mimo",
                provider_type="openai",
                model="mimo-vl",
                api_key="test-key",
                base_url="https://api.xiaomimimo.com/v1",
                timeout_seconds=30,
            )
        )

        class _StreamingResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def __iter__(self):
                return iter(
                    [
                        b'data: {"choices":[{"delta":{"content":"<thi"},"finish_reason":null}]}\n\n',
                        b'data: {"choices":[{"delta":{"content":"nk>hidden</think>visible"},"finish_reason":"stop"}]}\n\n',
                        b"data: [DONE]\n\n",
                    ]
                )

        text_chunks: list[str] = []
        thinking_events: list[dict] = []
        with patch("urllib.request.urlopen", return_value=_StreamingResponse()):
            turn = provider.complete(
                "system",
                [{"role": "user", "content": "hello"}],
                [],
                max_tokens=1024,
                text_callback=text_chunks.append,
                thinking_callback=thinking_events.append,
            )

        self.assertEqual("".join(text_chunks), "visible")
        self.assertEqual([event["delta"] for event in thinking_events], ["hidden"])
        self.assertEqual(turn.text_blocks, ["visible"])
        self.assertEqual(turn.content_blocks[0], {"type": "thinking", "thinking": "hidden"})
        self.assertEqual(turn.content_blocks[1], {"type": "text", "text": "visible"})

    def test_openai_provider_streaming_treats_null_tool_calls_as_empty(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="mimo_openai",
                provider_type="openai",
                model="mimo-v2.5-pro",
                api_key="test-key",
                base_url="https://api.xiaomimimo.com/v1",
                timeout_seconds=30,
            )
        )

        class _StreamingResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def __iter__(self):
                return iter(
                    [
                        b'data: {"choices":[{"delta":{"content":"hello","tool_calls":null},"finish_reason":null}]}\n\n',
                        b'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}]}\n\n',
                        b"data: [DONE]\n\n",
                    ]
                )

        chunks: list[str] = []
        with patch("urllib.request.urlopen", return_value=_StreamingResponse()):
            turn = provider.complete(
                "system",
                [{"role": "user", "content": "hello"}],
                [],
                max_tokens=1024,
                text_callback=chunks.append,
            )

        self.assertEqual("".join(chunks), "hello world")
        self.assertEqual(turn.text_blocks, ["hello world"])
        self.assertEqual(turn.tool_calls, [])

    def test_openai_provider_tolerates_invalid_chat_completion_tool_arguments(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-5",
                api_key="test-key",
                base_url="https://example.com/v1",
                timeout_seconds=30,
            )
        )

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "Need a tool.",
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": "bash",
                                                "arguments": '{"command":"pwd"',
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=_Response()):
            turn = provider.complete(
                "system",
                [{"role": "user", "content": "hello"}],
                [],
                max_tokens=1024,
            )

        self.assertEqual(turn.stop_reason, "tool_use")
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(turn.tool_calls[0].name, "bash")
        self.assertEqual(turn.tool_calls[0].input, {})
        self.assertIn("invalid JSON", "\n".join(turn.text_blocks))

    def test_openai_provider_debug_request_payload_includes_reasoning_effort(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-5.4",
                api_key="test-key",
                base_url="https://example.com/v1",
                timeout_seconds=30,
                reasoning_level="deep",
            )
        )

        payload = provider.debug_request_payload("system", [{"role": "user", "content": "hello"}], [], 1024, stream=False)

        self.assertEqual(payload["body"]["reasoning"], {"effort": "xhigh"})

    def test_openai_provider_debug_request_payload_defaults_to_reasoning_when_support_flag_is_unset(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-4.1",
                api_key="test-key",
                base_url="https://example.com/v1",
                timeout_seconds=30,
                reasoning_level="high",
            )
        )

        payload = provider.debug_request_payload("system", [{"role": "user", "content": "hello"}], [], 1024, stream=False)

        self.assertEqual(payload["body"]["reasoning"], {"effort": "high"})

    def test_openai_provider_uses_responses_api_for_official_reasoning_summary(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-5",
                api_key="test-key",
                base_url="https://api.openai.com/v1",
                timeout_seconds=30,
                reasoning_level="high",
            )
        )

        payload = provider.debug_request_payload("system", [{"role": "user", "content": "hello"}], [], 1024, stream=False)

        self.assertEqual(payload["url"], "https://api.openai.com/v1/responses")
        self.assertEqual(payload["body"]["instructions"], "system")
        self.assertEqual(payload["body"]["input"], [{"role": "user", "content": "hello"}])
        self.assertEqual(payload["body"]["max_output_tokens"], 1024)
        self.assertEqual(payload["body"]["reasoning"], {"effort": "high", "summary": "auto"})

    def test_openai_provider_maps_responses_reasoning_summary_to_thinking_callback(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-5",
                api_key="test-key",
                base_url="https://api.openai.com/v1",
                timeout_seconds=30,
                reasoning_level="medium",
            )
        )
        captured_requests: list[dict] = []

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps(
                    {
                        "output": [
                            {
                                "type": "reasoning",
                                "summary": [{"type": "summary_text", "text": "Checked constraints."}],
                            },
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "Done."}],
                            },
                        ],
                        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    }
                ).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            captured_requests.append(json.loads(request.data.decode("utf-8")))
            return _Response()

        thinking_events: list[dict] = []
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            turn = provider.complete(
                "system",
                [{"role": "user", "content": "hello"}],
                [],
                max_tokens=1024,
                thinking_callback=thinking_events.append,
            )

        self.assertEqual(captured_requests[0]["reasoning"], {"effort": "medium", "summary": "auto"})
        self.assertEqual(thinking_events, [{"event": "delta", "type": "reasoning_summary", "delta": "Checked constraints."}])
        self.assertEqual(turn.text_blocks, ["Done."])
        self.assertEqual(turn.stop_reason, "end_turn")
        self.assertEqual(turn.usage, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "source": "provider"})

    def test_openai_provider_maps_responses_function_call_to_tool_call(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-5",
                api_key="test-key",
                base_url="https://api.openai.com/v1",
                timeout_seconds=30,
                reasoning_level="medium",
            )
        )

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call-1",
                                "name": "bash",
                                "arguments": '{"command":"pwd","importance":"glance"}',
                            }
                        ]
                    }
                ).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=_Response()):
            turn = provider.complete(
                "system",
                [{"role": "user", "content": "hello"}],
                [{"name": "bash", "description": "Run shell", "input_schema": {"type": "object"}}],
                max_tokens=1024,
            )

        self.assertEqual(turn.stop_reason, "tool_use")
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(turn.tool_calls[0].id, "call-1")
        self.assertEqual(turn.tool_calls[0].name, "bash")
        self.assertEqual(turn.tool_calls[0].input, {"command": "pwd"})
        self.assertEqual(turn.tool_calls[0].importance, "glance")

    def test_openai_provider_streams_responses_reasoning_summary(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-5",
                api_key="test-key",
                base_url="https://api.openai.com/v1",
                timeout_seconds=30,
                reasoning_level="low",
            )
        )

        class _StreamingResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def __iter__(self):
                return iter(
                    [
                        b"event: response.reasoning_summary_text.delta\n",
                        b'data: {"delta":"Checked "}\n',
                        b"\n",
                        b"event: response.reasoning_summary_text.delta\n",
                        b'data: {"delta":"constraints."}\n',
                        b"\n",
                        b"event: response.output_text.delta\n",
                        b'data: {"delta":"Done."}\n',
                        b"\n",
                        b"event: response.completed\n",
                        b'data: {"response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Done."}]}]}}\n',
                        b"\n",
                    ]
                )

        text_chunks: list[str] = []
        thinking_events: list[dict] = []
        with patch("urllib.request.urlopen", return_value=_StreamingResponse()):
            turn = provider.complete(
                "system",
                [{"role": "user", "content": "hello"}],
                [],
                max_tokens=1024,
                text_callback=text_chunks.append,
                thinking_callback=thinking_events.append,
            )

        self.assertEqual(text_chunks, ["Done."])
        self.assertEqual([event["delta"] for event in thinking_events], ["Checked ", "constraints."])
        self.assertEqual(turn.text_blocks, ["Done."])

    def test_openai_provider_debug_request_payload_supports_local_input_image_blocks(self) -> None:
        image_root = self._stable_test_dir("vision-openai")
        image_path = image_root / "tiny.png"
        image_path.write_bytes(self._TINY_PNG_BYTES)

        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-4.1",
                api_key="test-key",
                base_url="https://example.com/v1",
                timeout_seconds=30,
            )
        )

        payload = provider.debug_request_payload(
            "system",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this image"},
                        {"type": "input_image", "absolute_path": str(image_path), "media_type": "image/png"},
                    ],
                }
            ],
            [],
            1024,
            stream=False,
        )

        content = payload["body"]["messages"][1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "describe this image"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_openai_provider_debug_request_payload_supports_image_tool_results(self) -> None:
        image_root = self._stable_test_dir("vision-openai-tool-result")
        image_path = image_root / "tiny.png"
        image_path.write_bytes(self._TINY_PNG_BYTES)

        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-4.1",
                api_key="test-key",
                base_url="https://example.com/v1",
                timeout_seconds=30,
            )
        )

        payload = provider.debug_request_payload(
            "system",
            [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_call", "id": "call-1", "name": "read_image", "input": {"path": "tiny.png"}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_call_id": "call-1",
                            "content": "Loaded image tiny.png (image/png) for model inspection.",
                            "tool_result_text": "Loaded image tiny.png (image/png) for model inspection.",
                            "content_blocks": [
                                {"type": "text", "text": "Tool read_image loaded local workspace image tiny.png (image/png) for inspection."},
                                {"type": "input_image", "absolute_path": str(image_path), "path": "tiny.png", "media_type": "image/png"},
                            ],
                        }
                    ],
                },
            ],
            [],
            1024,
            stream=False,
        )

        messages = payload["body"]["messages"]
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(messages[1]["tool_calls"][0]["function"]["name"], "read_image")
        self.assertEqual(messages[2]["role"], "tool")
        self.assertIn("tiny.png", messages[2]["content"])
        self.assertEqual(messages[3]["role"], "user")
        self.assertEqual(messages[3]["content"][0]["type"], "text")
        self.assertEqual(messages[3]["content"][1]["type"], "image_url")
        self.assertTrue(messages[3]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_openai_provider_debug_request_payload_renders_image_references_as_text(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-4.1",
                api_key="test-key",
                base_url="https://example.com/v1",
                timeout_seconds=30,
            )
        )

        payload = provider.debug_request_payload(
            "system",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "remember this image"},
                        {
                            "type": IMAGE_REFERENCE_BLOCK_TYPE,
                            "path": "tiny.png",
                            "absolute_path": "D:/workspace/tiny.png",
                            "media_type": "image/png",
                        },
                    ],
                }
            ],
            [],
            1024,
            stream=False,
        )

        content = payload["body"]["messages"][1]["content"]
        self.assertIsInstance(content, str)
        self.assertIn("remember this image", content)
        self.assertIn("Visual data omitted from active context", content)

    def test_openai_provider_ignores_compacted_image_tool_results(self) -> None:
        image_root = self._stable_test_dir("vision-openai-tool-result-compacted")
        image_path = image_root / "tiny.png"
        image_path.write_bytes(self._TINY_PNG_BYTES)

        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-4.1",
                api_key="test-key",
                base_url="https://example.com/v1",
                timeout_seconds=30,
            )
        )

        payload = provider.debug_request_payload(
            "system",
            [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_call", "id": "call-1", "name": "read_image", "input": {"path": "tiny.png"}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_call_id": "call-1",
                            "content": '[Image reference | one.png (image/png)] Visual data omitted from active context. Re-read with read_image(path="one.png") if needed.',
                            "tool_result_text": "Loaded image tiny.png (image/png) for model inspection.",
                            "content_blocks": [
                                {"type": "text", "text": "Tool read_image loaded local workspace image tiny.png (image/png) for inspection."},
                                {"type": "input_image", "absolute_path": str(image_path), "path": "tiny.png", "media_type": "image/png"},
                            ],
                        }
                    ],
                },
            ],
            [],
            1024,
            stream=False,
        )

        messages = payload["body"]["messages"]
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[2]["role"], "tool")

    def test_anthropic_provider_debug_request_payload_uses_adaptive_effort_for_supported_models(self) -> None:
        provider = AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-6",
                api_key="test-key",
                base_url="https://api.anthropic.com",
                timeout_seconds=30,
                reasoning_level="deep",
            )
        )

        payload = provider.debug_request_payload("system", [{"role": "user", "content": "hello"}], [], 64000, stream=False)

        self.assertEqual(payload["thinking"], {"type": "adaptive"})
        self.assertEqual(payload["output_config"], {"effort": "max"})

    def test_anthropic_provider_debug_request_payload_marks_prompt_cache_breakpoints(self) -> None:
        provider = AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-5",
                api_key="test-key",
                base_url="https://api.anthropic.com",
                timeout_seconds=30,
            )
        )
        system_prompt = (
            "## A. Core System Prompt\n"
            "Stable rules.\n\n"
            "## B. Runtime Injection\n"
            "Dynamic runtime details."
        )

        payload = provider.debug_request_payload(
            system_prompt,
            [{"role": "user", "content": "hello"}],
            [{"name": "bash", "description": "run", "input_schema": {"type": "object", "properties": {}}}],
            4096,
            stream=False,
        )

        self.assertIsInstance(payload["system"], list)
        self.assertEqual(payload["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", payload["system"][1])
        self.assertNotIn("cache_control", payload["tools"][0])
        self.assertEqual(payload["messages"][-1]["content"][-1]["cache_control"], {"type": "ephemeral"})

    def test_anthropic_provider_cache_control_skips_transient_last_message(self) -> None:
        provider = AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-5",
                api_key="test-key",
                base_url="https://api.anthropic.com",
                timeout_seconds=30,
            )
        )

        payload = provider.debug_request_payload(
            "system",
            [
                {"role": "user", "content": "stable history"},
                {"role": "user", "content": "<runtime-notice>dynamic</runtime-notice>", "transient": True},
            ],
            [],
            4096,
            stream=False,
        )

        self.assertEqual(payload["messages"][0]["content"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", payload["messages"][1]["content"][-1])
        self.assertNotIn("transient", payload["messages"][1])

    def test_anthropic_provider_debug_request_payload_marks_tools_when_system_is_empty(self) -> None:
        provider = AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-5",
                api_key="test-key",
                base_url="https://api.anthropic.com",
                timeout_seconds=30,
            )
        )

        payload = provider.debug_request_payload(
            "",
            [{"role": "user", "content": "hello"}],
            [{"name": "bash", "description": "run", "input_schema": {"type": "object", "properties": {}}}],
            4096,
            stream=False,
        )

        self.assertEqual(payload["tools"][0]["cache_control"], {"type": "ephemeral"})

    def test_anthropic_provider_usage_includes_prompt_cache_tokens(self) -> None:
        provider = AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-5",
                api_key="test-key",
                base_url="https://api.anthropic.com",
                timeout_seconds=30,
            )
        )

        usage = provider._extract_usage(
            SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=20,
                    cache_read_input_tokens=80,
                    cache_creation_input_tokens=10,
                )
            )
        )

        self.assertEqual(usage["cache_read_input_tokens"], 80)
        self.assertEqual(usage["cache_creation_input_tokens"], 10)

    def test_openai_provider_usage_includes_prompt_cache_tokens(self) -> None:
        provider = OpenAIProvider(
            ProviderSettings(
                name="openai",
                provider_type="openai",
                model="gpt-5",
                api_key="test-key",
                base_url="https://api.openai.com/v1",
                timeout_seconds=30,
            )
        )

        usage = provider._extract_usage(
            {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_tokens_details": {"cached_tokens": 64},
                }
            }
        )

        self.assertEqual(usage["cache_read_input_tokens"], 64)

        responses_usage = provider._extract_usage(
            {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "input_tokens_details": {"cached_tokens": 32},
                }
            }
        )
        self.assertEqual(responses_usage["cache_read_input_tokens"], 32)

    def test_anthropic_provider_debug_request_payload_supports_local_input_image_blocks(self) -> None:
        image_root = self._stable_test_dir("vision-anthropic")
        image_path = image_root / "tiny.png"
        image_path.write_bytes(self._TINY_PNG_BYTES)

        provider = AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-5",
                api_key="test-key",
                base_url="https://api.anthropic.com",
                timeout_seconds=30,
            )
        )

        payload = provider.debug_request_payload(
            "system",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this image"},
                        {"type": "input_image", "absolute_path": str(image_path), "media_type": "image/png"},
                    ],
                }
            ],
            [],
            4096,
            stream=False,
        )

        content = payload["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "describe this image"})
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(content[1]["source"]["type"], "base64")
        self.assertEqual(content[1]["source"]["media_type"], "image/png")
        self.assertTrue(content[1]["source"]["data"])

    def test_anthropic_provider_debug_request_payload_supports_image_tool_results(self) -> None:
        image_root = self._stable_test_dir("vision-anthropic-tool-result")
        image_path = image_root / "tiny.png"
        image_path.write_bytes(self._TINY_PNG_BYTES)

        provider = AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-5",
                api_key="test-key",
                base_url="https://api.anthropic.com",
                timeout_seconds=30,
            )
        )

        payload = provider.debug_request_payload(
            "system",
            [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_call", "id": "call-1", "name": "read_image", "input": {"path": "tiny.png"}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_call_id": "call-1",
                            "content": "Loaded image tiny.png (image/png) for model inspection.",
                            "tool_result_text": "Loaded image tiny.png (image/png) for model inspection.",
                            "content_blocks": [
                                {"type": "text", "text": "Tool read_image loaded local workspace image tiny.png (image/png) for inspection."},
                                {"type": "input_image", "absolute_path": str(image_path), "path": "tiny.png", "media_type": "image/png"},
                            ],
                        }
                    ],
                },
            ],
            [],
            4096,
            stream=False,
        )

        tool_result_block = payload["messages"][1]["content"][0]
        self.assertEqual(tool_result_block["type"], "tool_result")
        self.assertIsInstance(tool_result_block["content"], list)
        self.assertEqual(tool_result_block["content"][0]["type"], "text")
        self.assertEqual(tool_result_block["content"][1]["type"], "image")
        self.assertEqual(tool_result_block["content"][1]["source"]["media_type"], "image/png")
        self.assertTrue(tool_result_block["content"][1]["source"]["data"])

    def test_anthropic_provider_debug_request_payload_renders_image_references_as_text(self) -> None:
        provider = AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-5",
                api_key="test-key",
                base_url="https://api.anthropic.com",
                timeout_seconds=30,
            )
        )

        payload = provider.debug_request_payload(
            "system",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "remember this image"},
                        {
                            "type": IMAGE_REFERENCE_BLOCK_TYPE,
                            "path": "tiny.png",
                            "absolute_path": "D:/workspace/tiny.png",
                            "media_type": "image/png",
                        },
                    ],
                }
            ],
            [],
            4096,
            stream=False,
        )

        content = payload["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "remember this image"})
        self.assertEqual(content[1]["type"], "text")
        self.assertIn("Visual data omitted from active context", content[1]["text"])

    def test_anthropic_provider_debug_request_payload_clamps_legacy_budget(self) -> None:
        provider = AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-5",
                api_key="test-key",
                base_url="https://api.anthropic.com",
                timeout_seconds=30,
                reasoning_level="high",
            )
        )

        payload = provider.debug_request_payload("system", [{"role": "user", "content": "hello"}], [], 8000, stream=False)

        self.assertEqual(payload["thinking"], {"type": "enabled", "budget_tokens": 7999})

    def test_assistant_turn_as_message_preserves_thinking_blocks_while_filtering_tool_calls(self) -> None:
        turn = AssistantTurn(
            stop_reason="tool_use",
            text_blocks=["Need to inspect files."],
            tool_calls=[
                ToolCall("call-1", "bash", {"command": "pwd"}),
                ToolCall("call-2", "bash", {"command": "git status"}),
            ],
            content_blocks=[
                {"type": "thinking", "thinking": "private reasoning", "signature": "sig-1"},
                {"type": "text", "text": "Need to inspect files."},
                {"type": "tool_call", "id": "call-1", "name": "bash", "input": {"command": "pwd"}},
                {"type": "tool_call", "id": "call-2", "name": "bash", "input": {"command": "git status"}},
            ],
        )

        message = turn.as_message([turn.tool_calls[0]])

        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"][0]["type"], "thinking")
        self.assertEqual(message["content"][1]["type"], "text")
        self.assertEqual(len([item for item in message["content"] if item["type"] == "tool_call"]), 1)
        self.assertEqual(message["content"][2]["id"], "call-1")

    def test_anthropic_provider_preserves_signed_thinking_in_payload_history(self) -> None:
        provider = AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-5",
                api_key="test-key",
                base_url="https://api.anthropic.com",
                timeout_seconds=30,
                reasoning_level="medium",
            )
        )
        provider.client = SimpleNamespace(
            messages=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(
                    content=[
                        SimpleNamespace(type="thinking", thinking="private reasoning", signature="sig-1"),
                        SimpleNamespace(type="text", text="I need a tool."),
                        SimpleNamespace(type="tool_use", id="call-1", name="bash", input={"command": "pwd"}),
                    ],
                    stop_reason="tool_use",
                    usage=None,
                )
            )
        )

        thinking_events: list[dict] = []
        turn = provider.complete(
            "system",
            [{"role": "user", "content": "inspect"}],
            [],
            max_tokens=4096,
            thinking_callback=thinking_events.append,
        )
        payload = provider.debug_request_payload("system", [turn.as_message()], [], 4096, stream=False)

        assistant_content = payload["messages"][0]["content"]

        self.assertEqual(turn.content_blocks[0]["type"], "thinking")
        self.assertEqual(thinking_events[0]["thinking"], "private reasoning")
        self.assertEqual(
            assistant_content[0],
            {"type": "thinking", "thinking": "private reasoning", "signature": "sig-1"},
        )
        self.assertEqual(assistant_content[1], {"type": "text", "text": "I need a tool."})
        self.assertEqual(assistant_content[2]["type"], "tool_use")
        self.assertEqual(assistant_content[2]["name"], "bash")

    def test_anthropic_provider_streams_thinking_delta_without_replaying_final_block(self) -> None:
        provider = AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-5",
                api_key="test-key",
                base_url="https://example.com/anthropic",
                timeout_seconds=30,
                reasoning_level="medium",
            )
        )

        class FakeStream:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def __iter__(self):
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="thinking_delta", thinking="private "),
                )
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="thinking_delta", thinking="reasoning"),
                )
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="Visible answer."),
                )

            def get_final_message(self):
                return SimpleNamespace(
                    content=[
                        SimpleNamespace(type="thinking", thinking="private reasoning", signature="sig-1"),
                        SimpleNamespace(type="text", text="Visible answer."),
                    ],
                    stop_reason="end_turn",
                    usage=None,
                )

        provider.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kwargs: FakeStream()))

        text_chunks: list[str] = []
        thinking_events: list[dict] = []
        turn = provider.complete(
            "system",
            [{"role": "user", "content": "inspect"}],
            [],
            max_tokens=4096,
            text_callback=text_chunks.append,
            thinking_callback=thinking_events.append,
        )

        self.assertEqual(text_chunks, ["Visible answer."])
        self.assertEqual([event["delta"] for event in thinking_events], ["private ", "reasoning"])
        self.assertEqual(turn.content_blocks[0]["thinking"], "private reasoning")
        self.assertEqual(turn.text_blocks, ["Visible answer."])

    def test_anthropic_provider_falls_back_to_nonstreaming_on_stream_json_decode_error(self) -> None:
        provider = AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="mimo-v2.5-pro",
                api_key="test-key",
                base_url="https://example.com/anthropic",
                timeout_seconds=30,
            )
        )
        calls: list[str] = []

        class BrokenStream:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def __iter__(self):
                calls.append("stream_iter")
                raise json.JSONDecodeError("Unterminated string starting at", '{"type":"', 9)

        def create(**kwargs):
            calls.append("create")
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="Recovered answer.")],
                stop_reason="end_turn",
                usage=None,
            )

        provider.client = SimpleNamespace(
            messages=SimpleNamespace(
                stream=lambda **kwargs: BrokenStream(),
                create=create,
            )
        )

        text_chunks: list[str] = []
        turn = provider.complete(
            "system",
            [{"role": "user", "content": "hello"}],
            [],
            max_tokens=1024,
            text_callback=text_chunks.append,
        )

        self.assertEqual(calls, ["stream_iter", "create"])
        self.assertEqual(text_chunks, [])
        self.assertEqual(turn.text_blocks, ["Recovered answer."])

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

    def test_agent_loop_reraises_turn_interrupted_from_tool_execution(self) -> None:
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
        transcript_root = self._stable_test_dir("visible-progress-thinking") / "transcripts"
        runtime.transcript_store = SimpleNamespace(root=transcript_root, append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: None
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime._run_topic_shift_assist = lambda session, latest_user_message="": None
        runtime._run_automatic_context_janitor = lambda session: None
        runtime._record_provider_payload_result = lambda *args, **kwargs: None
        runtime._record_session_token_usage = lambda *args, **kwargs: None
        runtime._normalize_turn_usage = lambda *args, **kwargs: None
        runtime._tool_schemas_for_model = lambda actor: []
        runtime._messages_for_model = lambda messages, **kwargs: messages
        runtime._dump_provider_payload_if_enabled = lambda **kwargs: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=0, max_tokens=1000)
        runtime._agent_loop_result = lambda final_text, status="completed", session=None, **kwargs: SimpleNamespace(
            final_text=final_text,
            status=status,
        )
        runtime.interrupt_active_teammates = lambda reason="lead_interrupt": 0
        runtime.registry = SimpleNamespace(
            execute=lambda ctx, name, payload: (_ for _ in ()).throw(TurnInterrupted("Interrupted by user."))
        )
        runtime.complete = lambda *args, **kwargs: AssistantTurn(
            stop_reason="tool_use",
            text_blocks=["Searching..."],
            tool_calls=[ToolCall("call-1", "grep", {"pattern": "needle"})],
        )

        session = AgentSession(id="session-1")

        with self.assertRaises(TurnInterrupted):
            OpenAgentRuntime.run_turn(runtime, session, "search the repo")

    def test_agent_loop_consumes_ephemeral_images_after_payload_build_and_clears_caches(self) -> None:
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
        transcript_root = self._stable_test_dir("visible-progress-thinking") / "transcripts"
        runtime.transcript_store = SimpleNamespace(root=transcript_root, append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: None
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime._run_topic_shift_assist = lambda session, latest_user_message="": None
        runtime._run_automatic_context_janitor = lambda session: None
        runtime._record_provider_payload_result = lambda *args, **kwargs: None
        runtime._record_session_token_usage = lambda *args, **kwargs: None
        runtime._normalize_turn_usage = lambda *args, **kwargs: None
        runtime._tool_schemas_for_model = lambda actor: []
        runtime._messages_for_model = (
            lambda messages, **kwargs: json.loads(json.dumps(messages, ensure_ascii=False))
        )
        runtime._dump_provider_payload_if_enabled = lambda **kwargs: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=0, max_tokens=1000)
        runtime._agent_loop_result = lambda final_text, status="completed", session=None, **kwargs: final_text
        runtime.interrupt_active_teammates = lambda reason="lead_interrupt": 0
        runtime._hook_manager = lambda: SimpleNamespace(
            on_assistant_response=lambda *args, **kwargs: None,
            on_turn_failed=lambda *args, **kwargs: None,
        )
        runtime.registry = SimpleNamespace(execute=lambda ctx, name, payload: "ok")
        runtime._payload_message_cache = {"session-1": (("stale",), [{"role": "user", "content": "stale"}])}
        runtime._context_usage_cache = {"session-1": (("stale",), ContextWindowUsage(used_tokens=1, max_tokens=10))}
        runtime._recent_context_usage = {"session-1": ContextWindowUsage(used_tokens=1, max_tokens=10)}

        payloads: list[list[dict[str, object]]] = []

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            payloads.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return AssistantTurn(stop_reason="end_turn", text_blocks=["Done."])

        runtime.complete = fake_complete

        session = AgentSession(id="session-1")
        user_message = make_user_multimodal_message(
            "look at this image",
            [
                {
                    "type": "input_image",
                    "path": "scripts/image.png",
                    "absolute_path": "D:/workspace/scripts/image.png",
                    "media_type": "image/png",
                }
            ],
        )

        result = OpenAgentRuntime.run_turn(runtime, session, encode_embedded_user_message(user_message))

        self.assertEqual(result, "Done.")
        self.assertEqual(payloads[0][0]["content"][1]["type"], "input_image")
        self.assertEqual(session.messages[0]["content"][1]["type"], IMAGE_REFERENCE_BLOCK_TYPE)
        self.assertNotIn("session-1", runtime._payload_message_cache)
        self.assertNotIn("session-1", runtime._context_usage_cache)
        self.assertNotIn("session-1", runtime._recent_context_usage)

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
        transcript_root = self._stable_test_dir("visible-progress-history") / "transcripts"
        runtime.transcript_store = SimpleNamespace(root=transcript_root, append=lambda *args, **kwargs: None)
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

    def test_agent_loop_collapses_repeated_malformed_tool_names(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(
                max_agent_rounds=1,
                janitor_trigger_ratio=0.6,
                max_tool_output_chars=5000,
                max_tool_calls_per_turn=64,
            ),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.compact_manager = SimpleNamespace(auto_compact=lambda session_id, messages, preserve_from_index=None: messages)
        runtime.todo_manager = SimpleNamespace(has_open_items=lambda session: False)
        runtime.session_manager = SimpleNamespace(save=lambda session: None)
        transcript_root = self._stable_test_dir("visible-progress-thinking") / "transcripts"
        runtime.transcript_store = SimpleNamespace(root=transcript_root, append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: "log-1"
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)
        runtime._tool_schemas_for_model = lambda actor: []
        runtime._messages_for_model = lambda messages, **kwargs: messages
        runtime._dump_provider_payload_if_enabled = lambda **kwargs: None
        runtime._record_provider_payload_result = lambda *args, **kwargs: None
        runtime._record_session_token_usage = lambda *args, **kwargs: None
        runtime._normalize_turn_usage = lambda *args, **kwargs: None
        runtime._run_topic_shift_assist = lambda session, latest_user_message="": None
        runtime._run_automatic_context_janitor = lambda session: None

        executed_tools: list[str] = []
        runtime.registry = SimpleNamespace(
            names=lambda: ["edit_file", "read_file"],
            execute=lambda ctx, name, payload: executed_tools.append(name) or "ok",
        )
        runtime.complete = lambda *args, **kwargs: AssistantTurn(
            stop_reason="tool_use",
            text_blocks=["Updating file."],
            tool_calls=[ToolCall(f"call-{index}", "edit_file</arg_value>", {}) for index in range(20)],
        )

        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(runtime, session, "update file")

        self.assertEqual(getattr(result, "status", None), "stopped_after_max_rounds")
        self.assertEqual(executed_tools, [])
        assistant_message = session.messages[1]
        tool_calls = [item for item in assistant_message["content"] if item.get("type") == "tool_call"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["name"], "edit_file</arg_value>")
        tool_results = session.messages[2]["content"]
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(tool_results[0]["raw_output"]["error_type"], "malformed_tool_name")

    def test_agent_loop_stops_after_tool_call_limit(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(
                max_agent_rounds=1,
                janitor_trigger_ratio=0.6,
                max_tool_output_chars=5000,
                max_tool_calls_per_turn=2,
            ),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.compact_manager = SimpleNamespace(auto_compact=lambda session_id, messages, preserve_from_index=None: messages)
        runtime.todo_manager = SimpleNamespace(has_open_items=lambda session: False)
        runtime.session_manager = SimpleNamespace(save=lambda session: None)
        transcript_root = self._stable_test_dir("visible-progress-history") / "transcripts"
        runtime.transcript_store = SimpleNamespace(root=transcript_root, append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: "log-1"
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)
        runtime._tool_schemas_for_model = lambda actor: []
        runtime._messages_for_model = lambda messages, **kwargs: messages
        runtime._dump_provider_payload_if_enabled = lambda **kwargs: None
        runtime._record_provider_payload_result = lambda *args, **kwargs: None
        runtime._record_session_token_usage = lambda *args, **kwargs: None
        runtime._normalize_turn_usage = lambda *args, **kwargs: None
        runtime._run_topic_shift_assist = lambda session, latest_user_message="": None
        runtime._run_automatic_context_janitor = lambda session: None

        executed_tools: list[str] = []
        runtime.registry = SimpleNamespace(
            names=lambda: ["bash"],
            execute=lambda ctx, name, payload: executed_tools.append(name) or "ok",
        )
        runtime.complete = lambda *args, **kwargs: AssistantTurn(
            stop_reason="tool_use",
            tool_calls=[ToolCall(f"call-{index}", "bash", {"command": f"cmd-{index}"}) for index in range(5)],
        )

        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(runtime, session, "inspect")

        self.assertEqual(getattr(result, "status", None), "stopped_after_max_rounds")
        self.assertEqual(executed_tools, ["bash", "bash"])
        assistant_message = session.messages[1]
        tool_calls = [item for item in assistant_message["content"] if item.get("type") == "tool_call"]
        self.assertEqual([item["id"] for item in tool_calls], ["call-0", "call-1", "call-2"])
        tool_results = session.messages[2]["content"]
        self.assertEqual(len(tool_results), 3)
        self.assertEqual(tool_results[2]["raw_output"]["error_type"], "too_many_tool_calls")

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

    def test_agent_loop_emits_unstreamed_tool_turn_text_before_tool_execution(self) -> None:
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
                    text_blocks=["I need to inspect first."],
                    tool_calls=[ToolCall("call-1", "bash", {"command": "pwd"})],
                ),
                AssistantTurn(stop_reason="end_turn", text_blocks=["Done."]),
            ]
        )
        runtime.complete = lambda *args, **kwargs: next(turns)
        runtime.registry = _Registry()
        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(runtime, session, "inspect", text_callback=_Streamer())

        self.assertEqual(result, "Done.")
        self.assertLess(order.index(("text", "I need to inspect first.")), order.index(("flush", "")))
        self.assertLess(order.index(("flush", "")), order.index(("tool", "bash")))

    def test_agent_loop_emits_thinking_finished_before_tool_execution(self) -> None:
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
        transcript_root = self._stable_test_dir("thinking-before-tool") / "transcripts"
        runtime.transcript_store = SimpleNamespace(root=transcript_root, append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: order.append(("print_tool", args[1]))
        runtime.build_system_prompt = lambda: "system"
        runtime._capture_turn_file_changes = lambda session: None
        order: list[tuple[str, str]] = []

        class _Registry:
            def schemas(self):
                return []

            def execute(self, ctx, name, payload):
                order.append(("tool", name))
                return "ok"

        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[ToolCall("call-1", "bash", {"command": "pwd"})],
                    content_blocks=[
                        {"type": "thinking", "thinking": "inspect before calling tool"},
                        {"type": "tool_call", "id": "call-1", "name": "bash", "input": {"command": "pwd"}},
                    ],
                ),
                AssistantTurn(stop_reason="end_turn", text_blocks=["Done."]),
            ]
        )

        def thinking_callback(payload: dict[str, object]) -> None:
            event = str(payload.get("event", "")).strip()
            if event == "finished":
                order.append(("thinking", str(payload.get("characters", ""))))

        runtime.complete = lambda *args, **kwargs: next(turns)
        runtime.registry = _Registry()
        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(runtime, session, "inspect", thinking_callback=thinking_callback)

        self.assertEqual(result, "Done.")
        self.assertEqual([item[0] for item in order].count("thinking"), 1)
        self.assertLess(order.index(("thinking", "27")), order.index(("tool", "bash")))
        self.assertLess(order.index(("thinking", "27")), order.index(("print_tool", "bash")))

    def test_agent_loop_emits_thinking_finished_before_streamed_text(self) -> None:
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
        transcript_root = self._stable_test_dir("thinking-before-text") / "transcripts"
        runtime.transcript_store = SimpleNamespace(root=transcript_root, append=lambda *args, **kwargs: None)
        runtime.print_tool_event = lambda *args, **kwargs: None
        runtime.build_system_prompt = lambda: "system"
        runtime._capture_turn_file_changes = lambda session: None
        order: list[tuple[str, str]] = []

        class _Registry:
            def schemas(self):
                return []

        class _Streamer:
            def __call__(self, text: str):
                order.append(("text", text))

            def finish(self):
                order.append(("flush", ""))

        def fake_complete(system_prompt, messages, tools, text_callback=None, thinking_callback=None, should_interrupt=None):
            if thinking_callback is not None:
                thinking_callback({"event": "delta", "delta": "checking final answer"})
            if text_callback is not None:
                text_callback("Final answer.")
            return AssistantTurn(stop_reason="end_turn", text_blocks=["Final answer."])

        def thinking_callback(payload: dict[str, object]) -> None:
            event = str(payload.get("event", "")).strip()
            if event == "finished":
                order.append(("thinking", str(payload.get("characters", ""))))

        runtime.complete = fake_complete
        runtime.registry = _Registry()
        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(
            runtime,
            session,
            "inspect",
            text_callback=_Streamer(),
            thinking_callback=thinking_callback,
        )

        self.assertEqual(result, "Final answer.")
        self.assertEqual([item[0] for item in order].count("thinking"), 1)
        self.assertLess(order.index(("thinking", "21")), order.index(("text", "Final answer.")))

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
        self.assertEqual(reminder_counts, [0, 0, 0, 0])
        self.assertNotIn(reminder, json.dumps(session.messages, ensure_ascii=False))

    def test_agent_loop_does_not_inject_open_todo_reminder_every_round(self) -> None:
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
                AssistantTurn(
                    stop_reason="end_turn",
                    text_blocks=["Still done."],
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
        reconcile_reminder = OpenAgentRuntime.TODO_RECONCILE_REMINDER_TEXT
        reminder_counts = [json.dumps(payload, ensure_ascii=False).count(reminder) for payload in payloads]
        reconcile_counts = [json.dumps(payload, ensure_ascii=False).count(reconcile_reminder) for payload in payloads]

        self.assertEqual(result, "Still done.")
        self.assertEqual(reminder_counts, [0, 0, 0, 0, 0])
        self.assertEqual(reconcile_counts, [0, 0, 0, 0, 1])
        self.assertIn(OpenAgentRuntime.RUNTIME_NOTICE_TAG, json.dumps(payloads[4], ensure_ascii=False))
        self.assertNotIn(reminder, json.dumps(session.messages, ensure_ascii=False))
        self.assertNotIn(reconcile_reminder, json.dumps(session.messages, ensure_ascii=False))

    def test_agent_loop_runs_one_todo_reconcile_round_before_finishing_when_open_todos_remain(self) -> None:
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
                    stop_reason="end_turn",
                    text_blocks=["Done."],
                ),
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCall(
                            "call-1",
                            "TodoWrite",
                            {
                                "items": [
                                    {"content": "Fix route", "status": "completed", "activeForm": "Fixing route"},
                                    {"content": "Confirm tools/list scope", "status": "in_progress", "activeForm": "Waiting for confirmation"},
                                ]
                            },
                        )
                    ],
                ),
                AssistantTurn(
                    stop_reason="end_turn",
                    text_blocks=["Still open."],
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
            todo_items=[
                {"content": "Fix route", "status": "in_progress", "activeForm": "Fixing route"},
                {"content": "Confirm tools/list scope", "status": "pending", "activeForm": "Waiting for confirmation"},
            ],
            rounds_without_todo=1,
        )

        result = OpenAgentRuntime.run_turn(runtime, session, "inspect")
        reconcile_reminder = OpenAgentRuntime.TODO_RECONCILE_REMINDER_TEXT

        self.assertEqual(result, "Still open.")
        self.assertEqual(getattr(result, "status", None), "completed")
        self.assertEqual(session.todo_items[0]["status"], "completed")
        self.assertEqual(session.todo_items[1]["status"], "in_progress")
        self.assertEqual(session.rounds_without_todo, 0)
        self.assertEqual(len(payloads), 3)
        self.assertNotIn(reconcile_reminder, json.dumps(session.messages, ensure_ascii=False))
        self.assertEqual(json.dumps(payloads[0], ensure_ascii=False).count(reconcile_reminder), 0)
        self.assertEqual(json.dumps(payloads[1], ensure_ascii=False).count(reconcile_reminder), 1)
        self.assertEqual(json.dumps(payloads[2], ensure_ascii=False).count(reconcile_reminder), 0)

    def test_agent_loop_does_not_loop_forever_if_todo_reconcile_is_ignored_once(self) -> None:
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
        runtime.registry = SimpleNamespace(schemas=lambda: [], execute=lambda ctx, name, payload: "ok")

        payloads: list[list[dict]] = []
        turns = iter(
            [
                AssistantTurn(stop_reason="end_turn", text_blocks=["Done."]),
                AssistantTurn(stop_reason="end_turn", text_blocks=["Still done."]),
            ]
        )

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            payloads.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return next(turns)

        runtime.complete = fake_complete

        session = AgentSession(
            id="session-1",
            todo_items=[{"content": "Confirm scope", "status": "in_progress", "activeForm": "Waiting for confirmation"}],
            rounds_without_todo=1,
        )

        result = OpenAgentRuntime.run_turn(runtime, session, "inspect")
        reconcile_reminder = OpenAgentRuntime.TODO_RECONCILE_REMINDER_TEXT

        self.assertEqual(result, "Still done.")
        self.assertEqual(getattr(result, "status", None), "completed")
        self.assertEqual(len(payloads), 2)
        self.assertEqual(json.dumps(payloads[1], ensure_ascii=False).count(reconcile_reminder), 1)

    def test_agent_loop_injects_exploration_summary_reminder_after_soft_limit(self) -> None:
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
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)

        class _Registry:
            def schemas(self):
                return []

            def execute(self, ctx, name, payload):
                return "file content"

        payloads: list[list[dict]] = []
        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCall(f"call-{index}", "read_file", {"path": f"file-{index}.py"})
                        for index in range(OpenAgentRuntime.EXPLORATION_SOFT_LIMIT)
                    ],
                ),
                AssistantTurn(stop_reason="end_turn", text_blocks=["Interim conclusion."]),
            ]
        )

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            payloads.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return next(turns)

        runtime.complete = fake_complete
        runtime.registry = _Registry()

        result = OpenAgentRuntime.run_turn(runtime, AgentSession(id="session-1"), "inspect")

        self.assertEqual(result, "Interim conclusion.")
        self.assertEqual(len(payloads), 2)
        self.assertIn("You have been exploring for 10 consecutive", json.dumps(payloads[1], ensure_ascii=False))
        self.assertNotIn("You have been exploring", json.dumps(payloads[0], ensure_ascii=False))
        self.assertNotIn("You have been exploring", json.dumps(runtime.registry.__dict__, ensure_ascii=False))

    def test_agent_loop_resets_exploration_streak_after_visible_summary_text(self) -> None:
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
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)

        class _Registry:
            def schemas(self):
                return []

            def execute(self, ctx, name, payload):
                return "file content"

        payloads: list[list[dict]] = []
        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCall(f"call-{index}", "read_file", {"path": f"file-{index}.py"})
                        for index in range(OpenAgentRuntime.EXPLORATION_SOFT_LIMIT)
                    ],
                ),
                AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["Interim conclusion."],
                    tool_calls=[ToolCall("call-summary", "read_file", {"path": "follow-up.py"})],
                ),
                AssistantTurn(stop_reason="end_turn", text_blocks=["Final conclusion."]),
            ]
        )

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            payloads.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return next(turns)

        runtime.complete = fake_complete
        runtime.registry = _Registry()

        result = OpenAgentRuntime.run_turn(runtime, AgentSession(id="session-1"), "inspect")

        self.assertEqual(result, "Final conclusion.")
        self.assertEqual(len(payloads), 3)
        self.assertIn("You have been exploring for 10 consecutive", json.dumps(payloads[1], ensure_ascii=False))
        self.assertNotIn("You have been exploring", json.dumps(payloads[2], ensure_ascii=False))

    def test_agent_loop_stops_exploration_after_hard_streak_limit(self) -> None:
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
        runtime.build_system_prompt = lambda session=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)

        executed: list[str] = []

        class _Registry:
            def schemas(self):
                return []

            def execute(self, ctx, name, payload):
                executed.append(str(payload.get("path", "")))
                return "file content"

        payloads: list[list[dict]] = []
        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCall(f"call-{index}", "read_file", {"path": f"file-{index}.py"})
                        for index in range(OpenAgentRuntime.EXPLORATION_HARD_STREAK_LIMIT + 1)
                    ],
                ),
                AssistantTurn(stop_reason="end_turn", text_blocks=["Interim conclusion."]),
            ]
        )

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            payloads.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return next(turns)

        runtime.complete = fake_complete
        runtime.registry = _Registry()
        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(runtime, session, "inspect")
        rendered_session = json.dumps(session.messages, ensure_ascii=False)

        self.assertEqual(result, "Interim conclusion.")
        self.assertEqual(len(executed), OpenAgentRuntime.EXPLORATION_HARD_STREAK_LIMIT)
        self.assertIn("exploration_budget_exceeded", rendered_session)

    def test_agent_loop_stops_exploration_after_total_limit_even_with_action_between(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        hard_total_limit = 25
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(
                max_agent_rounds=5,
                janitor_trigger_ratio=0.6,
                max_tool_output_chars=5000,
                exploration_hard_total_limit=hard_total_limit,
            ),
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

        executed: list[str] = []

        class _Registry:
            def schemas(self):
                return []

            def execute(self, ctx, name, payload):
                executed.append(name)
                return "ok"

        payloads: list[list[dict]] = []
        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[
                        *[
                            ToolCall(f"read-a-{index}", "read_file", {"path": f"a-{index}.py"})
                            for index in range(OpenAgentRuntime.EXPLORATION_HARD_STREAK_LIMIT)
                        ],
                        ToolCall("todo-1", "TodoWrite", {"items": []}),
                        *[
                            ToolCall(f"read-b-{index}", "read_file", {"path": f"b-{index}.py"})
                            for index in range(
                                hard_total_limit
                                - OpenAgentRuntime.EXPLORATION_HARD_STREAK_LIMIT
                                + 1
                            )
                        ],
                    ],
                ),
                AssistantTurn(stop_reason="end_turn", text_blocks=["Interim conclusion."]),
            ]
        )

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            payloads.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return next(turns)

        runtime.complete = fake_complete
        runtime.registry = _Registry()
        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(runtime, session, "inspect")
        rendered_session = json.dumps(session.messages, ensure_ascii=False)

        self.assertEqual(result, "Interim conclusion.")
        self.assertEqual(executed.count("read_file"), hard_total_limit)
        self.assertEqual(executed.count("TodoWrite"), 1)
        self.assertIn("exploration_budget_exceeded", rendered_session)

    def test_agent_loop_injects_next_user_message_after_current_tool_boundary(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(max_agent_rounds=4, janitor_trigger_ratio=0.6, max_tool_output_chars=5000),
            provider=SimpleNamespace(max_tokens=1024),
        )
        runtime.background_manager = SimpleNamespace(drain=lambda: [])
        runtime.bus = SimpleNamespace(read_inbox=lambda actor: [])
        runtime.compact_manager = SimpleNamespace(
            auto_compact=lambda session_id, messages, preserve_from_index=None: messages,
            last_usage=None,
        )
        runtime.todo_manager = SimpleNamespace(has_open_items=lambda session: False)
        runtime.session_manager = SimpleNamespace(save=lambda session: None)
        transcript_entries: list[tuple[str, dict]] = []
        runtime.transcript_store = SimpleNamespace(
            append=lambda session_id, message: transcript_entries.append(
                (session_id, json.loads(json.dumps(message, ensure_ascii=False)))
            )
        )
        runtime.print_tool_event = lambda *args, **kwargs: "log-1"
        runtime.build_system_prompt = lambda session=None, actor=None, role=None: "system"
        runtime._capture_turn_file_changes = lambda session: None
        runtime.context_window_usage = lambda session: ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)
        runtime._tool_schemas_for_model = lambda actor: []
        runtime._messages_for_model = (
            lambda messages, session=None, system_prompt=None, tools=None: json.loads(
                json.dumps(messages, ensure_ascii=False)
            )
        )
        runtime._dump_provider_payload_if_enabled = lambda **kwargs: None
        runtime._record_provider_payload_result = lambda *args, **kwargs: None
        runtime._record_session_token_usage = lambda *args, **kwargs: None
        runtime._normalize_turn_usage = lambda *args, **kwargs: None
        runtime._hook_manager = lambda: SimpleNamespace(
            on_assistant_response=lambda *args, **kwargs: None,
            on_turn_failed=lambda *args, **kwargs: None,
        )
        topic_shift_messages: list[str] = []
        janitor_runs: list[int] = []
        runtime._run_topic_shift_assist = (
            lambda session, latest_user_message, actor="lead", role="lead coding agent": topic_shift_messages.append(
                latest_user_message
            )
            or ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)
        )
        runtime._run_automatic_context_janitor = (
            lambda session, actor="lead", role="lead coding agent": janitor_runs.append(len(session.messages))
            or ContextWindowUsage(used_tokens=10_000, max_tokens=100_000)
        )

        executed_commands: list[str] = []

        class _Registry:
            def schemas(self):
                return []

            def execute(self, ctx, name, payload):
                executed_commands.append(str(payload.get("command", "")))
                return "ok"

        payloads: list[list[dict]] = []
        turns = iter(
            [
                AssistantTurn(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCall("call-1", "bash", {"command": "pwd"}),
                        ToolCall("call-2", "bash", {"command": "git status"}),
                    ],
                ),
                AssistantTurn(
                    stop_reason="end_turn",
                    text_blocks=["Handled queued message."],
                ),
            ]
        )

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            payloads.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return next(turns)

        runtime.complete = fake_complete
        runtime.registry = _Registry()

        ready_messages: list[str] = []
        promoted = {"done": False}

        def prepare_next_loop_user_message():
            if promoted["done"]:
                return False
            promoted["done"] = True
            ready_messages.append("queued follow-up")
            return True

        def take_next_loop_user_message():
            if not ready_messages:
                return None
            return ready_messages.pop(0)

        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(
            runtime,
            session,
            "inspect",
            take_next_loop_user_message=take_next_loop_user_message,
            prepare_next_loop_user_message=prepare_next_loop_user_message,
        )

        self.assertEqual(result, "Handled queued message.")
        self.assertEqual(executed_commands, ["pwd"])
        self.assertEqual(topic_shift_messages, ["inspect", "queued follow-up"])
        self.assertEqual(len(janitor_runs), 2)
        self.assertEqual(session.messages[0]["content"], "inspect")
        self.assertEqual(session.messages[3]["content"], "queued follow-up")
        self.assertEqual(len(session.messages[1]["content"]), 1)
        self.assertEqual(session.messages[1]["content"][0]["input"]["command"], "pwd")
        self.assertIn("queued follow-up", json.dumps(payloads[1], ensure_ascii=False))
        self.assertIn(
            {"role": "user", "content": "queued follow-up"},
            [entry for _, entry in transcript_entries],
        )
        self.assertNotIn("git status", json.dumps(session.messages, ensure_ascii=False))

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
        self.assertEqual(reminder_counts, [0, 0])

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

    def test_agent_loop_does_not_complete_or_notify_on_empty_assistant_response(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            runtime=SimpleNamespace(max_agent_rounds=2, janitor_trigger_ratio=0.6, max_tool_output_chars=5000),
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
        runtime._tool_schemas_for_model = lambda actor: []
        runtime._dump_provider_payload_if_enabled = lambda **kwargs: None
        runtime._record_provider_payload_result = lambda *args, **kwargs: None
        runtime._record_session_token_usage = lambda *args, **kwargs: None
        runtime._normalize_turn_usage = lambda *args, **kwargs: None
        runtime._messages_for_model = (
            lambda messages, session=None, system_prompt=None, tools=None: json.loads(
                json.dumps(messages, ensure_ascii=False)
            )
        )
        assistant_notifications: list[dict] = []
        runtime._hook_manager = lambda: SimpleNamespace(
            on_assistant_response=lambda *args, **kwargs: assistant_notifications.append(kwargs),
            on_turn_failed=lambda *args, **kwargs: None,
        )

        payloads: list[list[dict]] = []

        def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            payloads.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return AssistantTurn(
                stop_reason="end_turn",
                content_blocks=[{"type": "thinking", "thinking": "still reasoning"}],
            )

        runtime.complete = fake_complete
        session = AgentSession(id="session-1")

        result = OpenAgentRuntime.run_turn(runtime, session, "inspect")

        self.assertEqual(result, "Stopped after max rounds.")
        self.assertEqual(getattr(result, "status", None), "stopped_after_max_rounds")
        self.assertEqual(assistant_notifications, [])
        self.assertIn(OpenAgentRuntime.EMPTY_ASSISTANT_RESPONSE_REPAIR_TEXT, json.dumps(payloads[1], ensure_ascii=False))

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

    def test_lead_inbox_drains_shutdown_responses_as_internal_control_messages(self) -> None:
        marked: list[tuple[str, str]] = []
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.bus = SimpleNamespace(
            read_inbox=lambda actor, session_id=None: [
                {"type": "shutdown_response", "request_id": "req-1", "content": "Shutting down."},
                {"type": "message", "from": "Worker", "content": "Visible update."},
            ]
        )
        runtime.request_tracker = SimpleNamespace(
            mark_shutdown_response=lambda request_id, status: marked.append((request_id, status))
        )

        visible = OpenAgentRuntime._drain_lead_visible_inbox(runtime, AgentSession(id="session-1"))

        self.assertEqual(visible, [{"type": "message", "from": "Worker", "content": "Visible update."}])
        self.assertEqual(marked, [("req-1", "accepted")])


if __name__ == "__main__":
    unittest.main()
