from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import inspect
import json
from queue import Queue
from threading import Event, Lock, Thread
from typing import Any, Iterator
import uuid

from open_somnia.app_service.events import (
    ASSISTANT_COMPLETED,
    ASSISTANT_DELTA,
    CONTEXT_USAGE_UPDATED,
    ERROR,
    INTERRUPT_COMPLETED,
    INTERRUPT_REQUESTED,
    LOOP_USER_MESSAGE_INJECTED,
    NEW_SESSION_APPROVED,
    SESSION_UPDATED,
    SUBAGENT_ACTIVITY,
    THINKING_DELTA,
    THINKING_FINISHED,
    TODO_UPDATED,
    TOOL_FINISHED,
    TOOL_STARTED,
    TURN_STARTED,
    make_event,
)
from open_somnia.app_service.interaction_service import InteractionService
from open_somnia.app_service.models import TurnHandle, TurnRunResult
from open_somnia.config.settings import _materialize_provider
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import decode_embedded_user_message, normalize_tool_result_content_blocks, render_text_content
from open_somnia.runtime.session import AgentSession

_MISSING = object()


def _is_lead_actor(actor: Any) -> bool:
    return str(actor or "").strip() in {"", "lead"}


def _combine_user_inputs(inputs: list[str | dict[str, Any]]) -> str | dict[str, Any] | None:
    if not inputs:
        return None
    if len(inputs) == 1:
        return _clone_value(inputs[0])
    if all(isinstance(item, str) for item in inputs):
        return "\n\n".join(str(item).strip() for item in inputs if str(item).strip())
    combined_content: list[Any] = []
    fallback_parts: list[str] = []
    for item in inputs:
        if isinstance(item, str):
            text = item.strip()
            if text:
                combined_content.append({"type": "text", "text": text})
            continue
        content = item.get("content")
        if isinstance(content, list):
            combined_content.extend(_clone_value(content))
            continue
        text = render_text_content(content)
        if text:
            combined_content.append({"type": "text", "text": text})
            continue
        fallback_parts.append(_user_input_text(item))
    for part in fallback_parts:
        text = part.strip()
        if text:
            combined_content.append({"type": "text", "text": text})
    return {"role": "user", "content": combined_content}


def _clone_value(value: Any) -> Any:
    try:
        return deepcopy(value)
    except Exception:
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return value


