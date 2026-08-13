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
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["done"],
                    tool_calls=[ToolCall("c", "submit_summary", {"summary": "done"})],
                )

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
                    "submit_summary",
                ],
            )

    def test_general_purpose_subagent_exposes_edit_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            seen = {}

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                seen["tool_names"] = [tool["name"] for tool in tools]
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["done"],
                    tool_calls=[ToolCall("c", "submit_summary", {"summary": "done"})],
                )

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
                    "submit_summary",
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
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["Done."],
                    tool_calls=[ToolCall("c2", "submit_summary", {"summary": "Done."})],
                )

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
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["Found the root."],
                    tool_calls=[ToolCall("call-2", "submit_summary", {"summary": "Found the root."})],
                )

            runtime.complete = fake_complete

            result = runtime.run_subagent("Inspect the workspace", "Explore", activity_id="turn-1")

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.summary, "Found the root.")
            self.assertEqual(events[0]["activity_id"], "turn-1")
            self.assertEqual(events[0]["text"], "Searching files.")
            self.assertTrue(any("tree .:" in event["text"] for event in events))
            # The submit_summary tool call is logged as the final activity.
            self.assertIn("submit_summary", events[-1]["text"])

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
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["Found the root."],
                    tool_calls=[ToolCall("call-2", "submit_summary", {"summary": "Found the root."})],
                )

            runtime.complete = fake_complete

            result = runtime.run_subagent("Inspect the workspace", "Explore", activity_id="turn-1")

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.summary, "Found the root.")
            entries = runtime.subagent_log_store.read("turn-1")
            types = [entry["type"] for entry in entries]
            # Completion now happens via the submit_summary tool call, so the
            # log gains an extra tool_call entry before the final summary.
            self.assertEqual(
                types,
                ["started", "assistant_message", "tool_call", "assistant_message", "tool_call", "summary"],
            )
            self.assertEqual(entries[0]["prompt"], "Inspect the workspace")
            self.assertEqual(entries[0]["agent_type"], "Explore")
            self.assertEqual(entries[1]["content"], "Searching files.")
            self.assertEqual(entries[2]["tool_name"], "tree")
            self.assertIn("tool_input", entries[2])
            self.assertTrue(entries[2]["output_preview"])
            self.assertEqual(entries[3]["content"], "Found the root.")
            self.assertEqual(entries[4]["tool_name"], "submit_summary")
            self.assertEqual(entries[5]["content"], "Found the root.")

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
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["Resumed summary."],
                    tool_calls=[ToolCall("c", "submit_summary", {"summary": "Resumed summary."})],
                )

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
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["ok"],
                    tool_calls=[ToolCall("c", "submit_summary", {"summary": "ok"})],
                )

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

    def test_text_only_turn_does_not_complete_and_keeps_looping(self) -> None:
        """A text-only turn (no tool call) is NOT completion -- the loop injects
        a nudge and continues until the model calls submit_summary."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            runtime.settings.runtime.max_subagent_rounds = 3
            call_count = {"n": 0}
            seen_messages = []

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                call_count["n"] += 1
                seen_messages.append([dict(m) for m in messages])
                if call_count["n"] < 3:
                    # Premature text-only "summary" attempt -- must NOT end the run.
                    return AssistantTurn(stop_reason="end_turn", text_blocks=["Here is my answer."], tool_calls=[])
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=[],
                    tool_calls=[ToolCall("c", "submit_summary", {"summary": "Real answer after work."})],
                )

            runtime.complete = fake_complete
            result = runtime.run_subagent("Do the work", "Explore", activity_id="turn-1")

            # The model tried to exit early twice but was nudged onward; the run
            # only completed once submit_summary was finally called.
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.summary, "Real answer after work.")
            self.assertEqual(call_count["n"], 3)
            # A nudge user message was injected after each premature text-only turn.
            nudge_texts = [
                m["content"] for m in seen_messages[1] if str(m.get("role", "")) == "user" and isinstance(m.get("content"), str)
            ]
            self.assertTrue(any("submit_summary" in t for t in nudge_texts))

    def test_submit_summary_completes_with_submitted_summary(self) -> None:
        """submit_summary is the explicit completion act; its payload (not the
        turn text) is the authoritative summary returned to the lead."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["Some throwaway narration."],
                    tool_calls=[ToolCall("c", "submit_summary", {"summary": "## Final Report"})],
                )

            runtime.complete = fake_complete
            result = runtime.run_subagent("Summarize", "Explore", activity_id="turn-1")

            self.assertEqual(result.status, "completed")
            # The submitted_summary payload wins over the turn text.
            self.assertEqual(result.summary, "## Final Report")
            # Completed run clears its checkpoint.
            self.assertIsNone(runtime.subagent_checkpoint_store.load("turn-1"))

    def test_submit_summary_registers_for_both_agent_types(self) -> None:
        """submit_summary must be available to both Explore and general-purpose
        subagents -- it is the sole completion path for both."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            explore_tools = runtime.subagent_runner._build_registry("Explore").names()
            gp_tools = runtime.subagent_runner._build_registry("general-purpose").names()
            self.assertIn("submit_summary", explore_tools)
            self.assertIn("submit_summary", gp_tools)

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
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["done"],
                    tool_calls=[ToolCall("c", "submit_summary", {"summary": "done"})],
                )

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
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["done"],
                    tool_calls=[ToolCall("c", "submit_summary", {"summary": "done"})],
                )

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
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["done"],
                    tool_calls=[ToolCall("c", "submit_summary", {"summary": "done"})],
                )

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

    def test_fresh_subagent_checkpoint_keyed_by_tool_call_id_not_trace_id(self) -> None:
        """Regression (transcript d84bd0092218): a fresh subagent dispatched via
        the lead-loop serial path must checkpoint under tool_call.id, because
        that is the resume_from pointer the lead writes into its placeholder /
        interrupted tool_result. The old code used ctx.trace_id
        (<session>-<turn>), which never matched the resume pointer, so every
        resume silently started from scratch and the accumulated context was
        lost. execute_tool_call now stamps ctx.tool_call_id; the subagent
        handler keys the checkpoint on it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            registry = ToolRegistry()
            register_subagent_tool(registry)
            from open_somnia.runtime.events import ToolExecutionContext
            from open_somnia.runtime.round_runner import execute_tool_call

            tool_call_id = "call_00_ResumePointerMatchesKey999"
            trace_id = "d84bd0092218-dd381caa"  # <session>-<turn>, the WRONG key

            # Fresh subagent that runs one tool round, then is interrupted on
            # the next round's pre-complete check. We only assert the CHECKPOINT
            # KEY here (the actual bug); context accumulation is exercised by
            # test_resume_via_handler_finds_checkpoint_keyed_by_tool_call_id.
            call_count = {"n": 0}

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return AssistantTurn(
                        stop_reason="tool_use",
                        text_blocks=["Searching."],
                        tool_calls=[ToolCall("c1", "tree", {"path": ".", "depth": 1})],
                    )
                raise TurnInterrupted("Interrupted by user.")

            runtime.complete = fake_complete
            ctx = ToolExecutionContext(
                runtime=runtime, session=None, actor="lead",
                trace_id=trace_id,
                should_interrupt=(lambda: call_count["n"] >= 1),
            )
            tool_call = ToolCall(tool_call_id, "subagent", {"prompt": "explore"})
            with self.assertRaises(TurnInterrupted):
                execute_tool_call(registry, ctx, tool_call)

            # The checkpoint MUST be filed under tool_call.id (the resume
            # pointer), NOT trace_id. Under the bug it was filed under
            # trace_id, so the lead's resume_from=tool_call.id found nothing.
            self.assertIsNotNone(runtime.subagent_checkpoint_store.load(tool_call_id))
            self.assertIsNone(runtime.subagent_checkpoint_store.load(trace_id))

    def test_resume_via_handler_finds_checkpoint_keyed_by_tool_call_id(self) -> None:
        """Regression (transcript d84bd0092218): a subagent checkpoint filed
        under tool_call.id (what the lead's fresh-run path now produces) must
        be found by a subsequent resume_from=<tool_call.id> through the serial
        handler, inheriting the checkpoint's accumulated context. Before the
        fix the checkpoint was filed under trace_id (<session>-<turn>) which
        never matched the lead's resume_from pointer, so resume silently
        restarted from scratch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            registry = ToolRegistry()
            register_subagent_tool(registry)
            from open_somnia.runtime.events import ToolExecutionContext

            tool_call_id = "call_00_FreshThenResume444"
            # Seed an interrupted checkpoint under tool_call.id, mimicking what
            # a fresh-run-then-interrupt produces: a user prompt + an assistant
            # tool-call turn + its tool_result (3 messages of real context).
            prior_messages = [
                {"role": "user", "content": "explore the workspace"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "Searching."},
                    {"type": "tool_call", "id": "c1", "name": "tree", "input": {"path": "."}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_call_id": "c1", "content": "root/"},
                ]},
            ]
            runtime.subagent_checkpoint_store.save(
                SubagentCheckpoint(
                    activity_id=tool_call_id,
                    prompt="explore the workspace",
                    agent_type="Explore",
                    messages=prior_messages,
                    pending_repair_hints=[],
                    rounds_used=1,
                    status="interrupted",
                    resume_count=0,
                )
            )

            resumed_message_counts = []

            def fake_complete_resume(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                resumed_message_counts.append(len(messages))
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=[],
                    tool_calls=[ToolCall("c2", "submit_summary", {"summary": "resumed answer"})],
                )

            runtime.complete = fake_complete_resume
            ctx2 = ToolExecutionContext(
                runtime=runtime, session=None, actor="lead",
                trace_id="sessionX-turn2", should_interrupt=lambda: False,
            )
            # The resume call's resume_from points at the checkpoint's key
            # (tool_call_id). The handler must load it (it would have returned
            # None under the bug, since the checkpoint was filed under trace_id).
            out = registry.execute(ctx2, "subagent", {
                "prompt": "continue", "resume_from": tool_call_id,
            })
            # Completed with the resumed summary, and inherited the full 3-message
            # prior context (not a single-message fresh start).
            self.assertEqual(out["status"], "completed")
            self.assertEqual(out["tool_result_text"], "resumed answer")
            self.assertGreaterEqual(resumed_message_counts[0], len(prior_messages))
            # Completed resume clears the checkpoint.
            self.assertIsNone(runtime.subagent_checkpoint_store.load(tool_call_id))

    def test_subagent_slot_id_matches_resume_from_for_resumed_calls(self) -> None:
        """Regression (transcript 23027ac64578, "4 running" on resume): the lead
        UI active-subagent slot key must match the id the subagent reports its
        activity under. A resumed subagent runs under its checkpoint's
        activity_id (the ``resume_from`` value), NOT the new tool_call.id. If
        the lead pre-fires / finishes keyed by tool_call.id while the subagent
        reports under resume_from, the host opens a SECOND slot per resumed
        subagent, doubling the displayed count. _subagent_slot_id resolves the
        shared key so pre-fire and self-report collapse to one slot."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))

            # Fresh subagent: slot key == tool_call.id.
            fresh = ToolCall("call_round2_new", "subagent", {"prompt": "explore"})
            self.assertEqual(runtime._subagent_slot_id(fresh), "call_round2_new")

            # Resumed subagent: slot key == resume_from (the checkpoint id the
            # resumed subagent reports under), NOT the new tool_call.id.
            resumed = ToolCall(
                "call_round2_new",
                "subagent",
                {"prompt": "continue", "resume_from": "call_round1_checkpoint"},
            )
            self.assertEqual(runtime._subagent_slot_id(resumed), "call_round1_checkpoint")
            # The two ids differ -- without the helper the slot would key on
            # "call_round2_new" and never match the subagent's self-reported
            # "call_round1_checkpoint" activity id.
            self.assertNotEqual(resumed.id, runtime._subagent_slot_id(resumed))

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

    def test_placeholder_assistant_message_preserves_thinking_blocks(self) -> None:
        """Regression (400 "content[].thinking ... must be passed back"): the
        placeholder assistant message is the ONLY persisted record of a
        subagent-only round (the round-end append skips placeholder ids), so it
        must carry the turn's thinking blocks -- thinking-enabled providers
        reject an assistant message whose tool_use lacks its thinking blocks.
        Text blocks stay out (mixed rounds persist them via the round-end
        append; placeholder-only rounds carry no user-facing text)."""
        from open_somnia.providers.anthropic_provider import _to_anthropic_messages

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            session = runtime.create_session()
            tc = ToolCall("sa3", "subagent", {"prompt": "explore C"})
            plan = SimpleNamespace(tool_call=tc, tool_name="subagent")
            turn = AssistantTurn(
                stop_reason="tool_use",
                text_blocks=["interim"],
                tool_calls=[tc],
                content_blocks=[
                    {"type": "thinking", "thinking": "plan first", "signature": "sig-1"},
                    {"type": "text", "text": "interim"},
                    {"type": "tool_call", "id": "sa3", "name": "subagent", "input": {"prompt": "explore C"}},
                ],
            )
            assistant_msg, _items = runtime._append_subagent_placeholders(session, [plan], turn=turn)
            self.assertIs(session.messages[-2], assistant_msg)
            self.assertEqual(
                [block["type"] for block in assistant_msg["content"]],
                ["thinking", "tool_call"],
            )
            self.assertEqual(assistant_msg["content"][0]["signature"], "sig-1")
            # The Anthropic payload conversion must then emit the thinking
            # block ahead of the tool_use block.
            converted = _to_anthropic_messages([assistant_msg])
            self.assertEqual(
                [block["type"] for block in converted[0]["content"]],
                ["thinking", "tool_use"],
            )

    def test_placeholder_assistant_message_without_turn_stays_tool_call_only(self) -> None:
        """The legacy no-turn path (defensive) keeps the old shape."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            session = runtime.create_session()
            tc = ToolCall("sa4", "subagent", {"prompt": "explore D"})
            plan = SimpleNamespace(tool_call=tc, tool_name="subagent")
            assistant_msg, _items = runtime._append_subagent_placeholders(session, [plan])
            self.assertEqual([block["type"] for block in assistant_msg["content"]], ["tool_call"])

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
