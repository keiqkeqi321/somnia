"""Order-preserving segment parallelism for tool dispatch.

Somnia's three agent loops historically executed tool calls strictly serially
(``for tool_call in turn.tool_calls``). Read-heavy exploration phases
(batch ``read_file`` / ``grep`` / ``glob`` / ``web_fetch``) pay the sum of
every call's I/O latency. This module shortens the *per-turn* wall-clock by
running maximal runs of independent read-only tools concurrently while
keeping every mutating / stateful / nested-loop tool strictly serial.

Design: order-preserving segment parallelism
--------------------------------------------
Scan ``tool_calls`` in input order and group *consecutive* calls that are
each in ``PARALLEL_SAFE_TOOL_NAMES`` into one segment. A segment of length >1
runs on a thread pool; everything else runs inline (single-threaded).
Segments are joined in input order, and the per-call results are reordered
back to their original indices before they are handed to the provider
adapters (Anthropic / OpenAI pair ``tool_result`` to the prior ``tool_use``
positionally, so order is a hard contract, not a cosmetic).

Equivalence to the serial baseline: a segment contains only pure read-only
tools with no shared mutable resource, so concurrent execution does not
change observable state; segments are concatenated in input order, so the
overall sequence of side effects equals the serial one. The lone behavioral
nuance is that a serial loop can ``break`` *immediately* after a tool,
whereas a parallel segment finishes its whole run before the shell checks
``should_stop_after_round`` -- but every call in such a segment is a
side-effect-free read, so completing the segment is harmless.

Escape hatch: ``SOMNIA_NO_PARALLEL_TOOLS=1`` forces every segment to length
1, i.e. pure serial execution identical to the pre-parallelism baseline
(useful for bisecting a misbehaving run, mirroring the existing
``SOMNIA_NO_RG`` switch for ripgrep delegation).
"""

from __future__ import annotations

import atexit
import os
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from typing import TYPE_CHECKING, Any, Callable, Iterator, Sequence

from open_somnia.runtime.events import ToolExecutionContext
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.tools.registry import ToolRegistry

if TYPE_CHECKING:
    # Imported lazily at runtime in _resolve_executor to avoid a circular
    # import: round_runner imports this module at its top level.
    from open_somnia.runtime.round_runner import ToolCallRecord


def _resolve_executor() -> Callable[..., Any]:
    """Return ``round_runner.execute_tool_call``, imported lazily.

    ``round_runner`` imports this module at its top level, so importing
    ``execute_tool_call`` at module scope here would create a circular import.
    The function is stateless after first import, so the lookup is cached.
    """
    cached = _resolve_executor._cached  # type: ignore[attr-defined]
    if cached is None:
        from open_somnia.runtime.round_runner import execute_tool_call as _exec

        _resolve_executor._cached = _exec  # type: ignore[attr-defined]
        cached = _exec
    return cached


_resolve_executor._cached = None  # type: ignore[attr-defined]


# Conservative whitelist of tools that are safe to run concurrently.
#
# Membership criteria (all must hold):
#   * pure read w.r.t. the workspace filesystem and runtime/session state --
#     no writes, no mutations of ``session.todo_items`` /
#     ``session.pending_file_changes`` / task store / team bus /
#     authorization counters;
#   * no control-flow side effects (does not end the turn, block on a user
#     handshake, mutate the system prompt, or spawn a nested agent loop);
#   * not a shell command (``bash`` cannot be statically classified as
#     read-only) and not an MCP tool (``mcp__*`` crosses a process boundary
#     with no read-only hint in the protocol layer here).
#
# This is intentionally a strict subset of
# ``execution_mode.READ_ONLY_TOOL_NAMES``: that set is a *mode-gating* list
# ("read-only with respect to the filesystem") and includes stateful tools
# such as ``TodoWrite`` / ``request_authorization`` / ``compress`` /
# ``load_skill`` that mutate session/context state with no locking. Do not
# treat membership in that set as "safe to parallelize".
PARALLEL_SAFE_TOOL_NAMES = frozenset(
    {
        "read_file",
        "read_image",
        "grep",
        "glob",
        "tree",
        "find_symbol",
        "web_fetch",
        "task_get",
        "task_list",
        "list_teammates",
        "check_background",
    }
)

# Environment variable that disables parallel dispatch entirely (troubleshooting).
NO_PARALLEL_ENV = "SOMNIA_NO_PARALLEL_TOOLS"


