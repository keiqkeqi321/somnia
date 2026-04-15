from __future__ import annotations

import time
import unittest
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch

from open_somnia.cli.prompting import PROMPT_BORDER
from open_somnia.cli.repl import (
    TurnQueueRunner,
    _ensure_accept_edits_for_command,
    _expand_skill_command,
    _handle_scan_command,
    _handle_symbols_command,
    _is_exit_command,
    _handle_mcp_command,
    _handle_model_command,
    _handle_providers_command,
    _handle_skills_command,
    _handle_undo_command,
    _resolve_authorization_requests,
    _resolve_mode_switch_requests,
)
from open_somnia.runtime.compact import ContextWindowUsage
from open_somnia.tools.todo import TodoManager


def _render_prompt_text(fragments) -> str:
    return "".join(text for _, text, *rest in fragments)


class ReplTodoTests(unittest.TestCase):
    def test_is_exit_command_requires_explicit_exit_text(self) -> None:
        self.assertFalse(_is_exit_command(""))
        self.assertFalse(_is_exit_command("   "))
        self.assertFalse(_is_exit_command("/compact"))
        self.assertTrue(_is_exit_command("q"))
        self.assertTrue(_is_exit_command(" exit "))
        self.assertTrue(_is_exit_command("/exit"))

    def test_current_model_label_uses_active_provider_and_model(self) -> None:
        runtime = SimpleNamespace(settings=SimpleNamespace(provider=SimpleNamespace(name="anthropic", model="glm-5")))
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        self.assertEqual(runner.current_model_label(), "model: anthropic / glm-5")
        self.assertIn("accept edits on", runner.execution_mode_label())

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
                ("fg:#94a3b8", "model: openai / gpt-5"),
                ("fg:#64748b", " | "),
                ("fg:#22c55e", "ctx: 20.0% (40.0k / 200.0k tokens)"),
            ],
        )

    def test_bottom_toolbar_shows_token_sum_when_session_has_usage(self) -> None:
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
                ("fg:#94a3b8", "model: openai / gpt-5"),
                ("fg:#64748b", " | "),
                ("fg:#22c55e", "ctx: 20.0% (40.0k / 200.0k tokens)"),
                ("fg:#64748b", " | "),
                ("fg:#7dd3fc", "sum: 12.3k"),
            ],
        )

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
                ("fg:#94a3b8", "model: openai / gpt-5"),
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
                ("fg:#94a3b8", "model: openai / gpt-5"),
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
                    }
                ],
                _format_member_summary=lambda member: "Analyst (algorithm analyst): working [tool grep] View team logs: /teamlog Analyst",
            )
        )
        runner = TurnQueueRunner(runtime, SimpleNamespace(todo_items=[]), stable_prompt=True)

        rendered = _render_prompt_text(runner.prompt_message())

        self.assertIn("team (1 active)", rendered)
        self.assertIn("View team logs: /teamlog Analyst", rendered)
        self.assertLess(rendered.index("team (1 active)"), rendered.index("accept edits on  (Shift+Tab to cycle)"))

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

    def test_scan_command_scans_current_target_path(self) -> None:
        invoked: list[dict[str, object]] = []

        def _invoke_tool(session, name, payload):
            invoked.append(payload)
            return "Project root: .\nCounts: 2 files, 1 dirs"

        runtime = SimpleNamespace(
            invoke_tool=_invoke_tool,
        )
        session = SimpleNamespace()

        with patch("builtins.print") as mock_print:
            _handle_scan_command(runtime, session, "/scan src")

        self.assertEqual(invoked, [{"path": "src", "depth": 2, "limit": 8}])
        mock_print.assert_any_call("Project root: .\nCounts: 2 files, 1 dirs")

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


if __name__ == "__main__":
    unittest.main()
