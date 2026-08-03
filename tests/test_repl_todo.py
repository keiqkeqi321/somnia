from __future__ import annotations

import base64
import io
import time
import unittest
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from open_somnia.cli.commands import ConsoleStreamer
from open_somnia.cli.prompting import PROMPT_BORDER
from open_somnia.cli.repl import (
    AuthorizationRequest,
    ModeSwitchRequest,
    TurnQueueRunner,
    _build_image_query,
    _build_init_query,
    _build_clipboard_image_query,
    _clipboard_image_command,
    _ensure_accept_edits_for_command,
    _expand_skill_command,
    _handle_hooks_command,
    _handle_reloadplugin_command,
    _handle_symbols_command,
    _is_exit_command,
    _handle_mcp_command,
    _handle_model_command,
    _handle_providers_command,
    _handle_reasoning_command,
    _handle_vision_command,
    _handle_skills_command,
    _handle_undo_command,
    _thinking_log_label,
    _resolve_authorization_requests,
    _resolve_mode_switch_requests,
    run_repl,
    _save_windows_clipboard_image,
)
from open_somnia.runtime.compact import ContextWindowUsage
from open_somnia.runtime.messages import decode_embedded_user_message
from open_somnia.tools.todo import TodoManager


def _render_prompt_text(fragments) -> str:
    return "".join(text for _, text, *rest in fragments)