def parallel_dispatch_enabled(settings: Any) -> bool:
    """True when parallel tool dispatch should be used for this process.

    Disabled by the ``SOMNIA_NO_PARALLEL_TOOLS=1`` escape hatch, or by an
    explicit ``runtime.parallel_tool_dispatch = false`` setting. Defaults to
    enabled, mirroring the ripgrep acceleration default.
    """
    if os.environ.get(NO_PARALLEL_ENV, "") == "1":
        return False
    runtime_settings = getattr(settings, "runtime", None)
    if runtime_settings is None:
        return True
    return bool(getattr(runtime_settings, "parallel_tool_dispatch", True))


def _max_workers(settings: Any) -> int:
    runtime_settings = getattr(settings, "runtime", None)
    if runtime_settings is None:
        return 8
    try:
        raw = getattr(runtime_settings, "parallel_tool_max_workers", 8)
        value = int(raw)
    except (TypeError, ValueError):
        return 8
    return max(1, value)


class _SharedThreadPool:
    """Process-lifetime thread pool, lazily created on first parallel run.

    Creating a pool per turn would dominate the per-call latency it saves.
    The pool is sized to the configured ``parallel_tool_max_workers`` and
    shared across the lead loop, subagent runner, and teammate loop. A single
    module-level lock guards the (lazy) hand-off so two loops racing into a
    parallel segment at startup do not each spin up a pool.
    """

    def __init__(self) -> None:
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()

    def acquire(self, max_workers: int) -> ThreadPoolExecutor:
        pool = self._pool
        if pool is not None:
            return pool
        with self._lock:
            pool = self._pool
            if pool is None:
                pool = ThreadPoolExecutor(
                    max_workers=max(2, max_workers),
                    thread_name_prefix="somnia-tool",
                )
                self._pool = pool
                atexit.register(self._shutdown)
            return pool

    def _shutdown(self) -> None:
        pool = self._pool
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


_POOL = _SharedThreadPool()


def is_parallel_safe(tool_name: str) -> bool:
    """True when a tool name may run inside a concurrent segment."""
    return str(tool_name or "").strip() in PARALLEL_SAFE_TOOL_NAMES


def is_explore_subagent_safe(tool_call: Any) -> bool:
    """True when a tool call spawns an Explore subagent that may run in parallel.

    ``subagent`` itself is a nested agent loop, so it is deliberately NOT in
    :data:`PARALLEL_SAFE_TOOL_NAMES` (that set is for pure read-only *tools*).
    But an ``agent_type=Explore`` subagent is itself confined to read-only tools
    (its ``bash`` is gated to read-only commands by the subagent runner), so
    running several Explore subagents concurrently is safe -- exactly the
    "delegate three explorations in parallel" win the parallelism was built for.
    ``general-purpose`` subagents keep write_file/edit_file and stay serial.

    This predicate is folded into the default ``segment_tool_calls`` predicate
    so both the lead loop and ``SessionlessRoundRunner`` group consecutive
    Explore-subagent calls into one segment. The lead loop then dispatches such
    a segment via :func:`run_parallel_explore_subagents` (a *separate* pool,
    not ``_POOL`` -- see its docstring for the deadlock rationale).
    """
    name = str(getattr(tool_call, "name", "") or "").strip()
    if name != "subagent":
        return False
    payload = getattr(tool_call, "input", None)
    if not isinstance(payload, dict):
        # Treat a missing/unknown payload as the default agent_type (Explore).
        return True
    agent_type = str(payload.get("agent_type", "Explore") or "Explore").strip()
    return agent_type == "Explore"


def _default_segment_predicate(tool_call: Any) -> bool:
    """Default safe-set for ``segment_tool_calls``: read-only tools OR Explore subagents."""
    return is_parallel_safe(getattr(tool_call, "name", "")) or is_explore_subagent_safe(tool_call)


def segment_tool_calls(tool_calls: Sequence[Any], *, safe: Callable[[Any], bool] | None = None) -> Iterator[list[int]]:
    """Split ``tool_calls`` indices into maximal runs of parallel-safe calls.

    Two adjacent indices land in the same segment only when both are
    parallel-safe; any non-safe index forms (or extends) its own singleton
    segment. The caller decides whether a length-1 segment runs inline or
    via the pool -- ``segment_tool_calls`` deliberately keeps singletons so
    the caller can treat every segment uniformly.

    ``safe`` defaults to :func:`_default_segment_predicate` (read-only tools OR
    Explore subagents) and may be overridden by callers (e.g. the lead loop)
    that need to fold in extra per-call constraints such as "decision == EXECUTE".
    """
    predicate = safe if safe is not None else _default_segment_predicate
    current: list[int] = []
    for index, tool_call in enumerate(tool_calls):
        if predicate(tool_call):
            current.append(index)
        else:
            if current:
                yield current
                current = []
            yield [index]
    if current:
        yield current


