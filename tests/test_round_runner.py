from __future__ import annotations

import unittest
from types import SimpleNamespace

from open_somnia.runtime.compact import ContextWindowUsage
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import AssistantTurn, ToolCall
from open_somnia.runtime.round_runner import (
    RoundHooks,
    SessionlessRoundRunner,
    execute_tool_call,
    finalize_tool_call,
)
from open_somnia.runtime.teammate import TeammateRuntimeManager
from open_somnia.tools.registry import ToolDefinition, ToolRegistry


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(runtime=None, session=None, actor="test", trace_id="t-1", should_interrupt=None)


def _echo_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def echo(ctx, payload):
        return {"status": "ok", "echo": payload.get("text", "")}

    registry.register(ToolDefinition(name="echo", description="echo back", input_schema={}, handler=echo))
    return registry


class _StubRegistry:
    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    def execute(self, ctx, name, payload):
        self.calls += 1
        raise self.exc

    def schemas(self):
        return []


class _FakeRuntime:
    """Minimal runtime surface SessionlessRoundRunner depends on."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.completed_payloads = []

    def _build_payload_messages(self, messages, session=None):
        return list(messages)

    def complete(self, system_prompt, payload_messages, schemas, should_interrupt=None):
        self.completed_payloads.append(payload_messages)
        return self.turns.pop(0)


class FinalizeToolCallTests(unittest.TestCase):
    def test_builds_result_item(self) -> None:
        tool_call = ToolCall(id="c1", name="echo", input={})
        record = finalize_tool_call(tool_call, {"status": "ok", "data": 1})
        self.assertEqual(record.persisted_output, {"status": "ok", "data": 1})
        self.assertEqual(record.result_item["type"], "tool_result")
        self.assertEqual(record.result_item["tool_call_id"], "c1")
        self.assertIsNone(record.repair_hint)
        self.assertIn('"data":1', record.rendered_output)

    def test_extracts_repair_hint_and_strips_it_from_persisted_output(self) -> None:
        tool_call = ToolCall(id="c1", name="echo", input={})
        output = {
            "status": "error",
            "error_type": "missing_required_params",
            "message": "missing text",
            "repair_hint": {"text": "str"},
        }
        record = finalize_tool_call(tool_call, output)
        self.assertIsNotNone(record.repair_hint)
        self.assertEqual(record.repair_hint["error_type"], "missing_required_params")
        self.assertNotIn("repair_hint", record.persisted_output)
        self.assertTrue(record.result_item["is_error"])


class ExecuteToolCallTests(unittest.TestCase):
    def test_success_runs_after_execute_hook(self) -> None:
        seen = []
        hooks = RoundHooks(after_execute=lambda tool_call, output: seen.append((tool_call.name, output)))
        record = execute_tool_call(_echo_registry(), _ctx(), ToolCall(id="c1", name="echo", input={"text": "hi"}), hooks=hooks)
        self.assertEqual(record.persisted_output, {"status": "ok", "echo": "hi"})
        self.assertEqual(seen, [("echo", {"status": "ok", "echo": "hi"})])

    def test_exception_falls_back_to_tool_error(self) -> None:
        errors = []
        hooks = RoundHooks(on_execute_error=errors.append)
        boom = RuntimeError("boom")
        record = execute_tool_call(_StubRegistry(boom), _ctx(), ToolCall(id="c1", name="echo", input={}), hooks=hooks)
        self.assertEqual(errors, [boom])
        self.assertEqual(record.persisted_output["error_type"], "tool_execution_failed")
        self.assertIn("boom", record.persisted_output["message"])

    def test_interrupted_propagates(self) -> None:
        with self.assertRaises(TurnInterrupted):
            execute_tool_call(_StubRegistry(TurnInterrupted("stop")), _ctx(), ToolCall(id="c1", name="echo", input={}))

    def test_before_execute_intercepts_and_skips_registry(self) -> None:
        registry = _echo_registry()
        hooks = RoundHooks(before_execute=lambda tool_call: "Entering idle phase.")
        record = execute_tool_call(registry, _ctx(), ToolCall(id="c1", name="echo", input={}), hooks=hooks)
        self.assertEqual(record.persisted_output, "Entering idle phase.")
        self.assertEqual(record.rendered_output, "Entering idle phase.")


class SessionlessRoundRunnerTests(unittest.TestCase):
    def _run(self, turns, *, messages=None, pending=None, hooks=None, should_interrupt=None):
        runtime = _FakeRuntime(turns)
        runner = SessionlessRoundRunner(runtime)
        result = runner.run_round(
            system_prompt="sys",
            messages=messages if messages is not None else [{"role": "user", "content": "hi"}],
            registry=_echo_registry(),
            pending_repair_hints=pending if pending is not None else [],
            actor="tester",
            trace_id="t-1",
            should_interrupt=should_interrupt,
            hooks=hooks,
        )
        return result, runtime

    def test_round_without_tool_calls(self) -> None:
        seen = []
        hooks = RoundHooks(on_assistant_message=lambda message, text: seen.append(text))
        result, _ = self._run([AssistantTurn(stop_reason="end_turn", text_blocks=["done"])], hooks=hooks)
        self.assertFalse(result.has_tool_calls)
        self.assertEqual(result.turn_text, "done")
        self.assertEqual(seen, ["done"])

    def test_round_executes_tools_and_backfills(self) -> None:
        messages = [{"role": "user", "content": "hi"}]
        turn = AssistantTurn(stop_reason="tool_use", tool_calls=[ToolCall(id="c1", name="echo", input={"text": "x"})])
        result, _ = self._run([turn], messages=messages)
        self.assertTrue(result.has_tool_calls)
        self.assertFalse(result.stop_after_round)
        self.assertEqual(len(result.records), 1)
        tool_result_message = messages[-1]
        self.assertEqual(tool_result_message["role"], "user")
        self.assertEqual(tool_result_message["content"][0]["type"], "tool_result")
        self.assertEqual(tool_result_message["content"][0]["tool_call_id"], "c1")

    def test_round_drains_pending_repair_hints_as_user_message(self) -> None:
        injected = []
        hooks = RoundHooks(on_repair_hint=injected.append)
        pending = [{"tool_name": "echo", "error_type": "missing_required_params", "message": "m", "repair_hint": {"text": "str"}}]
        messages = [{"role": "user", "content": "hi"}]
        result, _ = self._run(
            [AssistantTurn(stop_reason="end_turn", text_blocks=["ok"])],
            messages=messages,
            pending=pending,
            hooks=hooks,
        )
        self.assertEqual(pending, [])
        self.assertEqual(len(injected), 1)
        self.assertIn("<tool-repair-hints>", messages[1]["content"])
        self.assertEqual(result.turn_text, "ok")

    def test_round_collects_repair_hints_from_tool_errors(self) -> None:
        pending = []

        def failing(ctx, payload):
            raise KeyError("text")

        schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
        registry = ToolRegistry()
        registry.register(ToolDefinition(name="echo", description="d", input_schema=schema, handler=failing))
        runtime = _FakeRuntime([AssistantTurn(stop_reason="tool_use", tool_calls=[ToolCall(id="c1", name="echo", input={"text": "x"})])])
        runner = SessionlessRoundRunner(runtime)
        runner.run_round(
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            registry=registry,
            pending_repair_hints=pending,
            actor="tester",
            trace_id="t-1",
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["error_type"], "missing_required_params")

    def test_round_stop_after_round_hook(self) -> None:
        hooks = RoundHooks(should_stop_after_round=lambda record: record.tool_call.name == "echo")
        turn = AssistantTurn(stop_reason="tool_use", tool_calls=[ToolCall(id="c1", name="echo", input={})])
        result, _ = self._run([turn], hooks=hooks)
        self.assertTrue(result.stop_after_round)

    def test_round_interrupt_check(self) -> None:
        with self.assertRaises(TurnInterrupted):
            self._run([AssistantTurn(stop_reason="end_turn", text_blocks=["x"])], should_interrupt=lambda: True)


def _readonly_registry():
    """Registry of parallel-safe tools that record whether they overlapped."""
    import threading
    import time

    lock = threading.Lock()
    active: list[str] = []
    log: list[tuple[str, str]] = []

    def make(name, delay=0.0):
        def handler(ctx, payload):
            with lock:
                active.append(name)
                log.append((name, "start"))
            if delay:
                time.sleep(delay)
            with lock:
                active.remove(name)
                log.append((name, "end"))
            return {"status": "ok", "tool": name}
        return handler

    registry = ToolRegistry()
    for name in ("read_file", "grep", "glob"):
        registry.register(ToolDefinition(name=name, description="d", input_schema={}, handler=make(name)))
    # A non-safe tool to verify serialization around it.
    registry.register(
        ToolDefinition(name="write_file", description="d", input_schema={}, handler=make("write_file"))
    )
    return registry, log


class SessionlessRoundRunnerParallelTests(unittest.TestCase):
    """The run_round loop drives order-preserving segment parallelism."""

    def _run_turn(self, tool_calls, registry):
        runtime = _FakeRuntime([AssistantTurn(stop_reason="tool_use", tool_calls=tool_calls)])
        runtime.settings = SimpleNamespace(runtime=SimpleNamespace(parallel_tool_dispatch=True))
        runner = SessionlessRoundRunner(runtime)
        messages = [{"role": "user", "content": "hi"}]
        result = runner.run_round(
            system_prompt="sys",
            messages=messages,
            registry=registry,
            pending_repair_hints=[],
            actor="tester",
            trace_id="t-1",
        )
        return result, messages

    def test_parallel_readonly_tools_run_concurrently(self) -> None:
        import threading
        import time

        active: list[str] = []
        overlap_seen = {"value": False}
        guard = threading.Lock()

        def make(name, delay):
            def handler(ctx, payload):
                with guard:
                    active.append(name)
                    if len(active) > 1:
                        overlap_seen["value"] = True
                time.sleep(delay)
                with guard:
                    active.remove(name)
                return {"status": "ok", "tool": name}
            return handler

        registry = ToolRegistry()
        for name in ("read_file", "grep", "glob"):
            registry.register(ToolDefinition(name=name, description="d", input_schema={}, handler=make(name, 0.15)))

        calls = [
            ToolCall(id="r1", name="read_file", input={}),
            ToolCall(id="r2", name="grep", input={}),
            ToolCall(id="r3", name="glob", input={}),
        ]
        t0 = time.monotonic()
        result, _ = self._run_turn(calls, registry)
        elapsed = time.monotonic() - t0
        self.assertTrue(result.has_tool_calls)
        self.assertEqual(len(result.records), 3)
        # All three ran concurrently (overlap observed) and finished in ~1 delay, not 3.
        self.assertTrue(overlap_seen["value"], "read-only tools should run concurrently")
        self.assertLess(elapsed, 0.40)

    def test_results_preserve_input_order_under_parallelism(self) -> None:
        import time

        registry = ToolRegistry()

        def slow(ctx, p):
            time.sleep(0.15)
            return {"status": "ok", "who": "slow"}

        def fast(ctx, p):
            return {"status": "ok", "who": "fast"}

        registry.register(ToolDefinition(name="read_file", description="d", input_schema={}, handler=slow))
        registry.register(ToolDefinition(name="grep", description="d", input_schema={}, handler=fast))
        # slow first: finishes last, but result order must stay [slow, fast].
        calls = [ToolCall(id="slow", name="read_file", input={}), ToolCall(id="fast", name="grep", input={})]
        result, _ = self._run_turn(calls, registry)
        self.assertEqual([r.tool_call.id for r in result.records], ["slow", "fast"])
        self.assertEqual(result.records[0].persisted_output, {"status": "ok", "who": "slow"})

    def test_write_then_read_runs_serially(self) -> None:
        """A write tool must not be reordered ahead of a trailing read."""
        import threading
        import time

        log: list[str] = []
        guard = threading.Lock()

        def make(name, delay=0.05):
            def handler(ctx, payload):
                with guard:
                    log.append(name)
                time.sleep(delay)
                return {"status": "ok", "tool": name}
            return handler

        registry = ToolRegistry()
        registry.register(ToolDefinition(name="write_file", description="d", input_schema={}, handler=make("write_file")))
        registry.register(ToolDefinition(name="read_file", description="d", input_schema={}, handler=make("read_file")))
        calls = [ToolCall(id="w", name="write_file", input={}), ToolCall(id="r", name="read_file", input={})]
        result, _ = self._run_turn(calls, registry)
        self.assertEqual(len(result.records), 2)
        # write_file executes fully before read_file starts (no reordering).
        self.assertEqual(log, ["write_file", "read_file"])

    def test_on_tool_record_fires_in_input_order(self) -> None:
        import time

        seen: list[str] = []
        hooks = RoundHooks(on_tool_record=lambda rec: seen.append(rec.tool_call.id))

        registry = ToolRegistry()

        def slow(ctx, p):
            time.sleep(0.1)
            return {"status": "ok", "who": "slow"}

        registry.register(ToolDefinition(name="read_file", description="d", input_schema={}, handler=slow))
        registry.register(ToolDefinition(name="grep", description="d", input_schema={}, handler=lambda c, p: {"status": "ok"}))
        runtime = _FakeRuntime(
            [AssistantTurn(stop_reason="tool_use", tool_calls=[
                ToolCall(id="slow", name="read_file", input={}),
                ToolCall(id="fast", name="grep", input={}),
            ])]
        )
        runtime.settings = SimpleNamespace(runtime=SimpleNamespace(parallel_tool_dispatch=True))
        runner = SessionlessRoundRunner(runtime)
        runner.run_round(
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            registry=registry,
            pending_repair_hints=[],
            actor="tester",
            trace_id="t-1",
            hooks=hooks,
        )
        # Hooks fire in input order despite concurrent execution.
        self.assertEqual(seen, ["slow", "fast"])

    def test_disable_runs_serially(self) -> None:
        registry = ToolRegistry()
        import threading
        import time

        active: list[str] = []
        overlap = {"value": False}
        guard = threading.Lock()

        def make(name):
            def handler(ctx, p):
                with guard:
                    active.append(name)
                    if len(active) > 1:
                        overlap["value"] = True
                time.sleep(0.1)
                with guard:
                    active.remove(name)
                return {"status": "ok"}
            return handler

        registry.register(ToolDefinition(name="read_file", description="d", input_schema={}, handler=make("read_file")))
        registry.register(ToolDefinition(name="grep", description="d", input_schema={}, handler=make("grep")))

        runtime = _FakeRuntime([AssistantTurn(stop_reason="tool_use", tool_calls=[
            ToolCall(id="a", name="read_file", input={}),
            ToolCall(id="b", name="grep", input={}),
        ])])
        runtime.settings = SimpleNamespace(runtime=SimpleNamespace(parallel_tool_dispatch=False))
        runner = SessionlessRoundRunner(runtime)
        runner.run_round(
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            registry=registry,
            pending_repair_hints=[],
            actor="tester",
            trace_id="t-1",
        )
        self.assertFalse(overlap["value"], "with parallel dispatch disabled, tools must not overlap")


class TeammateCompactTests(unittest.TestCase):
    def _manager(self, usage: ContextWindowUsage) -> tuple[TeammateRuntimeManager, list[tuple[str, list]]]:
        compact_calls: list[tuple[str, list]] = []

        class _CompactManager:
            def auto_compact(self, session_id, messages):
                compact_calls.append((session_id, list(messages)))
                return [{"role": "user", "content": "[compressed]"}]

        runtime = SimpleNamespace(
            _count_payload_usage=lambda system_prompt, messages, tools: usage,
            compact_manager=_CompactManager(),
        )
        team_store = SimpleNamespace(
            append_log=lambda *args, **kwargs: None,
            load=lambda session_id=None: {"team_name": "default", "members": []},
        )
        manager = TeammateRuntimeManager(runtime, team_store, bus=None, task_store=None, request_tracker=None)
        return manager, compact_calls

    def test_compacts_when_usage_crosses_threshold(self) -> None:
        manager, compact_calls = self._manager(ContextWindowUsage(used_tokens=900, max_tokens=1000))
        messages = [{"role": "user", "content": "x"}]
        manager._compact_context_if_needed("worker", "sys", messages, registry=_echo_registry(), session_id="s1")
        self.assertEqual(compact_calls, [("teammate-worker", [{"role": "user", "content": "x"}])])
        self.assertEqual(messages, [{"role": "user", "content": "[compressed]"}])

    def test_skips_below_threshold(self) -> None:
        manager, compact_calls = self._manager(ContextWindowUsage(used_tokens=500, max_tokens=1000))
        messages = [{"role": "user", "content": "x"}]
        manager._compact_context_if_needed("worker", "sys", messages, registry=_echo_registry(), session_id="s1")
        self.assertEqual(compact_calls, [])
        self.assertEqual(messages, [{"role": "user", "content": "x"}])


if __name__ == "__main__":
    unittest.main()
