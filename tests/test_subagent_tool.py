from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from open_somnia.config.models import AgentSettings, AppSettings, ProviderSettings, RuntimeSettings, StorageSettings
from open_somnia.runtime.agent import OpenAgentRuntime
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

            def fake_complete(system_prompt, messages, tools, text_callback=None):
                seen["tool_names"] = [tool["name"] for tool in tools]
                return AssistantTurn(stop_reason="end_turn", text_blocks=["done"], tool_calls=[])

            runtime.complete = fake_complete

            runtime.run_subagent("Inspect the repo", "Explore")

            self.assertEqual(
                seen["tool_names"],
                ["bash", "project_scan", "tree", "find_symbol", "glob", "grep", "read_file", "load_skill"],
            )

    def test_general_purpose_subagent_exposes_edit_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            seen = {}

            def fake_complete(system_prompt, messages, tools, text_callback=None):
                seen["tool_names"] = [tool["name"] for tool in tools]
                return AssistantTurn(stop_reason="end_turn", text_blocks=["done"], tool_calls=[])

            runtime.complete = fake_complete

            runtime.run_subagent("Patch a file", "general-purpose")

            self.assertEqual(
                seen["tool_names"],
                ["bash", "project_scan", "tree", "find_symbol", "glob", "grep", "read_file", "write_file", "edit_file", "load_skill"],
            )

    def test_explore_subagent_can_use_bash_in_accept_edits_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = OpenAgentRuntime(self._make_settings(Path(tmpdir)))
            runtime.execution_mode = "accept_edits"
            steps = []

            def fake_complete(system_prompt, messages, tools, text_callback=None):
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
            ),
        )
