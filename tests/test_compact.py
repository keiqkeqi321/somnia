from __future__ import annotations

import unittest
from types import SimpleNamespace

from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.compact import (
    ContextWindowUsage,
    MICROCOMPACT_RECENT_TOOL_BUDGET_CHARS,
    build_payload_messages,
    should_auto_compact,
)
from open_somnia.runtime.session import AgentSession


def _tool_call(call_id: str, name: str) -> dict:
    return {
        "role": "assistant",
        "content": [
            {
                "type": "tool_call",
                "id": call_id,
                "name": name,
                "input": {},
            }
        ],
    }


def _tool_result(call_id: str, content: str) -> dict:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_call_id": call_id,
                "content": content,
                "raw_output": {"full": content},
                "log_id": f"log-{call_id}",
            }
        ],
    }


class CompactTests(unittest.TestCase):
    def test_build_payload_messages_compacts_old_rounds_without_mutating_history(self) -> None:
        messages = [
            _tool_call("call-1", "bash"),
            _tool_result("call-1", "a" * 300),
            _tool_call("call-2", "grep"),
            _tool_result("call-2", "b" * 300),
            _tool_call("call-3", "read_file"),
            _tool_result("call-3", "c" * 300),
        ]

        payload_messages = build_payload_messages(messages)

        self.assertEqual(messages[1]["content"][0]["content"], "a" * 300)
        oldest_payload_result = payload_messages[1]["content"][0]
        self.assertTrue(str(oldest_payload_result["content"]).startswith("[tool:bash]"))
        self.assertNotIn("raw_output", oldest_payload_result)
        self.assertNotIn("log_id", oldest_payload_result)
        self.assertEqual(payload_messages[3]["content"][0]["content"], "b" * 300)
        self.assertEqual(payload_messages[5]["content"][0]["content"], "c" * 300)

    def test_build_payload_messages_shrinks_large_recent_tool_rounds_to_budget(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_call", "id": "call-1", "name": "bash", "input": {}},
                    {"type": "tool_call", "id": "call-2", "name": "grep", "input": {}},
                    {"type": "tool_call", "id": "call-3", "name": "read_file", "input": {}},
                    {"type": "tool_call", "id": "call-4", "name": "bash", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_call_id": "call-1", "content": "a" * 5_000, "raw_output": "a" * 5_000},
                    {"type": "tool_result", "tool_call_id": "call-2", "content": "b" * 5_000, "raw_output": "b" * 5_000},
                    {"type": "tool_result", "tool_call_id": "call-3", "content": "c" * 5_000, "raw_output": "c" * 5_000},
                    {"type": "tool_result", "tool_call_id": "call-4", "content": "d" * 5_000, "raw_output": "d" * 5_000},
                ],
            },
        ]

        payload_messages = build_payload_messages(messages)
        payload_results = payload_messages[1]["content"]
        compacted_total = sum(len(str(item["content"])) for item in payload_results)
        original_total = sum(len(str(item["content"])) for item in messages[1]["content"])

        self.assertLess(compacted_total, original_total)
        self.assertLessEqual(compacted_total, MICROCOMPACT_RECENT_TOOL_BUDGET_CHARS)
        self.assertTrue(any(str(item["content"]).startswith("[tool:") for item in payload_results))

    def test_should_auto_compact_uses_ratio_or_hard_threshold(self) -> None:
        self.assertTrue(
            should_auto_compact(
                ContextWindowUsage(used_tokens=72_000, max_tokens=100_000),
                hard_threshold=200_000,
            )
        )
        self.assertTrue(
            should_auto_compact(
                ContextWindowUsage(used_tokens=100_000, max_tokens=None),
                hard_threshold=100_000,
            )
        )
        self.assertFalse(
            should_auto_compact(
                ContextWindowUsage(used_tokens=70_000, max_tokens=100_000),
                hard_threshold=100_000,
            )
        )

    def test_context_window_usage_counts_compacted_payload_messages(self) -> None:
        captured_messages: list[dict] = []
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(
            provider=SimpleNamespace(name="openai", model="gpt-4.1", context_window_tokens=128_000)
        )

        def _count_tokens(system_prompt, messages, tools):
            captured_messages.clear()
            captured_messages.extend(messages)
            return 12_345

        runtime.provider = SimpleNamespace(
            count_tokens=_count_tokens,
            token_counter_name=lambda: "tiktoken",
            context_window_tokens=lambda: 128_000,
        )
        runtime.registry = SimpleNamespace(schemas=lambda: [])
        runtime.worker_registry = SimpleNamespace(schemas=lambda: [])
        runtime.build_system_prompt = lambda actor="lead", role="lead coding agent": "system"
        runtime.execution_mode = "accept_edits"
        runtime._context_usage_cache = {}

        session = AgentSession(
            id="session-1",
            messages=[
                _tool_call("call-1", "bash"),
                _tool_result("call-1", "a" * 300),
                _tool_call("call-2", "grep"),
                _tool_result("call-2", "b" * 300),
                _tool_call("call-3", "read_file"),
                _tool_result("call-3", "c" * 300),
            ],
        )

        usage = OpenAgentRuntime.context_window_usage(runtime, session)

        self.assertEqual(usage.used_tokens, 12_345)
        self.assertEqual(usage.usage_percent, 12_345 / 128_000 * 100.0)
        self.assertEqual(session.messages[1]["content"][0]["content"], "a" * 300)
        oldest_payload_result = captured_messages[1]["content"][0]
        self.assertTrue(str(oldest_payload_result["content"]).startswith("[tool:bash]"))
        self.assertNotIn("raw_output", oldest_payload_result)


if __name__ == "__main__":
    unittest.main()
