from __future__ import annotations

import os
import threading
import time
import unittest
from types import SimpleNamespace

from open_somnia.runtime import parallel_dispatch
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import ToolCall
from open_somnia.runtime.parallel_dispatch import (
    PARALLEL_SAFE_TOOL_NAMES,
    dispatch_parallel_segment,
    is_parallel_safe,
    parallel_dispatch_enabled,
    segment_tool_calls,
)
from open_somnia.tools.registry import ToolDefinition, ToolRegistry


def _ctx_factory():
    def make():
        return SimpleNamespace(runtime=None, session=None, actor="test", trace_id="t", should_interrupt=None)
    return make


def _registry_with(tool_name: str, handler):
    registry = ToolRegistry()
    registry.register(ToolDefinition(name=tool_name, description="d", input_schema={}, handler=handler))
    return registry


def _delay_registry(tool_name: str, delay: float, sink: list, tag: str):
    """Records start/finish into ``sink`` so tests can observe concurrency/order."""

    def handler(ctx, payload):
        sink.append((tag, "start"))
        time.sleep(delay)
        sink.append((tag, "end"))
        return {"status": "ok", "tag": tag}

    return _registry_with(tool_name, handler)


class ParallelSafeSetTests(unittest.TestCase):
    def test_includes_core_readonly_tools(self) -> None:
        for name in ("read_file", "grep", "glob", "tree", "find_symbol", "web_fetch"):
            self.assertIn(name, PARALLEL_SAFE_TOOL_NAMES)

    def test_excludes_mutating_and_stateful_tools(self) -> None:
        for name in ("write_file", "edit_file", "bash", "background_run", "TodoWrite",
                     "request_authorization", "subagent", "spawn_teammate",
                     "task_create_batch", "send_message"):
            self.assertNotIn(name, PARALLEL_SAFE_TOOL_NAMES)

    def test_excludes_mcp_tools(self) -> None:
        self.assertFalse(is_parallel_safe("mcp__foo__bar"))

    def test_is_parallel_safe_handles_garbage(self) -> None:
        self.assertFalse(is_parallel_safe(""))
        self.assertFalse(is_parallel_safe(None))  # type: ignore[arg-type]


class SegmentToolCallsTests(unittest.TestCase):
    def _calls(self, *names: str):
        return [ToolCall(id=f"c{i}", name=n, input={}) for i, n in enumerate(names)]

    def test_all_safe_single_segment(self) -> None:
        segs = list(segment_tool_calls(self._calls("read_file", "grep", "glob")))
        self.assertEqual(segs, [[0, 1, 2]])

    def test_writes_break_segments(self) -> None:
        segs = list(segment_tool_calls(self._calls("read_file", "write_file", "read_file")))
        self.assertEqual(segs, [[0], [1], [2]])

    def test_leading_and_trailing_unsafe(self) -> None:
        segs = list(segment_tool_calls(self._calls("bash", "read_file", "grep", "edit_file")))
        self.assertEqual(segs, [[0], [1, 2], [3]])

    def test_empty(self) -> None:
        self.assertEqual(list(segment_tool_calls([])), [])

    def test_singletons_for_all_unsafe(self) -> None:
        segs = list(segment_tool_calls(self._calls("write_file", "edit_file")))
        self.assertEqual(segs, [[0], [1]])

    def test_custom_predicate(self) -> None:
        # Simulate the lead loop folding an extra constraint (e.g. decision != EXECUTE).
        # An unsafe item still forms its own singleton segment; it is not dropped.
        calls = self._calls("read_file", "read_file", "read_file")
        segs = list(segment_tool_calls(calls, safe=lambda tc: tc.name == "read_file" and tc.id != "c1"))
        self.assertEqual(segs, [[0], [1], [2]])

    def test_custom_predicate_groups_allowed(self) -> None:
        calls = self._calls("read_file", "read_file", "read_file")
        segs = list(segment_tool_calls(calls, safe=lambda tc: tc.name == "read_file"))
        self.assertEqual(segs, [[0, 1, 2]])


class ParallelDispatchEnabledTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(parallel_dispatch.NO_PARALLEL_ENV, None)

    def tearDown(self):
        if self._saved is not None:
            os.environ[parallel_dispatch.NO_PARALLEL_ENV] = self._saved
        else:
            os.environ.pop(parallel_dispatch.NO_PARALLEL_ENV, None)

    def test_default_enabled(self) -> None:
        self.assertTrue(parallel_dispatch_enabled(SimpleNamespace()))

    def test_env_disables(self) -> None:
        os.environ[parallel_dispatch.NO_PARALLEL_ENV] = "1"
        self.assertFalse(parallel_dispatch_enabled(SimpleNamespace()))
        # Even with the setting explicitly True, the env hatch wins.
        self.assertFalse(parallel_dispatch_enabled(SimpleNamespace(runtime=SimpleNamespace(parallel_tool_dispatch=True))))

    def test_setting_disables(self) -> None:
        self.assertFalse(parallel_dispatch_enabled(SimpleNamespace(runtime=SimpleNamespace(parallel_tool_dispatch=False))))

    def test_missing_runtime_defaults_true(self) -> None:
        self.assertTrue(parallel_dispatch_enabled(None))  # type: ignore[arg-type]


class DispatchParallelSegmentTests(unittest.TestCase):
    def setUp(self):
        # Parallel dispatch must be enabled for these tests.
        self._saved = os.environ.pop(parallel_dispatch.NO_PARALLEL_ENV, None)

    def tearDown(self):
        if self._saved is not None:
            os.environ[parallel_dispatch.NO_PARALLEL_ENV] = self._saved

    def test_concurrent_execution_overlaps(self) -> None:
        registry = _delay_registry("read_file", 0.15, sink := [], tag="A")
        # Register two tools; dispatch runs two read_file calls concurrently.
        registry.register(ToolDefinition(name="grep", description="d", input_schema={},
                                         handler=lambda ctx, p: {"status": "ok", "tag": "B"}))
        calls = [ToolCall(id="r1", name="read_file", input={}), ToolCall(id="r2", name="grep", input={})]
        t0 = time.monotonic()
        records = dispatch_parallel_segment(
            registry, _ctx_factory(), calls,
            settings=SimpleNamespace(runtime=SimpleNamespace(parallel_tool_dispatch=True)),
        )
        elapsed = time.monotonic() - t0
        self.assertEqual(len(records), 2)
        # If they ran in parallel, total time is well under 2*delay.
        self.assertLess(elapsed, 0.28)

    def test_results_preserve_input_order(self) -> None:
        # Two tools with very different latencies; result order must follow call order, not finish order.
        def slow(ctx, p):
            time.sleep(0.2)
            return {"status": "ok", "who": "slow"}

        def fast(ctx, p):
            return {"status": "ok", "who": "fast"}

        registry = ToolRegistry()
        registry.register(ToolDefinition(name="read_file", description="d", input_schema={}, handler=slow))
        registry.register(ToolDefinition(name="grep", description="d", input_schema={}, handler=fast))
        # Put the slow tool first so it finishes last; order must still be slow, fast.
        calls = [ToolCall(id="slow", name="read_file", input={}), ToolCall(id="fast", name="grep", input={})]
        records = dispatch_parallel_segment(
            registry, _ctx_factory(), calls,
            settings=SimpleNamespace(runtime=SimpleNamespace(parallel_tool_dispatch=True)),
        )
        self.assertEqual(records[0].persisted_output, {"status": "ok", "who": "slow"})
        self.assertEqual(records[1].persisted_output, {"status": "ok", "who": "fast"})
        self.assertEqual(records[0].tool_call.id, "slow")
        self.assertEqual(records[1].tool_call.id, "fast")

    def test_single_call_is_serial_fast_path(self) -> None:
        registry = _registry_with("read_file", lambda ctx, p: {"status": "ok"})
        calls = [ToolCall(id="only", name="read_file", input={})]
        records = dispatch_parallel_segment(
            registry, _ctx_factory(), calls,
            settings=SimpleNamespace(runtime=SimpleNamespace(parallel_tool_dispatch=True)),
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].persisted_output, {"status": "ok"})

    def test_interrupt_raises_before_submit(self) -> None:
        calls_made: list[str] = []

        def handler(ctx, p):
            calls_made.append("ran")
            return {"status": "ok"}

        registry = _registry_with("read_file", handler)
        calls = [ToolCall(id="r1", name="read_file", input={}), ToolCall(id="r2", name="read_file", input={})]
        with self.assertRaises(TurnInterrupted):
            dispatch_parallel_segment(
                registry, _ctx_factory(), calls,
                should_interrupt=lambda: True,
                settings=SimpleNamespace(runtime=SimpleNamespace(parallel_tool_dispatch=True)),
            )
        self.assertEqual(calls_made, [])

    def test_disabled_setting_runs_serially(self) -> None:
        # When disabled, calls run sequentially even in a multi-call segment.
        order: list[str] = []
        lock = threading.Lock()

        def make_handler(tag):
            def handler(ctx, p):
                with lock:
                    order.append(f"{tag}-start")
                time.sleep(0.1)
                with lock:
                    order.append(f"{tag}-end")
                return {"status": "ok", "tag": tag}
            return handler

        registry = ToolRegistry()
        registry.register(ToolDefinition(name="read_file", description="d", input_schema={}, handler=make_handler("A")))
        registry.register(ToolDefinition(name="grep", description="d", input_schema={}, handler=make_handler("B")))
        calls = [ToolCall(id="a", name="read_file", input={}), ToolCall(id="b", name="grep", input={})]
        dispatch_parallel_segment(
            registry, _ctx_factory(), calls,
            settings=SimpleNamespace(runtime=SimpleNamespace(parallel_tool_dispatch=False)),
        )
        # Serial: A fully completes before B starts.
        self.assertEqual(order, ["A-start", "A-end", "B-start", "B-end"])


