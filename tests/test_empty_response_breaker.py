"""Tests for the thinking-only (empty response) circuit breaker and the
reasoning-budget exhaustion recovery in the agent loop.

Background: a reasoning model on the OpenAI-compatible path shares one
``max_tokens`` budget between reasoning and visible output. When the model burns
the whole budget on reasoning and is truncated mid-thought, it produces a turn
with only a ``thinking`` block and no text or tool calls. Previously the loop
retried with the same budget up to ``max_agent_rounds`` (default 100), spinning
for dozens of turns. These tests cover the two safeguards:

- detection of reasoning-budget exhaustion + a one-shot clamped budget bump (C);
- a consecutive-empty-response circuit breaker that stops the turn (B).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.messages import AssistantTurn


def _runtime_with_provider(*, max_tokens: int, context_window_tokens: int | None = 32768) -> OpenAgentRuntime:
    runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
    provider = SimpleNamespace(
        settings=SimpleNamespace(
            max_tokens=max_tokens,
            context_window_tokens=context_window_tokens,
        ),
    )

    def _context_window_tokens():
        return context_window_tokens

    provider.context_window_tokens = _context_window_tokens
    runtime.provider = provider
    runtime.settings = SimpleNamespace(provider=provider.settings)
    runtime._transient_max_tokens_override = None
    return runtime


class ReasoningBudgetDetectionTests(unittest.TestCase):
    def test_detects_explicit_reasoning_tokens_near_max(self):
        runtime = _runtime_with_provider(max_tokens=8000)
        turn = AssistantTurn(
            stop_reason="end_turn",
            text_blocks=[],
            usage={"output_tokens": 8000, "reasoning_tokens": 7800},
        )
        self.assertTrue(runtime._detect_reasoning_budget_exhaustion(turn, current_max_tokens=8000))

    def test_ignores_when_reasoning_tokens_small(self):
        runtime = _runtime_with_provider(max_tokens=8000)
        turn = AssistantTurn(
            stop_reason="end_turn",
            text_blocks=[],
            usage={"output_tokens": 1200, "reasoning_tokens": 900},
        )
        self.assertFalse(runtime._detect_reasoning_budget_exhaustion(turn, current_max_tokens=8000))

    def test_falls_back_to_output_tokens_ratio_when_no_reasoning_breakdown(self):
        runtime = _runtime_with_provider(max_tokens=8000)
        turn = AssistantTurn(
            stop_reason="end_turn",
            text_blocks=[],
            usage={"output_tokens": 7900},
        )
        self.assertTrue(runtime._detect_reasoning_budget_exhaustion(turn, current_max_tokens=8000))

    def test_no_usage_returns_false(self):
        runtime = _runtime_with_provider(max_tokens=8000)
        turn = AssistantTurn(stop_reason="end_turn", text_blocks=[], usage=None)
        self.assertFalse(runtime._detect_reasoning_budget_exhaustion(turn, current_max_tokens=8000))


class ReasoningBudgetBumpTests(unittest.TestCase):
    def test_bump_doubles_and_clamps_to_context_window(self):
        runtime = _runtime_with_provider(max_tokens=8000, context_window_tokens=32768)
        target = runtime._compute_reasoning_budget_bump(8000)
        self.assertEqual(target, 16000)

    def test_bump_never_exceeds_seventy_five_percent_of_context_window(self):
        # Tiny context window: 75% of 10000 = 7500, which is below 8000, so no bump.
        runtime = _runtime_with_provider(max_tokens=8000, context_window_tokens=10000)
        self.assertIsNone(runtime._compute_reasoning_budget_bump(8000))

    def test_bump_caps_at_hard_ceiling(self):
        # Already near the hard ceiling: a meaningful (>=20%) bump is impossible,
        # so no bump is offered and the loop falls through to the circuit breaker.
        runtime = _runtime_with_provider(max_tokens=60000, context_window_tokens=200000)
        self.assertIsNone(runtime._compute_reasoning_budget_bump(60000))

    def test_bump_uses_hard_ceiling_when_context_window_generous(self):
        # 40000 * 2 = 80000 > 65536 ceiling, but 65536 is still a >=20% bump over
        # 40000 (65536 >= 48000), so it is clamped to the hard ceiling and returned.
        runtime = _runtime_with_provider(max_tokens=40000, context_window_tokens=200000)
        target = runtime._compute_reasoning_budget_bump(40000)
        self.assertEqual(target, OpenAgentRuntime.EMPTY_RESPONSE_REASONING_BUMP_MAX_TOKENS)

    def test_maybe_raise_stages_one_shot_override(self):
        runtime = _runtime_with_provider(max_tokens=8000, context_window_tokens=32768)
        turn = AssistantTurn(
            stop_reason="end_turn",
            text_blocks=[],
            usage={"output_tokens": 8000, "reasoning_tokens": 8000},
        )
        staged = runtime._maybe_raise_reasoning_budget(turn)
        self.assertIsNotNone(staged)
        self.assertEqual(runtime._transient_max_tokens_override, staged)

    def test_maybe_raise_returns_none_when_not_exhausted(self):
        runtime = _runtime_with_provider(max_tokens=8000)
        turn = AssistantTurn(
            stop_reason="end_turn",
            text_blocks=["partial answer"],
            usage={"output_tokens": 500, "reasoning_tokens": 400},
        )
        self.assertIsNone(runtime._maybe_raise_reasoning_budget(turn))
        self.assertIsNone(runtime._transient_max_tokens_override)

    def test_maybe_raise_does_not_stack_bumps(self):
        runtime = _runtime_with_provider(max_tokens=8000, context_window_tokens=32768)
        runtime._set_transient_max_tokens_override(16000)
        turn = AssistantTurn(
            stop_reason="end_turn",
            text_blocks=[],
            usage={"output_tokens": 8000, "reasoning_tokens": 8000},
        )
        self.assertIsNone(runtime._maybe_raise_reasoning_budget(turn))
        # The pre-existing override is left intact (not consumed by the peek).
        self.assertEqual(runtime._transient_max_tokens_override, 16000)


class TransientOverrideConsumeTests(unittest.TestCase):
    def test_consume_returns_override_then_falls_back(self):
        runtime = _runtime_with_provider(max_tokens=8000)
        runtime._set_transient_max_tokens_override(16000)
        self.assertEqual(runtime._consume_transient_max_tokens_override(8000), 16000)
        # Second call reverts to the configured value.
        self.assertEqual(runtime._consume_transient_max_tokens_override(8000), 8000)
        self.assertIsNone(runtime._transient_max_tokens_override)

    def test_consume_falls_back_when_no_override(self):
        runtime = _runtime_with_provider(max_tokens=8000)
        self.assertEqual(runtime._consume_transient_max_tokens_override(8000), 8000)


if __name__ == "__main__":
    unittest.main()
