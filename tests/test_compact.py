from __future__ import annotations

import unittest
from types import SimpleNamespace

from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.compact import (
    CompactManager,
    ContextWindowUsage,
    SemanticCompressionDecision,
    build_payload_messages,
    extract_tool_result_candidates,
    persist_semantic_compression,
    should_auto_compact,
    should_run_semantic_janitor,
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
    def test_build_payload_messages_preserves_tool_result_content_without_mutating_history(self) -> None:
        messages = [
            _tool_call("call-1", "bash"),
            _tool_result("call-1", "a" * 300),
            _tool_call("call-2", "grep"),
            _tool_result("call-2", "b" * 300),
            _tool_call("call-3", "read_file"),
            _tool_result("call-3", "c" * 300),
            _tool_call("call-4", "tree"),
            _tool_result("call-4", "d" * 300),
        ]

        payload_messages = build_payload_messages(messages)

        self.assertEqual(messages[1]["content"][0]["content"], "a" * 300)
        oldest_payload_result = payload_messages[1]["content"][0]
        self.assertEqual(oldest_payload_result["content"], "a" * 300)
        self.assertEqual(payload_messages[3]["content"][0]["content"], "b" * 300)
        self.assertEqual(payload_messages[5]["content"][0]["content"], "c" * 300)
        self.assertEqual(payload_messages[7]["content"][0]["content"], "d" * 300)

    def test_build_payload_messages_strips_tool_result_metadata(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_call", "id": "call-1", "name": "bash", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-1",
                        "content": "a" * 5_000,
                        "raw_output": "a" * 5_000,
                        "log_id": "log-1",
                    },
                ],
            },
        ]

        payload_messages = build_payload_messages(messages)
        payload_result = payload_messages[1]["content"][0]

        self.assertEqual(payload_result["content"], "a" * 5_000)
        self.assertNotIn("raw_output", payload_result)
        self.assertNotIn("log_id", payload_result)
        self.assertIn("raw_output", messages[1]["content"][0])
        self.assertIn("log_id", messages[1]["content"][0])

    def test_build_payload_messages_can_apply_semantic_compression_without_mutating_history(self) -> None:
        messages = [
            _tool_call("call-1", "grep"),
            _tool_result("call-1", "needle found in main.py:12"),
        ]

        payload_messages = build_payload_messages(
            messages,
            semantic_decisions=[
                SemanticCompressionDecision(
                    message_index=1,
                    item_index=0,
                    state="condensed",
                    summary="[Semantic Summary | grep | log log-call-1] Confirmed the needle appears in main.py around line 12.",
                )
            ],
        )

        self.assertEqual(messages[1]["content"][0]["content"], "needle found in main.py:12")
        self.assertEqual(
            payload_messages[1]["content"][0]["content"],
            "[Semantic Summary | grep | log log-call-1] Confirmed the needle appears in main.py around line 12.",
        )
        self.assertNotIn("raw_output", payload_messages[1]["content"][0])
        self.assertNotIn("log_id", payload_messages[1]["content"][0])

    def test_persist_semantic_compression_updates_history_and_drops_raw_output(self) -> None:
        messages = [
            _tool_call("call-1", "grep"),
            _tool_result("call-1", "needle found in main.py:12"),
        ]

        changed = persist_semantic_compression(
            messages,
            semantic_decisions=[
                SemanticCompressionDecision(
                    message_index=1,
                    item_index=0,
                    state="condensed",
                    summary="[Semantic Summary | grep | log log-call-1] Confirmed the needle appears in main.py around line 12.",
                )
            ],
        )

        self.assertTrue(changed)
        self.assertEqual(
            messages[1]["content"][0]["content"],
            "[Semantic Summary | grep | log log-call-1] Confirmed the needle appears in main.py around line 12.",
        )
        self.assertEqual(messages[1]["content"][0]["semantic_state"], "condensed")
        self.assertNotIn("raw_output", messages[1]["content"][0])
        self.assertIn("log_id", messages[1]["content"][0])

    def test_extract_tool_result_candidates_skips_already_compacted_items(self) -> None:
        messages = [
            _tool_call("call-1", "grep"),
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-1",
                        "content": "[Semantic Summary | grep | log log-call-1] compacted",
                        "log_id": "log-call-1",
                        "semantic_state": "condensed",
                    }
                ],
            },
            _tool_call("call-2", "read_file"),
            _tool_result("call-2", "fresh content"),
            _tool_call("call-3", "grep"),
            _tool_result("call-3", "recent content"),
        ]

        candidates = extract_tool_result_candidates(messages, preserve_recent_rounds=1)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].tool_call_id, "call-2")

    def test_should_auto_compact_uses_ratio_only(self) -> None:
        self.assertTrue(
            should_auto_compact(ContextWindowUsage(used_tokens=82_000, max_tokens=100_000))
        )
        self.assertFalse(
            should_auto_compact(ContextWindowUsage(used_tokens=81_000, max_tokens=100_000))
        )
        self.assertFalse(
            should_auto_compact(ContextWindowUsage(used_tokens=100_000, max_tokens=None))
        )

    def test_should_run_semantic_janitor_uses_ratio_only(self) -> None:
        self.assertTrue(
            should_run_semantic_janitor(ContextWindowUsage(used_tokens=60_000, max_tokens=100_000))
        )
        self.assertFalse(
            should_run_semantic_janitor(ContextWindowUsage(used_tokens=59_000, max_tokens=100_000))
        )
        self.assertFalse(
            should_run_semantic_janitor(ContextWindowUsage(used_tokens=40_000, max_tokens=None))
        )

    def test_auto_compact_can_preserve_recent_task_window_while_summarizing_older_history(self) -> None:
        snapshots: list[list[dict]] = []
        provider = SimpleNamespace(complete=lambda **kwargs: SimpleNamespace(text_blocks=["Older history summary"]))
        manager = CompactManager(
            provider=provider,
            transcript_store=SimpleNamespace(save_snapshot=lambda session_id, messages: snapshots.append(list(messages))),
            model_max_tokens=16_000,
        )
        messages = [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "recent question"},
            {"role": "assistant", "content": "recent answer"},
            {"role": "user", "content": "current question"},
        ]

        compacted = manager.auto_compact("session-1", messages, preserve_from_index=2)

        self.assertEqual(snapshots[0], messages)
        self.assertEqual(compacted[0]["role"], "user")
        self.assertIn("Older history summary", compacted[0]["content"])
        self.assertEqual(compacted[2:], messages[2:])

    def test_context_window_usage_counts_full_payload_messages_without_tool_microcompact(self) -> None:
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
                _tool_call("call-4", "tree"),
                _tool_result("call-4", "d" * 300),
            ],
        )

        usage = OpenAgentRuntime.context_window_usage(runtime, session)

        self.assertEqual(usage.used_tokens, 12_345)
        self.assertEqual(usage.usage_percent, 12_345 / 128_000 * 100.0)
        self.assertEqual(session.messages[1]["content"][0]["content"], "a" * 300)
        oldest_payload_result = captured_messages[1]["content"][0]
        self.assertEqual(oldest_payload_result["content"], "a" * 300)
        self.assertNotIn("raw_output", oldest_payload_result)
        self.assertNotIn("log_id", oldest_payload_result)


if __name__ == "__main__":
    unittest.main()