class _PlannerHost:
    """Minimal stand-in exposing only the methods/attrs the lead planner uses.

    ``_plan_lead_tool_calls`` references ``_is_exploration_tool_call``,
    ``_malformed_tool_name_reason``, ``_exploration_budget_error``,
    ``EXPLORATION_TOOL_NAMES``, ``EXPLORATION_SHELL_PREFIXES`` and
    ``TURN_BOUNDARY_TOOL_NAMES``. We bind the real implementation from
    ``OpenAgentRuntime`` so the planner is exercised against production logic
    without constructing a full runtime.
    """

    EXPLORATION_TOOL_NAMES = OpenAgentRuntime.EXPLORATION_TOOL_NAMES
    EXPLORATION_SHELL_PREFIXES = OpenAgentRuntime.EXPLORATION_SHELL_PREFIXES
    EXPLORATION_HARD_STREAK_LIMIT = OpenAgentRuntime.EXPLORATION_HARD_STREAK_LIMIT
    EXPLORATION_SUMMARY_REMINDER_TEXT = OpenAgentRuntime.EXPLORATION_SUMMARY_REMINDER_TEXT
    TURN_BOUNDARY_TOOL_NAMES = OpenAgentRuntime.TURN_BOUNDARY_TOOL_NAMES

    _is_exploration_tool_call = OpenAgentRuntime._is_exploration_tool_call
    _malformed_tool_name_reason = OpenAgentRuntime._malformed_tool_name_reason
    _exploration_budget_error = OpenAgentRuntime._exploration_budget_error
    _plan_lead_tool_calls = OpenAgentRuntime._plan_lead_tool_calls


