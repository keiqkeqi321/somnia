from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from open_somnia.config.models import AgentSettings, AppSettings, ProviderSettings, RuntimeSettings, StorageSettings
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import AssistantTurn, ToolCall
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
                    "project_scan",
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
                    "project_scan",
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

            self.assertEqual(result, "Done.")

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

            self.assertEqual(result, "Found the root.")
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

            self.assertEqual(result, "Found the root.")
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
