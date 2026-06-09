from __future__ import annotations

import json
import threading
import time

from open_somnia.runtime.events import ToolExecutionContext
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import (
    consume_ephemeral_image_blocks,
    make_tool_result_item,
    make_tool_result_message,
)
from open_somnia.storage.common import now_ts
from open_somnia.tools.registry import ToolRegistry
from open_somnia.tools.tool_errors import (
    extract_transient_repair_hint,
    render_transient_repair_hint_message,
    sanitize_tool_output_for_persistence,
    serialize_tool_output,
    tool_error_from_exception,
)

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
        self._active_session_id: str | None = None

    def _member_key(self, name: str, session_id: str | None = None) -> str:
        normalized_name = str(name or "").strip()
        normalized_session_id = str(session_id or "").strip()
        return f"{normalized_session_id}\0{normalized_name}" if normalized_session_id else normalized_name

    def activate_session(self, session_id: str | None) -> None:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return
        with self._lock:
            if self._active_session_id == normalized_session_id:
                return
            self._active_session_id = normalized_session_id
        self._suspend_non_session_members(normalized_session_id)
        self._suspend_legacy_active_members(normalized_session_id)
        self._restore_state(normalized_session_id)

    def _restore_state(self, session_id: str) -> None:
        resume_specs: list[tuple[str, str, str, list[dict]]] = []
        with self._lock:
            config = self._load_for_session(session_id)
            changed = False
            for member in config.get("members", []):
                status = member.get("status")
                name = str(member.get("name", "")).strip()
                if member.get("session_id") != session_id:
                    continue
                should_restore_shutdown = (
                    status == "shutdown"
                    and member.get("shutdown_reason") == "idle_timeout"
                    and name
                    and self._has_owned_open_task(name, session_id=session_id)
                )
                should_restore_suspended = status == "suspended" and member.get("shutdown_reason") == "session_not_active"
                if status in {"starting", "working", "idle"} or should_restore_shutdown or should_restore_suspended:
                    role = str(member.get("role", "")).strip() or "teammate"
                    prompt, messages = self._restore_prompt_and_messages(name, session_id=session_id)
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
                    owned_open = self._owned_open_tasks(name, session_id=session_id)
                    if owned_open:
                        member["current_task_id"] = owned_open[0].get("id")
                    member["last_transition_at"] = now_ts()
                    member["last_activity_at"] = now_ts()
                    if had_active_tool:
                        member["current_tool_name"] = None
                        member["current_tool_log_id"] = None
                    resume_specs.append((name, role, prompt, messages))
                    changed = True
            if changed:
                self._save_for_session(config, session_id)
        for name, role, prompt, messages in resume_specs:
            self._start_thread_for_member(name, role, prompt, session_id=session_id, initial_messages=messages, resumed=True)

    def _suspend_non_session_members(self, session_id: str) -> None:
        with self._lock:
            config = self.team_store.load()
            for member in config.get("members", []):
                name = str(member.get("name", "")).strip()
                if not member.get("session_id"):
                    continue
                if member.get("session_id") == session_id:
                    continue
                if str(member.get("status", "")).strip() not in self.ACTIVE_STATUSES:
                    continue
                if name:
                    self._request_stop(name, "session_not_active", session_id=member.get("session_id"))
                member["status"] = "suspended"
                member["activity"] = "session_not_active"
                member["shutdown_reason"] = "session_not_active"
                member["last_transition_at"] = now_ts()
                self._save_member(member)

    def _suspend_legacy_active_members(self, session_id: str) -> None:
        with self._lock:
            config = self._load_legacy()
            changed = False
            for member in config.get("members", []):
                if member.get("session_id"):
                    continue
                if str(member.get("status", "")).strip() not in self.ACTIVE_STATUSES:
                    continue
                name = str(member.get("name", "")).strip()
                if name:
                    self._request_stop(name, "legacy_no_session", session_id=None)
                member["status"] = "suspended"
                member["activity"] = "legacy_no_session"
                member["shutdown_reason"] = "legacy_no_session"
                member["last_transition_at"] = now_ts()
                changed = True
            if changed:
                self.team_store.save(config)

    def _restore_prompt_and_messages(self, name: str, session_id: str | None = None) -> tuple[str | None, list[dict]]:
        prompt: str | None = None
        messages: list[dict] = []
        for entry in self._read_log(name, session_id=session_id):
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

    def _load_for_session(self, session_id: str | None) -> dict:
        with self._lock:
            try:
                return self.team_store.load(session_id=session_id)
            except TypeError:
                payload = self.team_store.load()
                normalized_session_id = str(session_id or "").strip()
                if not normalized_session_id:
                    return payload
                return {
                    "team_name": payload.get("team_name", "default"),
                    "members": [
                        member
                        for member in payload.get("members", [])
                        if member.get("session_id") == normalized_session_id
                    ],
                }

    def _load_legacy(self) -> dict:
        loader = getattr(self.team_store, "load_legacy", None)
        if callable(loader):
            return loader()
        payload = self.team_store.load()
        return {
            "team_name": payload.get("team_name", "default"),
            "members": [member for member in payload.get("members", []) if not member.get("session_id")],
        }

    def _save_for_session(self, payload: dict, session_id: str | None) -> None:
        with self._lock:
            try:
                self.team_store.save(payload, session_id=session_id)
            except TypeError:
                normalized_session_id = str(session_id or "").strip()
                if not normalized_session_id:
                    self.team_store.save(payload)
                    return
                replacement_members = [dict(member) for member in payload.get("members", [])]
                aggregate = dict(self.team_store.load())
                aggregate["members"] = [dict(member) for member in aggregate.get("members", [])]
                replacement_names = {member.get("name") for member in replacement_members}
                aggregate["members"] = [
                    member
                    for member in aggregate.get("members", [])
                    if not (member.get("session_id") == normalized_session_id and member.get("name") in replacement_names)
                ]
                aggregate.setdefault("members", []).extend(replacement_members)
                self.team_store.save(aggregate)

    def _save(self, payload: dict) -> None:
        with self._lock:
            self.team_store.save(payload)

    def _save_member(self, updated_member: dict) -> None:
        session_id = str(updated_member.get("session_id") or "").strip() or None
        if session_id:
            try:
                config = self.team_store.load(session_id=session_id)
                for index, member in enumerate(config.get("members", [])):
                    if member.get("name") == updated_member.get("name"):
                        config["members"][index] = dict(updated_member)
                        break
                else:
                    config.setdefault("members", []).append(dict(updated_member))
                self.team_store.save(config, session_id=session_id)
                return
            except TypeError:
                pass
        config = self.team_store.load() if session_id else self._load_legacy()
        for index, member in enumerate(config.get("members", [])):
            if member.get("name") == updated_member.get("name"):
                config["members"][index] = dict(updated_member)
                break
        else:
            config.setdefault("members", []).append(dict(updated_member))
        if session_id:
            self.team_store.save(config)
            return
        self._save_for_session(config, session_id)

    def _read_log(self, name: str, session_id: str | None = None) -> list[dict]:
        try:
            return self.team_store.read_log(name, session_id=session_id)
        except TypeError:
            return self.team_store.read_log(name)

    def _reset_log(self, name: str, payload: dict, session_id: str | None = None) -> None:
        try:
            self.team_store.reset_log(name, payload, session_id=session_id)
        except TypeError:
            self.team_store.reset_log(name, payload)

    def _find(self, name: str, session_id: str | None | object = UNSET) -> dict | None:
        normalized_session_id = (
            str(session_id or "").strip()
            if session_id is not UNSET
            else str(self._active_session_id or "").strip()
        )
        with self._lock:
            configs = []
            if normalized_session_id:
                configs.append(self._load_for_session(normalized_session_id))
            else:
                configs.append(self.team_store.load())
            for config in configs:
                for member in config.get("members", []):
                    if member.get("name") == name and (
                        not normalized_session_id or member.get("session_id") == normalized_session_id
                    ):
                        return dict(member)
            return None

    def _upsert_member(self, name: str, role: str, status: str, activity: str, *, session_id: str | None = None) -> None:
        ts = now_ts()
        normalized_session_id = str(session_id or "").strip() or None
        with self._lock:
            config = self._load_for_session(normalized_session_id) if normalized_session_id else self._load_legacy()
            for member in config.get("members", []):
                if member.get("name") == name:
                    member["role"] = role
                    member["status"] = status
                    member["activity"] = activity
                    member["session_id"] = normalized_session_id
                    member["last_transition_at"] = ts
                    member["last_activity_at"] = ts
                    member["shutdown_reason"] = None
                    member["current_task_id"] = None
                    member["last_error"] = None
                    member["current_tool_name"] = None
                    member["current_tool_log_id"] = None
                    self._save_for_session(config, normalized_session_id)
                    return
            config.setdefault("members", []).append(
                {
                    "name": name,
                    "role": role,
                    "status": status,
                    "activity": activity,
                    "session_id": normalized_session_id,
                    "last_transition_at": ts,
                    "last_activity_at": ts,
                    "shutdown_reason": None,
                    "current_task_id": None,
                    "last_error": None,
                    "current_tool_name": None,
                    "current_tool_log_id": None,
                }
            )
            self._save_for_session(config, normalized_session_id)

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
        session_id: str | None | object = UNSET,
    ) -> None:
        with self._lock:
            existing_member = self._find(name, session_id=session_id)
            session_id = str(existing_member.get("session_id") or "").strip() if existing_member else None
            config = self._load_for_session(session_id) if session_id else self._load_legacy()
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
                    self._save_for_session(config, session_id)
                    return

    def spawn(self, name: str, role: str, prompt: str, *, session_id: str | None = None) -> str:
        normalized_session_id = str(session_id or self._active_session_id or "").strip() or None
        member = self._find(name, session_id=normalized_session_id)
        if (
            member
            and member.get("session_id") in {None, normalized_session_id}
            and member.get("status") not in {"idle", "shutdown", "suspended"}
        ):
            return f"Error: '{name}' is currently {member['status']}"
        self._upsert_member(name, role, "starting", "booting", session_id=normalized_session_id)
        self._reset_log(
            name,
            {
                "type": "session_started",
                "timestamp": now_ts(),
                "name": name,
                "role": role,
                "prompt": prompt,
                "session_id": normalized_session_id,
            },
            session_id=normalized_session_id,
        )
        self._start_thread_for_member(name, role, prompt, session_id=normalized_session_id)
        return f"Spawned '{name}' (role: {role})"

    def _start_thread_for_member(
        self,
        name: str,
        role: str,
        prompt: str,
        *,
        session_id: str | None = None,
        initial_messages: list[dict] | None = None,
        resumed: bool = False,
    ) -> None:
        try:
            self._start_thread(
                name,
                role,
                prompt,
                session_id=session_id,
                initial_messages=initial_messages,
                resumed=resumed,
            )
        except TypeError:
            self._start_thread(name, role, prompt, initial_messages=initial_messages, resumed=resumed)

    def _start_thread(
        self,
        name: str,
        role: str,
        prompt: str,
        *,
        session_id: str | None = None,
        initial_messages: list[dict] | None = None,
        resumed: bool = False,
    ) -> None:
        normalized_session_id = str(session_id or "").strip() or None
        self._reset_stop_request(name, session_id=normalized_session_id)
        thread = threading.Thread(
            target=self._loop,
            args=(name, role, prompt, initial_messages, resumed, normalized_session_id),
            daemon=True,
        )
        thread.start()
        self.threads[self._member_key(name, normalized_session_id)] = thread

    def assign_claimable_tasks(self, session_id: str | None = None) -> int:
        """Assign ready tasks to available teammates after dependencies unblock."""
        normalized_session_id = str(session_id or self._active_session_id or "").strip() or None
        is_paused = getattr(self.task_store, "is_auto_assign_paused", None)
        if callable(is_paused) and is_paused(normalized_session_id):
            return 0
        list_claimable = getattr(self.task_store, "list_claimable", None)
        if not callable(list_claimable):
            return 0
        try:
            claimable = list(list_claimable(session_id=normalized_session_id) or [])
        except TypeError:
            claimable = list(list_claimable() or [])
        if not claimable:
            return 0

        self._refresh_thread_health()
        config = self._load_for_session(normalized_session_id) if normalized_session_id else self._load()
        candidates: list[dict] = []
        for member in config.get("members", []):
            name = str(member.get("name", "")).strip()
            if not name:
                continue
            if normalized_session_id and member.get("session_id") != normalized_session_id:
                continue
            status = str(member.get("status", "")).strip()
            can_resume_idle_timeout = status == "shutdown" and member.get("shutdown_reason") == "idle_timeout"
            if status != "idle" and not can_resume_idle_timeout:
                continue
            if self._has_owned_open_task(name, session_id=member.get("session_id")):
                continue
            if can_resume_idle_timeout:
                prompt, _messages = self._restore_prompt_and_messages(name, session_id=member.get("session_id"))
                if not prompt:
                    continue
            candidates.append(dict(member))

        assigned = 0
        used_names: set[str] = set()
        for task in claimable:
            preferred_owner = str(task.get("preferred_owner") or "").strip()
            selected = None
            if preferred_owner:
                selected = next(
                    (
                        member
                        for member in candidates
                        if member.get("name") == preferred_owner and member.get("name") not in used_names
                    ),
                    None,
                )
            if selected is None and not preferred_owner:
                selected = next((member for member in candidates if member.get("name") not in used_names), None)
            if selected is None:
                continue

            name = str(selected["name"])
            member_session_id = str(selected.get("session_id") or "").strip() or normalized_session_id
            try:
                self.task_store.claim(int(task["id"]), name, session_id=member_session_id)
            except TypeError:
                self.task_store.claim(int(task["id"]), name)
            used_names.add(name)
            assigned += 1
            assignment_message = (
                f"<auto-assigned>Task #{task['id']}: {task['subject']}\n"
                f"{task.get('description', '')}</auto-assigned>"
            )
            self.bus.send("lead", name, assignment_message, msg_type="task_assignment", session_id=member_session_id)
            if selected.get("status") == "shutdown" and selected.get("shutdown_reason") == "idle_timeout":
                prompt, messages = self._restore_prompt_and_messages(name, session_id=member_session_id)
                role = str(selected.get("role", "")).strip() or "teammate"
                self._update_member(
                    name,
                    status="starting",
                    activity="restoring_for_assigned_task",
                    shutdown_reason="",
                    current_task_id=int(task["id"]),
                    session_id=member_session_id,
                )
                self._start_thread_for_member(
                    name,
                    role,
                    prompt or assignment_message,
                    session_id=member_session_id,
                    initial_messages=messages,
                    resumed=True,
                )
            else:
                self._update_member(name, activity="auto_assigned_task", current_task_id=int(task["id"]), session_id=member_session_id)
        return assigned

    def _reset_stop_request(self, name: str, session_id: str | None = None) -> threading.Event:
        key = self._member_key(name, session_id)
        with self._lock:
            event = self._stop_events.get(key)
            if event is None:
                event = threading.Event()
                self._stop_events[key] = event
            else:
                event.clear()
            self._stop_reasons.pop(key, None)
            return event

    def _request_stop(self, name: str, reason: str, session_id: str | None = None) -> None:
        key = self._member_key(name, session_id)
        with self._lock:
            event = self._stop_events.get(key)
            if event is None:
                event = threading.Event()
                self._stop_events[key] = event
            self._stop_reasons[key] = reason
            event.set()

    def _stop_reason(self, name: str, session_id: str | None = None) -> str | None:
        key = self._member_key(name, session_id)
        with self._lock:
            event = self._stop_events.get(key)
            if event is None or not event.is_set():
                return None
            return self._stop_reasons.get(key, "interrupt_requested")

    def _shutdown_if_stop_requested(self, name: str, activity: str = "interrupt_requested", session_id: str | None = None) -> bool:
        reason = self._stop_reason(name, session_id=session_id)
        if reason is None:
            return False
        if reason == "session_not_active":
            self._update_member(
                name,
                status="suspended",
                activity="session_not_active",
                shutdown_reason=reason,
                current_task_id=None,
                session_id=session_id,
            )
            return True
        self._update_member(
            name,
            status="shutdown",
            activity=activity,
            shutdown_reason=reason,
            current_task_id=None,
            session_id=session_id,
        )
        return True

    def _owned_task_reminder_message(self, task: dict) -> str:
        task_id = task.get("id", "unknown")
        subject = str(task.get("subject") or "").strip()
        status = str(task.get("status") or "unknown").strip()
        description = str(task.get("description") or "").strip()
        lines = [
            f"<owned-task-reminder>Task #{task_id} is still {status}.",
            "If the work is complete, call task_update with status completed now.",
            "If it is not complete, continue working on this task before going idle.",
        ]
        if subject:
            lines.append(f"Subject: {subject}")
        if description:
            lines.append(f"Description: {description}")
        lines.append("</owned-task-reminder>")
        return "\n".join(lines)

    def _sync_completed_task_state(self, name: str, tool_input: dict, tool_output, session_id: str | None) -> None:
        if str(tool_input.get("status") or "").strip() != "completed":
            return
        if isinstance(tool_output, dict) and tool_output.get("status") == "error":
            return
        try:
            task_id = int(tool_input["task_id"])
        except (KeyError, TypeError, ValueError):
            return
        try:
            task = self.task_store.get(task_id, session_id=session_id)
        except Exception:
            return
        if task.get("status") != "completed" or task.get("owner") != name:
            return
        member = self._find(name, session_id=session_id)
        if member is not None and member.get("current_task_id") not in {None, task_id}:
            return
        self._update_member(
            name,
            activity="task_completed",
            current_task_id=None,
            current_tool_name=None,
            session_id=session_id,
        )

    def interrupt_active(self, reason: str = "lead_interrupt") -> int:
        self._refresh_thread_health()
        count = 0
        config = self._load()
        for member in config.get("members", []):
            name = str(member.get("name", "")).strip()
            if not name:
                continue
            member_session_id = str(member.get("session_id") or "").strip() or None
            thread = self.threads.get(self._member_key(name, member_session_id))
            if thread is None or not thread.is_alive():
                continue
            if member.get("status") == "shutdown":
                continue
            self._request_stop(name, reason, session_id=member_session_id)
            self._update_member(name, activity="interrupt_requested", session_id=member_session_id)
            count += 1
        return count

    def _loop(
        self,
        name: str,
        role: str,
        prompt: str,
        initial_messages: list[dict] | None = None,
        resumed: bool = False,
        session_id: str | None = None,
    ) -> None:
        normalized_session_id = str(session_id or "").strip() or None
        messages = list(initial_messages) if initial_messages else [{"role": "user", "content": prompt}]
        pending_tool_repair_hints: list[dict[str, object]] = []
        registry = ToolRegistry()
        self.runtime.register_worker_tools(registry)
        system_prompt = self.runtime.build_system_prompt(actor=name, role=role)
        stop_event = self._reset_stop_request(name, session_id=normalized_session_id)
        self._update_member(name, status="working", activity="starting_work_loop", session_id=normalized_session_id)
        if resumed:
            self._append_log(name, "session_resumed", {"reason": "runtime_restore", "message_count": len(messages)}, session_id=normalized_session_id)
        else:
            self._append_log(name, "user_message", {"content": prompt, "source": "prompt"}, session_id=normalized_session_id)

        try:
            while True:
                if self._shutdown_if_stop_requested(name, session_id=normalized_session_id):
                    return
                for _ in range(self.runtime.settings.runtime.max_agent_rounds):
                    if self._shutdown_if_stop_requested(name, session_id=normalized_session_id):
                        return
                    if pending_tool_repair_hints:
                        repair_message = render_transient_repair_hint_message(pending_tool_repair_hints)
                        pending_tool_repair_hints = []
                        if repair_message:
                            messages.append({"role": "user", "content": repair_message})
                            self._append_log(
                                name,
                                "user_message",
                                {"content": repair_message, "source": "tool_repair_hint"},
                                session_id=normalized_session_id,
                            )
                    self._update_member(name, status="working", activity="checking_inbox", session_id=normalized_session_id)
                    member_session_id = normalized_session_id
                    inbox = self.bus.read_inbox(name, session_id=member_session_id)
                    for message in inbox:
                        if self._handle_control_message(name, message, session_id=normalized_session_id):
                            return
                        messages.append({"role": "user", "content": json.dumps(message, ensure_ascii=False)})
                        self._append_log(name, "user_message", {"content": message, "source": "inbox"}, session_id=normalized_session_id)

                    self._update_member(name, status="working", activity="waiting_for_model", session_id=normalized_session_id)
                    payload_builder = getattr(self.runtime, "_build_payload_messages", None)
                    if callable(payload_builder):
                        payload_messages = payload_builder(messages, session=None)
                    else:
                        payload_messages = messages
                    consume_ephemeral_image_blocks(messages)
                    turn = self.runtime.complete(
                        system_prompt,
                        payload_messages,
                        registry.schemas(),
                        should_interrupt=lambda: self._stop_reason(name, session_id=normalized_session_id) is not None,
                    )
                    if self._shutdown_if_stop_requested(name, activity="interrupted_after_model", session_id=normalized_session_id):
                        return
                    assistant_message = turn.as_message()
                    messages.append(assistant_message)
                    self._append_log(name, "assistant_message", {"content": assistant_message.get("content")}, session_id=normalized_session_id)
                    if not turn.has_tool_calls():
                        break
                    ctx = ToolExecutionContext(
                        runtime=self.runtime,
                        session=None,
                        actor=name,
                        trace_id=f"{name}-{int(time.time())}",
                        should_interrupt=lambda: self._stop_reason(name, session_id=normalized_session_id) is not None,
                    )
                    tool_results: list[dict] = []
                    idle_requested = False
                    for tool_call in turn.tool_calls:
                        if self._shutdown_if_stop_requested(name, activity="interrupted_before_tool", session_id=normalized_session_id):
                            return
                        if tool_call.name == "idle":
                            idle_requested = True
                            self._update_member(name, status="working", activity="preparing_for_idle", session_id=normalized_session_id)
                            output = "Entering idle phase."
                        else:
                            self._update_member(name, status="working", activity=f"running_tool:{tool_call.name}", session_id=normalized_session_id)
                            try:
                                output = registry.execute(ctx, tool_call.name, tool_call.input)
                                if tool_call.name == "claim_task":
                                    task_id = int(tool_call.input["task_id"])
                                    self._update_member(name, current_task_id=task_id, session_id=normalized_session_id)
                                elif tool_call.name == "task_update":
                                    self._sync_completed_task_state(
                                        name,
                                        tool_call.input,
                                        output,
                                        normalized_session_id,
                                    )
                            except TurnInterrupted:
                                if self._shutdown_if_stop_requested(name, activity="interrupted_during_tool", session_id=normalized_session_id):
                                    return
                                raise
                            except Exception as exc:
                                output = tool_error_from_exception(tool_call.name, exc)
                                self._update_member(name, last_error=str(exc), session_id=normalized_session_id)
                        repair_hint = extract_transient_repair_hint(output)
                        if repair_hint is not None:
                            pending_tool_repair_hints.append(repair_hint)
                        persisted_output = sanitize_tool_output_for_persistence(output)
                        rendered_output = serialize_tool_output(persisted_output)
                        log_id = self.runtime.print_tool_event(name, tool_call.name, tool_call.input, persisted_output)
                        self._update_member(name, current_tool_log_id=log_id, session_id=normalized_session_id)
                        self._append_log(
                            name,
                            "tool_call",
                            {
                                "tool_name": tool_call.name,
                                "tool_input": tool_call.input,
                                "output_preview": self.runtime._compact_preview(rendered_output, limit=120),
                                "tool_log_id": log_id,
                            },
                            session_id=normalized_session_id,
                        )
                        tool_results.append(
                            make_tool_result_item(
                                tool_call.id,
                                persisted_output,
                                rendered_output=rendered_output,
                            )
                        )
                    messages.append(make_tool_result_message(tool_results))
                    self._append_log(name, "tool_result_message", {"content": tool_results}, session_id=normalized_session_id)
                    if idle_requested:
                        break

                initial_owned_open = []
                list_owned_open = getattr(self.task_store, "list_owned_open", None)
                if callable(list_owned_open):
                    try:
                        initial_owned_open = list_owned_open(name, session_id=normalized_session_id) or []
                    except TypeError:
                        initial_owned_open = list_owned_open(name) or []
                retained_task_id = initial_owned_open[0]["id"] if initial_owned_open else None
                initial_activity = "idle_waiting_on_owned_task" if initial_owned_open else "idle_polling"
                self._update_member(
                    name,
                    status="idle",
                    activity=initial_activity,
                    current_task_id=retained_task_id,
                    session_id=normalized_session_id,
                )
                resume = False
                poll_total = max(self.runtime.settings.runtime.teammate_idle_timeout_seconds, 1)
                poll_interval = max(self.runtime.settings.runtime.teammate_poll_interval_seconds, 1)
                while True:
                    for _ in range(max(poll_total // poll_interval, 1)):
                        if stop_event.wait(poll_interval):
                            if self._shutdown_if_stop_requested(name, session_id=normalized_session_id):
                                return
                        self._update_member(name, status="idle", activity="idle_polling", session_id=normalized_session_id)
                        member_session_id = normalized_session_id
                        inbox = self.bus.read_inbox(name, session_id=member_session_id)
                        if inbox:
                            for message in inbox:
                                if self._handle_control_message(name, message, session_id=normalized_session_id):
                                    return
                                messages.append({"role": "user", "content": json.dumps(message, ensure_ascii=False)})
                                self._append_log(name, "user_message", {"content": message, "source": "idle_inbox"}, session_id=normalized_session_id)
                            self._update_member(name, status="working", activity="resuming_from_inbox", session_id=normalized_session_id)
                            resume = True
                            break
                        owned_open = []
                        has_open_task = False
                        list_owned_open = getattr(self.task_store, "list_owned_open", None)
                        if callable(list_owned_open):
                            try:
                                owned_open = list_owned_open(name, session_id=normalized_session_id) or []
                            except TypeError:
                                owned_open = list_owned_open(name) or []
                            has_open_task = bool(owned_open)
                        else:
                            try:
                                has_open_task = bool(
                                    getattr(self.task_store, "has_open_task", lambda owner: False)(
                                        name,
                                        session_id=normalized_session_id,
                                    )
                                )
                            except TypeError:
                                has_open_task = bool(getattr(self.task_store, "has_open_task", lambda owner: False)(name))
                        if has_open_task:
                            current_task_id = owned_open[0]["id"] if owned_open else member.get("current_task_id") if (member := self._find(name, session_id=normalized_session_id)) else None
                            self._update_member(name, status="idle", activity="idle_waiting_on_owned_task", current_task_id=current_task_id, session_id=normalized_session_id)
                            continue
                        is_paused = getattr(self.task_store, "is_auto_assign_paused", None)
                        if callable(is_paused) and is_paused(normalized_session_id):
                            self._update_member(name, status="idle", activity="idle_auto_assign_paused", session_id=normalized_session_id)
                            continue
                        list_claimable_for = getattr(self.task_store, "list_claimable_for", None)
                        if callable(list_claimable_for):
                            claimable = list_claimable_for(name, session_id=normalized_session_id)
                        else:
                            claimable = self.task_store.list_claimable()
                        if claimable:
                            task = claimable[0]
                            self.task_store.claim(task["id"], name, session_id=normalized_session_id)
                            self._update_member(name, status="working", activity="auto_claimed_task", current_task_id=task["id"], session_id=normalized_session_id)
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
                                session_id=normalized_session_id,
                            )
                            messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                            self._append_log(name, "assistant_message", {"content": f"Claimed task #{task['id']}. Working on it."}, session_id=normalized_session_id)
                            resume = True
                            break
                    if resume or not self._has_owned_open_task(name, session_id=normalized_session_id):
                        break
                    owned_open = self._owned_open_tasks(name, session_id=normalized_session_id)
                    if owned_open:
                        reminder_message = self._owned_task_reminder_message(owned_open[0])
                        messages.append({"role": "user", "content": reminder_message})
                        self._append_log(
                            name,
                            "user_message",
                            {"content": reminder_message, "source": "owned_task_reminder"},
                            session_id=normalized_session_id,
                        )
                        self._update_member(
                            name,
                            status="working",
                            activity="resuming_owned_task",
                            current_task_id=int(owned_open[0]["id"]),
                            session_id=normalized_session_id,
                        )
                        resume = True
                        break
                    self._update_member(name, status="idle", activity="idle_waiting_on_owned_task", session_id=normalized_session_id)
                if not resume:
                    self._update_member(
                        name,
                        status="shutdown",
                        activity="idle_timeout",
                        shutdown_reason="idle_timeout",
                        current_task_id=None,
                        session_id=normalized_session_id,
                    )
                    return
                self._update_member(name, status="working", activity="resuming_work", session_id=normalized_session_id)
        except Exception as exc:
            if self._shutdown_if_stop_requested(name, session_id=normalized_session_id):
                return
            self._append_log(name, "runtime_error", {"error": str(exc)}, session_id=normalized_session_id)
            self._update_member(
                name,
                status="shutdown",
                activity="runtime_error",
                shutdown_reason="runtime_error",
                current_task_id=None,
                last_error=str(exc),
                session_id=normalized_session_id,
            )
            return

    def _owned_open_tasks(self, name: str, session_id: str | None | object = UNSET) -> list[dict]:
        list_owned_open = getattr(self.task_store, "list_owned_open", None)
        try:
            if callable(list_owned_open):
                try:
                    return list(list_owned_open(name, session_id=self._member_session_id(name, session_id=session_id)) or [])
                except TypeError:
                    return list(list_owned_open(name) or [])
        except Exception:
            return []
        return []

    def _member_session_id(self, name: str, session_id: str | None | object = UNSET) -> str | None:
        member = self._find(name, session_id=session_id)
        value = member.get("session_id") if member else None
        return str(value).strip() if value else None

    def _has_owned_open_task(self, name: str, session_id: str | None | object = UNSET) -> bool:
        owned_open = self._owned_open_tasks(name, session_id=session_id)
        if owned_open:
            return True
        try:
            has_open_task = getattr(self.task_store, "has_open_task", None)
            if not callable(has_open_task):
                return False
            try:
                return bool(has_open_task(name, session_id=self._member_session_id(name, session_id=session_id)))
            except TypeError:
                return bool(has_open_task(name))
        except Exception:
            return False

    def _handle_control_message(self, name: str, message: dict, session_id: str | None = None) -> bool:
        if message.get("type") != "shutdown_request":
            return False
        request_id = message.get("request_id")
        self._request_stop(name, "shutdown_request", session_id=session_id)
        if request_id:
            self.request_tracker.mark_shutdown_response(request_id, "accepted")
        self._update_member(
            name,
            status="shutdown",
            activity="shutdown_request",
            shutdown_reason="shutdown_request",
            current_task_id=None,
            session_id=session_id,
        )
        return True

    def _append_log(self, name: str, event_type: str, payload: dict, session_id: str | None | object = UNSET) -> None:
        session_id = self._member_session_id(name, session_id=session_id)
        entry = {
            "type": event_type,
            "timestamp": now_ts(),
            **payload,
        }
        if session_id and not entry.get("session_id"):
            entry["session_id"] = session_id
        try:
            self.team_store.append_log(name, entry, session_id=session_id)
        except TypeError:
            self.team_store.append_log(name, entry)

    def _refresh_thread_health(self) -> None:
        with self._lock:
            config = self.team_store.load()
            for member in config.get("members", []):
                name = member.get("name")
                member_session_id = str(member.get("session_id") or "").strip() or None
                thread = self.threads.get(self._member_key(str(name or ""), member_session_id))
                if thread is None:
                    continue
                if not thread.is_alive() and member.get("status") not in {"shutdown"}:
                    member["status"] = "shutdown"
                    member["activity"] = "thread_exited"
                    member["shutdown_reason"] = member.get("shutdown_reason") or "thread_exited"
                    member["last_transition_at"] = now_ts()
                    self._save_member(member)

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

    def list_all(self, session_id: str | None = None) -> str:
        self._refresh_thread_health()
        normalized_session_id = str(session_id or "").strip()
        config = self._load_for_session(normalized_session_id) if normalized_session_id else self._load()
        normalized_session_id = str(session_id or "").strip()
        members = [
            member
            for member in config.get("members", [])
            if not normalized_session_id or member.get("session_id") == normalized_session_id
        ]
        if not members:
            return "No teammates."
        lines = [f"Team: {config.get('team_name', 'default')}"]
        for member in members:
            lines.append("  " + self._format_member_summary(member))
        return "\n".join(lines)

    def member_names(self, session_id: str | None = None) -> list[str]:
        normalized_session_id = str(session_id or self._active_session_id or "").strip()
        return [
            member["name"]
            for member in (self._load_for_session(normalized_session_id) if normalized_session_id else self._load()).get("members", [])
            if (not normalized_session_id or member.get("session_id") == normalized_session_id)
            and str(member.get("status", "")).strip() in self.ACTIVE_STATUSES
        ]

    def active_member_summaries(self, session_id: str | None = None) -> list[dict]:
        self._refresh_thread_health()
        normalized_session_id = str(session_id or self._active_session_id or "").strip()
        members: list[dict] = []
        config = self._load_for_session(normalized_session_id) if normalized_session_id else self._load()
        for member in config.get("members", []):
            if normalized_session_id and member.get("session_id") != normalized_session_id:
                continue
            if str(member.get("status", "")).strip() in self.ACTIVE_STATUSES:
                summary = dict(member)
                summary["recent_interactions"] = self._recent_interaction_summaries(str(member.get("name", "")))
                members.append(summary)
        return members

    def render_log(self, name: str, session_id: str | None = None) -> str:
        normalized_session_id = str(session_id or "").strip() or None
        member = self._find(name, session_id=normalized_session_id) if normalized_session_id else self._find(name)
        entries = self._read_log(name, session_id=member.get("session_id") if member else None)
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

    def log_entries(self, name: str, session_id: str | None = None) -> list[dict]:
        normalized_session_id = str(session_id or "").strip() or None
        member = self._find(name, session_id=normalized_session_id) if normalized_session_id else self._find(name)
        return [dict(entry) for entry in self._read_log(name, session_id=member.get("session_id") if member else normalized_session_id)]

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
            display_tool_name = self._display_tool_name(entry.get("tool_name", "unknown"))
            lines = [
                f"- tool {display_tool_name}: {self._compact_text(json.dumps(entry.get('tool_input', {}), ensure_ascii=False))}",
                f"  result: {self._compact_text(str(entry.get('output_preview', '(no output)')))}",
            ]
            tool_log_id = str(entry.get("tool_log_id", "")).strip()
            if tool_log_id:
                lines.append(f"  Tool log: /toollog {tool_log_id}")
            return lines
        if event_type == "runtime_error":
            return [f"- runtime_error: {self._compact_text(str(entry.get('error', 'unknown error')))}"]
        return [f"- {event_type}: {self._compact_text(json.dumps(entry, ensure_ascii=False))}"]

    def _recent_interaction_summaries(self, name: str, *, limit: int = 8) -> list[str]:
        summaries: list[str] = []
        for entry in reversed(self.team_store.read_log(name)):
            summary = self._interaction_summary(entry)
            if not summary:
                continue
            if summary in summaries:
                continue
            summaries.append(summary)
            if len(summaries) >= limit:
                break
        summaries.reverse()
        return summaries

    def _interaction_summary(self, entry: dict) -> str:
        event_type = str(entry.get("type", "event"))
        if event_type == "user_message":
            source = str(entry.get("source", "message")).strip() or "message"
            return f"{source}: {self._compact_text(self._render_log_content(entry.get('content')), limit=140)}"
        if event_type == "assistant_message":
            return f"assistant: {self._compact_text(self._render_log_content(entry.get('content')), limit=140)}"
        if event_type == "tool_call":
            display_tool_name = self._display_tool_name(entry.get("tool_name", "unknown"))
            output = self._compact_text(str(entry.get("output_preview", "(no output)")), limit=120)
            if output:
                return f"tool {display_tool_name}: {output}"
            return f"tool {display_tool_name}"
        if event_type == "runtime_error":
            return f"runtime_error: {self._compact_text(str(entry.get('error', 'unknown error')), limit=140)}"
        return ""

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
            return f"tool {self._display_tool_name(raw.split(':', 1)[1])}"
        return raw.replace("_", " ")

    def _format_member_summary(self, member: dict) -> str:
        extras: list[str] = [self._format_activity(member.get("activity", "unknown"))]
        current_tool = self._display_tool_name(member.get("current_tool_name", ""))
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

    def _display_tool_name(self, tool_name) -> str:
        normalized = str(tool_name or "").strip()
        if normalized == "edit_file":
            return "Update"
        return normalized