def dispatch_parallel_segment(
    registry: ToolRegistry,
    ctx_factory: Callable[[], ToolExecutionContext],
    segment_calls: Sequence[Any],
    *,
    should_interrupt: Callable[[], bool] | None = None,
    settings: Any = None,
    hooks: Any = None,
    **result_item_kwargs: Any,
) -> list[ToolCallRecord]:
    """Execute a segment of independent tool calls concurrently, in input order.

    Each worker gets a fresh ``ToolExecutionContext`` from ``ctx_factory`` (the
    context object is not documented as thread-safe, and the lead loop
    already rebuilds it per call). Results are reordered to the segment's
    input order before return so downstream provider pairing stays correct.

    Interruption is cooperative: we check before submitting and again as each
    future completes; once an interrupt is seen we stop submitting, cancel
    pending futures, collect whatever already finished, and raise
    ``TurnInterrupted``. Worker exceptions other than ``TurnInterrupted`` are
    already converted to tool-error outputs by ``execute_tool_call``, so the
    only exception that escapes this function is ``TurnInterrupted`` itself.
    """
    enabled = parallel_dispatch_enabled(settings)
    if len(segment_calls) <= 1 or not enabled:
        # Serial fast path: single call, or parallelism disabled entirely.
        # Still run every call in the segment (in order) so callers can hand a
        # multi-call segment here regardless of the toggle.
        execute = _resolve_executor()
        out: list[ToolCallRecord] = []
        for tool_call in segment_calls:
            if should_interrupt is not None and should_interrupt():
                raise TurnInterrupted("Interrupted by user.")
            ctx = ctx_factory()
            out.append(execute(registry, ctx, tool_call, hooks=hooks, **result_item_kwargs))
        return out

    pool = _POOL.acquire(_max_workers(settings))
    futures: list[tuple[int, Future[ToolCallRecord]]] = []

    for offset, tool_call in enumerate(segment_calls):
        if should_interrupt is not None and should_interrupt():
            break
        # Capture the per-call ctx outside the worker: ctx_factory is cheap
        # and lets each worker own its own object (no shared mutable fields).
        ctx = ctx_factory()
        future = pool.submit(_run_one, registry, ctx, tool_call, hooks, result_item_kwargs)
        futures.append((offset, future))

    results: list[ToolCallRecord | None] = [None] * len(segment_calls)
    interrupted = False
    for offset, future in futures:
        try:
            results[offset] = future.result()
        except TurnInterrupted:
            interrupted = True
        # Other exceptions cannot reach here: execute_tool_call converts them
        # into tool-error outputs. If the worker itself blew up before
        # execute_tool_call could wrap it (e.g. ctx_factory raised), surface a
        # best-effort error rather than poisoning the results list with None.
        except Exception as exc:  # pragma: no cover - defensive
            from open_somnia.tools.tool_errors import tool_error_from_exception
            from open_somnia.runtime.round_runner import finalize_tool_call

            tool_call = segment_calls[offset]
            error_output = tool_error_from_exception(getattr(tool_call, "name", "tool"), exc)
            results[offset] = finalize_tool_call(tool_call, error_output, **result_item_kwargs)

    # Cancel anything not yet started on interrupt / early break.
    if interrupted or any(r is None for r in results):
        for _, future in futures:
            future.cancel()
        if interrupted or should_interrupt is not None and should_interrupt():
            raise TurnInterrupted("Interrupted by user.")

    return [r for r in results if r is not None]  # type: ignore[list-item]


def _run_one(
    registry: ToolRegistry,
    ctx: ToolExecutionContext,
    tool_call: Any,
    hooks: Any,
    result_item_kwargs: dict[str, Any],
) -> ToolCallRecord:
    """Per-worker entrypoint; thin wrapper so ``execute_tool_call`` owns all error policy."""
    return _resolve_executor()(registry, ctx, tool_call, hooks=hooks, **result_item_kwargs)


