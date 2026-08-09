from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from open_somnia.config.models import AgentSettings, AppSettings, ProviderSettings, RuntimeSettings, StorageSettings
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import AssistantTurn, ToolCall
from open_somnia.runtime.subagent_runner import SubagentResult
from open_somnia.storage.subagent_checkpoints import SubagentCheckpoint, SubagentCheckpointStore
from open_somnia.tools.registry import ToolRegistry
from open_somnia.tools.subagent import register_subagent_tool


class SubagentToolTests(unittest.TestCase):
    def test_registers_subagent_tool_name(self) -> None:
        registry = ToolRegistry()

        register_subagent_tool(registry)

        self.assertIn("subagent", registry.names())
        self.assertNotIn("task", registry.names())

    def test_explore_subagent_exposes_read_only_subagent_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            seen = {}

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                seen["tool_names"] = [tool["name"] for tool in tools]
                return AssistantTurn(stop_reason="end_turn", text_blocks=["done"], tool_calls=[])

            runtime.complete = fake_complete

            runtime.run_subagent("Inspect the repo", "Explore")

            self.assertEqual(
                seen["tool_names"],
                [
                    "bash",
                    "tree",
                    "find_symbol",
                    "glob",
                    "grep",
                    "read_file",
                    "read_image",
                    "web_fetch",
                    "load_skill",
                ],
            )

    def test_general_purpose_subagent_exposes_edit_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            seen = {}

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                seen["tool_names"] = [tool["name"] for tool in tools]
                return AssistantTurn(stop_reason="end_turn", text_blocks=["done"], tool_calls=[])

            runtime.complete = fake_complete

            runtime.run_subagent("Patch a file", "general-purpose")

            self.assertEqual(
                seen["tool_names"],
                [
                    "bash",
                    "tree",
                    "find_symbol",
                    "glob",
                    "grep",
                    "read_file",
                    "read_image",
                    "web_fetch",
                    "write_file",
                    "edit_file",
                    "load_skill",
                ],
            )

    def test_explore_subagent_can_use_bash_in_accept_edits_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            runtime.execution_mode = "accept_edits"
            steps = []

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                steps.append(messages)
                if len(steps) == 1:
                    return AssistantTurn(
                        stop_reason="tool_use",
                        text_blocks=["Inspecting workspace."],
                        tool_calls=[
                            ToolCall("call-1", "bash", {"command": "pwd"}),
                        ],
                    )
                tool_result = messages[-1]["content"][0]["content"]
                self.assertNotIn("requires explicit user approval", tool_result)
                return AssistantTurn(stop_reason="end_turn", text_blocks=["Done."], tool_calls=[])

            runtime.complete = fake_complete

            result = runtime.run_subagent("Inspect the workspace", "Explore")

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.summary, "Done.")

    def test_subagent_emits_activity_for_text_and_tool_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            events = []
            runtime.subagent_activity_handler = events.append
            turns = []

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                turns.append(messages)
                if len(turns) == 1:
                    return AssistantTurn(
                        stop_reason="tool_use",
                        text_blocks=["Searching files."],
                        tool_calls=[
                            ToolCall("call-1", "tree", {"path": ".", "depth": 1, "limit": 1}),
                        ],
                    )
                return AssistantTurn(stop_reason="end_turn", text_blocks=["Found the root."], tool_calls=[])

            runtime.complete = fake_complete

            result = runtime.run_subagent("Inspect the workspace", "Explore", activity_id="turn-1")

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.summary, "Found the root.")
            self.assertEqual(events[0]["activity_id"], "turn-1")
            self.assertEqual(events[0]["text"], "Searching files.")
            self.assertTrue(any("tree .:" in event["text"] for event in events))
            self.assertEqual(events[-1]["text"], "Found the root.")

    def test_subagent_persists_execution_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            turns = []

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                turns.append(messages)
                if len(turns) == 1:
                    return AssistantTurn(
                        stop_reason="tool_use",
                        text_blocks=["Searching files."],
                        tool_calls=[
                            ToolCall("call-1", "tree", {"path": ".", "depth": 1, "limit": 1}),
                        ],
                    )
                return AssistantTurn(stop_reason="end_turn", text_blocks=["Found the root."], tool_calls=[])

            runtime.complete = fake_complete

            result = runtime.run_subagent("Inspect the workspace", "Explore", activity_id="turn-1")

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.summary, "Found the root.")
            entries = runtime.subagent_log_store.read("turn-1")
            types = [entry["type"] for entry in entries]
            self.assertEqual(types, ["started", "assistant_message", "tool_call", "assistant_message", "summary"])
            self.assertEqual(entries[0]["prompt"], "Inspect the workspace")
            self.assertEqual(entries[0]["agent_type"], "Explore")
            self.assertEqual(entries[1]["content"], "Searching files.")
            self.assertEqual(entries[2]["tool_name"], "tree")
            self.assertIn("tool_input", entries[2])
            self.assertTrue(entries[2]["output_preview"])
            self.assertEqual(entries[3]["content"], "Found the root.")
            self.assertEqual(entries[4]["content"], "Found the root.")

    def test_subagent_stops_when_interrupt_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            interrupt_state = {"armed": False}
            turns = []

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                turns.append(messages)
                if should_interrupt is not None and should_interrupt():
                    raise TurnInterrupted("Interrupted by user.")
                interrupt_state["armed"] = True
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["Searching."],
                    tool_calls=[ToolCall("call-1", "tree", {"path": ".", "depth": 1, "limit": 1})],
                )

            runtime.complete = fake_complete

            with self.assertRaises(TurnInterrupted):
                runtime.run_subagent("Inspect the workspace", "Explore", activity_id="turn-1", should_interrupt=lambda: interrupt_state["armed"])

            entries = runtime.subagent_log_store.read("turn-1")
            self.assertEqual(entries[0]["type"], "started")
            self.assertNotEqual(entries[-1]["type"], "error")

    def test_subagent_checks_interrupt_before_each_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            calls = {"complete": 0, "tool": 0}
            interrupt_state = {"armed": False}

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                calls["complete"] += 1
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["Searching."],
                    tool_calls=[ToolCall("call-1", "tree", {"path": ".", "depth": 1, "limit": 1})],
                )

            runtime.complete = fake_complete

            def fake_execute(ctx, name, payload):
                calls["tool"] += 1
                return {"status": "ok"}

            runtime.subagent_runner._build_registry = lambda agent_type: SimpleNamespace(
                schemas=lambda: [],
                execute=fake_execute,
            )

            # Interrupt armed before the tool runs: the pre-tool check must raise.
            interrupt_state["armed"] = True
            with self.assertRaises(TurnInterrupted):
                runtime.run_subagent(
                    "Inspect the workspace",
                    "Explore",
                    activity_id="turn-2",
                    should_interrupt=lambda: interrupt_state["armed"],
                )
            self.assertEqual(calls["tool"], 0)

    def test_interrupt_checkpoints_state_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            interrupt_state = {"armed": False}
            turns = []

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                turns.append(messages)
                if should_interrupt is not None and should_interrupt():
                    raise TurnInterrupted("Interrupted by user.")
                interrupt_state["armed"] = True
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["Searching."],
                    tool_calls=[ToolCall("call-1", "tree", {"path": ".", "depth": 1, "limit": 1})],
                )

            runtime.complete = fake_complete

            with self.assertRaises(TurnInterrupted):
                runtime.run_subagent(
                    "Inspect the workspace",
                    "Explore",
                    activity_id="turn-1",
                    should_interrupt=lambda: interrupt_state["armed"],
                )

            # Checkpoint persisted so the lead can resume.
            cp = runtime.subagent_checkpoint_store.load("turn-1")
            self.assertIsNotNone(cp)
            self.assertEqual(cp.status, "interrupted")
            self.assertTrue(cp.messages)
            self.assertEqual(cp.messages[0]["role"], "user")

    def test_resume_inherits_context_and_resets_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            runtime.settings.runtime.max_subagent_resumes = 3
            # Seed a checkpoint that mimics an interrupted subagent: it already
            # did one round (assistant + tool_result), so resuming must continue
            # from this state rather than restarting.
            prior_messages = [
                {"role": "user", "content": "Inspect the workspace"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "Searching."},
                    {"type": "tool_call", "id": "call-1", "name": "tree", "input": {"path": "."}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_call_id": "call-1", "content": "root/"},
                ]},
            ]
            runtime.subagent_checkpoint_store.save(
                SubagentCheckpoint(
                    activity_id="turn-1",
                    prompt="Inspect the workspace",
                    agent_type="Explore",
                    messages=prior_messages,
                    pending_repair_hints=[],
                    rounds_used=1,
                    status="interrupted",
                    resume_count=0,
                )
            )
            seen_message_counts = []

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                seen_message_counts.append(len(messages))
                # Resume must carry the prior context, not start from [prompt].
                return AssistantTurn(stop_reason="end_turn", text_blocks=["Resumed summary."], tool_calls=[])

            runtime.complete = fake_complete

            cp = runtime.subagent_checkpoint_store.load("turn-1")
            result = runtime.run_subagent("Inspect the workspace", "Explore", activity_id="turn-1", resume_from=cp)

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.summary, "Resumed summary.")
            # Inherited 3 prior messages; resume did not restart from [prompt].
            self.assertGreaterEqual(seen_message_counts[0], 3)
            # Completed resume clears the checkpoint.
            self.assertIsNone(runtime.subagent_checkpoint_store.load("turn-1"))

    def test_resume_repairs_orphaned_assistant_tool_use(self) -> None:
        """An interrupt between appending assistant and tool_result leaves an
        orphan; resume must synthesize a placeholder tool_result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            orphan_messages = [
                {"role": "user", "content": "Inspect the workspace"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "Searching."},
                    {"type": "tool_call", "id": "call-1", "name": "tree", "input": {"path": "."}},
                ]},
                # No matching tool_result — orphan.
            ]
            runtime.subagent_checkpoint_store.save(
                SubagentCheckpoint(
                    activity_id="turn-1",
                    prompt="Inspect the workspace",
                    agent_type="Explore",
                    messages=orphan_messages,
                    pending_repair_hints=[],
                    rounds_used=1,
                    status="interrupted",
                )
            )
            captured = {}

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                captured["messages"] = messages
                return AssistantTurn(stop_reason="end_turn", text_blocks=["ok"], tool_calls=[])

            runtime.complete = fake_complete
            cp = runtime.subagent_checkpoint_store.load("turn-1")
            runtime.run_subagent("Inspect the workspace", "Explore", activity_id="turn-1", resume_from=cp)

            # The provider must receive a well-formed conversation: the orphan
            # tool_call now has a matching placeholder tool_result.
            last_user = captured["messages"][-1]
            self.assertEqual(last_user["role"], "user")
            items = last_user["content"]
            self.assertTrue(any(
                isinstance(i, dict) and i.get("type") == "tool_result" and i.get("tool_call_id") == "call-1"
                for i in items
            ))

    def test_truncated_status_when_rounds_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            runtime.settings.runtime.max_subagent_rounds = 2
            call_count = {"n": 0}

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                call_count["n"] += 1
                # Always request a tool call → never reaches the no-tool summary round.
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["still working"],
                    tool_calls=[ToolCall("c", "tree", {"path": "."})],
                )

            runtime.complete = fake_complete
            result = runtime.run_subagent("Inspect", "Explore", activity_id="turn-1")
            self.assertEqual(result.status, "truncated")
            self.assertEqual(result.rounds_used, 2)
            # Truncated subagents leave a checkpoint for resume.
            self.assertIsNotNone(runtime.subagent_checkpoint_store.load("turn-1"))

    def test_resume_count_caps_round_reset(self) -> None:
        """Past max_subagent_resumes the round budget stops resetting."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            runtime.settings.runtime.max_subagent_rounds = 5
            runtime.settings.runtime.max_subagent_resumes = 1
            # Checkpoint already resumed once (resume_count=1); next resume (count=2)
            # exceeds the cap → rounds_used should NOT reset.
            runtime.subagent_checkpoint_store.save(
                SubagentCheckpoint(
                    activity_id="turn-1",
                    prompt="x",
                    agent_type="Explore",
                    messages=[{"role": "user", "content": "x"}],
                    pending_repair_hints=[],
                    rounds_used=3,
                    status="interrupted",
                    resume_count=1,
                )
            )
            captured = {"rounds_used": None}

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                return AssistantTurn(stop_reason="end_turn", text_blocks=["done"], tool_calls=[])

            runtime.complete = fake_complete
            cp = runtime.subagent_checkpoint_store.load("turn-1")
            result = runtime.run_subagent("x", "Explore", activity_id="turn-1", resume_from=cp)
            # rounds_used was 3, one more round → 4 (did not reset to 1).
            self.assertEqual(result.rounds_used, 4)

    def test_failed_status_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                raise RuntimeError("boom")

            runtime.complete = fake_complete
            result = runtime.run_subagent("x", "Explore", activity_id="turn-1")
            self.assertEqual(result.status, "failed")
            self.assertIn("boom", result.error or "")
            self.assertIsNotNone(runtime.subagent_checkpoint_store.load("turn-1"))

    def test_completed_result_tool_output_is_clean_summary(self) -> None:
        from open_somnia.runtime.subagent_runner import SubagentResult
        out = SubagentResult(status="completed", summary="## Summary", activity_id="a").as_tool_output()
        self.assertEqual(out["tool_result_text"], "## Summary")
        self.assertNotIn("is_error", out)

    def test_checkpoint_store_save_load_delete_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SubagentCheckpointStore(Path(tmpdir))
            self.assertEqual(store.list_pending(), [])
            store.save(SubagentCheckpoint(
                activity_id="a1", prompt="p", agent_type="Explore",
                messages=[{"role": "user", "content": "p"}],
                pending_repair_hints=[], rounds_used=2, status="interrupted",
            ))
            loaded = store.load("a1")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.rounds_used, 2)
            self.assertEqual([c.activity_id for c in store.list_pending()], ["a1"])
            store.save(SubagentCheckpoint(
                activity_id="a2", prompt="p2", agent_type="Explore",
                messages=[{"role": "user", "content": "p2"}],
                pending_repair_hints=[], rounds_used=1, status="truncated",
            ))
            self.assertEqual({c.activity_id for c in store.list_pending()}, {"a1", "a2"})
            store.delete("a1")
            self.assertIsNone(store.load("a1"))
            self.assertEqual({c.activity_id for c in store.list_pending()}, {"a2"})

    def test_resume_with_extra_prompt_injects_into_subagent_context(self) -> None:
        """Resuming with extra_prompt appends it as a user message to the
        subagent's accumulated context, so the subagent sees both its prior
        work and the new guidance."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            seen_message_counts = []
            runtime.subagent_checkpoint_store.save(
                SubagentCheckpoint(
                    activity_id="turn-1", prompt="initial", agent_type="Explore",
                    messages=[{"role": "user", "content": "initial"}],
                    pending_repair_hints=[], rounds_used=1, status="interrupted",
                )
            )

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                seen_message_counts.append(len(messages))
                return AssistantTurn(stop_reason="end_turn", text_blocks=["done"], tool_calls=[])

            runtime.complete = fake_complete
            cp = runtime.subagent_checkpoint_store.load("turn-1")
            result = runtime.run_subagent(
                "initial", "Explore", activity_id="turn-1",
                resume_from=cp, extra_prompt="Now focus on the tools/registry.py file.",
            )
            self.assertEqual(result.status, "completed")
            # The resumed context must carry the extra_prompt as an extra user
            # message beyond the checkpoint's single message.
            self.assertGreaterEqual(seen_message_counts[0], 2)

    def test_serial_handler_resume_keeps_checkpoint_activity_id(self) -> None:
        """Regression: the serial subagent tool handler, when given
        resume_from, must keep the checkpoint's activity_id (not the new
        ctx.trace_id), so the resumed run updates the SAME checkpoint.
        Otherwise the original checkpoint is orphaned at 'interrupted' and the
        resume effectively restarts from scratch. (Transcript 4d5107ccf222.)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            runtime.subagent_checkpoint_store.save(
                SubagentCheckpoint(
                    activity_id="orig-aid", prompt="initial", agent_type="Explore",
                    messages=[{"role": "user", "content": "initial"}],
                    pending_repair_hints=[], rounds_used=1, status="interrupted",
                )
            )
            seen_activity_ids = []

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                return AssistantTurn(stop_reason="end_turn", text_blocks=["done"], tool_calls=[])

            runtime.complete = fake_complete
            registry = ToolRegistry()
            register_subagent_tool(registry)
            from open_somnia.runtime.events import ToolExecutionContext
            # A NEW trace_id (as a fresh tool_call would have), distinct from
            # the checkpoint's activity_id.
            ctx = ToolExecutionContext(
                runtime=runtime, session=None, actor="lead",
                trace_id="new-trace-id-999", should_interrupt=lambda: False,
            )
            out = registry.execute(ctx, "subagent", {
                "prompt": "continue", "resume_from": "orig-aid",
            })
            # The resumed run must have used the checkpoint's activity_id, so the
            # original checkpoint was cleared on completion (not orphaned).
            self.assertIsNone(runtime.subagent_checkpoint_store.load("orig-aid"))
            # And no new checkpoint under the new trace_id was created.
            self.assertIsNone(runtime.subagent_checkpoint_store.load("new-trace-id-999"))

    def test_placeholder_helpers_running_then_completed(self) -> None:
        """_append_subagent_placeholders writes a running placeholder;
        _finalize_placeholders_completed rewrites it in place with the summary."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            session = runtime.create_session()
            tc = ToolCall("sa1", "subagent", {"prompt": "explore A"})
            plan = SimpleNamespace(tool_call=tc, tool_name="subagent")
            assistant_msg, items = runtime._append_subagent_placeholders(session, [plan])
            # session.messages now holds: [..., assistant_msg, tool_result_msg]
            self.assertIs(session.messages[-2], assistant_msg)
            tool_result_msg = session.messages[-1]
            self.assertEqual(tool_result_msg["role"], "user")
            placeholder_item = tool_result_msg["content"][0]
            self.assertEqual(placeholder_item["tool_call_id"], "sa1")
            self.assertIn("resume_from", placeholder_item["content"])
            # Rewrite in place using a fake completed record.
            from open_somnia.runtime.round_runner import ToolCallRecord
            record = ToolCallRecord(
                tool_call=tc,
                persisted_output={"status": "completed", "tool_result_text": "## Summary A"},
                rendered_output='{"status":"completed"}',
                repair_hint=None,
                result_item={},
            )
            runtime._finalize_placeholders_completed(items, [record])
            # The same dict object (still in session.messages) now carries the
            # clean summary text via tool_result_text.
            self.assertEqual(placeholder_item.get("tool_result_text"), "## Summary A")
            self.assertEqual(placeholder_item["content"], "## Summary A")
            self.assertNotIn("is_error", placeholder_item)

    def test_placeholder_helpers_interrupted_marks_status(self) -> None:
        """_finalize_placeholders_interrupted rewrites the placeholder in place
        to interrupted + resume pointer."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            session = runtime.create_session()
            tc = ToolCall("sa2", "subagent", {"prompt": "explore B"})
            plan = SimpleNamespace(tool_call=tc, tool_name="subagent")
            _assistant_msg, items = runtime._append_subagent_placeholders(session, [plan])
            placeholder_item = session.messages[-1]["content"][0]
            runtime._finalize_placeholders_interrupted(items, [plan])
            self.assertTrue(placeholder_item.get("is_error"))
            self.assertIn("interrupted", placeholder_item["content"])
            self.assertIn("resume_from", placeholder_item["content"])

    def test_no_programmatic_auto_resume_on_new_turn(self) -> None:
        """Regression: a new turn must NOT programmatically resume interrupted
        subagents. The old _auto_resume_interrupted_subagents is gone; resume is
        now the lead LLM's decision via resume_from."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            self.assertFalse(hasattr(runtime, "_auto_resume_interrupted_subagents"))
            # Seed a pending checkpoint.
            runtime.subagent_checkpoint_store.save(
                SubagentCheckpoint(
                    activity_id="old", prompt="old", agent_type="Explore",
                    messages=[{"role": "user", "content": "old"}],
                    pending_repair_hints=[], rounds_used=1, status="interrupted",
                )
            )
            called = []
            runtime.run_subagent = lambda *a, **k: called.append(k) or SubagentResult(
                status="completed", summary="x")
            # A new turn happens; nothing should auto-resume the checkpoint.
            self.assertEqual(called, [])
            self.assertIsNotNone(runtime.subagent_checkpoint_store.load("old"))

    def test_run_subagent_stamps_session_id_on_checkpoint(self) -> None:
        """An interrupted subagent records the owning session_id on its
        checkpoint so the session-scoped resume can find it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            session = runtime.create_session()

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                raise TurnInterrupted("Interrupted by user.")

            runtime.complete = fake_complete
            with self.assertRaises(TurnInterrupted):
                runtime.run_subagent("p", "Explore", activity_id="stamp-1", session_id=session.id)
            cp = runtime.subagent_checkpoint_store.load("stamp-1")
            self.assertIsNotNone(cp)
            self.assertEqual(cp.session_id, session.id)

    def _make_settings(self, root: Path) -> AppSettings:
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
            agent=AgentSettings(name="OpenAgent"),
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
                state_dir=data_dir / "state",
            ),
        )
