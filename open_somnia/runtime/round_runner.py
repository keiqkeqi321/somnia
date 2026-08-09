"""Shared round primitives for the three agent loops.

The lead agent loop, subagent runner, and teammate loop each own their
lifecycle (session persistence, one-shot summary, threaded idle state
machine). This module is the layer below those shells: the mechanics of a
single round that all three used to reimplement separately.

Two layers:

- ``finalize_tool_call`` / ``execute_tool_call``: the single-tool pipeline
  (execute -> error fallback -> repair-hint extraction -> sanitize ->
  tool_result item). Used by all three loops.
- ``SessionlessRoundRunner``: one full round without a session (repair-hint
  injection -> payload build -> provider completion -> tool execution ->
  result backfill). Used by the subagent and teammate loops; the lead loop
  keeps its own round body because streaming, thinking logs, guards, and
  context governance are interleaved with its provider call.

Shells keep their own logging, persistence, and state synchronization via
``RoundHooks``; this module never touches sessions, transcripts, or todos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from open_somnia.runtime.events import ToolExecutionContext
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import (
    consume_ephemeral_image_blocks,
    make_tool_result_item,
    make_tool_result_message,
    make_user_text_message,
)
from open_somnia.runtime.parallel_dispatch import (
    dispatch_parallel_segment,
    segment_tool_calls,
)
from open_somnia.tools.registry import ToolRegistry
from open_somnia.tools.tool_errors import (
    extract_transient_repair_hint,
    render_transient_repair_hint_message,
    sanitize_tool_output_for_persistence,
    serialize_tool_output,
    tool_error_from_exception,
)


@dataclass(slots=True)
class ToolCallRecord:
    """Everything a shell needs to log and backfill one executed tool call."""

    tool_call: Any
    persisted_output: Any
    rendered_output: str
    repair_hint: dict[str, Any] | None
    result_item: dict[str, Any]


def finalize_tool_call(tool_call: Any, output: Any, **result_item_kwargs: Any) -> ToolCallRecord:
    """Shared post-processing for a tool output that already exists.

    Used directly by shells that produced ``output`` without ``registry.execute``
    (e.g. the lead loop's guard errors); otherwise call ``execute_tool_call``.
    The repair hint is extracted from the raw output; persistence drops it.
    """
    persisted_output = sanitize_tool_output_for_persistence(output)
    rendered_output = serialize_tool_output(persisted_output)
    return ToolCallRecord(
        tool_call=tool_call,
        persisted_output=persisted_output,
        rendered_output=rendered_output,
        repair_hint=extract_transient_repair_hint(output),
        result_item=make_tool_result_item(
            tool_call.id,
            persisted_output,
            rendered_output=rendered_output,
            **result_item_kwargs,
        ),
    )


@dataclass(slots=True)
class RoundHooks:
    """Optional interception points for shells using the round primitives.

    - ``before_execute``: return a non-None output to skip ``registry.execute``
      (teammate's ``idle`` tool); return None to execute normally.
    - ``after_execute``: runs only after a successful ``registry.execute``
      (teammate's claim_task/task_update member sync).
    - ``on_execute_error``: runs with the original exception before it is
      converted into a tool-error output.
    - ``on_tool_record``: runs per finalized tool call (shell logging).
    - ``should_stop_after_round``: True marks the round result with
      ``stop_after_round`` (teammate's idle request).
    - ``on_assistant_message``: runs after the assistant message is appended,
      receiving ``(assistant_message, turn_text)``.
    - ``on_repair_hint``: runs when a pending repair hint is injected as a
      user message (session-less loops have no transient-notice channel).
    """

    before_execute: Callable[[Any], Any | None] | None = None
    after_execute: Callable[[Any, Any], None] | None = None
    on_execute_error: Callable[[Exception], None] | None = None
    on_tool_record: Callable[[ToolCallRecord], None] | None = None
    should_stop_after_round: Callable[[ToolCallRecord], bool] | None = None
    on_assistant_message: Callable[[dict[str, Any], str], None] | None = None
    on_repair_hint: Callable[[str], None] | None = None


def execute_tool_call(
    registry: ToolRegistry,
    ctx: ToolExecutionContext,
    tool_call: Any,
    *,
    hooks: RoundHooks | None = None,
    **result_item_kwargs: Any,
) -> ToolCallRecord:
    """Execute one tool call through the pipeline shared by all three loops.

    Execution errors fall back to ``tool_error_from_exception``;
    ``TurnInterrupted`` propagates unchanged.
    """
    if hooks is not None and hooks.before_execute is not None:
        intercepted = hooks.before_execute(tool_call)
        if intercepted is not None:
            return finalize_tool_call(tool_call, intercepted, **result_item_kwargs)
    # Stamp the per-call id so tools that need a stable identifier keyed to
    # THIS tool_call (the subagent handler's checkpoint key, which must match
    # the lead's resume_from pointer) can read it. Set here rather than at ctx
    # construction because the ctx is built per-round/per-segment (often
    # before the specific tool_call is chosen) and this is the single funnel
    # for serial, fast-serial, and parallel dispatch.
    tool_call_id = getattr(tool_call, "id", None)
    if isinstance(tool_call_id, str) and tool_call_id:
        ctx.tool_call_id = tool_call_id
    try:
        output = registry.execute(ctx, tool_call.name, tool_call.input)
    except TurnInterrupted:
        raise
    except Exception as exc:
        if hooks is not None and hooks.on_execute_error is not None:
            hooks.on_execute_error(exc)
        output = tool_error_from_exception(tool_call.name, exc)
    else:
        if hooks is not None and hooks.after_execute is not None:
            hooks.after_execute(tool_call, output)
    return finalize_tool_call(tool_call, output, **result_item_kwargs)


@dataclass(slots=True)
class RoundResult:
    turn_text: str
    has_tool_calls: bool
    stop_after_round: bool = False
    records: list[ToolCallRecord] = field(default_factory=list)
    assistant_message: dict[str, Any] | None = None


class SessionlessRoundRunner:
    """One agent round for loops that have no AgentSession.

    A round drains pending repair hints into a user message, builds the
    payload, calls the provider, appends the assistant message, executes any
    tool calls, and backfills the tool-result message. Both ``messages`` and
    ``pending_repair_hints`` are mutated in place so the shell's lists carry
    across rounds.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def run_round(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        registry: ToolRegistry,
        pending_repair_hints: list[dict[str, Any]],
        actor: str,
        trace_id: str,
        should_interrupt=None,
        hooks: RoundHooks | None = None,
    ) -> RoundResult:
        self._raise_if_interrupted(should_interrupt)
        if pending_repair_hints:
            repair_message = render_transient_repair_hint_message(pending_repair_hints)
            pending_repair_hints.clear()
            if repair_message:
                messages.append(make_user_text_message(repair_message))
                if hooks is not None and hooks.on_repair_hint is not None:
                    hooks.on_repair_hint(repair_message)
        payload_builder = getattr(self.runtime, "_build_payload_messages", None)
        # Minimal fake runtimes (tests, embedders) may not provide the payload
        # builder; fall back to the raw message list like the teammate loop did.
        payload_messages = payload_builder(messages, session=None) if callable(payload_builder) else list(messages)
        consume_ephemeral_image_blocks(messages)
        turn = self.runtime.complete(
            system_prompt,
            payload_messages,
            registry.schemas(),
            should_interrupt=should_interrupt,
        )
        self._raise_if_interrupted(should_interrupt)
        assistant_message = turn.as_message()
        messages.append(assistant_message)
        turn_text = "\n".join(turn.text_blocks).strip()
        if hooks is not None and hooks.on_assistant_message is not None:
            hooks.on_assistant_message(assistant_message, turn_text)
        if not turn.has_tool_calls():
            return RoundResult(
                turn_text=turn_text,
                has_tool_calls=False,
                assistant_message=assistant_message,
            )
        ctx = ToolExecutionContext(
            runtime=self.runtime,
            session=None,
            actor=actor,
            trace_id=trace_id,
            should_interrupt=should_interrupt,
        )

        # Order-preserving segment parallelism: maximal runs of independent
        # read-only tools run concurrently on a shared pool; everything else
        # (writes, shell, subagent, stateful tools) stays serial. Results are
        # always returned in input order so provider tool_result pairing holds.
        # Hooks (on_tool_record / should_stop_after_round) and repair-hint
        # collection fire sequentially, in input order, after each segment
        # completes -- preserving the exact serial-observable behavior. The
        # toggle lives in parallel_dispatch; SOMNIA_NO_PARALLEL_TOOLS=1 or
        # runtime.parallel_tool_dispatch=false degrades every segment to a
        # serial run identical to the old loop.
        settings = getattr(self.runtime, "settings", None)
        records: list[ToolCallRecord] = []
        stop_after_round = False
        for seg_indices in segment_tool_calls(turn.tool_calls):
            self._raise_if_interrupted(should_interrupt)
            seg_calls = [turn.tool_calls[i] for i in seg_indices]
            if len(seg_calls) > 1:
                seg_records = dispatch_parallel_segment(
                    registry,
                    lambda: ctx,
                    seg_calls,
                    should_interrupt=should_interrupt,
                    settings=settings,
                    hooks=hooks,
                )
            else:
                seg_records = [execute_tool_call(registry, ctx, seg_calls[0], hooks=hooks)]
            for record in seg_records:
                if record.repair_hint is not None:
                    pending_repair_hints.append(record.repair_hint)
                if hooks is not None and hooks.on_tool_record is not None:
                    hooks.on_tool_record(record)
                records.append(record)
                if hooks is not None and hooks.should_stop_after_round is not None:
                    stop_after_round = stop_after_round or bool(hooks.should_stop_after_round(record))
        messages.append(make_tool_result_message([record.result_item for record in records]))
        return RoundResult(
            turn_text=turn_text,
            has_tool_calls=True,
            stop_after_round=stop_after_round,
            records=records,
            assistant_message=assistant_message,
        )

    @staticmethod
    def _raise_if_interrupted(should_interrupt) -> None:
        if should_interrupt is not None and should_interrupt():
            raise TurnInterrupted("Interrupted by user.")
