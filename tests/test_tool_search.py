from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from open_somnia.config.models import ProviderSettings
from open_somnia.providers.anthropic_provider import AnthropicProvider
from open_somnia.providers.openai_provider import OpenAIProvider
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.session import AgentSession
from open_somnia.runtime.system_prompt import SystemPromptBuilder
from open_somnia.tools.registry import ToolDefinition, ToolRegistry


def _handler(ctx, payload):
    return "ok"


def _tool(name: str, description: str = "tool", *, deferred: bool = False) -> ToolDefinition:
    return ToolDefinition(name, description, {"type": "object", "properties": {}}, _handler, deferred=deferred)


def _make_runtime(*, gate: bool) -> OpenAgentRuntime:
    runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
    runtime.settings = SimpleNamespace(
        runtime=SimpleNamespace(tool_search=gate),
        workspace_root=Path("D:/workspace"),
        agent=SimpleNamespace(system_prompt=None, name="Somnia"),
        provider=SimpleNamespace(name="openai", model="gpt-5"),
    )
    registry = ToolRegistry()
    registry.register(_tool("bash", "run commands"))
    registry.register(_tool("read_file", "read files"))
    registry.register(_tool("task_list", "List all tasks.", deferred=True))
    registry.register(_tool("send_message", "Send a message to a teammate.", deferred=True))
    runtime.registry = registry
    runtime.worker_registry = ToolRegistry()
    return runtime


def _names(schemas: list[dict]) -> list[str]:
    return [schema["name"] for schema in schemas]


class ToolSearchCompositionTests(unittest.TestCase):
    def test_gate_off_returns_all_tools_sorted(self) -> None:
        runtime = _make_runtime(gate=False)
        session = AgentSession(id="s1")
        session.loaded_tools.extend(["send_message", "task_list"])

        schemas = OpenAgentRuntime._tool_schemas_for_model(runtime, "lead", session=session)

        self.assertEqual(_names(schemas), ["bash", "read_file", "send_message", "task_list"])

    def test_gate_on_hides_deferred_until_loaded_then_appends_in_load_order(self) -> None:
        runtime = _make_runtime(gate=True)
        OpenAgentRuntime._register_tool_search_tool(runtime, runtime.registry)
        session = AgentSession(id="s1")

        schemas_before = OpenAgentRuntime._tool_schemas_for_model(runtime, "lead", session=session)
        self.assertEqual(_names(schemas_before), ["bash", "read_file", "tool_search"])

        session.loaded_tools.extend(["send_message", "task_list"])
        schemas_after = OpenAgentRuntime._tool_schemas_for_model(runtime, "lead", session=session)

        # Loaded tools append in load order; the resident prefix is byte-identical.
        self.assertEqual(_names(schemas_after), ["bash", "read_file", "tool_search", "send_message", "task_list"])
        self.assertEqual(schemas_after[: len(schemas_before)], schemas_before)
        self.assertIn("importance", schemas_after[-1]["input_schema"]["properties"])

    def test_gate_on_skips_loaded_names_no_longer_registered(self) -> None:
        runtime = _make_runtime(gate=True)
        session = AgentSession(id="s1")
        session.loaded_tools.extend(["ghost_tool", "task_list"])

        schemas = OpenAgentRuntime._tool_schemas_for_model(runtime, "lead", session=session)

        self.assertNotIn("ghost_tool", _names(schemas))
        self.assertEqual(_names(schemas)[-1], "task_list")


class ToolSearchHandlerTests(unittest.TestCase):
    def test_batch_load_unknown_and_idempotent(self) -> None:
        runtime = _make_runtime(gate=True)
        session = AgentSession(id="s1")
        ctx = SimpleNamespace(session=session, runtime=runtime)

        result = OpenAgentRuntime._execute_tool_search(
            runtime,
            ctx,
            {"queries": [{"name": "task_list"}, {"name": "send_message"}, {"name": "nope"}, {}]},
        )

        self.assertEqual(session.loaded_tools, ["task_list", "send_message"])
        self.assertIn("Loaded and now callable: task_list, send_message", result)
        self.assertIn("no such deferred tool: nope", result)
        self.assertIn("task_list", result)
        payload = json.loads(result.split("\n\n", 1)[0])
        self.assertEqual(payload[0]["name"], "task_list")
        self.assertIn("input_schema", payload[0])
        self.assertIn("available", payload[2])

        again = OpenAgentRuntime._execute_tool_search(runtime, ctx, {"queries": [{"name": "task_list"}]})

        self.assertEqual(session.loaded_tools, ["task_list", "send_message"])
        self.assertIn("No new tools were loaded", again)

    def test_register_local_tools_registers_tool_search_only_when_gate_on(self) -> None:
        for gate, expected in ((True, True), (False, False)):
            runtime = _make_runtime(gate=gate)
            runtime.skill_loader = SimpleNamespace(load=lambda name: "ok")
            registry = ToolRegistry()

            OpenAgentRuntime._register_local_tools(runtime, registry)

            self.assertEqual("tool_search" in registry.names(), expected, f"gate={gate}")