# ---------------------------------------------------------------------------
# Parallel Explore-subagent dispatch (separate pool, NOT ``_POOL``)
# ---------------------------------------------------------------------------
#
# Why a separate pool? A subagent is a nested agent loop: its internal rounds
# run through ``SessionlessRoundRunner`` → ``dispatch_parallel_segment`` →
# ``_POOL``. If the lead loop dispatched the subagent *calls* themselves on
# ``_POOL``, each subagent would occupy a ``_POOL`` worker for its whole
# multi-round lifetime, and when those subagents' internal read-only tools
# tried to acquire ``_POOL`` workers they could find the pool exhausted --
# classic thread-pool deadlock (all workers busy holding subagent loops that
# are themselves waiting for a worker). Teammates sidestep this by running on
# their own daemon threads; we mirror that here with a dedicated pool. The
# subagent pool is sized independently (capped at ``parallel_tool_max_workers``)
# so a burst of Explore delegations cannot spawn unbounded threads.


class _SharedSubagentThreadPool:
    """Process-lifetime pool for parallel Explore-subagent loops.

    Separate from :class:`_SharedThreadPool` to avoid the nested-dispatch
    deadlock described above. Lazily created on first parallel Explore run,
    sized to the configured ``parallel_tool_max_workers``.
    """

    def __init__(self) -> None:
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()

    def acquire(self, max_workers: int) -> ThreadPoolExecutor:
        pool = self._pool
        if pool is not None:
            return pool
        with self._lock:
            pool = self._pool
            if pool is None:
                pool = ThreadPoolExecutor(
                    max_workers=max(2, max_workers),
                    thread_name_prefix="somnia-subagent",
                )
                self._pool = pool
                atexit.register(self._shutdown)
            return pool

    def _shutdown(self) -> None:
        pool = self._pool
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


_SUBAGENT_POOL = _SharedSubagentThreadPool()


def run_parallel_explore_subagents(
    run_subagent_fn: Callable[..., Any],
    segment_calls: Sequence[Any],
    *,
    should_interrupt: Callable[[], bool] | None = None,
    settings: Any = None,
    session_id: str | None = None,
    extra_prompts: dict[str, str] | None = None,
    checkpoint_store: Any = None,
) -> list[dict[str, Any]]:
    """Run a segment of Explore-subagent calls concurrently, in input order.

    Each ``tool_call`` is dispatched to ``run_subagent_fn(prompt, agent_type,
    activity_id=..., should_interrupt=...)`` -- the same signature the lead
    loop's ``AgentRuntime.run_subagent`` exposes (it returns a
    ``SubagentResult``). The shared ``should_interrupt`` checker is forwarded
    into every subagent so a user interrupt propagates into running subagent
    loops (their internal rounds poll it between tool calls). Results
    (subagent structured outputs, dicts) are reordered to the segment's input
    order so the lead loop's provider ``tool_result`` pairing stays positional.

    Uses :data:`_SUBAGENT_POOL` (not ``_POOL``) to avoid the nested-dispatch
    deadlock: a subagent loop internally submits its read-only tools to
    ``_POOL``, so the subagent calls themselves must not consume ``_POOL``
    workers. Interruption is cooperative and mirrors
    :func:`dispatch_parallel_segment`: we check before submitting and again as
    each future completes; once an interrupt is seen we stop submitting, cancel
    pending futures, collect whatever finished, and raise ``TurnInterrupted``.

    When parallel dispatch is disabled (``SOMNIA_NO_PARALLEL_TOOLS=1`` or
    ``runtime.parallel_tool_dispatch = false``) or the segment is a single
    call, runs serially inline -- identical to the pre-parallel behavior.
    """
    enabled = parallel_dispatch_enabled(settings)
    # Resolve the interrupt checker once: the same lead-loop checker is shared
    # by every subagent so an interrupt aborts the whole parallel batch.
    interrupt_checker = should_interrupt
    prompts_by_id = extra_prompts or {}
    if len(segment_calls) <= 1 or not enabled:
        out: list[dict[str, Any]] = []
        for tool_call in segment_calls:
            if interrupt_checker is not None and interrupt_checker():
                raise TurnInterrupted("Interrupted by user.")
            ep = prompts_by_id.get(getattr(tool_call, "id", ""))
            out.append(_invoke_subagent(run_subagent_fn, tool_call, interrupt_checker, session_id, ep, checkpoint_store))
        return out

    pool = _SUBAGENT_POOL.acquire(_max_workers(settings))
    futures: list[tuple[int, Future[dict[str, Any]]]] = []
    for offset, tool_call in enumerate(segment_calls):
        if interrupt_checker is not None and interrupt_checker():
            break
        ep = prompts_by_id.get(getattr(tool_call, "id", ""))
        future = pool.submit(_invoke_subagent, run_subagent_fn, tool_call, interrupt_checker, session_id, ep, checkpoint_store)
        futures.append((offset, future))

    results: list[dict[str, Any] | None] = [None] * len(segment_calls)
    interrupted = False
    for offset, future in futures:
        try:
            results[offset] = future.result()
        except TurnInterrupted:
            interrupted = True
        except Exception as exc:  # pragma: no cover - defensive
            # A subagent loop blowing up should already be converted to a
            # structured result by its own error handling; if something escapes,
            # surface a best-effort structured error rather than a None slot.
            results[offset] = _subagent_failed_output(exc)

    if interrupted or any(r is None for r in results):
        for _, future in futures:
            future.cancel()
        if interrupted or (interrupt_checker is not None and interrupt_checker()):
            raise TurnInterrupted("Interrupted by user.")

    return [r for r in results if r is not None]  # type: ignore[list-item]