class LeadToolCallPlannerTests(unittest.TestCase):
    """Stage A: the deterministic pre-scan must reproduce the serial guard logic."""

    def _plan(self, tool_calls, **overrides):
        host = _PlannerHost()
        defaults = dict(
            max_tool_calls=64,
            known_tool_names={"read_file", "grep", "glob", "tree", "write_file", "bash"},
            exploration_streak=0,
            exploration_total=0,
            exploration_soft_limit=10,
            exploration_hard_streak_limit=14,
            exploration_hard_total_limit=0,
        )
        defaults.update(overrides)
        return host._plan_lead_tool_calls(tool_calls, **defaults)

    def _calls(self, *names):
        return [ToolCall(id=f"c{i}", name=n, input={}) for i, n in enumerate(names)]

    def test_all_execute_calls_marked_parallel_safe(self) -> None:
        plan, _, _, _ = self._plan(self._calls("read_file", "grep", "glob"))
        self.assertEqual([p.decision for p in plan], ["execute", "execute", "execute"])
        self.assertTrue(all(p.parallel_safe for p in plan))

    def test_write_and_bash_not_parallel_safe(self) -> None:
        plan, _, _, _ = self._plan(self._calls("write_file", "bash"))
        self.assertTrue(all(p.decision == "execute" for p in plan))
        self.assertFalse(plan[0].parallel_safe)
        self.assertFalse(plan[1].parallel_safe)

    def test_unknown_tool_produces_error_decision(self) -> None:
        plan, _, _, _ = self._plan(self._calls("nonexistent_tool"))
        self.assertEqual(plan[0].decision, "unknown_error")
        self.assertIsNotNone(plan[0].guard_output)
        self.assertFalse(plan[0].parallel_safe)

    def test_duplicate_unknown_dropped_from_plan(self) -> None:
        # Serial loop ``continue``s on a duplicate; the plan must not contain it.
        plan, _, _, _ = self._plan(self._calls("nonexistent_tool", "nonexistent_tool", "read_file"))
        self.assertEqual(len(plan), 2)  # first unknown reported, then read_file
        self.assertEqual(plan[0].decision, "unknown_error")
        self.assertEqual(plan[1].decision, "execute")

    def test_flood_guard_truncates_with_error(self) -> None:
        calls = self._calls(*(["read_file"] * 5))
        plan, _, _, _ = self._plan(calls, max_tool_calls=2)
        # First two execute; third hits flood guard; the plan stops there.
        self.assertEqual(len(plan), 3)
        self.assertEqual(plan[0].decision, "execute")
        self.assertEqual(plan[1].decision, "execute")
        self.assertEqual(plan[2].decision, "flood_error")
        self.assertTrue(plan[2].end_turn_after)

    def test_exploration_budget_triggers_budget_error_and_end_turn(self) -> None:
        calls = self._calls(*(["read_file"] * 16))
        plan, streak, total, pending = self._plan(
            calls, exploration_hard_streak_limit=3, exploration_soft_limit=2
        )
        # The 4th read exceeds streak limit 3 -> budget_error, end_turn.
        budget_indices = [i for i, p in enumerate(plan) if p.decision == "budget_error"]
        self.assertEqual(len(budget_indices), 1)
        self.assertEqual(budget_indices[0], 3)
        self.assertTrue(plan[budget_indices[0]].end_turn_after)
        self.assertTrue(pending)
        # Streak counts only the 3 successful reads before the budget tripped.
        self.assertEqual(streak, 3)

    def test_non_exploration_tool_resets_streak(self) -> None:
        # Two reads (streak 2), then a write resets streak, then reads again.
        calls = self._calls("read_file", "read_file", "write_file", "read_file")
        plan, streak, _, _ = self._plan(calls)
        self.assertEqual(plan[2].tool_name, "write_file")
        self.assertEqual(streak, 1)  # reset by write_file, then one read
        # The write breaks the parallel-safe run: reads before/after are separate.
        self.assertTrue(plan[0].parallel_safe)
        self.assertFalse(plan[2].parallel_safe)

    def test_turn_boundary_tool_flagged(self) -> None:
        plan, _, _, _ = self._plan(
            self._calls("read_file", "request_authorization"),
            known_tool_names={"read_file", "request_authorization"},
        )
        self.assertTrue(plan[1].is_turn_boundary)
        self.assertFalse(plan[1].parallel_safe)


if __name__ == "__main__":
    unittest.main()