class ToolSearchGuardTests(unittest.TestCase):
    def test_deferred_tool_unloaded(self) -> None:
        runtime = _make_runtime(gate=True)
        session = AgentSession(id="s1")

        self.assertTrue(OpenAgentRuntime._deferred_tool_unloaded(runtime, "task_list", session=session))
        session.loaded_tools.append("task_list")
        self.assertFalse(OpenAgentRuntime._deferred_tool_unloaded(runtime, "task_list", session=session))
        self.assertFalse(OpenAgentRuntime._deferred_tool_unloaded(runtime, "bash", session=session))

    def test_deferred_tool_unloaded_gate_off(self) -> None:
        runtime = _make_runtime(gate=False)
        session = AgentSession(id="s1")

        self.assertFalse(OpenAgentRuntime._deferred_tool_unloaded(runtime, "task_list", session=session))


class ToolSearchSessionTests(unittest.TestCase):
    def test_loaded_tools_payload_round_trip(self) -> None:
        session = AgentSession(id="s1")
        session.loaded_tools.extend(["task_list", "send_message"])

        restored = AgentSession.from_payload(session.to_payload())

        self.assertEqual(restored.loaded_tools, ["task_list", "send_message"])

    def test_loaded_tools_defaults_empty_for_legacy_payloads(self) -> None:
        restored = AgentSession.from_payload({"id": "s1"})

        self.assertEqual(restored.loaded_tools, [])


class ToolSearchRosterTests(unittest.TestCase):
    def _builder_runtime(self, *, gate: bool) -> OpenAgentRuntime:
        runtime = _make_runtime(gate=gate)
        runtime.mcp_registry = SimpleNamespace(all_servers=[], server_tools={})
        runtime.execution_mode = "accept_edits"
        runtime.skill_loader = SimpleNamespace(prompt_index=lambda: "short skill index", descriptions=lambda: "long")
        runtime.current_working_file_path = lambda: ""
        return runtime

    def test_roster_in_stable_section_and_byte_static(self) -> None:
        runtime = self._builder_runtime(gate=True)

        sections_one = SystemPromptBuilder(runtime).build_prompt_bundle().to_payload()
        sections_two = SystemPromptBuilder(runtime).build_prompt_bundle().to_payload()

        runtime_section = sections_one[1]
        self.assertEqual(runtime_section["title"], "B. Runtime Injection")
        self.assertFalse(runtime_section["dynamic"])
        self.assertIn("Deferred tools (NOT loaded", runtime_section["content"])
        self.assertIn("- send_message: Send a message to a teammate.", runtime_section["content"])
        self.assertIn("tool_search", runtime_section["content"])
        self.assertEqual(sections_one[1]["content"], sections_two[1]["content"])

    def test_roster_absent_when_gate_off(self) -> None:
        runtime = self._builder_runtime(gate=False)

        sections = SystemPromptBuilder(runtime).build_prompt_bundle().to_payload()

        self.assertNotIn("Deferred tools", sections[1]["content"])


class ToolSearchProviderPayloadTests(unittest.TestCase):
    def _anthropic_provider(self) -> AnthropicProvider:
        return AnthropicProvider(
            ProviderSettings(
                name="anthropic",
                provider_type="anthropic",
                model="claude-sonnet-4-5",
                api_key="test-key",
                base_url="https://api.anthropic.com",
                timeout_seconds=30,
            )
        )

    def test_anthropic_tools_tier_stable_after_load(self) -> None:
        runtime = _make_runtime(gate=True)
        OpenAgentRuntime._register_tool_search_tool(runtime, runtime.registry)
        session = AgentSession(id="s1")
        provider = self._anthropic_provider()
        messages = [{"role": "user", "content": "hello"}]

        schemas_before = OpenAgentRuntime._tool_schemas_for_model(runtime, "lead", session=session)
        payload_before = provider.debug_request_payload("system", messages, schemas_before, 4096, stream=False)

        session.loaded_tools.extend(["send_message", "task_list"])
        schemas_after = OpenAgentRuntime._tool_schemas_for_model(runtime, "lead", session=session)
        payload_after = provider.debug_request_payload("system", messages, schemas_after, 4096, stream=False)

        # Append-only growth: ignoring the breakpoint marker itself (which always
        # moves to the newest last tool for steady-state cacheability), the
        # resident segment is byte-identical. On strict-prefix providers the
        # load turn is the accepted one-time tools-tier rebuild; subsequent
        # turns hit the full enlarged tier.
        def strip_cache_control(tools: list[dict]) -> list[dict]:
            return [{key: value for key, value in tool.items() if key != "cache_control"} for tool in tools]

        self.assertEqual(
            strip_cache_control(payload_after["tools"][: len(payload_before["tools"])]),
            strip_cache_control(payload_before["tools"]),
        )
        self.assertEqual(payload_before["tools"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(payload_after["tools"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", payload_after["tools"][-2])

    def test_openai_payload_carries_tool_search_not_deferred(self) -> None:
        runtime = _make_runtime(gate=True)
        OpenAgentRuntime._register_tool_search_tool(runtime, runtime.registry)
        session = AgentSession(id="s1")
        provider = OpenAIProvider(
            ProviderSettings(
                name="deepseek",
                provider_type="openai",
                model="deepseek-chat",
                api_key="test-key",
                base_url="https://api.deepseek.com/v1",
                timeout_seconds=30,
            )
        )

        schemas = OpenAgentRuntime._tool_schemas_for_model(runtime, "lead", session=session)
        payload = provider.debug_request_payload("system", [{"role": "user", "content": "hello"}], schemas, 1024, stream=False)

        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("tool_search", text)
        self.assertNotIn("task_list", text)
        self.assertNotIn("send_message", text)


if __name__ == "__main__":
    unittest.main()