def _subagent_failed_output(exc: BaseException) -> dict[str, Any]:
    """Build a structured tool output for an escaped subagent exception.

    Normal failures are handled inside ``run_subagent`` (which returns a
    ``SubagentResult(status="failed")``). This covers only exceptions that
    escape that boundary, e.g. a pool/pickling error.
    """
    from open_somnia.runtime.subagent_runner import SubagentResult

    return SubagentResult(status="failed", error=str(exc)).as_tool_output()


def _invoke_subagent(
    run_subagent_fn: Callable[..., Any],
    tool_call: Any,
    should_interrupt: Callable[[], bool] | None = None,
    session_id: str | None = None,
    extra_prompt: str | None = None,
    checkpoint_store: Any = None,
) -> dict[str, Any]:
    """Worker entrypoint: unpack a subagent tool_call and call run_subagent_fn.

    Mirrors the lead-loop subagent handler's argument mapping
    (``tools/subagent.py``): prompt + agent_type (default Explore) +
    activity_id (the lead trace_id) + should_interrupt passthrough.
    ``run_subagent_fn`` returns a ``SubagentResult``; we convert it to its
    structured tool-output dict here so the lead loop's ``finalize_tool_call``
    handles it uniformly with the serial tool-handler path. ``session_id`` is
    forwarded so checkpoints are stamped with the owning session;
    ``extra_prompt`` carries an optional resume-time instruction.

    Resume: when the payload carries ``resume_from`` (an activity_id), load the
    checkpoint from ``checkpoint_store`` and forward it as ``resume_from``. On
    resume the activity_id is kept as the CHECKPOINT's activity_id (not the new
    tool_call.id) so the resumed subagent updates the SAME checkpoint file
    instead of opening a new one -- otherwise the lead's resume decision
    silently starts a fresh subagent and leaves the original checkpoint orphaned.
    """
    payload = getattr(tool_call, "input", None)
    if not isinstance(payload, dict):
        payload = {}
    prompt = str(payload.get("prompt", "") or "")
    agent_type = str(payload.get("agent_type", "Explore") or "Explore")
    new_activity_id = getattr(tool_call, "id", None) or getattr(tool_call, "trace_id", None)
    resume_from = None
    activity_id = new_activity_id
    resume_aid = str(payload.get("resume_from") or "").strip()
    if resume_aid and checkpoint_store is not None:
        try:
            resume_from = checkpoint_store.load(resume_aid)
        except Exception:
            resume_from = None
        # Keep the checkpoint's activity_id so the resumed run updates the same
        # checkpoint (and the lead's next resume_from points to a valid id).
        if resume_from is not None:
            activity_id = getattr(resume_from, "activity_id", resume_aid) or resume_aid
    result = run_subagent_fn(
        prompt,
        agent_type,
        activity_id=activity_id,
        should_interrupt=should_interrupt,
        session_id=session_id,
        resume_from=resume_from,
        extra_prompt=extra_prompt,
    )
    # run_subagent returns a SubagentResult; normalize to the structured tool
    # output dict. Guard against a legacy str return so an older runtime stays
    # compatible.
    if isinstance(result, dict):
        return result
    if hasattr(result, "as_tool_output"):
        return result.as_tool_output()
    return {"status": "completed", "tool_result_text": str(result)}