def _context_usage_payload(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    used_tokens = int(getattr(usage, "used_tokens", 0) or 0)
    max_tokens = getattr(usage, "max_tokens", None)
    usage_percent = getattr(usage, "usage_percent", None)
    return {
        "used_tokens": used_tokens,
        "max_tokens": int(max_tokens) if max_tokens else None,
        "usage_percent": float(usage_percent) if usage_percent is not None else None,
        "counter_name": str(getattr(usage, "counter_name", "") or "estimate"),
    }


def _session_snapshot(session: AgentSession, *, context_window_usage: dict[str, Any] | None = None) -> dict[str, Any]:
    if callable(getattr(session, "to_payload", None)):
        payload = _clone_value(session.to_payload())
    else:
        payload = {"id": getattr(session, "id", None)}
    if context_window_usage is not None:
        payload["context_window_usage"] = _clone_value(context_window_usage)
    return payload


def _open_todo_count(session: AgentSession) -> int:
    count = 0
    for item in list(getattr(session, "todo_items", []) or []):
        status = str(item.get("status", "pending")).strip().lower()
        if status in {"pending", "in_progress"}:
            count += 1
    return count


def _user_input_text(user_input: str | dict[str, Any]) -> str:
    if isinstance(user_input, dict):
        return render_text_content(user_input.get("content", ""))
    decoded = decode_embedded_user_message(user_input)
    if decoded is not None:
        return render_text_content(decoded.get("content", ""))
    return str(user_input)


@dataclass(slots=True)
class _ActiveTurn:
    id: str
    runtime: OpenAgentRuntime | None
    session: AgentSession
    user_input: str | dict[str, Any]
    event_queue: Queue
    done_event: Event
    interrupt_event: Event
    handle: TurnHandle
    thread: Thread | None = None
    last_todo_items: list[dict[str, Any]] = field(default_factory=list)
    take_next_loop_user_message: Any = None
    prepare_next_loop_user_message: Any = None
    loop_injection_lock: Lock = field(default_factory=Lock)
    pending_loop_injections: list[dict[str, Any]] = field(default_factory=list)
    ready_loop_injections: list[dict[str, Any]] = field(default_factory=list)
    accepted_loop_injection_ids: set[str] = field(default_factory=set)
    last_context_usage: dict[str, Any] | None = None


class RuntimeHost:
    MAX_ACTIVE_TURNS = 2

    def __init__(self, runtime: OpenAgentRuntime) -> None:
        self.runtime = runtime
        self.interaction_service = InteractionService(runtime, self._emit)
        self._state_lock = Lock()
        self._active_turns: dict[str, _ActiveTurn] = {}
        self._primary_runtime_in_use = False
        self._turn_runtime_cache: dict[tuple[str, str], OpenAgentRuntime] = {}

    def peek_turn_runtime(self, session: AgentSession) -> OpenAgentRuntime:
        """The runtime whose provider matches this session's effective model.

        Read-only counterpart of ``_new_turn_runtime``: it never marks the
        primary runtime as in use and never consults the busy-fallback branch.
        Pinned sessions run turns on a cached per-pair runtime; read paths
        (e.g. context-window usage) must consult the same runtime, otherwise
        they compute with the workspace default model's provider and report
        its context window instead of the pinned model's.
        """
        override_provider = str(getattr(session, "provider_override", "") or "").strip().lower()
        override_model = str(getattr(session, "model_override", "") or "").strip()
        if override_provider and override_model:
            settings = deepcopy(self.runtime.settings)
            profile = settings.provider_profiles.get(override_provider)
            if profile is not None and override_model in profile.models:
                settings.provider = _materialize_provider(profile, override_model)
                return self._cached_turn_runtime(override_provider, override_model, settings)
        return self.runtime

    def _new_turn_runtime(self, session: AgentSession) -> OpenAgentRuntime:
        override_provider = str(getattr(session, "provider_override", "") or "").strip().lower()
        override_model = str(getattr(session, "model_override", "") or "").strip()
        if override_provider and override_model:
            # A session pinned to its own model must never share the primary
            # runtime: pinning and global switches both mutate runtimes, and a
            # copied-settings runtime keeps this turn isolated from either.
            # Fresh runtimes are expensive to build (provider SDK + SSL
            # initialization), so they are cached per (provider, model) and
            # reused across turns instead of rebuilt every time.
            settings = deepcopy(self.runtime.settings)
            profile = settings.provider_profiles.get(override_provider)
            if profile is not None and override_model in profile.models:
                settings.provider = _materialize_provider(profile, override_model)
                return self._cached_turn_runtime(override_provider, override_model, settings)
        with self._state_lock:
            if not self._primary_runtime_in_use:
                self._primary_runtime_in_use = True
                return self.runtime
        provider_name = str(getattr(self.runtime.settings.provider, "name", "")).strip().lower()
        model_name = str(getattr(self.runtime.settings.provider, "model", "")).strip()
        return self._cached_turn_runtime(provider_name, model_name, self.runtime.settings)

    def invalidate_turn_runtime(self, provider_name: str, model: str) -> None:
        """Drop the cached turn runtime for one (provider, model) pair.

        Cached runtimes snapshot their settings at build time, so a traits
        change (e.g. reasoning level) never reaches them. Callers that mutate
        a model's traits must invalidate the pair or pinned sessions keep
        running on the stale runtime until the sidecar restarts. The primary
        runtime is never cached, so every dropped entry is safe to close.
        """
        cache_key = (str(provider_name).strip().lower(), str(model).strip().lower())
        with self._state_lock:
            cached = self._turn_runtime_cache.pop(cache_key, None)
        if cached is not None and cached is not self.runtime:
            cached.close()

    def _cached_turn_runtime(self, provider_name: str, model_name: str, settings: Any) -> OpenAgentRuntime:
        cache_key = (provider_name, model_name)
        with self._state_lock:
            cached = self._turn_runtime_cache.get(cache_key)
            if cached is not None:
                return cached
        # Construction is slow (provider SDK import + SSL context); never hold
        # the state lock while building, or concurrent turn starts would stall.
        runtime = self._fresh_turn_runtime(settings)
        with self._state_lock:
            existing = self._turn_runtime_cache.get(cache_key)
            if existing is not None:
                runtime.close()
                return existing
            self._turn_runtime_cache[cache_key] = runtime
            return runtime

    def _fresh_turn_runtime(self, settings: Any) -> OpenAgentRuntime:
        runtime = OpenAgentRuntime(settings)
        runtime.execution_mode = getattr(self.runtime, "execution_mode", getattr(runtime, "execution_mode", None))
        return runtime

    def close(self) -> None:
        with self._state_lock:
            active_turns = list(self._active_turns.values())
        for active_turn in active_turns:
            active_turn.interrupt_event.set()
            runtime = active_turn.runtime
            if runtime is not None and runtime is not self.runtime:
                runtime.close()
        with self._state_lock:
            for runtime in self._turn_runtime_cache.values():
                if runtime is not self.runtime:
                    runtime.close()
            self._turn_runtime_cache.clear()

    def run_turn(
        self,
        session: AgentSession,
        user_input: str | dict[str, Any],
        *,
        take_next_loop_user_message=None,
        prepare_next_loop_user_message=None,
    ) -> TurnHandle:
        with self._state_lock:
            active_turns = [turn for turn in self._active_turns.values() if not turn.done_event.is_set()]
            if len(active_turns) >= self.MAX_ACTIVE_TURNS:
                raise RuntimeError("This project already has two turns running.")
            if any(turn.session.id == session.id for turn in active_turns):
                raise RuntimeError("This session already has a turn running.")
            turn_id = uuid.uuid4().hex[:8]
            event_queue: Queue = Queue()
            done_event = Event()
            interrupt_event = Event()
            handle = TurnHandle(
                turn_id=turn_id,
                session=session,
                event_queue=event_queue,
                done_event=done_event,
            )
            active_turn = _ActiveTurn(
                id=turn_id,
                runtime=None,
                session=session,
                user_input=user_input,
                event_queue=event_queue,
                done_event=done_event,
                interrupt_event=interrupt_event,
                handle=handle,
                last_todo_items=_clone_value(list(getattr(session, "todo_items", []) or [])),
                take_next_loop_user_message=take_next_loop_user_message,
                prepare_next_loop_user_message=prepare_next_loop_user_message,
            )
            if take_next_loop_user_message is None:
                active_turn.take_next_loop_user_message = lambda: self._take_next_loop_user_message(active_turn)
            if prepare_next_loop_user_message is None:
                active_turn.prepare_next_loop_user_message = lambda: self._prepare_next_loop_user_message(active_turn)
            worker = Thread(
                target=self._run_turn_worker,
                args=(active_turn,),
                name=f"open-somnia-app-turn-{turn_id}",
                daemon=True,
            )
            active_turn.thread = worker
            self._active_turns[turn_id] = active_turn
        worker.start()
        return handle

    def interrupt_turn(self, turn_id: str) -> bool:
        with self._state_lock:
            active_turn = self._active_turns.get(turn_id)
            if active_turn is None or active_turn.done_event.is_set():
                return False
            if active_turn.interrupt_event.is_set():
                return False
            active_turn.interrupt_event.set()
        self._emit_for_turn(active_turn, INTERRUPT_REQUESTED, reason="Interrupted by user.")
        interrupter = getattr(self.runtime, "interrupt_active_teammates", None)
        if callable(interrupter):
            try:
                interrupter(reason="lead_interrupt")
            except Exception:
                pass
        self.interaction_service.cancel_turn_requests(turn_id, reason="Interrupted by user.")
        return True

    def queue_loop_injection(self, turn_id: str, user_input: str | dict[str, Any], *, injection_id: str | None = None) -> bool:
        with self._state_lock:
            active_turn = self._active_turns.get(str(turn_id).strip())
            if active_turn is None or active_turn.done_event.is_set():
                return False
        normalized_injection_id = str(injection_id or uuid.uuid4().hex[:8]).strip()
        with active_turn.loop_injection_lock:
            if normalized_injection_id in active_turn.accepted_loop_injection_ids:
                return True
            active_turn.accepted_loop_injection_ids.add(normalized_injection_id)
            injection = {
                "id": normalized_injection_id,
                "user_input": _clone_value(user_input),
            }
            active_turn.pending_loop_injections.append(injection)
        return True

    def cancel_loop_injection(self, turn_id: str, injection_id: str) -> bool:
        with self._state_lock:
            active_turn = self._active_turns.get(str(turn_id).strip())
            if active_turn is None or active_turn.done_event.is_set():
                return False
        normalized_injection_id = str(injection_id or "").strip()
        if not normalized_injection_id:
            return False
        with active_turn.loop_injection_lock:
            before = len(active_turn.pending_loop_injections) + len(active_turn.ready_loop_injections)
            active_turn.pending_loop_injections = [
                injection
                for injection in active_turn.pending_loop_injections
                if str(injection.get("id", "")) != normalized_injection_id
            ]
            active_turn.ready_loop_injections = [
                injection
                for injection in active_turn.ready_loop_injections
                if str(injection.get("id", "")) != normalized_injection_id
            ]
            remaining = len(active_turn.pending_loop_injections) + len(active_turn.ready_loop_injections)
            if remaining == before:
                # Already drained into the running agent loop, or never queued.
                return False
            active_turn.accepted_loop_injection_ids.discard(normalized_injection_id)
            return True

    def _prepare_next_loop_user_message(self, active_turn: _ActiveTurn) -> bool:
        with active_turn.loop_injection_lock:
            if active_turn.ready_loop_injections:
                return True
            if not active_turn.pending_loop_injections:
                return False
            active_turn.ready_loop_injections.extend(active_turn.pending_loop_injections)
            active_turn.pending_loop_injections = []
            return True

    def _take_next_loop_user_message(self, active_turn: _ActiveTurn) -> str | dict[str, Any] | None:
        with active_turn.loop_injection_lock:
            if not active_turn.ready_loop_injections:
                return None
            injections = active_turn.ready_loop_injections
            active_turn.ready_loop_injections = []
        user_inputs = [_clone_value(injection.get("user_input")) for injection in injections]
        combined_user_input = _combine_user_inputs(user_inputs)
        for injection, user_input in zip(injections, user_inputs, strict=False):
            self._emit_for_turn(
                active_turn,
                LOOP_USER_MESSAGE_INJECTED,
                injection_id=str(injection.get("id", "")),
                user_input=_clone_value(user_input),
                text=_user_input_text(user_input),
            )
        return combined_user_input

    def _emit(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        **payload: Any,
    ) -> None:
        with self._state_lock:
            active_turn = self._active_turns.get(turn_id or "")
            if active_turn is None and len(self._active_turns) == 1:
                active_turn = next(iter(self._active_turns.values()))
        if active_turn is None:
            return
        if turn_id is not None and active_turn.id != turn_id:
            return
        self._emit_for_turn(active_turn, event_type, session_id=session_id, turn_id=turn_id, **payload)

    def _emit_for_turn(
        self,
        active_turn: _ActiveTurn,
        event_type: str,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        **payload: Any,
    ) -> None:
        event = make_event(
            event_type,
            session_id=session_id or active_turn.session.id,
            turn_id=turn_id or active_turn.id,
            **payload,
        )
        active_turn.event_queue.put(event)

    def _emit_todo_if_changed(self, active_turn: _ActiveTurn) -> None:
        current_items = _clone_value(list(getattr(active_turn.session, "todo_items", []) or []))
        if current_items == active_turn.last_todo_items:
            return
        active_turn.last_todo_items = current_items
        self._emit_for_turn(active_turn, TODO_UPDATED, items=current_items)

    def _emit_context_usage_if_changed(self, active_turn: _ActiveTurn, usage: Any) -> None:
        payload = _context_usage_payload(usage)
        if payload is None or payload == active_turn.last_context_usage:
            return
        active_turn.last_context_usage = _clone_value(payload)
        self._emit_for_turn(active_turn, CONTEXT_USAGE_UPDATED, context_window_usage=payload)

    def _latest_context_usage_payload(self, active_turn: _ActiveTurn) -> dict[str, Any] | None:
        if active_turn.last_context_usage is not None:
            return _clone_value(active_turn.last_context_usage)
        getter = getattr(active_turn.runtime, "recent_context_window_usage", None)
        if not callable(getter):
            return None
        try:
            payload = _context_usage_payload(getter(active_turn.session))
        except Exception:
            return None
        if payload is not None:
            active_turn.last_context_usage = _clone_value(payload)
        return payload

    @contextmanager
    def _patched_context_usage_events(self, active_turn: _ActiveTurn) -> Iterator[None]:
        original_context_window_usage = getattr(active_turn.runtime, "context_window_usage", None)
        if not callable(original_context_window_usage):
            yield
            return

        def wrapped_context_window_usage(session: AgentSession, *args: Any, **kwargs: Any) -> Any:
            usage = original_context_window_usage(session, *args, **kwargs)
            if getattr(session, "id", None) == active_turn.session.id:
                self._emit_context_usage_if_changed(active_turn, usage)
            return usage

        active_turn.runtime.context_window_usage = wrapped_context_window_usage
        try:
            yield
        finally:
            active_turn.runtime.context_window_usage = original_context_window_usage

    @contextmanager
    def _patched_tool_logging(self, active_turn: _ActiveTurn) -> Iterator[None]:
        original_print_tool_started = active_turn.runtime.print_tool_started
        original_print_tool_event = active_turn.runtime.print_tool_event
        original_print_tool_finished = getattr(active_turn.runtime, "print_tool_finished", None)
        renderer = active_turn.runtime._tool_event_renderer()

        def wrapped_print_tool_started(
            actor: str,
            tool_name: str,
            tool_input: dict[str, Any],
            *,
            tool_call_id: str | None = None,
        ) -> None:
            if _is_lead_actor(actor):
                self._emit_for_turn(
                    active_turn,
                    TOOL_STARTED,
                    actor=actor,
                    tool_name=tool_name,
                    tool_input=_clone_value(tool_input),
                    tool_call_id=tool_call_id,
                    trace_id=f"{active_turn.session.id}-{active_turn.session.latest_turn_id}",
                    rendered_lines=renderer.render_tool_started_lines(tool_name, tool_input),
                )

        def wrapped_print_tool_finished(
            actor: str,
            tool_name: str,
            tool_input: dict[str, Any],
            *,
            tool_call_id: str | None = None,
        ) -> None:
            # Emits a lightweight TOOL_FINISHED marker for paths that bypass
            # ``print_tool_event``'s full result rendering -- specifically the
            # parallel Explore-subagent path, which pre-fires TOOL_STARTED but
            # needs a matching finish so the frontend clears the active
            # subagent card keyed by ``tool_call_id``.
            if _is_lead_actor(actor):
                self._emit_for_turn(
                    active_turn,
                    TOOL_FINISHED,
                    actor=actor,
                    tool_name=tool_name,
                    tool_input=_clone_value(tool_input),
                    tool_call_id=tool_call_id,
                    trace_id=f"{active_turn.session.id}-{active_turn.session.latest_turn_id}",
                )

        def wrapped_print_tool_event(actor: str, tool_name: str, tool_input: dict[str, Any], output: Any) -> str:
            category = "MCP" if tool_name.startswith("mcp__") else "TOOL"
            content_blocks = normalize_tool_result_content_blocks(output.get("tool_result_content")) if isinstance(output, dict) else []
            log_entry = active_turn.runtime.tool_log_store.write(
                actor=actor,
                tool_name=tool_name,
                tool_input=tool_input,
                output=output,
                category=category,
            )
            if _is_lead_actor(actor):
                self._emit_for_turn(
                    active_turn,
                    TOOL_FINISHED,
                    actor=actor,
                    tool_name=tool_name,
                    tool_input=_clone_value(tool_input),
                    output=_clone_value(output),
                    content_blocks=_clone_value(content_blocks),
                    log_id=log_entry["id"],
                    category=category,
                    rendered_lines=renderer.render_tool_event_lines(
                        tool_name,
                        tool_input,
                        output,
                        log_id=log_entry["id"],
                    ),
                )
            if tool_name == "TodoWrite":
                self._emit_todo_if_changed(active_turn)
            return log_entry["id"]

        active_turn.runtime.print_tool_started = wrapped_print_tool_started
        active_turn.runtime.print_tool_event = wrapped_print_tool_event
        active_turn.runtime.print_tool_finished = wrapped_print_tool_finished
        try:
            yield
        finally:
            active_turn.runtime.print_tool_started = original_print_tool_started
            active_turn.runtime.print_tool_event = original_print_tool_event
            if original_print_tool_finished is not None:
                active_turn.runtime.print_tool_finished = original_print_tool_finished
            else:
                try:
                    delattr(active_turn.runtime, "print_tool_finished")
                except AttributeError:
                    pass

    @contextmanager
    def _patched_subagent_activity(self, active_turn: _ActiveTurn) -> Iterator[None]:
        original_handler = getattr(active_turn.runtime, "subagent_activity_handler", _MISSING)

        def emit_subagent_activity(payload: dict[str, Any]) -> None:
            if not isinstance(payload, dict):
                return
            self._emit_for_turn(active_turn, SUBAGENT_ACTIVITY, **_clone_value(payload))

        active_turn.runtime.subagent_activity_handler = emit_subagent_activity
        try:
            yield
        finally:
            if original_handler is _MISSING:
                try:
                    delattr(active_turn.runtime, "subagent_activity_handler")
                except AttributeError:
                    pass
            else:
                active_turn.runtime.subagent_activity_handler = original_handler

    def _run_turn_worker(self, active_turn: _ActiveTurn) -> None:
        turn_result: TurnRunResult | None = None
        # Runtime construction (provider SDK, SSL context) is expensive and used
        # to block the HTTP turn request for seconds. Build it here instead so
        # the turn starts immediately and the composer stays usable.
        try:
            runtime = self._new_turn_runtime(active_turn.session)
        except Exception as exc:
            self._emit_for_turn(
                active_turn,
                ERROR,
                message=str(exc),
                exception_type=type(exc).__name__,
            )
            active_turn.handle._set_result(
                TurnRunResult(
                    session=active_turn.session,
                    text="",
                    status="failed",
                    open_todo_count=_open_todo_count(active_turn.session),
                    error=str(exc),
                    error_kind=getattr(exc, "kind", None),
                )
            )
            active_turn.done_event.set()
            with self._state_lock:
                self._active_turns.pop(active_turn.id, None)
            return
        with self._state_lock:
            active_turn.runtime = runtime
        self._emit_for_turn(
            active_turn,
            TURN_STARTED,
            user_input=_clone_value(active_turn.user_input),
            text=_user_input_text(active_turn.user_input),
        )
        try:
            with self.interaction_service.bind_turn(session_id=active_turn.session.id, turn_id=active_turn.id, runtime=runtime):
                with (
                    self._patched_context_usage_events(active_turn),
                    self._patched_tool_logging(active_turn),
                    self._patched_subagent_activity(active_turn),
                ):
                    run_turn = getattr(active_turn.runtime, "run_turn")
                    turn_kwargs = {
                        "text_callback": lambda text: self._emit_for_turn(active_turn, ASSISTANT_DELTA, delta=text),
                        "thinking_callback": lambda payload: self._emit_for_turn(
                            active_turn,
                            THINKING_FINISHED if str(payload.get("event", "")).strip() == "finished" else THINKING_DELTA,
                            **_clone_value(payload),
                        ),
                        "should_interrupt": active_turn.interrupt_event.is_set,
                        "take_next_loop_user_message": active_turn.take_next_loop_user_message,
                        "prepare_next_loop_user_message": active_turn.prepare_next_loop_user_message,
                    }
                    try:
                        run_turn_parameters = inspect.signature(run_turn).parameters
                    except (TypeError, ValueError):
                        run_turn_parameters = {}
                    accepts_var_kwargs = any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in run_turn_parameters.values()
                    )
                    turn_kwargs = {
                        key: value
                        for key, value in turn_kwargs.items()
                        if key in run_turn_parameters or accepts_var_kwargs
                    }
                    response = run_turn(active_turn.session, active_turn.user_input, **turn_kwargs)
            turn_result = TurnRunResult(
                session=active_turn.session,
                text=str(response),
                status=str(getattr(response, "status", "")).strip() or "completed",
                open_todo_count=int(getattr(response, "open_todo_count", _open_todo_count(active_turn.session)) or 0),
            )
            self._emit_for_turn(
                active_turn,
                ASSISTANT_COMPLETED,
                text=turn_result.text,
                status=turn_result.status,
                open_todo_count=turn_result.open_todo_count,
            )
        except TurnInterrupted:
            turn_result = TurnRunResult(
                session=active_turn.session,
                text="",
                status="interrupted",
                open_todo_count=_open_todo_count(active_turn.session),
                interrupted=True,
            )
            self._emit_for_turn(
                active_turn,
                INTERRUPT_COMPLETED,
                open_todo_count=turn_result.open_todo_count,
            )
        except Exception as exc:
            turn_result = TurnRunResult(
                session=active_turn.session,
                text="",
                status="failed",
                open_todo_count=_open_todo_count(active_turn.session),
                error=str(exc),
                error_kind=getattr(exc, "kind", None),
            )
            self._emit_for_turn(
                active_turn,
                ERROR,
                message=str(exc),
                exception_type=type(exc).__name__,
            )
        finally:
            self._emit_todo_if_changed(active_turn)
            self._emit_for_turn(
                active_turn,
                SESSION_UPDATED,
                session=_session_snapshot(
                    active_turn.session,
                    context_window_usage=self._latest_context_usage_payload(active_turn),
                ),
            )
            # An agent-approved session swap (request_new_session) is consumed
            # here, on the runtime that ran the turn, and surfaced as an event
            # so the session driver (REPL worker / desktop UI) can swap in the
            # fresh session and run the handoff.
            if active_turn.runtime is not None:
                pending_handoff = active_turn.runtime.consume_pending_new_session(active_turn.session.id)
                if pending_handoff is not None:
                    self._emit_for_turn(active_turn, NEW_SESSION_APPROVED, handoff=pending_handoff)
            if turn_result is None:
                turn_result = TurnRunResult(
                    session=active_turn.session,
                    text="",
                    status="failed",
                    open_todo_count=_open_todo_count(active_turn.session),
                    error="Turn finished without a result.",
                )
            active_turn.handle._set_result(turn_result)
            active_turn.done_event.set()
            with self._state_lock:
                self._active_turns.pop(active_turn.id, None)
                if active_turn.runtime is self.runtime:
                    self._primary_runtime_in_use = False
                else:
                    active_turn.runtime.close()