class ReplTodoTests(unittest.TestCase):
    _TINY_PNG_BYTES = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2ioAAAAASUVORK5CYII="
    )

    def test_is_exit_command_requires_explicit_exit_text(self) -> None:
        self.assertFalse(_is_exit_command(""))
        self.assertFalse(_is_exit_command("   "))
        self.assertFalse(_is_exit_command("/compact"))
        self.assertTrue(_is_exit_command("q"))
        self.assertTrue(_is_exit_command(" exit "))
        self.assertTrue(_is_exit_command("/exit"))

    def test_build_image_query_embeds_structured_user_message(self) -> None:
        image_root = Path.cwd() / ".tmp-tests" / f"repl-image-{time.time_ns()}"
        image_root.mkdir(parents=True, exist_ok=True)
        image_path = image_root / "tiny.png"
        image_path.write_bytes(self._TINY_PNG_BYTES)
        runtime = SimpleNamespace(settings=SimpleNamespace(workspace_root=Path.cwd()))

        query = _build_image_query(runtime, f'/image ".tmp-tests/{image_root.name}/tiny.png" describe this')
        decoded = decode_embedded_user_message(query)

        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["content"][0], {"type": "text", "text": "describe this"})
        self.assertEqual(decoded["content"][1]["type"], "input_image")
        self.assertEqual(decoded["content"][1]["media_type"], "image/png")

    def test_build_init_query_refuses_existing_agents_without_force(self) -> None:
        root = Path.cwd() / ".tmp-tests" / f"repl-init-existing-{time.time_ns()}"
        root.mkdir(parents=True, exist_ok=True)
        (root / "AGENTS.md").write_text("existing\n", encoding="utf-8")
        runtime = SimpleNamespace(settings=SimpleNamespace(workspace_root=root))

        with patch("builtins.print") as mock_print:
            query = _build_init_query(runtime, "/init")

        self.assertIsNone(query)
        self.assertIn("AGENTS.md already exists", mock_print.call_args[0][0])

    def test_build_init_query_allows_force_and_creates_agent_loop_prompt(self) -> None:
        root = Path.cwd() / ".tmp-tests" / f"repl-init-force-{time.time_ns()}"
        root.mkdir(parents=True, exist_ok=True)
        (root / "AGENTS.md").write_text("existing\n", encoding="utf-8")
        (root / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
        runtime = SimpleNamespace(settings=SimpleNamespace(workspace_root=root))

        with patch("builtins.print") as mock_print:
            query = _build_init_query(runtime, "/init --force")

        self.assertIsNotNone(query)
        self.assertIn("Initialize project instructions", str(query))
        self.assertIn("Overwrite existing file: yes", str(query))
        self.assertIn("real repository inspection loop", str(query))
        self.assertIn("write_file or edit_file", str(query))
        self.assertIn("[init queued]", mock_print.call_args[0][0])

    def test_build_init_query_accepts_extra_prompt_after_options(self) -> None:
        root = Path.cwd() / ".tmp-tests" / f"repl-init-extra-{time.time_ns()}"
        root.mkdir(parents=True, exist_ok=True)
        (root / "AGENTS.md").write_text("existing\n", encoding="utf-8")
        runtime = SimpleNamespace(settings=SimpleNamespace(workspace_root=root))

        with patch("builtins.print"):
            query = _build_init_query(runtime, "/init --force 重点分析 MCP 和权限模式")

        self.assertIsNotNone(query)
        self.assertIn("User extra instructions for this initialization:", str(query))
        self.assertIn("重点分析 MCP 和权限模式", str(query))

    def test_build_init_query_rejects_unknown_option_before_extra_prompt(self) -> None:
        root = Path.cwd() / ".tmp-tests" / f"repl-init-unknown-{time.time_ns()}"
        root.mkdir(parents=True, exist_ok=True)
        runtime = SimpleNamespace(settings=SimpleNamespace(workspace_root=root))

        with patch("builtins.print") as mock_print:
            query = _build_init_query(runtime, "/init --target CLAUDE.md")

        self.assertIsNone(query)
        self.assertIn("unknown option: --target", mock_print.call_args_list[0][0][0])

    def test_clipboard_image_command_targets_workspace_temp_image(self) -> None:
        workspace_root = Path.cwd()
        saved_path = workspace_root / ".open_somnia" / "temp" / "clipboard-test.png"
        runtime = SimpleNamespace(
            settings=SimpleNamespace(
                workspace_root=workspace_root,
                storage=SimpleNamespace(data_dir=workspace_root / ".open_somnia"),
            )
        )

        with patch("open_somnia.cli.repl._save_clipboard_image", return_value=saved_path):
            command = _clipboard_image_command(runtime)

        self.assertEqual(command, "/image .open_somnia/temp/clipboard-test.png ")

    def test_build_clipboard_image_query_uses_saved_clipboard_path(self) -> None:
        workspace_root = Path.cwd()
        saved_path = workspace_root / ".open_somnia" / "temp" / "clipboard-test.png"
        runtime = SimpleNamespace(
            settings=SimpleNamespace(
                workspace_root=workspace_root,
                storage=SimpleNamespace(data_dir=workspace_root / ".open_somnia"),
            )
        )

        with patch("open_somnia.cli.repl._save_clipboard_image", return_value=saved_path):
            with patch(
                "open_somnia.cli.repl._build_image_query",
                side_effect=lambda _runtime, command: command,
            ):
                query = _build_clipboard_image_query(runtime, "describe this")

        self.assertEqual(query, "/image .open_somnia/temp/clipboard-test.png describe this")

    def test_save_windows_clipboard_image_prefers_file_copy_when_available(self) -> None:
        temp_root = Path.cwd() / ".tmp-tests" / f"repl-clipboard-copy-{time.time_ns()}"
        temp_root.mkdir(parents=True, exist_ok=True)
        copied_path = temp_root / "clipboard-test.png"
        copied_path.write_bytes(self._TINY_PNG_BYTES)

        with patch("open_somnia.cli.repl._copy_windows_clipboard_image_file", return_value=copied_path):
            with patch(
                "open_somnia.cli.repl._read_windows_clipboard_dib_bytes",
                side_effect=AssertionError("bitmap fallback should not run after file copy"),
            ):
                saved_path = _save_windows_clipboard_image(temp_root, "clipboard-test")

        self.assertEqual(saved_path, copied_path)

    def test_save_windows_clipboard_image_converts_dib_bytes_and_cleans_up_bmp(self) -> None:
        temp_root = Path.cwd() / ".tmp-tests" / f"repl-clipboard-dib-{time.time_ns()}"
        temp_root.mkdir(parents=True, exist_ok=True)
        expected_png = temp_root / "clipboard-test.png"

        def _fake_convert(source_path: Path, destination: Path) -> Path:
            self.assertEqual(source_path.name, "clipboard-test.bmp")
            self.assertTrue(source_path.exists())
            destination.write_bytes(self._TINY_PNG_BYTES)
            return destination

        with patch("open_somnia.cli.repl._copy_windows_clipboard_image_file", return_value=None):
            with patch("open_somnia.cli.repl._read_windows_clipboard_dib_bytes", return_value=b"fake-dib"):
                with patch("open_somnia.cli.repl._dib_to_bmp_bytes", return_value=b"fake-bmp"):
                    with patch("open_somnia.cli.repl._convert_bmp_file_to_png", side_effect=_fake_convert):
                        saved_path = _save_windows_clipboard_image(temp_root, "clipboard-test")

        self.assertEqual(saved_path, expected_png)
        self.assertTrue(expected_png.exists())
        self.assertFalse((temp_root / "clipboard-test.bmp").exists())

    def test_current_model_label_uses_active_provider_model_and_auto_reasoning(self) -> None:
        runtime = SimpleNamespace(settings=SimpleNamespace(provider=SimpleNamespace(name="anthropic", model="glm-5")))
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        self.assertEqual(runner.current_model_label(), "model: anthropic / glm-5|auto")
        self.assertIn("accept edits on", runner.execution_mode_label())

    def test_current_model_label_appends_reasoning_level_after_model_name(self) -> None:
        runtime = SimpleNamespace(
            settings=SimpleNamespace(provider=SimpleNamespace(name="openai", model="gpt-5.4", reasoning_level="high"))
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        self.assertEqual(runner.current_model_label(), "model: openai / gpt-5.4|high")

    def test_bottom_toolbar_shows_model_and_context_window(self) -> None:
        runtime = SimpleNamespace(
            settings=SimpleNamespace(provider=SimpleNamespace(name="openai", model="gpt-5")),
            context_window_usage=lambda session: ContextWindowUsage(
                used_tokens=40_000,
                max_tokens=200_000,
                counter_name="tiktoken",
            ),
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        self.assertEqual(
            runner.bottom_toolbar(),
            [
                ("fg:#94a3b8", "model: openai / gpt-5|auto"),
                ("fg:#64748b", " | "),
                ("fg:#22c55e", "ctx: 20.0% (40.0k / 200.0k tokens)"),
            ],
        )

    def test_bottom_toolbar_hides_token_sum_when_session_has_usage(self) -> None:
        runtime = SimpleNamespace(
            settings=SimpleNamespace(provider=SimpleNamespace(name="openai", model="gpt-5")),
            context_window_usage=lambda session: ContextWindowUsage(
                used_tokens=40_000,
                max_tokens=200_000,
                counter_name="tiktoken",
            ),
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[], token_usage={"total_tokens": 12_345}), stable_prompt=True)

        self.assertEqual(
            runner.bottom_toolbar(),
            [
                ("fg:#94a3b8", "model: openai / gpt-5|auto"),
                ("fg:#64748b", " | "),
                ("fg:#22c55e", "ctx: 20.0% (40.0k / 200.0k tokens)"),
            ],
        )

    def test_bottom_toolbar_shows_session_cache_hit_rate(self) -> None:
        runtime = SimpleNamespace(
            settings=SimpleNamespace(provider=SimpleNamespace(name="openai", model="gpt-5")),
            context_window_usage=lambda session: ContextWindowUsage(
                used_tokens=40_000,
                max_tokens=200_000,
                counter_name="tiktoken",
            ),
        )
        runner = TurnQueueRunner(
            runtime,
            SimpleNamespace(
                todo_items=[],
                token_usage={
                    "input_tokens": 25_000,
                    "cache_read_input_tokens": 75_000,
                    "total_tokens": 30_000,
                },
            ),
            stable_prompt=True,
        )

        self.assertIn(("fg:#86efac", "cache: 75.0% (75.0k read)"), runner.bottom_toolbar())

    def test_tool_started_updates_repl_panel_without_printing_body_output(self) -> None:
        runtime = SimpleNamespace(settings=SimpleNamespace(provider=SimpleNamespace(name="openai", model="gpt-5")))
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)
        event = SimpleNamespace(
            type="tool_started",
            payload={
                "actor": "lead",
                "tool_name": "bash",
                "tool_input": {"command": "Start-Sleep -Seconds 5"},
                "tool_call_id": "call-1",
            },
        )

        fake_stdout = io.StringIO()
        with patch("sys.stdout", fake_stdout):
            runner._process_service_event(event, ConsoleStreamer())

        prompt_text = _render_prompt_text(runner.prompt_message())
        self.assertEqual(fake_stdout.getvalue(), "")
        self.assertIn("tools (1 running)", prompt_text)
        self.assertIn("bash", prompt_text)
        self.assertIn("Start-Sleep -Seconds 5", prompt_text)

    def test_bottom_toolbar_includes_recent_context_governance_label(self) -> None:
        runtime = SimpleNamespace(
            settings=SimpleNamespace(provider=SimpleNamespace(name="openai", model="gpt-5")),
            context_window_usage=lambda session: ContextWindowUsage(
                used_tokens=40_000,
                max_tokens=200_000,
                counter_name="tiktoken",
            ),
            recent_context_governance_label=lambda session: "janitor reduced 1 tool result(s)",
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        self.assertEqual(
            runner.bottom_toolbar(),
            [
                ("fg:#94a3b8", "model: openai / gpt-5|auto"),
                ("fg:#64748b", " | "),
                ("fg:#22c55e", "ctx: 20.0% (40.0k / 200.0k tokens)"),
                ("fg:#64748b", " | "),
                ("fg:#67e8f9", "janitor reduced 1 tool result(s)"),
            ],
        )

    def test_bottom_toolbar_prefers_recent_context_cache_over_live_recount(self) -> None:
        runtime = SimpleNamespace(
            settings=SimpleNamespace(provider=SimpleNamespace(name="openai", model="gpt-5")),
            recent_context_window_usage=lambda session: ContextWindowUsage(
                used_tokens=40_000,
                max_tokens=200_000,
                counter_name="tiktoken",
            ),
            context_window_usage=lambda session: (_ for _ in ()).throw(AssertionError("live recount should not run")),
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        self.assertEqual(
            runner.bottom_toolbar(),
            [
                ("fg:#94a3b8", "model: openai / gpt-5|auto"),
                ("fg:#64748b", " | "),
                ("fg:#22c55e", "ctx: 20.0% (40.0k / 200.0k tokens)"),
            ],
        )

    def test_context_health_gradient_styles_follow_thresholds(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)

        cases = [
            (ContextWindowUsage(used_tokens=30, max_tokens=100), "fg:#22c55e"),
            (ContextWindowUsage(used_tokens=60, max_tokens=100), "fg:#84cc16"),
            (ContextWindowUsage(used_tokens=80, max_tokens=100), "fg:#f59e0b"),
            (ContextWindowUsage(used_tokens=81, max_tokens=100), "fg:#ef4444"),
        ]

        for usage, expected_style in cases:
            runner.runtime = SimpleNamespace(context_window_usage=lambda session, usage=usage: usage)
            self.assertEqual(runner.current_context_style(), expected_style)

    def test_prompt_message_shows_open_todos_before_mode_and_prompt(self) -> None:
        session = SimpleNamespace(
            todo_items=[
                {"content": "Refactor module", "status": "in_progress", "activeForm": "Refactoring module"},
                {"content": "Add tests", "status": "pending", "activeForm": "Adding tests"},
                {"content": "Run checks", "status": "completed", "activeForm": "Running checks"},
            ]
        )
        runner = TurnQueueRunner(SimpleNamespace(), session, stable_prompt=True)
        runner._status = "thinking"
        runner._thinking_phrase = "Loading genius"
        runner._status_changed_at = 0.0

        rendered = _render_prompt_text(runner.prompt_message())

        self.assertTrue(rendered.startswith("│ "))
        self.assertIn(f"\n{PROMPT_BORDER}\n❯ ", rendered)
        self.assertIn("todo (1/3 completed)", rendered)
        self.assertIn("accept edits on  (Shift+Tab to cycle)", rendered)
        self.assertIn("Refactor module <- Refactoring module", rendered)
        self.assertIn("Add tests", rendered)
        self.assertIn("Run checks", rendered)
        self.assertNotIn(f"{PROMPT_BORDER}\n│ Loading genius", rendered)
        self.assertLess(rendered.index("│ Loading genius"), rendered.index("│ todo (1/3 completed)"))
        self.assertLess(rendered.index("todo (1/3 completed)"), rendered.index("accept edits on  (Shift+Tab to cycle)"))
        self.assertLess(rendered.index("accept edits on  (Shift+Tab to cycle)"), rendered.rindex(PROMPT_BORDER))
        self.assertLess(rendered.rindex(PROMPT_BORDER), rendered.index("❯ "))
        self.assertNotIn("model: unknown", rendered)

    def test_thinking_preview_wraps_word_deltas_into_lines(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner.THINKING_PREVIEW_LINES = 5

        for delta in ["I ", "am ", "checking ", "the ", "current ", "implementation ", "before ", "editing."]:
            runner._note_thinking_event({"event": "delta", "delta": delta, "path": "thinking/session.turn.jsonl"})

        lines = [line for _, line in runner._thinking_lines()]

        self.assertEqual(lines[0], "think")
        self.assertTrue(any("I am checking the current implementation before editing." in line for line in lines))
        self.assertFalse(any(line == "↳ I" for line in lines))
        self.assertLessEqual(len([line for line in lines if line.startswith("↳ ")]), 5)
        self.assertEqual(lines[-1], "")

    def test_thinking_log_label_uses_bullet_prefix(self) -> None:
        self.assertEqual(
            _thinking_log_label(99, "thinking/session.turn.jsonl"),
            "● think 99 chars -> thinking/session.turn.jsonl",
        )
        self.assertEqual(_thinking_log_label(99, ""), "● think 99 chars")
        self.assertIn(
            "\x1b[38;2;167;139;250m●\x1b[0m think 99 chars",
            _thinking_log_label(99, "", ansi=True),
        )

    def test_prompt_message_keeps_status_bar_and_todos_in_one_persistent_panel(self) -> None:
        runtime = SimpleNamespace(
            settings=SimpleNamespace(
                provider=SimpleNamespace(name="openai", model="gpt-test", reasoning_level="medium")
            ),
            context_window_usage=lambda session: ContextWindowUsage(
                used_tokens=8_000,
                max_tokens=100_000,
                counter_name="test_counter",
            ),
        )
        session = SimpleNamespace(
            todo_items=[
                {"content": "Refactor module", "status": "in_progress", "activeForm": "Refactoring module"},
                {"content": "Add tests", "status": "pending", "activeForm": "Adding tests"},
            ]
        )
        runner = TurnQueueRunner(runtime, session, stable_prompt=True)

        rendered = _render_prompt_text(runner.prompt_message())

        self.assertIn("model: openai / gpt-test|medium", rendered)
        self.assertIn("ctx: 8.0% (8.0k / 100.0k tokens)", rendered)
        self.assertIn("todo (0/2 completed)", rendered)
        self.assertLess(rendered.index("model: openai / gpt-test|medium"), rendered.index("todo (0/2 completed)"))
        self.assertLess(rendered.index("todo (0/2 completed)"), rendered.index("accept edits on  (Shift+Tab to cycle)"))

    def test_prompt_message_hides_todos_when_all_completed(self) -> None:
        session = SimpleNamespace(
            todo_items=[
                {"content": "Refactor module", "status": "completed", "activeForm": "Refactoring module"},
                {"content": "Add tests", "status": "completed", "activeForm": "Adding tests"},
            ]
        )
        runner = TurnQueueRunner(SimpleNamespace(), session, stable_prompt=True)

        rendered = _render_prompt_text(runner.prompt_message())

        self.assertNotIn("todo (", rendered)
        self.assertEqual(rendered, f"│ ⏵⏵ accept edits on  (Shift+Tab to cycle)\n{PROMPT_BORDER}\n❯ ")

    def test_prompt_message_shows_compacting_status(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner._status = "compacting"
        runner._status_changed_at = 0.0

        rendered = _render_prompt_text(runner.prompt_message())

        self.assertIn("compacting context", rendered)

    def test_cancel_task_drops_pending_queued_prompts(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner.enqueue("first")
        runner.enqueue("second")
        runner.enqueue("third")

        self.assertEqual(runner.stats(), (False, 3))

        self.assertTrue(runner.cancel_task(2))
        self.assertEqual(runner.stats(), (False, 2))
        # Cancelling an already-cancelled or unknown id reports failure.
        self.assertFalse(runner.cancel_task(2))
        self.assertFalse(runner.cancel_task(99))

        # Preview lines advertise the cancel command for each queued item.
        lines = runner._queue_preview_lines()
        self.assertTrue(any("/cancel 3" in line for line in lines))

    def test_cancel_task_drops_ready_loop_injection(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner.enqueue("first")
        runner.enqueue("second")

        # Promote the head queued prompt to the ready stage for the next loop.
        runner._active = True
        self.assertTrue(runner.request_loop_injection())
        self.assertTrue(runner.prepare_next_loop_injection())
        self.assertEqual(runner.stats(), (True, 1))

        self.assertTrue(runner.cancel_task(1))
        self.assertIsNone(runner.take_next_loop_injection())
        self.assertEqual(runner._ready_loop_injection_ids, [])
        # The still-pending second prompt remains listed with its cancel hint.
        self.assertEqual(runner._queue_preview_lines(), ["second  (/cancel 2)"])

    def test_prompt_message_shows_recent_janitor_hint_before_mode_and_prompt(self) -> None:
        runtime = SimpleNamespace(
            recent_context_governance_label=lambda session: "janitor reduced 2 tool result(s)",
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        rendered = _render_prompt_text(runner.prompt_message())

        self.assertIn("janitor reduced 2 tool result(s)", rendered)
        self.assertLess(rendered.index("janitor reduced 2 tool result(s)"), rendered.index("accept edits on"))

    def test_prompt_message_shows_recent_auto_compact_hint_before_mode_and_prompt(self) -> None:
        runtime = SimpleNamespace(
            recent_context_governance_label=lambda session: "auto-compacted older history",
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        rendered = _render_prompt_text(runner.prompt_message())

        self.assertIn("auto-compacted older history", rendered)
        self.assertLess(rendered.index("auto-compacted older history"), rendered.index("accept edits on"))

    def test_prompt_message_shows_active_teammates_before_mode_and_prompt(self) -> None:
        runtime = SimpleNamespace(
            team_manager=SimpleNamespace(
                active_member_summaries=lambda: [
                    {
                        "name": "Analyst",
                        "role": "algorithm analyst",
                        "status": "working",
                        "activity": "running_tool:grep",
                        "current_tool_name": "grep",
                        "last_activity_at": 0.0,
                        "recent_interactions": ["tool grep: Found 12 matches"],
                    },
                    {
                        "name": "Writer",
                        "role": "report writer",
                        "status": "idle",
                        "activity": "idle_waiting_on_owned_task",
                        "last_activity_at": 0.0,
                        "recent_interactions": ["assistant: Waiting for analysis results"],
                    }
                ],
                _format_member_summary=lambda member: f"{member['name']} ({member['role']}): {member['status']} View team logs: /teamlog {member['name']}",
            )
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        rendered = _render_prompt_text(runner.prompt_message())

        self.assertIn("team (2 active)", rendered)
        self.assertIn("View team logs: /teamlog Analyst", rendered)
        self.assertIn("View team logs: /teamlog Writer", rendered)
        self.assertIn("↳ Analyst: tool grep: Found 12 matches", rendered)
        self.assertIn("↳ Writer: assistant: Waiting for analysis results", rendered)
        self.assertLess(rendered.index("team (2 active)"), rendered.index("accept edits on  (Shift+Tab to cycle)"))

    def test_prompt_message_shows_active_subagent_before_mode_and_prompt(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)

        runner._note_tool_started(
            {
                "actor": "lead",
                "tool_name": "subagent",
                "trace_id": "turn-1",
                "tool_input": {
                    "agent_type": "Explore",
                    "prompt": "Inspect the authentication module and report the key risks.",
                },
            }
        )

        rendered = _render_prompt_text(runner.prompt_message())

        self.assertIn("subagent (1 running)", rendered)
        self.assertIn("⏳ Explore: Inspect the authentication module", rendered)
        self.assertLess(rendered.index("subagent (1 running)"), rendered.index("accept edits on  (Shift+Tab to cycle)"))

    def test_prompt_message_rotates_subagent_fact_line(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner._note_tool_started(
            {
                "actor": "lead",
                "tool_name": "subagent",
                "trace_id": "turn-1",
                "tool_input": {"agent_type": "Explore", "prompt": "Inspect routing."},
            }
        )
        runner._note_subagent_activity(
            {
                "activity_id": "turn-1",
                "agent_type": "Explore",
                "prompt": "Inspect routing.",
                "kind": "tool_result",
                "text": "grep route: found open_somnia/cli/repl.py",
            }
        )

        rendered = _render_prompt_text(runner.prompt_message())

        self.assertIn("↳ grep route: found open_somnia/cli/repl.py", rendered)
        self.assertLess(rendered.index("↳ grep route"), rendered.index("accept edits on  (Shift+Tab to cycle)"))

    def test_service_subagent_events_update_persistent_panel(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        streamer = ConsoleStreamer(start_on_new_line=True)
        started_payload = {
            "actor": "lead",
            "tool_name": "subagent",
            "tool_input": {
                "agent_type": "general-purpose",
                "prompt": "Patch the parser tests.",
            },
        }

        runner._process_service_event(SimpleNamespace(type="tool_started", payload=started_payload), streamer)
        self.assertIn("subagent (1 running)", _render_prompt_text(runner.prompt_message()))

        runner._process_service_event(
            SimpleNamespace(
                type="subagent_activity",
                payload={
                    "agent_type": "general-purpose",
                    "prompt": "Patch the parser tests.",
                    "text": "edit_file tests/test_parser.py: Updated file",
                },
            ),
            streamer,
        )
        self.assertIn("↳ edit_file tests/test_parser.py: Updated file", _render_prompt_text(runner.prompt_message()))

        runner._process_service_event(SimpleNamespace(type="tool_finished", payload=started_payload), streamer)
        self.assertNotIn("subagent (", _render_prompt_text(runner.prompt_message()))

    def test_prompt_message_omits_cancelled_items_from_visible_todo_block(self) -> None:
        session = SimpleNamespace(
            todo_items=[
                {"content": "Refactor module", "status": "in_progress", "activeForm": "Refactoring module"},
                {
                    "content": "Drop old approach",
                    "status": "cancelled",
                    "activeForm": "Dropping old approach",
                    "cancelledReason": "Superseded by the new approach",
                },
                {"content": "Run checks", "status": "completed", "activeForm": "Running checks"},
            ]
        )
        runner = TurnQueueRunner(SimpleNamespace(), session, stable_prompt=True)

        rendered = _render_prompt_text(runner.prompt_message())

        self.assertIn("todo (1/2 completed)", rendered)
        self.assertIn("Refactor module <- Refactoring module", rendered)
        self.assertIn("Run checks", rendered)
        self.assertNotIn("Drop old approach", rendered)

    def test_prompt_message_shows_context_window_before_mode(self) -> None:
        runtime = SimpleNamespace(
            context_window_usage=lambda session: ContextWindowUsage(
                used_tokens=64_000,
                max_tokens=200_000,
                counter_name="anthropic_native",
            )
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        rendered = _render_prompt_text(runner.prompt_message())
        context_fragments = [fragment for fragment in runner.prompt_message() if fragment[1] == "ctx: 32.0% (64.0k / 200.0k tokens)"]

        self.assertIn("ctx: 32.0% (64.0k / 200.0k tokens)", rendered)
        self.assertLess(rendered.index("ctx: 32.0% (64.0k / 200.0k tokens)"), rendered.index("accept edits on"))
        self.assertEqual(context_fragments, [("fg:#84cc16", "ctx: 32.0% (64.0k / 200.0k tokens)")])

    def test_prompt_message_shows_queue_notice_and_previews_before_mode(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner._queued_previews = [
            (1, "first queued prompt"),
            (2, "second queued prompt"),
        ]

        rendered = _render_prompt_text(runner.prompt_message())

        self.assertIn(
            "Queued: after turn; Esc sends next after tool",
            rendered,
        )
        self.assertIn("1. first queued prompt", rendered)
        self.assertIn("2. second queued prompt", rendered)
        self.assertLess(
            rendered.index("Queued: after turn; Esc sends next after tool"),
            rendered.index("accept edits on  (Shift+Tab to cycle)"),
        )

    def test_request_loop_injection_arms_next_turn_for_tool_boundary(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner._active = True
        runner.enqueue("first queued prompt")
        runner.enqueue_compact()

        requested = runner.request_loop_injection()

        self.assertTrue(requested)
        self.assertIn("Queued: next one sends after current tool", _render_prompt_text(runner.prompt_message()))
        self.assertTrue(runner.prepare_next_loop_injection())
        rendered = _render_prompt_text(runner.prompt_message())
        self.assertIn("[next] first queued prompt", rendered)
        self.assertIn("/compact", rendered)
        self.assertEqual(runner.take_next_loop_injection(), "first queued prompt")
        self.assertEqual(runner.stats(), (True, 1))

    def test_request_loop_injection_requires_pending_turn_message(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner._active = True
        runner.enqueue_compact()

        requested = runner.request_loop_injection()

        self.assertFalse(requested)

    def test_request_loop_injection_is_idempotent_once_next_message_is_already_armed(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner._active = True
        runner.enqueue("first queued prompt")

        first_request = runner.request_loop_injection()
        second_request = runner.request_loop_injection()

        self.assertTrue(first_request)
        self.assertTrue(second_request)
        self.assertTrue(runner.prepare_next_loop_injection())
        self.assertEqual(runner.take_next_loop_injection(), "first queued prompt")

    def test_take_next_loop_injection_echoes_message_to_output(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner._active = True
        runner.enqueue("first queued prompt")
        runner.request_loop_injection()
        runner.prepare_next_loop_injection()

        with patch("open_somnia.cli.repl.print_user_message") as mock_print_user_message:
            payload = runner.take_next_loop_injection()

        self.assertEqual(payload, "first queued prompt")
        mock_print_user_message.assert_called_once_with("first queued prompt")

    def test_clear_pending_drops_ready_loop_injection_messages(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner._ready_loop_injections = ["first queued prompt"]
        runner._ready_loop_injection_previews = ["first queued prompt"]
        runner._loop_injection_requests = 1

        dropped = runner._clear_pending()

        self.assertEqual(dropped, 1)
        self.assertEqual(runner._loop_injection_requests, 0)
        self.assertIsNone(runner.take_next_loop_injection())

    def test_todo_manager_treats_cancelled_items_as_closed_and_hidden(self) -> None:
        session = SimpleNamespace(todo_items=[])
        manager = TodoManager()

        rendered = manager.update(
            session,
            [
                {
                    "content": "Drop old approach",
                    "status": "cancelled",
                    "activeForm": "Dropping old approach",
                    "cancelledReason": "Superseded by the new approach",
                }
            ],
        )

        self.assertEqual(session.todo_items[0]["status"], "cancelled")
        self.assertEqual(session.todo_items[0]["cancelledReason"], "Superseded by the new approach")
        self.assertFalse(manager.has_open_items(session))
        self.assertEqual(rendered, "No todos.")

    def test_todo_manager_requires_cancelled_reason_for_cancelled_items(self) -> None:
        session = SimpleNamespace(todo_items=[])
        manager = TodoManager()

        with self.assertRaisesRegex(ValueError, "cancelledReason required"):
            manager.update(
                session,
                [
                    {
                        "content": "Drop old approach",
                        "status": "cancelled",
                        "activeForm": "Dropping old approach",
                    }
                ],
            )

    def test_cycle_execution_mode_advances_in_danger_order(self) -> None:
        runtime = SimpleNamespace(settings=SimpleNamespace(provider=SimpleNamespace(name="anthropic", model="glm-5")))
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        self.assertEqual(runner.current_execution_mode().key, "accept_edits")
        runner.cycle_execution_mode()
        self.assertEqual(runner.current_execution_mode().key, "yolo")
        runner.cycle_execution_mode()
        self.assertEqual(runner.current_execution_mode().key, "shortcuts")
        runner.cycle_execution_mode()
        self.assertEqual(runner.current_execution_mode().key, "plan")
        runner.cycle_execution_mode()
        self.assertEqual(runner.current_execution_mode().key, "accept_edits")
        self.assertEqual(runtime.execution_mode, "accept_edits")

    def test_model_command_switches_provider_and_model_from_interactive_choices(self) -> None:
        runtime = SimpleNamespace(
            configured_provider_profiles=lambda: {
                "anthropic": SimpleNamespace(default_model="glm-5", models=["glm-5", "claude-sonnet-4-5"])
            },
            switch_provider_model=lambda provider, model: f"switched {provider}:{model}",
        )

        with patch("open_somnia.cli.repl.choose_item_interactively", side_effect=["anthropic", "glm-5"]), patch(
            "builtins.print"
        ) as mock_print:
            _handle_model_command(runtime)

        mock_print.assert_called_with("switched anthropic:glm-5")

    def test_vision_command_sets_configured_model_directly(self) -> None:
        runtime = SimpleNamespace(
            settings=SimpleNamespace(provider=SimpleNamespace(name="openai", model="text-model")),
            configured_provider_profiles=lambda: {
                "openai": SimpleNamespace(models=["text-model"]),
                "vision": SimpleNamespace(models=["vision-model"]),
            },
            set_vision_model=lambda vision_provider, model: f"set vision:{vision_provider}:{model}",
        )

        with patch("builtins.print") as mock_print:
            _handle_vision_command(runtime, "/vision vision vision-model")

        mock_print.assert_called_once_with("set vision:vision:vision-model")

    def test_reasoning_command_sets_reasoning_level_directly(self) -> None:
        runtime = SimpleNamespace(set_reasoning_level=lambda level: f"set reasoning:{level}")

        with patch("builtins.print") as mock_print:
            _handle_reasoning_command(runtime, "/reasoning high")

        mock_print.assert_called_once_with("set reasoning:high")

    def test_reasoning_command_uses_interactive_picker_when_no_argument_is_given(self) -> None:
        runtime = SimpleNamespace(
            settings=SimpleNamespace(provider=SimpleNamespace(reasoning_level="medium")),
            set_reasoning_level=lambda level: f"set reasoning:{level}",
        )

        with patch("open_somnia.cli.repl.choose_item_interactively", return_value="deep") as mock_choose, patch(
            "builtins.print"
        ) as mock_print:
            _handle_reasoning_command(runtime, "/reasoning")

        mock_choose.assert_called_once()
        self.assertEqual(mock_choose.call_args.args[0], "Choose Reasoning")
        self.assertEqual(mock_choose.call_args.args[2][0][0], "medium")
        mock_print.assert_called_once_with("set reasoning:deep")

    def test_reasoning_command_supports_auto_to_restore_unset_state(self) -> None:
        runtime = SimpleNamespace(set_reasoning_level=lambda level: f"set reasoning:{level}")

        with patch("builtins.print") as mock_print:
            _handle_reasoning_command(runtime, "/reasoning auto")

        mock_print.assert_called_once_with("set reasoning:None")

    def test_reasoning_command_keeps_none_as_compatibility_alias(self) -> None:
        runtime = SimpleNamespace(set_reasoning_level=lambda level: f"set reasoning:{level}")

        with patch("builtins.print") as mock_print:
            _handle_reasoning_command(runtime, "/reasoning none")

        mock_print.assert_called_once_with("set reasoning:None")

    def test_reasoning_command_rejects_invalid_argument(self) -> None:
        runtime = SimpleNamespace(set_reasoning_level=lambda level: f"set reasoning:{level}")

        with patch("builtins.print") as mock_print:
            _handle_reasoning_command(runtime, "/reasoning turbo")

        mock_print.assert_called_once_with("[usage: /reasoning <auto|low|medium|high|deep>]")

    def test_symbols_command_chooses_match_and_previews_source(self) -> None:
        parsed_matches = [
            {"path": "src/app.py", "line": 12, "kind": "function", "name": "build_app"},
            {"path": "src/lib.py", "line": 4, "kind": "class", "name": "Builder"},
        ]
        invoked: list[dict[str, object]] = []

        def _invoke_tool(session, name, payload):
            invoked.append(payload)
            return "src/app.py:12:function build_app\nsrc/lib.py:4:class Builder"

        runtime = SimpleNamespace(
            invoke_tool=_invoke_tool,
            parse_symbol_output=lambda output: parsed_matches,
            render_symbol_preview=lambda relative_path, line_number: f"{relative_path}:{line_number}\n>   12 | def build_app():",
        )
        session = SimpleNamespace()

        with patch("open_somnia.cli.repl.choose_item_interactively", return_value="1"), patch("builtins.print") as mock_print:
            _handle_symbols_command(runtime, session, "/symbols build")

        self.assertEqual(invoked, [{"query": "build", "path": ".", "limit": 50}])
        mock_print.assert_called_with("src/app.py:12\n>   12 | def build_app():")

    def test_symbols_command_passes_pipe_separated_query_through_to_tool(self) -> None:
        invoked: list[dict[str, object]] = []

        def _invoke_tool(session, name, payload):
            invoked.append(payload)
            return "(no matches)"

        runtime = SimpleNamespace(
            invoke_tool=_invoke_tool,
            parse_symbol_output=lambda output: [],
            render_symbol_preview=lambda relative_path, line_number: "",
        )
        session = SimpleNamespace()

        with patch("builtins.print"):
            _handle_symbols_command(runtime, session, "/symbols build|builder|factory")

        self.assertEqual(invoked, [{"query": "build|builder|factory", "path": ".", "limit": 50}])

    def test_providers_command_updates_existing_active_provider_and_reloads_runtime(self) -> None:
        reloaded: list[tuple[str, str]] = []
        runtime = SimpleNamespace(
            configured_provider_profiles=lambda: {
                "openrouter": SimpleNamespace(
                    name="openrouter",
                    provider_type="openai",
                    default_model="gpt-5",
                    models=["gpt-5"],
                    api_key="sk-old",
                    base_url="https://openrouter.ai/api/v1",
                )
            },
            settings=SimpleNamespace(provider=SimpleNamespace(name="openrouter", model="gpt-5")),
            reload_provider_configuration=lambda provider_name, model: reloaded.append((provider_name, model)),
        )

        with patch("open_somnia.cli.repl.choose_provider_target_interactively", return_value="openrouter"), patch(
            "open_somnia.cli.repl.collect_provider_profile_interactively",
            return_value=SimpleNamespace(
                previous_provider_name="openrouter",
                provider_name="openrouter-main",
                provider_type="openai",
                base_url="https://openrouter.ai/api/v1",
                api_key="sk-old",
                models=["gpt-4.1-mini"],
            ),
        ), patch(
            "open_somnia.cli.repl.persist_provider_profile",
            return_value="C:/Users/test/.open_somnia/open_somnia.toml",
        ), patch("builtins.print") as mock_print:
            _handle_providers_command(runtime)

        self.assertEqual(reloaded, [("openrouter-main", "gpt-4.1-mini")])
        mock_print.assert_called_once()

    def test_mcp_command_uses_interactive_browser_instead_of_printing_status(self) -> None:
        runtime = SimpleNamespace(
            mcp_registry=SimpleNamespace(
                server_summaries=lambda: [
                    {
                        "name": "minimal",
                        "transport": "stdio",
                        "target": "python",
                        "status": "connected",
                        "error": "",
                        "tool_count": 2,
                    }
                ],
                tool_summaries=lambda server_name: [
                    {
                        "name": "echo",
                        "description": "Echo text",
                        "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}},
                    }
                ],
            ),
            mcp_status=lambda: "should not print",
        )

        with patch(
            "open_somnia.cli.repl.choose_item_interactively",
            side_effect=["minimal", "echo", "__back__", "__back__", None],
        ) as mock_choose, patch("builtins.print") as mock_print:
            _handle_mcp_command(runtime)

        self.assertEqual(mock_choose.call_count, 5)
        mock_print.assert_not_called()

    def test_reloadplugin_command_prints_reload_summary(self) -> None:
        progress_messages: list[str] = []

        def reload_plugin_configuration(progress_callback=None):
            if callable(progress_callback):
                progress_callback("registering MCP tools")
                progress_messages.append("called")
            return {
                "mcp_server_count": 1,
                "mcp_tool_count": 2,
                "skill_count": 3,
                "project_instruction_count": 1,
                "mcp_errors": {"broken": "connection failed"},
            }

        runtime = SimpleNamespace(
            reload_plugin_configuration=reload_plugin_configuration,
        )

        with patch("builtins.print") as mock_print:
            _handle_reloadplugin_command(runtime)

        printed_calls = [call.args[0] for call in mock_print.call_args_list]
        self.assertEqual(progress_messages, ["called"])
        self.assertIn("[reloadplugin] started", printed_calls)
        self.assertIn("[reloadplugin] registering MCP tools...", printed_calls)
        printed = printed_calls[-1]
        self.assertIn("[reloadplugin complete]", printed)
        self.assertIn("MCP servers: 1", printed)
        self.assertIn("MCP tools: 2", printed)
        self.assertIn("Skills: 3", printed)
        self.assertIn("Project instruction files: 1", printed)
        self.assertIn("- broken: connection failed", printed)

    def test_hooks_command_browses_events_and_toggles_selected_hook(self) -> None:
        builtin_hook = SimpleNamespace(
            event="AssistantResponse",
            enabled=True,
            managed_by="somnia_builtin_notify",
            command="python",
            args=["notify_user.py"],
            config_scope="global",
        )
        custom_hook = SimpleNamespace(
            event="AssistantResponse",
            enabled=False,
            managed_by=None,
            command="python",
            args=["hooks/custom_notify.py"],
            config_scope="workspace",
        )
        hooks_state = [builtin_hook, custom_hook]
        toggles: list[tuple[object, bool]] = []

        def configured_hooks():
            return list(hooks_state)

        def set_hook_enabled(hook, enabled: bool) -> str:
            toggles.append((hook, enabled))
            hook.enabled = enabled
            return f"hook toggled to {enabled}"

        runtime = SimpleNamespace(
            configured_hooks=configured_hooks,
            set_hook_enabled=set_hook_enabled,
        )

        with patch(
            "open_somnia.cli.repl.choose_item_interactively",
            side_effect=["AssistantResponse", "1", "toggle", "__back__", None],
        ) as mock_choose, patch("builtins.print") as mock_print:
            _handle_hooks_command(runtime)

        self.assertEqual(mock_choose.call_count, 5)
        self.assertEqual(toggles, [(builtin_hook, False)])
        mock_print.assert_called_once_with("hook toggled to False")

    def test_request_interrupt_marks_runner_interrupting(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner._active = True

        requested = runner.request_interrupt()

        self.assertTrue(requested)
        self.assertTrue(runner.should_interrupt())
        self.assertEqual(runner._status, "interrupting")

    def test_request_interrupt_propagates_to_active_teammates(self) -> None:
        reasons: list[str] = []
        runtime = SimpleNamespace(
            interrupt_active_teammates=lambda reason="lead_interrupt": reasons.append(reason) or 1,
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner._active = True

        requested = runner.request_interrupt()

        self.assertTrue(requested)
        self.assertEqual(reasons, ["lead_interrupt"])

    def test_request_interrupt_uses_service_for_active_turn(self) -> None:
        interrupted_turns: list[str] = []
        service = SimpleNamespace(interrupt_turn=lambda turn_id: interrupted_turns.append(turn_id) or True)
        runtime = SimpleNamespace(
            interrupt_active_teammates=lambda reason="lead_interrupt": (_ for _ in ()).throw(
                AssertionError("runtime teammate interrupt should not run in service mode")
            )
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True, service=service)
        runner._active = True
        runner._active_turn_handle = SimpleNamespace(turn_id="turn-1")

        requested = runner.request_interrupt()

        self.assertTrue(requested)
        self.assertTrue(runner.should_interrupt())
        self.assertEqual(interrupted_turns, ["turn-1"])

    def test_status_for_response_marks_open_todo_max_round_stop_explicitly(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)

        status = runner._status_for_response(SimpleNamespace(status="stopped_with_open_todos"))

        self.assertEqual(status, "stopped_with_open_todos")
        runner._status = status
        self.assertEqual(runner._status_line(), "stopped_with_open_todos")

    def test_status_for_response_marks_generic_max_round_stop_explicitly(self) -> None:
        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)

        status = runner._status_for_response(SimpleNamespace(status="stopped_after_max_rounds"))

        self.assertEqual(status, "stopped_after_max_rounds")
        runner._status = status
        self.assertEqual(runner._status_line(), "stopped_after_max_rounds")

    def test_status_for_response_marks_completed_turn_with_open_todos_explicitly(self) -> None:
        runner = TurnQueueRunner(
            SimpleNamespace(),
            SimpleNamespace(todo_items=[{"content": "Confirm scope", "status": "in_progress"}]),
            stable_prompt=True,
        )

        status = runner._status_for_response(SimpleNamespace(status="completed"))

        self.assertEqual(status, "waiting_on_open_todos")
        runner._status = status
        self.assertEqual(runner._status_line(), "waiting_on_open_todos")

    def test_status_for_response_marks_completed_turn_with_active_teammates_explicitly(self) -> None:
        runtime = SimpleNamespace(
            team_manager=SimpleNamespace(
                active_member_summaries=lambda: [{"name": "Architect", "status": "working"}],
            )
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        status = runner._status_for_response(SimpleNamespace(status="completed"))

        self.assertEqual(status, "waiting_on_teammates")
        runner._status = status
        self.assertEqual(runner._status_line(), "waiting_on_teammates")

    def test_status_for_response_keeps_done_when_all_todos_are_closed(self) -> None:
        runner = TurnQueueRunner(
            SimpleNamespace(),
            SimpleNamespace(todo_items=[{"content": "Ship it", "status": "completed"}]),
            stable_prompt=True,
        )

        status = runner._status_for_response(SimpleNamespace(status="completed"))

        self.assertEqual(status, "done")

    def test_compact_task_runs_before_queued_turn(self) -> None:
        events: list[str] = []
        runtime = SimpleNamespace(
            compact_session=lambda session: events.append("compact"),
            run_turn=lambda session, query, text_callback=None, should_interrupt=None: events.append(f"turn:{query}") or "Done.",
            print_last_turn_file_summary=lambda session: False,
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)
        runner.start()

        runner.enqueue_compact()
        runner.enqueue("follow-up prompt")
        runner.close(drain=True)

        self.assertEqual(events, ["compact", "turn:follow-up prompt"])

    def test_service_backed_runner_uses_service_turn_path(self) -> None:
        captured: list[tuple[object, str, bool, bool]] = []

        class _DoneHandle:
            def __init__(self, result) -> None:
                self.turn_id = "turn-1"
                self.result = result

            def is_done(self) -> bool:
                return True

            def drain_events(self, *, block: bool = False, timeout: float | None = None):
                return []

        service = SimpleNamespace(
            run_turn=lambda session, query, **kwargs: captured.append(
                (
                    session,
                    query,
                    callable(kwargs.get("take_next_loop_user_message")),
                    callable(kwargs.get("prepare_next_loop_user_message")),
                )
            )
            or _DoneHandle(SimpleNamespace(status="completed", text="Done.", interrupted=False)),
        )
        runtime = SimpleNamespace(
            compact_session=lambda session: None,
            run_turn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runtime.run_turn should not be used")),
            print_last_turn_file_summary=lambda session: False,
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True, service=service)

        with patch("builtins.print"):
            runner.start()
            runner.enqueue("service prompt")
            runner.close(drain=True)

        self.assertEqual(captured, [(runner.session, "service prompt", True, True)])

    def test_service_event_flushes_stream_before_tool_output(self) -> None:
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

        runner = TurnQueueRunner(SimpleNamespace(), SimpleNamespace(todo_items=[]), stable_prompt=True)
        streamer = ConsoleStreamer(start_on_new_line=True)
        fake_stdout = _StdoutCapture()

        with patch("sys.stdout", fake_stdout):
            runner._process_service_event(
                SimpleNamespace(type="assistant_delta", payload={"delta": "Preparing update."}),
                streamer,
            )
            runner._process_service_event(
                SimpleNamespace(
                    type="tool_finished",
                    payload={
                        "actor": "lead",
                        "tool_name": "edit_file",
                        "rendered_lines": ["TOOL: Update(file.py)"],
                    },
                ),
                streamer,
            )

        output = fake_stdout.getvalue()
        self.assertLess(output.index("Preparing update."), output.index("TOOL: Update(file.py)"))

    def test_expand_skill_command_wraps_loaded_skill_and_user_request(self) -> None:
        runtime = SimpleNamespace(
            skill_loader=SimpleNamespace(load=lambda name: f"<skill name=\"{name}\">body</skill>"),
        )

        expanded = _expand_skill_command(runtime, "/+unity inspect this folder")

        self.assertIn("<skill name=\"unity\">body</skill>", expanded)
        self.assertIn("The user explicitly requested skill 'unity'.", expanded)
        self.assertTrue(expanded.endswith("inspect this folder"))

    def test_skills_command_returns_selected_skill_prefix(self) -> None:
        runtime = SimpleNamespace(
            skill_loader=SimpleNamespace(
                list_entries=lambda: [
                    {
                        "name": "Review",
                        "description": "review code",
                        "path": "D:/skills/Review/SKILL.md",
                        "scope": "workspace",
                    }
                ]
            )
        )

        with patch("open_somnia.cli.repl.choose_item_interactively", return_value="Review"):
            prefix = _handle_skills_command(runtime)

        self.assertEqual(prefix, "/+Review ")

    def test_skills_command_prints_no_skills_when_empty(self) -> None:
        runtime = SimpleNamespace(skill_loader=SimpleNamespace(list_entries=lambda: []))

        with patch("builtins.print") as mock_print:
            prefix = _handle_skills_command(runtime)

        self.assertIsNone(prefix)
        mock_print.assert_called_once_with("No skills.")

    def test_request_authorization_is_resolved_on_main_thread(self) -> None:
        runtime = SimpleNamespace(settings=SimpleNamespace(provider=SimpleNamespace(name="anthropic", model="glm-5")))
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)
        result: dict[str, dict[str, str]] = {}

        worker = Thread(
            target=lambda: result.setdefault(
                "value",
                runner.request_authorization(
                    tool_name="bash",
                    reason="Need to inspect git state",
                    argument_summary="git status",
                    execution_mode="accept_edits",
                ),
            )
        )
        worker.start()

        with patch("open_somnia.cli.repl.choose_authorization_interactively", return_value="once"):
            for _ in range(50):
                if _resolve_authorization_requests(runner):
                    break
                time.sleep(0.01)

        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result["value"]["status"], "approved")
        self.assertEqual(result["value"]["scope"], "once")

    def test_service_authorization_request_is_resolved_through_app_service(self) -> None:
        resolved: list[tuple[str, str, bool, str]] = []
        service = SimpleNamespace(
            resolve_authorization=lambda request_id, *, scope, approved=True, reason="": resolved.append(
                (request_id, scope, approved, reason)
            )
            or True
        )
        runtime = SimpleNamespace(settings=SimpleNamespace(provider=SimpleNamespace(name="anthropic", model="glm-5")))
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True, service=service)
        runner._authorization_requests.append(
            AuthorizationRequest(
                tool_name="bash",
                reason="Need git status",
                argument_summary="git status",
                execution_mode="accept_edits",
                completed=Event(),
                request_id="auth-1",
            )
        )

        with patch("open_somnia.cli.repl.choose_authorization_interactively", return_value="workspace"):
            handled = _resolve_authorization_requests(runner)

        self.assertTrue(handled)
        self.assertEqual(resolved, [("auth-1", "workspace", True, "Allowed in this workspace.")])

    def test_request_mode_switch_is_resolved_on_main_thread(self) -> None:
        runtime = SimpleNamespace(settings=SimpleNamespace(provider=SimpleNamespace(name="anthropic", model="glm-5")))
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)
        result: dict[str, dict[str, str]] = {}

        worker = Thread(
            target=lambda: result.setdefault(
                "value",
                runner.request_mode_switch(
                    target_mode="accept_edits",
                    reason="Plan is complete",
                    current_mode="plan",
                ),
            )
        )
        worker.start()

        with patch("open_somnia.cli.repl.choose_mode_switch_interactively", return_value="switch"):
            for _ in range(50):
                if _resolve_mode_switch_requests(runner):
                    break
                time.sleep(0.01)

        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(result["value"]["approved"])
        self.assertEqual(result["value"]["active_mode"], "accept_edits")
        self.assertEqual(runtime.execution_mode, "accept_edits")

    def test_service_mode_switch_request_is_resolved_through_app_service(self) -> None:
        resolved: list[tuple[str, bool, str, str]] = []
        service = SimpleNamespace(
            resolve_mode_switch=lambda request_id, *, approved, active_mode=None, reason="": resolved.append(
                (request_id, approved, active_mode, reason)
            )
            or True
        )
        runtime = SimpleNamespace(
            settings=SimpleNamespace(provider=SimpleNamespace(name="anthropic", model="glm-5")),
            execution_mode="plan",
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True, service=service)
        runner._mode_switch_requests.append(
            ModeSwitchRequest(
                target_mode="accept_edits",
                current_mode="plan",
                reason="Ready to implement",
                completed=Event(),
                request_id="mode-1",
            )
        )

        with patch("open_somnia.cli.repl.choose_mode_switch_interactively", return_value="switch"):
            handled = _resolve_mode_switch_requests(runner)

        self.assertTrue(handled)
        self.assertEqual(resolved[0][:3], ("mode-1", True, "accept_edits"))
        self.assertIn("accept edits on", resolved[0][3])

    def test_undo_command_confirms_before_running(self) -> None:
        runtime = SimpleNamespace(undo_last_turn=lambda session: "undid last change set")
        session = SimpleNamespace(undo_stack=[{"turn_id": "turn-1"}])

        with patch("open_somnia.cli.repl.choose_item_interactively", return_value="confirm"), patch(
            "builtins.print"
        ) as mock_print:
            _handle_undo_command(runtime, session)

        mock_print.assert_called_with("undid last change set")

    def test_undo_command_cancels_by_default_without_action(self) -> None:
        runtime = SimpleNamespace(undo_last_turn=lambda session: "should not run")
        session = SimpleNamespace(undo_stack=[{"turn_id": "turn-1"}])

        with patch("open_somnia.cli.repl.choose_item_interactively", return_value="cancel"), patch(
            "builtins.print"
        ) as mock_print:
            _handle_undo_command(runtime, session)

        mock_print.assert_not_called()

    def test_mutating_command_requires_accept_edits_mode(self) -> None:
        runtime = SimpleNamespace(execution_mode="plan")
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        with patch("open_somnia.cli.repl.choose_mode_switch_interactively", return_value="stay"), patch(
            "builtins.print"
        ) as mock_print:
            allowed = _ensure_accept_edits_for_command(
                runner,
                "/rollback",
                "Rollback reverts workspace files and restores session state.",
            )

        self.assertFalse(allowed)
        self.assertEqual(runner.current_execution_mode().key, "plan")
        self.assertIn("/rollback requires", mock_print.call_args[0][0])

    def test_mutating_command_can_switch_into_accept_edits_mode(self) -> None:
        runtime = SimpleNamespace(execution_mode="shortcuts")
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        with patch("open_somnia.cli.repl.choose_mode_switch_interactively", return_value="switch"):
            allowed = _ensure_accept_edits_for_command(
                runner,
                "/checkpoint",
                "Saving a checkpoint updates the persisted session state.",
            )

        self.assertTrue(allowed)
        self.assertEqual(runner.current_execution_mode().key, "accept_edits")

    def test_run_repl_falls_back_when_prompt_toolkit_prompt_fails(self) -> None:
        class _BrokenPromptSession:
            app = SimpleNamespace(invalidate=lambda: None, exit=lambda result=None: None)

            def prompt(self, *args, **kwargs):
                raise OSError(10055, "buffer space unavailable")

        runtime = SimpleNamespace(
            settings=SimpleNamespace(
                workspace_root=Path.cwd(),
                provider=SimpleNamespace(name="anthropic", model="glm-5"),
            ),
            execution_mode="accept_edits",
        )
        session = SimpleNamespace(id="session-1", todo_items=[])
        stderr = io.StringIO()

        with patch("open_somnia.cli.repl.create_prompt_session", return_value=_BrokenPromptSession()):
            with patch("open_somnia.cli.repl.patch_stdout", None):
                with patch("builtins.input", return_value="/exit") as mock_input:
                    with patch("sys.stderr", stderr):
                        exit_code = run_repl(runtime, session)

        self.assertEqual(exit_code, 0)
        self.assertIn("prompt unavailable; falling back to basic input", stderr.getvalue())
        self.assertIn("accept edits on", mock_input.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
