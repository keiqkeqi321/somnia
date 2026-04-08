from __future__ import annotations

import json
import threading
import time

from openagent.runtime.events import ToolExecutionContext
from openagent.runtime.messages import make_tool_result_message
from openagent.storage.common import now_ts
from openagent.tools.registry import ToolRegistry

UNSET = object()


class TeammateRuntimeManager:
    ACTIVE_STATUSES = {"starting", "working", "idle"}

    def __init__(self, runtime, team_store, bus, task_store, request_tracker):
        self.runtime = runtime
        self.team_store = team_store
        self.bus = bus
        self.task_store = task_store
        self.request_tracker = request_tracker
        self.threads: dict[str, threading.Thread] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._stop_reasons: dict[str, str] = {}
        self._lock = threading.RLock()
        self._restore_state()

    def _restore_state(self) -> None:
        resume_specs: list[tuple[str, str, str, list[dict]]] = []
        with self._lock:
            config = self.team_store.load()
            changed = False
            for member in config.get("members", []):
                if member.get("status") in {"starting", "working", "idle"}:
                    name = str(member.get("name", "")).strip()
                    role = str(member.get("role", "")).strip() or "teammate"
                    prompt, messages = self._restore_prompt_and_messages(name)
                    had_active_tool = bool(member.get("current_tool_name") or member.get("current_tool_log_id"))
                    if not name or not prompt:
                        member["status"] = "shutdown"
                        member["activity"] = "stale_on_boot"
                        member["shutdown_reason"] = "runtime_restarted"
                        member["last_transition_at"] = now_ts()
                        changed = True
                        continue
                    member["status"] = "starting"
                    member["activity"] = "restoring_on_boot"
                    member["shutdown_reason"] = None
                    member["last_transition_at"] = now_ts()
                    member["last_activity_at"] = now_ts()
                    if had_active_tool:
                        member["current_tool_name"] = None
                        member["current_tool_log_id"] = None
                    resume_specs.append((name, role, prompt, messages))
                    changed = True
            if changed:
                self.team_store.save(config)
        for name, role, prompt, messages in resume_specs:
            self._start_thread(name, role, prompt, initial_messages=messages, resumed=True)

    def _restore_prompt_and_messages(self, name: str) -> tuple[str | None, list[dict]]:
        prompt: str | None = None
        messages: list[dict] = []
        for entry in self.team_store.read_log(name):
            entry_type = entry.get("type")
            if entry_type == "session_started":
                value = entry.get("prompt")
                if isinstance(value, str) and value.strip():
                    prompt = value
                continue
            if entry_type == "user_message":
                content = entry.get("content")
                source = entry.get("source")
                if source == "prompt" and isinstance(content, str) and content.strip():
                    prompt = prompt or content
                if isinstance(content, str):
                    messages.append({"role": "user", "content": content})
                elif content is not None:
                    messages.append({"role": "user", "content": json.dumps(content, ensure_ascii=False)})
                continue
            if entry_type == "assistant_message":
                messages.append({"role": "assistant", "content": entry.get("content")})
                continue
            if entry_type == "tool_result_message":
                messages.append({"role": "user", "content": entry.get("content", [])})
        if not messages and prompt:
            messages = [{"role": "user", "content": prompt}]
        return prompt, messages

    def _load(self) -> dict:
        with self._lock:
            return self.team_store.load()

    def _save(self, payload: dict) -> None:
        with self._lock:
            self.team_store.save(payload)

    def _find(self, name: str) -> dict | None:
        with self._lock:
            config = self.team_store.load()
            for member in config.get("members", []):
                if member.get("name") == name:
                    return dict(member)
            return None

    def _upsert_member(self, name: str, role: str, status: str, activity: str) -> None:
        ts = now_ts()
        with self._lock:
            config = self.team_store.load()
            for member in config.get("members", []):
                if member.get("name") == name:
                    member["role"] = role
                    member["status"] = status
                    member["activity"] = activity
                    member["last_transition_at"] = ts
                    member["last_activity_at"] = ts
                    member["shutdown_reason"] = None
                    member["current_task_id"] = None
                    member["last_error"] = None
                    member["current_tool_name"] = None
                    member["current_tool_log_id"] = None
                    self.team_store.save(config)
                    return
            config.setdefault("members", []).append(
                {
                    "name": name,
                    "role": role,
                    "status": status,
                    "activity": activity,
                    "last_transition_at": ts,
                    "last_activity_at": ts,
                    "shutdown_reason": None,
                    "current_task_id": None,
                    "last_error": None,
                    "current_tool_name": None,
                    "current_tool_log_id": None,
                }
            )
            self.team_store.save(config)

    def _update_member(
        self,
        name: str,
        *,
        status: str | None = None,
        activity: str | None = None,
        shutdown_reason: str | None = None,
        current_task_id: int | None | object = UNSET,
        current_tool_name: str | None | object = UNSET,
        current_tool_log_id: str | None | object = UNSET,
        last_error: str | None = None,
        touch_activity: bool = True,
    ) -> None:
        with self._lock:
            config = self.team_store.load()
            for member in config.get("members", []):
                if member.get("name") == name:
                    if status is not None and member.get("status") != status:
                        member["status"] = status
                        member["last_transition_at"] = now_ts()
                    if activity is not None:
                        member["activity"] = activity
                    if shutdown_reason is not None or status == "shutdown":
                        member["shutdown_reason"] = shutdown_reason
                    if current_task_id is not UNSET:
                        member["current_task_id"] = current_task_id
                    if current_tool_name is not UNSET:
                        member["current_tool_name"] = current_tool_name
                    elif activity is not None:
                        if str(activity).startswith("running_tool:"):
                            member["current_tool_name"] = str(activity).split(":", 1)[1]
                        else:
                            member["current_tool_name"] = None
                    if current_tool_log_id is not UNSET:
                        member["current_tool_log_id"] = current_tool_log_id
                    elif activity is not None and not str(activity).startswith("running_tool:"):
                        member["current_tool_log_id"] = None
                    if last_error is not None:
                        member["last_error"] = last_error
                    if touch_activity:
                        member["last_activity_at"] = now_ts()
                    self.team_store.save(config)
                    return

    def spawn(self, name: str, role: str, prompt: str) -> str:
        member = self._find(name)
        if member and member.get("status") not in {"idle", "shutdown"}:
            return f"Error: '{name}' is currently {member['status']}"
        self._upsert_member(name, role, "starting", "booting")
        self.team_store.reset_log(
            name,
            {
                "type": "session_started",
                "timestamp": now_ts(),
                "name": name,
                "role": role,
                "prompt": prompt,
            },
        )
        self._start_thread(name, role, prompt)
        return f"Spawned '{name}' (role: {role})"

    def _start_thread(
        self,
        name: str,
        role: str,
        prompt: str,
        *,
        initial_messages: list[dict] | None = None,
        resumed: bool = False,
    ) -> None:
        self._reset_stop_request(name)
        thread = threading.Thread(target=self._loop, args=(name, role, prompt, initial_messages, resumed), daemon=True)
        thread.start()
        self.threads[name] = thread

    def _reset_stop_request(self, name: str) -> threading.Event:
        with self._lock:
            event = self._stop_events.get(name)
            if event is None:
                event = threading.Event()
                self._stop_events[name] = event
            else:
                event.clear()
            self._stop_reasons.pop(name, None)
            return event

    def _request_stop(self, name: str, reason: str) -> None:
        with self._lock:
            event = self._stop_events.get(name)
            if event is None:
                event = threading.Event()
                self._stop_events[name] = event
            self._stop_reasons[name] = reason
            event.set()

    def _stop_reason(self, name: str) -> str | None:
        with self._lock:
            event = self._stop_events.get(name)
            if event is None or not event.is_set():
                return None
            return self._stop_reasons.get(name, "interrupt_requested")

    def _shutdown_if_stop_requested(self, name: str, activity: str = "interrupt_requested") -> bool:
        reason = self._stop_reason(name)
        if reason is None:
            return False
        self._update_member(
            name,
            status="shutdown",
            activity=activity,
            shutdown_reason=reason,
            current_task_id=None,
        )
        return True

    def interrupt_active(self, reason: str = "lead_interrupt") -> int:
        self._refresh_thread_health()
        count = 0
        config = self._load()
        for member in config.get("members", []):
            name = str(member.get("name", "")).strip()
            if not name:
                continue
            thread = self.threads.get(name)
            if thread is None or not thread.is_alive():
                continue
            if member.get("status") == "shutdown":
                continue
            self._request_stop(name, reason)
            self._update_member(name, activity="interrupt_requested")
            count += 1
        return count

    def _loop(
        self,
        name: str,
        role: str,
        prompt: str,
        initial_messages: list[dict] | None = None,
        resumed: bool = False,
    ) -> None:
        messages = list(initial_messages) if initial_messages else [{"role": "user", "content": prompt}]
        registry = ToolRegistry()
        self.runtime.register_worker_tools(registry)
        system_prompt = self.runtime.build_system_prompt(actor=name, role=role)
        stop_event = self._reset_stop_request(name)
        self._update_member(name, status="working", activity="starting_work_loop")
        if resumed:
            self._append_log(name, "session_resumed", {"reason": "runtime_restore", "message_count": len(messages)})
        else:
            self._append_log(name, "user_message", {"content": prompt, "source": "prompt"})

        try:
            while True:
                if self._shutdown_if_stop_requested(name):
                    return
                for _ in range(self.runtime.settings.runtime.max_agent_rounds):
                    if self._shutdown_if_stop_requested(name):
                        return
                    self._update_member(name, status="working", activity="checking_inbox")
                    inbox = self.bus.read_inbox(name)
                    for message in inbox:
                        if self._handle_control_message(name, message):
                            return
                        messages.append({"role": "user", "content": json.dumps(message, ensure_ascii=False)})
                        self._append_log(name, "user_message", {"content": message, "source": "inbox"})

                    self._update_member(name, status="working", activity="waiting_for_model")
                    turn = self.runtime.complete(
                        system_prompt,
                        messages,
                        registry.schemas(),
                        should_interrupt=lambda: self._stop_reason(name) is not None,
                    )
                    if self._shutdown_if_stop_requested(name, activity="interrupted_after_model"):
                        return
                    assistant_message = turn.as_message()
                    messages.append(assistant_message)
                    self._append_log(name, "assistant_message", {"content": assistant_message.get("content")})
                    if not turn.has_tool_calls():
                        break
                    ctx = ToolExecutionContext(runtime=self.runtime, session=None, actor=name, trace_id=f"{name}-{int(time.time())}")
                    tool_results: list[dict] = []
                    idle_requested = False
                    for tool_call in turn.tool_calls:
                        if self._shutdown_if_stop_requested(name, activity="interrupted_before_tool"):
                            return
                        if tool_call.name == "idle":
                            idle_requested = True
                            self._update_member(name, status="working", activity="preparing_for_idle")
                            output = "Entering idle phase."
                        else:
                            self._update_member(name, status="working", activity=f"running_tool:{tool_call.name}")
                            try:
                                output = registry.execute(ctx, tool_call.name, tool_call.input)
                                if tool_call.name == "claim_task":
                                    task_id = int(tool_call.input["task_id"])
                                    self._update_member(name, current_task_id=task_id)
                            except Exception as exc:
                                output = f"Error: {exc}"
                                self._update_member(name, last_error=str(exc))
                        log_id = self.runtime.print_tool_event(name, tool_call.name, tool_call.input, output)
                        self._update_member(name, current_tool_log_id=log_id)
                        self._append_log(
                            name,
                            "tool_call",
                            {
                                "tool_name": tool_call.name,
                                "tool_input": tool_call.input,
                                "output_preview": self.runtime._compact_preview(str(output), limit=120),
                                "tool_log_id": log_id,
                            },
                        )
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_call_id": tool_call.id,
                                "content": str(output),
                            }
                        )
                    messages.append(make_tool_result_message(tool_results))
                    self._append_log(name, "tool_result_message", {"content": tool_results})
                    if idle_requested:
                        break

                initial_owned_open = []
                list_owned_open = getattr(self.task_store, "list_owned_open", None)
                if callable(list_owned_open):
                    initial_owned_open = list_owned_open(name) or []
                retained_task_id = initial_owned_open[0]["id"] if initial_owned_open else None
                initial_activity = "idle_waiting_on_owned_task" if initial_owned_open else "idle_polling"
                self._update_member(
                    name,
                    status="idle",
                    activity=initial_activity,
                    current_task_id=retained_task_id,
                )
                resume = False
                poll_total = max(self.runtime.settings.runtime.teammate_idle_timeout_seconds, 1)
                poll_interval = max(self.runtime.settings.runtime.teammate_poll_interval_seconds, 1)
                for _ in range(max(poll_total // poll_interval, 1)):
                    if stop_event.wait(poll_interval):
                        if self._shutdown_if_stop_requested(name):
                            return
                    self._update_member(name, status="idle", activity="idle_polling")
                    inbox = self.bus.read_inbox(name)
                    if inbox:
                        for message in inbox:
                            if self._handle_control_message(name, message):
                                return
                            messages.append({"role": "user", "content": json.dumps(message, ensure_ascii=False)})
                            self._append_log(name, "user_message", {"content": message, "source": "idle_inbox"})
                        self._update_member(name, status="working", activity="resuming_from_inbox")
                        resume = True
                        break
                    owned_open = []
                    has_open_task = False
                    list_owned_open = getattr(self.task_store, "list_owned_open", None)
                    if callable(list_owned_open):
                        owned_open = list_owned_open(name) or []
                        has_open_task = bool(owned_open)
                    else:
                        has_open_task = bool(getattr(self.task_store, "has_open_task", lambda owner: False)(name))
                    if has_open_task:
                        current_task_id = owned_open[0]["id"] if owned_open else member.get("current_task_id") if (member := self._find(name)) else None
                        self._update_member(name, status="idle", activity="idle_waiting_on_owned_task", current_task_id=current_task_id)
                        continue
                    list_claimable_for = getattr(self.task_store, "list_claimable_for", None)
                    if callable(list_claimable_for):
                        claimable = list_claimable_for(name)
                    else:
                        claimable = self.task_store.list_claimable()
                    if claimable:
                        task = claimable[0]
                        self.task_store.claim(task["id"], name)
                        self._update_member(name, status="working", activity="auto_claimed_task", current_task_id=task["id"])
                        messages.append(
                            {
                                "role": "user",
                                "content": f"<auto-claimed>Task #{task['id']}: {task['subject']}\n{task.get('description', '')}</auto-claimed>",
                            }
                        )
                        self._append_log(
                            name,
                            "user_message",
                            {
                                "content": f"Task #{task['id']}: {task['subject']}\n{task.get('description', '')}",
                                "source": "auto_claimed",
                            },
                        )
                        messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                        self._append_log(name, "assistant_message", {"content": f"Claimed task #{task['id']}. Working on it."})
                        resume = True
                        break
                if not resume:
                    self._update_member(
                        name,
                        status="shutdown",
                        activity="idle_timeout",
                        shutdown_reason="idle_timeout",
                        current_task_id=None,
                    )
                    return
                self._update_member(name, status="working", activity="resuming_work")
        except Exception as exc:
            if self._shutdown_if_stop_requested(name):
                return
            self._append_log(name, "runtime_error", {"error": str(exc)})
            self._update_member(
                name,
                status="shutdown",
                activity="runtime_error",
                shutdown_reason="runtime_error",
                current_task_id=None,
                last_error=str(exc),
            )
            return

    def _handle_control_message(self, name: str, message: dict) -> bool:
        if message.get("type") != "shutdown_request":
            return False
        request_id = message.get("request_id")
        self._request_stop(name, "shutdown_request")
        if request_id:
            self.request_tracker.mark_shutdown_response(request_id, "accepted")
            self.bus.send(name, "lead", "Shutting down.", "shutdown_response", {"request_id": request_id})
        self._update_member(
            name,
            status="shutdown",
            activity="shutdown_request",
            shutdown_reason="shutdown_request",
            current_task_id=None,
        )
        return True

    def _append_log(self, name: str, event_type: str, payload: dict) -> None:
        self.team_store.append_log(
            name,
            {
                "type": event_type,
                "timestamp": now_ts(),
                **payload,
            },
        )

    def _refresh_thread_health(self) -> None:
        with self._lock:
            config = self.team_store.load()
            changed = False
            for member in config.get("members", []):
                name = member.get("name")
                thread = self.threads.get(name or "")
                if thread is None:
                    continue
                if not thread.is_alive() and member.get("status") not in {"shutdown"}:
                    member["status"] = "shutdown"
                    member["activity"] = "thread_exited"
                    member["shutdown_reason"] = member.get("shutdown_reason") or "thread_exited"
                    member["last_transition_at"] = now_ts()
                    changed = True
            if changed:
                self.team_store.save(config)

    def _format_age(self, ts: float | None) -> str:
        if not ts:
            return "unknown"
        delta = max(int(now_ts() - ts), 0)
        if delta < 60:
            return f"{delta}s"
        minutes, seconds = divmod(delta, 60)
        if minutes < 60:
            return f"{minutes}m{seconds:02d}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h{minutes:02d}m"

    def list_all(self) -> str:
        self._refresh_thread_health()
        config = self._load()
        members = config.get("members", [])
        if not members:
            return "No teammates."
        lines = [f"Team: {config.get('team_name', 'default')}"]
        for member in members:
            lines.append("  " + self._format_member_summary(member))
        return "\n".join(lines)

    def member_names(self) -> list[str]:
        return [member["name"] for member in self._load().get("members", [])]

    def active_member_summaries(self) -> list[dict]:
        self._refresh_thread_health()
        members: list[dict] = []
        for member in self._load().get("members", []):
            if str(member.get("status", "")).strip() in self.ACTIVE_STATUSES:
                members.append(dict(member))
        return members

    def render_log(self, name: str) -> str:
        member = self._find(name)
        entries = self.team_store.read_log(name)
        if member is None and not entries:
            return f"Teammate '{name}' not found."
        lines = [f"[team log {name}]"]
        if member is not None:
            lines.extend(
                [
                    f"Role: {member.get('role', 'unknown')}",
                    f"Status: {member.get('status', 'unknown')}",
                    f"Activity: {self._format_activity(member.get('activity', 'unknown'))}",
                ]
            )
        if not entries:
            lines.append("No team log entries yet.")
            return "\n".join(lines)
        lines.append("Events:")
        for entry in entries:
            lines.extend(self._render_log_entry(entry))
        return "\n".join(lines)

    def _render_log_entry(self, entry: dict) -> list[str]:
        event_type = str(entry.get("type", "event"))
        if event_type == "session_started":
            return [
                f"- session started ({entry.get('role', 'unknown')})",
                f"  prompt: {self._compact_text(str(entry.get('prompt', '')))}",
            ]
        if event_type == "user_message":
            return [f"- user[{entry.get('source', 'message')}]: {self._compact_text(self._render_log_content(entry.get('content')))}"]
        if event_type == "assistant_message":
            return [f"- assistant: {self._compact_text(self._render_log_content(entry.get('content')))}"]
        if event_type == "tool_call":
            lines = [
                f"- tool {entry.get('tool_name', 'unknown')}: {self._compact_text(json.dumps(entry.get('tool_input', {}), ensure_ascii=False))}",
                f"  result: {self._compact_text(str(entry.get('output_preview', '(no output)')))}",
            ]
            tool_log_id = str(entry.get("tool_log_id", "")).strip()
            if tool_log_id:
                lines.append(f"  Tool log: /toollog {tool_log_id}")
            return lines
        if event_type == "runtime_error":
            return [f"- runtime_error: {self._compact_text(str(entry.get('error', 'unknown error')))}"]
        return [f"- {event_type}: {self._compact_text(json.dumps(entry, ensure_ascii=False))}"]

    def _render_log_content(self, content) -> str:
        if isinstance(content, (dict, list)):
            return json.dumps(content, ensure_ascii=False)
        return str(content)

    def _compact_text(self, text: str, limit: int = 180) -> str:
        compact = " ".join(str(text).split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    def _format_activity(self, activity: str) -> str:
        raw = str(activity or "unknown").strip()
        if raw.startswith("running_tool:"):
            return f"tool {raw.split(':', 1)[1]}"
        return raw.replace("_", " ")

    def _format_member_summary(self, member: dict) -> str:
        extras: list[str] = [self._format_activity(member.get("activity", "unknown"))]
        current_tool = str(member.get("current_tool_name", "")).strip()
        if current_tool and f"tool {current_tool}" not in extras:
            extras.append(f"tool {current_tool}")
        if member.get("current_task_id") is not None:
            extras.append(f"task #{member['current_task_id']}")
        if member.get("shutdown_reason"):
            extras.append(f"reason={member['shutdown_reason']}")
        last_seen = self._format_age(member.get("last_activity_at"))
        return (
            f"{member['name']} ({member['role']}): {member['status']} "
            f"[{', '.join(extras)}] last_seen={last_seen} View team logs: /teamlog {member['name']}"
        )
