from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from open_somnia.collaboration.bus import MessageBus
from open_somnia.collaboration.protocols import RequestTracker
from open_somnia.runtime.events import ToolExecutionContext
from open_somnia.runtime.messages import AssistantTurn, ToolCall
from open_somnia.runtime.teammate import TeammateRuntimeManager
from open_somnia.storage.inbox import InboxStore
from open_somnia.storage.tasks import TaskStore
from open_somnia.storage.team import TeamStore
from open_somnia.tools.registry import ToolDefinition, ToolRegistry
from open_somnia.tools.tasks import register_task_tools


class TeammateRuntimeTests(unittest.TestCase):
    def _stop_manager(self, manager: TeammateRuntimeManager, reason: str = "test_cleanup") -> None:
        manager.interrupt_active(reason=reason)
        for name in list(manager.threads.keys()):
            manager._request_stop(name, reason)
        for thread in list(manager.threads.values()):
            thread.join(timeout=2)

    def _make_memory_team_store(self, payload: dict, logs: dict[str, list[dict]]):
        class _MemoryTeamStore:
            def __init__(self, initial_payload: dict, initial_logs: dict[str, list[dict]]) -> None:
                self.payload = initial_payload
                self.logs = initial_logs

            def load(self) -> dict:
                return self.payload

            def save(self, payload: dict) -> None:
                self.payload = payload

            def reset_log(self, name: str, payload: dict) -> None:
                self.logs[name] = [payload]

            def append_log(self, name: str, payload: dict) -> None:
                self.logs.setdefault(name, []).append(payload)

            def read_log(self, name: str) -> list[dict]:
                return list(self.logs.get(name, []))

        return _MemoryTeamStore(payload, logs)

    def test_bus_peek_does_not_drain_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = MessageBus(InboxStore(Path(tmp) / "inbox"))
            bus.send("worker", "lead", "done")

            self.assertEqual(len(bus.peek_inbox("lead")), 1)
            self.assertTrue(bus.has_inbox_messages("lead"))
            self.assertEqual(len(bus.peek_inbox("lead")), 1)

            drained = bus.read_inbox("lead")

            self.assertEqual(len(drained), 1)
            self.assertEqual(drained[0]["content"], "done")
            self.assertFalse(bus.has_inbox_messages("lead"))

    def test_wait_for_inbox_returns_when_message_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = MessageBus(InboxStore(Path(tmp) / "inbox"))

            def send_later() -> None:
                time.sleep(0.05)
                bus.send("worker", "lead", "ready")

            thread = threading.Thread(target=send_later)
            thread.start()
            started_at = time.monotonic()
            messages = bus.wait_for_inbox("lead", timeout_seconds=2, poll_interval_seconds=0.01)
            elapsed = time.monotonic() - started_at
            thread.join(timeout=1)

            self.assertLess(elapsed, 1)
            self.assertEqual([message["content"] for message in messages], ["ready"])
            self.assertEqual(bus.read_inbox("lead"), [])

    def test_bus_session_filter_preserves_other_session_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = MessageBus(InboxStore(Path(tmp) / "inbox"))
            bus.send("old", "Worker", "old work", session_id="old-session")
            bus.send("new", "Worker", "new work", session_id="new-session")

            new_messages = bus.read_inbox("Worker", session_id="new-session")

            self.assertEqual([message["content"] for message in new_messages], ["new work"])
            self.assertEqual(
                [message["content"] for message in bus.peek_inbox("Worker", session_id="old-session")],
                ["old work"],
            )
            self.assertEqual(bus.read_inbox("Worker", session_id="new-session"), [])
            self.assertEqual([message["content"] for message in bus.read_inbox("Worker")], ["old work"])

    def test_wait_for_inbox_ignores_other_session_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = MessageBus(InboxStore(Path(tmp) / "inbox"))
            bus.send("old", "lead", "stale", session_id="old-session")

            messages = bus.wait_for_inbox(
                "lead",
                session_id="new-session",
                timeout_seconds=0.05,
                poll_interval_seconds=0.01,
            )

            self.assertEqual(messages, [])
            self.assertEqual(
                [message["content"] for message in bus.read_inbox("lead", session_id="old-session")],
                ["stale"],
            )

    def test_shutdown_request_marks_tracker_without_lead_inbox_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bus = MessageBus(InboxStore(root / "inbox"))
            tracker = RequestTracker(root / "requests")
            manager = TeammateRuntimeManager(
                runtime=SimpleNamespace(),
                team_store=TeamStore(root / "team"),
                bus=bus,
                task_store=TaskStore(root / "tasks"),
                request_tracker=tracker,
            )
            manager._upsert_member("Worker", "worker", "idle", "idle_polling", session_id="session-1")
            request = tracker.create_shutdown_request("Worker")

            handled = manager._handle_control_message(
                "Worker",
                {"type": "shutdown_request", "request_id": request["request_id"]},
            )

            self.assertTrue(handled)
            self.assertEqual(bus.read_inbox("lead", session_id="session-1"), [])
            self.assertEqual(tracker._load(tracker.shutdown_path)[request["request_id"]]["status"], "accepted")
            self.assertEqual(manager._find("Worker")["shutdown_reason"], "shutdown_request")

    def test_list_all_and_render_log_show_team_log_entry_points(self) -> None:
        class _MemoryTeamStore:
            def __init__(self) -> None:
                self.payload = {"team_name": "default", "members": []}
                self.logs: dict[str, list[dict]] = {}

            def load(self) -> dict:
                return self.payload

            def save(self, payload: dict) -> None:
                self.payload = payload

            def reset_log(self, name: str, payload: dict) -> None:
                self.logs[name] = [payload]

            def append_log(self, name: str, payload: dict) -> None:
                self.logs.setdefault(name, []).append(payload)

            def read_log(self, name: str) -> list[dict]:
                return list(self.logs.get(name, []))

        team_store = _MemoryTeamStore()
        manager = TeammateRuntimeManager(
            runtime=SimpleNamespace(),
            team_store=team_store,
            bus=SimpleNamespace(),
            task_store=SimpleNamespace(),
            request_tracker=SimpleNamespace(),
        )

        manager._upsert_member("Analyst", "algorithm analyst", "working", "running_tool:grep")
        manager._update_member("Analyst", current_tool_log_id="abc123", current_task_id=7)
        manager._append_log("Analyst", "assistant_message", {"content": "I will inspect crease generation."})
        manager._append_log(
            "Analyst",
            "tool_call",
            {
                "tool_name": "grep",
                "tool_input": {"pattern": "crease"},
                "output_preview": "Found 12 matches",
                "tool_log_id": "abc123",
            },
        )

        roster = manager.list_all()
        log_output = manager.render_log("Analyst")
        summaries = manager.active_member_summaries()

        self.assertIn("View team logs: /teamlog Analyst", roster)
        self.assertIn("tool grep", roster)
        self.assertIn("[team log Analyst]", log_output)
        self.assertIn("assistant: I will inspect crease generation.", log_output)
        self.assertIn("Tool log: /toollog abc123", log_output)
        self.assertEqual(len(summaries), 1)
        self.assertIn("assistant: I will inspect crease generation.", summaries[0]["recent_interactions"])
        self.assertIn("tool grep: Found 12 matches", summaries[0]["recent_interactions"])

    def test_team_ui_displays_update_for_edit_file_tool(self) -> None:
        class _MemoryTeamStore:
            def __init__(self) -> None:
                self.payload = {"team_name": "default", "members": []}
                self.logs: dict[str, list[dict]] = {}

            def load(self) -> dict:
                return self.payload

            def save(self, payload: dict) -> None:
                self.payload = payload

            def reset_log(self, name: str, payload: dict) -> None:
                self.logs[name] = [payload]

            def append_log(self, name: str, payload: dict) -> None:
                self.logs.setdefault(name, []).append(payload)

            def read_log(self, name: str) -> list[dict]:
                return list(self.logs.get(name, []))

        team_store = _MemoryTeamStore()
        manager = TeammateRuntimeManager(
            runtime=SimpleNamespace(),
            team_store=team_store,
            bus=SimpleNamespace(),
            task_store=SimpleNamespace(),
            request_tracker=SimpleNamespace(),
        )

        manager._upsert_member("Builder", "frontend builder", "working", "running_tool:edit_file")
        manager._update_member("Builder", current_tool_name="edit_file", current_tool_log_id="edit-log")
        manager._append_log(
            "Builder",
            "tool_call",
            {
                "tool_name": "edit_file",
                "tool_input": {"path": "frontend/src/App.tsx"},
                "output_preview": "Added 3 lines",
                "tool_log_id": "edit-log",
            },
        )

        roster = manager.list_all()
        log_output = manager.render_log("Builder")

        self.assertIn("tool Update", roster)
        self.assertNotIn("tool edit_file", roster)
        self.assertIn("- tool Update:", log_output)
        self.assertNotIn("- tool edit_file:", log_output)

    def test_interrupt_active_stops_teammate_before_tool_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            executed_tools: list[str] = []
            model_seen = False

            runtime = SimpleNamespace(
                settings=SimpleNamespace(
                    runtime=SimpleNamespace(
                        max_agent_rounds=1,
                        teammate_idle_timeout_seconds=1,
                        teammate_poll_interval_seconds=1,
                    )
                ),
                build_system_prompt=lambda actor, role: "system",
                print_tool_event=lambda *args, **kwargs: None,
            )

            def register_worker_tools(registry) -> None:
                registry.register(
                    ToolDefinition(
                        name="probe",
                        description="Test tool.",
                        input_schema={"type": "object", "properties": {}},
                        handler=lambda ctx, payload: executed_tools.append("probe") or "ok",
                    )
                )

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                nonlocal model_seen
                model_seen = True
                deadline = time.time() + 2
                while time.time() < deadline:
                    if should_interrupt is not None and should_interrupt():
                        break
                    time.sleep(0.01)
                return AssistantTurn(
                    stop_reason="tool_use",
                    text_blocks=["I will inspect files."],
                    tool_calls=[ToolCall("call-1", "probe", {})],
                )

            runtime.register_worker_tools = register_worker_tools
            runtime.complete = fake_complete

            manager = TeammateRuntimeManager(
                runtime=runtime,
                team_store=TeamStore(root / "team"),
                bus=MessageBus(InboxStore(root / "inbox")),
                task_store=SimpleNamespace(list_claimable=lambda: [], claim=lambda task_id, owner: None),
                request_tracker=RequestTracker(root / "requests"),
            )

            spawn_result = manager.spawn("worker", "explore", "Inspect the workspace.")

            self.assertIn("Spawned 'worker'", spawn_result)

            deadline = time.time() + 2
            while time.time() < deadline and not model_seen:
                time.sleep(0.01)
            self.assertTrue(model_seen)

            interrupted = manager.interrupt_active(reason="lead_interrupt")
            worker_thread = manager.threads["worker"]
            worker_thread.join(timeout=2)

            self.assertEqual(interrupted, 1)
            self.assertFalse(worker_thread.is_alive())
            self.assertEqual(executed_tools, [])

            member = manager._find("worker")
            self.assertIsNotNone(member)
            self.assertEqual(member["status"], "shutdown")
            self.assertEqual(member["shutdown_reason"], "lead_interrupt")

    def test_restore_state_resumes_active_teammate_instead_of_marking_runtime_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            resumed = threading.Event()
            release = threading.Event()

            def register_worker_tools(registry) -> None:
                registry.register(
                    ToolDefinition(
                        name="idle",
                        description="Enter idle state.",
                        input_schema={"type": "object", "properties": {}},
                        handler=lambda ctx, payload: "Entering idle phase.",
                    )
                )

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                resumed.set()
                deadline = time.time() + 2
                while time.time() < deadline:
                    if release.is_set():
                        break
                    if should_interrupt is not None and should_interrupt():
                        break
                    time.sleep(0.01)
                return AssistantTurn(stop_reason="end_turn", text_blocks=["Resumed teammate is alive."])

            runtime = SimpleNamespace(
                settings=SimpleNamespace(
                    runtime=SimpleNamespace(
                        max_agent_rounds=2,
                        teammate_idle_timeout_seconds=2,
                        teammate_poll_interval_seconds=1,
                    )
                ),
                build_system_prompt=lambda actor, role: "system",
                print_tool_event=lambda *args, **kwargs: "log-1",
                _compact_preview=lambda text, limit=120: text[:limit],
                register_worker_tools=register_worker_tools,
                complete=fake_complete,
            )

            team_store = TeamStore(root / "team")
            team_store.save(
                {
                    "team_name": "default",
                    "members": [
                        {
                            "name": "Planner",
                            "role": "planner",
                            "session_id": "session-1",
                            "status": "idle",
                            "activity": "idle_polling",
                            "last_transition_at": time.time(),
                            "last_activity_at": time.time(),
                            "shutdown_reason": None,
                            "current_task_id": None,
                            "last_error": None,
                            "current_tool_name": None,
                            "current_tool_log_id": None,
                        }
                    ],
                }
            )
            team_store.reset_log(
                "Planner",
                {
                    "type": "session_started",
                    "timestamp": time.time(),
                    "name": "Planner",
                    "role": "planner",
                    "prompt": "Stay available for follow-up work.",
                },
            )
            team_store.append_log(
                "Planner",
                {
                    "type": "user_message",
                    "timestamp": time.time(),
                    "content": "Stay available for follow-up work.",
                    "source": "prompt",
                },
            )

            manager = TeammateRuntimeManager(
                runtime=runtime,
                team_store=team_store,
                bus=MessageBus(InboxStore(root / "inbox")),
                task_store=TaskStore(root / "tasks"),
                request_tracker=RequestTracker(root / "requests"),
            )
            manager.activate_session("session-1")
            try:
                self.assertTrue(resumed.wait(timeout=1))
                member = manager._find("Planner")
                self.assertIsNotNone(member)
                self.assertNotEqual(member["shutdown_reason"], "runtime_restarted")
                self.assertIn(member["status"], {"starting", "working", "idle"})
                log_output = team_store.log_path("Planner").read_text(encoding="utf-8")
                self.assertIn("session_resumed", log_output)
            finally:
                release.set()
                self._stop_manager(manager)

    def test_restore_state_clears_stale_tool_state_when_resuming_active_teammate(self) -> None:
        class _RecordingManager(TeammateRuntimeManager):
            def __init__(self, *args, **kwargs) -> None:
                self.resume_specs: list[tuple[str, str, str, list[dict], bool]] = []
                super().__init__(*args, **kwargs)

            def _start_thread(
                self,
                name: str,
                role: str,
                prompt: str,
                *,
                initial_messages: list[dict] | None = None,
                resumed: bool = False,
            ) -> None:
                self.resume_specs.append((name, role, prompt, list(initial_messages or []), resumed))

        payload = {
            "team_name": "default",
            "members": [
                {
                    "name": "Planner",
                    "role": "planner",
                    "session_id": "session-1",
                    "status": "working",
                    "activity": "running_tool:grep",
                    "last_transition_at": time.time(),
                    "last_activity_at": time.time(),
                    "shutdown_reason": None,
                    "current_task_id": 7,
                    "last_error": None,
                    "current_tool_name": "grep",
                    "current_tool_log_id": "tool-log-1",
                }
            ],
        }
        logs = {
            "Planner": [
                {
                    "type": "session_started",
                    "timestamp": time.time(),
                    "name": "Planner",
                    "role": "planner",
                    "prompt": "Inspect the workspace.",
                },
                {
                    "type": "user_message",
                    "timestamp": time.time(),
                    "content": "Inspect the workspace.",
                    "source": "prompt",
                },
            ]
        }
        team_store = self._make_memory_team_store(payload, logs)

        manager = _RecordingManager(
            runtime=SimpleNamespace(),
            team_store=team_store,
            bus=SimpleNamespace(),
            task_store=SimpleNamespace(),
            request_tracker=SimpleNamespace(),
        )
        manager.activate_session("session-1")

        member = team_store.load()["members"][0]
        self.assertEqual(member["status"], "starting")
        self.assertEqual(member["activity"], "restoring_on_boot")
        self.assertIsNone(member["current_tool_name"])
        self.assertIsNone(member["current_tool_log_id"])
        self.assertEqual(member["current_task_id"], 7)
        self.assertEqual(manager.resume_specs[0][:3], ("Planner", "planner", "Inspect the workspace."))
        self.assertTrue(manager.resume_specs[0][4])

    def test_restore_state_preserves_missing_tool_fields_when_no_stale_tool_state_exists(self) -> None:
        class _RecordingManager(TeammateRuntimeManager):
            def __init__(self, *args, **kwargs) -> None:
                self.resume_specs: list[tuple[str, str, str, list[dict], bool]] = []
                super().__init__(*args, **kwargs)

            def _start_thread(
                self,
                name: str,
                role: str,
                prompt: str,
                *,
                initial_messages: list[dict] | None = None,
                resumed: bool = False,
            ) -> None:
                self.resume_specs.append((name, role, prompt, list(initial_messages or []), resumed))

        payload = {
            "team_name": "default",
            "members": [
                {
                    "name": "Analyst",
                    "role": "analyst",
                    "session_id": "session-1",
                    "status": "idle",
                    "activity": "idle_polling",
                    "last_transition_at": time.time(),
                    "last_activity_at": time.time(),
                    "shutdown_reason": None,
                    "current_task_id": None,
                    "last_error": None,
                }
            ],
        }
        logs = {
            "Analyst": [
                {
                    "type": "session_started",
                    "timestamp": time.time(),
                    "name": "Analyst",
                    "role": "analyst",
                    "prompt": "Stay available.",
                },
                {
                    "type": "user_message",
                    "timestamp": time.time(),
                    "content": "Stay available.",
                    "source": "prompt",
                },
            ]
        }
        team_store = self._make_memory_team_store(payload, logs)

        manager = _RecordingManager(
            runtime=SimpleNamespace(),
            team_store=team_store,
            bus=SimpleNamespace(),
            task_store=SimpleNamespace(),
            request_tracker=SimpleNamespace(),
        )
        manager.activate_session("session-1")

        member = team_store.load()["members"][0]
        self.assertEqual(member["status"], "starting")
        self.assertEqual(member["activity"], "restoring_on_boot")
        self.assertNotIn("current_tool_name", member)
        self.assertNotIn("current_tool_log_id", member)
        self.assertEqual(manager.resume_specs[0][:3], ("Analyst", "analyst", "Stay available."))
        self.assertTrue(manager.resume_specs[0][4])

    def test_activate_session_suspends_other_session_teammates_without_resuming_them(self) -> None:
        class _RecordingManager(TeammateRuntimeManager):
            def __init__(self, *args, **kwargs) -> None:
                self.resume_specs: list[tuple[str, str, str, list[dict], bool]] = []
                super().__init__(*args, **kwargs)

            def _start_thread(
                self,
                name: str,
                role: str,
                prompt: str,
                *,
                initial_messages: list[dict] | None = None,
                resumed: bool = False,
            ) -> None:
                self.resume_specs.append((name, role, prompt, list(initial_messages or []), resumed))

        payload = {
            "team_name": "default",
            "members": [
                {
                    "name": "OldWorker",
                    "role": "old session worker",
                    "session_id": "old-session",
                    "status": "working",
                    "activity": "waiting_for_model",
                    "last_transition_at": time.time(),
                    "last_activity_at": time.time(),
                    "shutdown_reason": None,
                    "current_task_id": None,
                    "last_error": None,
                    "current_tool_name": None,
                    "current_tool_log_id": None,
                },
                {
                    "name": "NewWorker",
                    "role": "new session worker",
                    "session_id": "new-session",
                    "status": "idle",
                    "activity": "idle_polling",
                    "last_transition_at": time.time(),
                    "last_activity_at": time.time(),
                    "shutdown_reason": None,
                    "current_task_id": None,
                    "last_error": None,
                    "current_tool_name": None,
                    "current_tool_log_id": None,
                },
            ],
        }
        logs = {
            "OldWorker": [
                {
                    "type": "session_started",
                    "timestamp": time.time(),
                    "name": "OldWorker",
                    "role": "old session worker",
                    "prompt": "Old work.",
                    "session_id": "old-session",
                },
                {"type": "user_message", "timestamp": time.time(), "content": "Old work.", "source": "prompt"},
            ],
            "NewWorker": [
                {
                    "type": "session_started",
                    "timestamp": time.time(),
                    "name": "NewWorker",
                    "role": "new session worker",
                    "prompt": "New work.",
                    "session_id": "new-session",
                },
                {"type": "user_message", "timestamp": time.time(), "content": "New work.", "source": "prompt"},
            ],
        }
        team_store = self._make_memory_team_store(payload, logs)
        manager = _RecordingManager(
            runtime=SimpleNamespace(),
            team_store=team_store,
            bus=SimpleNamespace(),
            task_store=SimpleNamespace(),
            request_tracker=SimpleNamespace(),
        )

        manager.activate_session("new-session")

        old_member, new_member = team_store.load()["members"]
        self.assertEqual(old_member["status"], "suspended")
        self.assertEqual(old_member["shutdown_reason"], "session_not_active")
        self.assertEqual(new_member["status"], "starting")
        self.assertEqual([spec[0] for spec in manager.resume_specs], ["NewWorker"])
        self.assertEqual(manager.member_names(session_id="new-session"), ["NewWorker"])
        self.assertEqual(manager.member_names(session_id="old-session"), [])

    def test_restore_state_can_continue_claimed_task_from_persisted_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inputs = root / "inputs"
            artifacts = root / "artifacts"
            inputs.mkdir(parents=True, exist_ok=True)
            artifacts.mkdir(parents=True, exist_ok=True)
            (inputs / "beta.md").write_text("Beta feature note", encoding="utf-8")

            task_store = TaskStore(root / "tasks")
            task = task_store.create("Summarize beta", session_id="session-1")
            task_store.claim(task["id"], "Writer")
            complete_calls = {"count": 0}

            def register_worker_tools(registry) -> None:
                registry.register(
                    ToolDefinition(
                        name="read_file",
                        description="Read a file.",
                        input_schema={
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                        handler=lambda ctx, payload: Path(payload["path"]).read_text(encoding="utf-8"),
                    )
                )
                registry.register(
                    ToolDefinition(
                        name="write_file",
                        description="Write a file.",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                        handler=lambda ctx, payload: Path(payload["path"]).write_text(
                            payload["content"], encoding="utf-8"
                        )
                        or f"Wrote {payload['path']}",
                    )
                )
                registry.register(
                    ToolDefinition(
                        name="task_update",
                        description="Update task status.",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "task_id": {"type": "integer"},
                                "status": {"type": "string"},
                            },
                            "required": ["task_id", "status"],
                        },
                        handler=lambda ctx, payload: task_store.update(
                            int(payload["task_id"]), status=payload["status"]
                        )
                        or f"Updated task #{payload['task_id']}",
                    )
                )

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                complete_calls["count"] += 1
                if complete_calls["count"] == 1:
                    self.assertTrue(
                        any(
                            msg.get("role") == "user"
                            and isinstance(msg.get("content"), list)
                            and any("Claimed task #1 for Writer" in str(item.get("content", "")) for item in msg["content"])
                            for msg in messages
                        )
                    )
                    return AssistantTurn(
                        stop_reason="tool_use",
                        tool_calls=[
                            ToolCall("call-1", "read_file", {"path": str(inputs / "beta.md")}),
                            ToolCall(
                                "call-2",
                                "write_file",
                                {"path": str(artifacts / "beta_summary.md"), "content": "Beta summary"},
                            ),
                            ToolCall("call-3", "task_update", {"task_id": 1, "status": "completed"}),
                        ],
                    )
                return AssistantTurn(stop_reason="tool_use", tool_calls=[ToolCall("call-4", "idle", {})])

            runtime = SimpleNamespace(
                settings=SimpleNamespace(
                    runtime=SimpleNamespace(
                        max_agent_rounds=3,
                        teammate_idle_timeout_seconds=1,
                        teammate_poll_interval_seconds=1,
                    )
                ),
                build_system_prompt=lambda actor, role: "system",
                print_tool_event=lambda *args, **kwargs: "log-1",
                _compact_preview=lambda text, limit=120: text[:limit],
                register_worker_tools=register_worker_tools,
                complete=fake_complete,
            )

            team_store = TeamStore(root / "team")
            team_store.save(
                {
                    "team_name": "default",
                    "members": [
                        {
                            "name": "Writer",
                            "role": "writer",
                            "session_id": "session-1",
                            "status": "working",
                            "activity": "waiting_for_model",
                            "last_transition_at": time.time(),
                            "last_activity_at": time.time(),
                            "shutdown_reason": None,
                            "current_task_id": 1,
                            "last_error": None,
                            "current_tool_name": None,
                            "current_tool_log_id": None,
                        }
                    ],
                }
            )
            team_store.reset_log(
                "Writer",
                {
                    "type": "session_started",
                    "timestamp": time.time(),
                    "name": "Writer",
                    "role": "writer",
                    "prompt": "Summarize the claimed task.",
                },
            )
            team_store.append_log(
                "Writer",
                {
                    "type": "user_message",
                    "timestamp": time.time(),
                    "content": "Summarize the claimed task.",
                    "source": "prompt",
                },
            )
            team_store.append_log(
                "Writer",
                {
                    "type": "assistant_message",
                    "timestamp": time.time(),
                    "content": [
                        {
                            "type": "tool_call",
                            "id": "claim-1",
                            "name": "claim_task",
                            "input": {"task_id": 1},
                        }
                    ],
                },
            )
            team_store.append_log(
                "Writer",
                {
                    "type": "tool_result_message",
                    "timestamp": time.time(),
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_call_id": "claim-1",
                            "content": "Claimed task #1 for Writer",
                        }
                    ],
                },
            )

            manager = TeammateRuntimeManager(
                runtime=runtime,
                team_store=team_store,
                bus=MessageBus(InboxStore(root / "inbox")),
                task_store=task_store,
                request_tracker=RequestTracker(root / "requests"),
            )
            manager.activate_session("session-1")
            try:
                deadline = time.time() + 2
                while time.time() < deadline:
                    if (artifacts / "beta_summary.md").exists() and task_store.get(1)["status"] == "completed":
                        break
                    time.sleep(0.02)
                self.assertTrue((artifacts / "beta_summary.md").exists())
                self.assertEqual(task_store.get(1)["status"], "completed")
                self.assertGreaterEqual(complete_calls["count"], 1)
                self.assertIn("session_resumed", team_store.log_path("Writer").read_text(encoding="utf-8"))
            finally:
                self._stop_manager(manager)

    def test_idle_teammate_with_owned_open_task_does_not_auto_claim_another_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_store = TaskStore(root / "tasks")
            task_one = task_store.create("Task one")
            task_two = task_store.create("Task two")
            task_store.claim(task_one["id"], "Planner")
            idle_called = threading.Event()
            release = threading.Event()

            def register_worker_tools(registry) -> None:
                registry.register(
                    ToolDefinition(
                        name="idle",
                        description="Enter idle state.",
                        input_schema={"type": "object", "properties": {}},
                        handler=lambda ctx, payload: "Entering idle phase.",
                    )
                )

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                if not idle_called.is_set():
                    idle_called.set()
                    return AssistantTurn(stop_reason="tool_use", tool_calls=[ToolCall("call-1", "idle", {})])
                while not release.is_set():
                    if should_interrupt is not None and should_interrupt():
                        break
                    time.sleep(0.01)
                return AssistantTurn(stop_reason="end_turn", text_blocks=["cleanup"])

            runtime = SimpleNamespace(
                settings=SimpleNamespace(
                    runtime=SimpleNamespace(
                        max_agent_rounds=2,
                        teammate_idle_timeout_seconds=1,
                        teammate_poll_interval_seconds=1,
                    )
                ),
                build_system_prompt=lambda actor, role: "system",
                print_tool_event=lambda *args, **kwargs: "log-1",
                _compact_preview=lambda text, limit=120: text[:limit],
                register_worker_tools=register_worker_tools,
                complete=fake_complete,
            )

            manager = TeammateRuntimeManager(
                runtime=runtime,
                team_store=TeamStore(root / "team"),
                bus=MessageBus(InboxStore(root / "inbox")),
                task_store=task_store,
                request_tracker=RequestTracker(root / "requests"),
            )
            manager.spawn("Planner", "planner", "Idle while waiting on the current task.")
            try:
                self.assertTrue(idle_called.wait(timeout=1))
                time.sleep(0.3)
                member = manager._find("Planner")
                self.assertIsNotNone(member)
                self.assertEqual(task_store.get(task_two["id"])["owner"], None)
                self.assertEqual(task_store.get(task_one["id"])["owner"], "Planner")
                self.assertEqual(member["current_task_id"], task_one["id"])
                self.assertIn(member["activity"], {"idle_waiting_on_owned_task", "working", "waiting_for_model", "idle_polling"})
            finally:
                release.set()
                self._stop_manager(manager)

    def test_idle_teammate_with_owned_open_task_survives_timeout_and_resumes_from_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_store = TaskStore(root / "tasks")
            task = task_store.create("Wait for lead input", preferred_owner="Reporter")
            task_store.claim(task["id"], "Reporter")
            idle_called = threading.Event()
            resumed_from_inbox = threading.Event()
            release = threading.Event()

            def register_worker_tools(registry) -> None:
                registry.register(
                    ToolDefinition(
                        name="idle",
                        description="Enter idle state.",
                        input_schema={"type": "object", "properties": {}},
                        handler=lambda ctx, payload: "Entering idle phase.",
                    )
                )

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                if not idle_called.is_set():
                    idle_called.set()
                    return AssistantTurn(stop_reason="tool_use", tool_calls=[ToolCall("call-1", "idle", {})])
                if any("Here is the lead input" in str(message.get("content", "")) for message in messages):
                    resumed_from_inbox.set()
                while not release.is_set():
                    if should_interrupt is not None and should_interrupt():
                        break
                    time.sleep(0.01)
                return AssistantTurn(stop_reason="end_turn", text_blocks=["resumed"])

            runtime = SimpleNamespace(
                settings=SimpleNamespace(
                    runtime=SimpleNamespace(
                        max_agent_rounds=2,
                        teammate_idle_timeout_seconds=1,
                        teammate_poll_interval_seconds=1,
                    )
                ),
                build_system_prompt=lambda actor, role: "system",
                print_tool_event=lambda *args, **kwargs: "log-1",
                _compact_preview=lambda text, limit=120: text[:limit],
                register_worker_tools=register_worker_tools,
                complete=fake_complete,
            )
            bus = MessageBus(InboxStore(root / "inbox"))
            manager = TeammateRuntimeManager(
                runtime=runtime,
                team_store=TeamStore(root / "team"),
                bus=bus,
                task_store=task_store,
                request_tracker=RequestTracker(root / "requests"),
            )
            manager.spawn("Reporter", "reporter", "Wait for lead input.")
            try:
                self.assertTrue(idle_called.wait(timeout=1))
                time.sleep(1.3)
                member = manager._find("Reporter")
                self.assertIsNotNone(member)
                self.assertEqual(member["status"], "idle")
                self.assertEqual(member["current_task_id"], task["id"])
                self.assertEqual(task_store.get(task["id"])["owner"], "Reporter")

                bus.send("lead", "Reporter", "Here is the lead input")

                self.assertTrue(resumed_from_inbox.wait(timeout=2))
            finally:
                release.set()
                self._stop_manager(manager)

    def test_restore_state_resumes_idle_timeout_teammate_with_owned_open_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_store = TaskStore(root / "tasks")
            task = task_store.create("Summarize received reports", preferred_owner="Reporter", session_id="session-1")
            task_store.claim(task["id"], "Reporter")
            resumed_from_inbox = threading.Event()
            release = threading.Event()

            def register_worker_tools(registry) -> None:
                registry.register(
                    ToolDefinition(
                        name="idle",
                        description="Enter idle state.",
                        input_schema={"type": "object", "properties": {}},
                        handler=lambda ctx, payload: "Entering idle phase.",
                    )
                )

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                if any("Reports are ready" in str(message.get("content", "")) for message in messages):
                    resumed_from_inbox.set()
                while not release.is_set():
                    if should_interrupt is not None and should_interrupt():
                        break
                    time.sleep(0.01)
                return AssistantTurn(stop_reason="end_turn", text_blocks=["resumed"])

            runtime = SimpleNamespace(
                settings=SimpleNamespace(
                    runtime=SimpleNamespace(
                        max_agent_rounds=2,
                        teammate_idle_timeout_seconds=1,
                        teammate_poll_interval_seconds=1,
                    )
                ),
                build_system_prompt=lambda actor, role: "system",
                print_tool_event=lambda *args, **kwargs: "log-1",
                _compact_preview=lambda text, limit=120: text[:limit],
                register_worker_tools=register_worker_tools,
                complete=fake_complete,
            )
            team_store = TeamStore(root / "team")
            team_store.save(
                {
                    "team_name": "default",
                    "members": [
                        {
                            "name": "Reporter",
                            "role": "reporter",
                            "session_id": "session-1",
                            "status": "shutdown",
                            "activity": "idle_timeout",
                            "last_transition_at": time.time(),
                            "last_activity_at": time.time(),
                            "shutdown_reason": "idle_timeout",
                            "current_task_id": None,
                            "last_error": None,
                            "current_tool_name": None,
                            "current_tool_log_id": None,
                        }
                    ],
                }
            )
            team_store.reset_log(
                "Reporter",
                {
                    "type": "session_started",
                    "timestamp": time.time(),
                    "name": "Reporter",
                    "role": "reporter",
                    "prompt": "Wait for reports.",
                },
            )
            team_store.append_log(
                "Reporter",
                {
                    "type": "user_message",
                    "timestamp": time.time(),
                    "content": "Wait for reports.",
                    "source": "prompt",
                },
            )
            bus = MessageBus(InboxStore(root / "inbox"))
            bus.send("lead", "Reporter", "Reports are ready", session_id="session-1")
            manager = TeammateRuntimeManager(
                runtime=runtime,
                team_store=team_store,
                bus=bus,
                task_store=task_store,
                request_tracker=RequestTracker(root / "requests"),
            )
            manager.activate_session("session-1")
            try:
                self.assertTrue(resumed_from_inbox.wait(timeout=2))
                member = manager._find("Reporter")
                self.assertIsNotNone(member)
                self.assertNotEqual(member["shutdown_reason"], "idle_timeout")
            finally:
                release.set()
                self._stop_manager(manager)

    def test_list_claimable_for_prefers_task_preferred_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_store = TaskStore(root / "tasks")
            neutral = task_store.create("Neutral task", session_id="session-1")
            writer_task = task_store.create("Writer task", preferred_owner="Writer", session_id="session-1")
            planner_task = task_store.create("Planner task", preferred_owner="Planner", session_id="session-1")
            task_store.create("Other session task", preferred_owner="Writer", session_id="session-2")

            writer_claimable = task_store.list_claimable_for("Writer", session_id="session-1")
            planner_claimable = task_store.list_claimable_for("Planner", session_id="session-1")
            other_claimable = task_store.list_claimable_for("Other", session_id="session-1")

            self.assertEqual([task["id"] for task in writer_claimable], [writer_task["id"], neutral["id"]])
            self.assertEqual([task["id"] for task in planner_claimable], [planner_task["id"], neutral["id"]])
            self.assertEqual([task["id"] for task in other_claimable], [neutral["id"]])

    def test_completed_task_assigns_multiple_unblocked_tasks_to_idle_teammates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_store = TaskStore(root / "tasks")
            first = task_store.create("Initial dependency", session_id="session-1")
            second, third = task_store.create_many(
                [
                    {
                        "subject": "Follow-up two",
                        "preferred_owner": "Planner",
                        "depends_on": [first["id"]],
                    },
                    {
                        "subject": "Follow-up three",
                        "preferred_owner": "Writer",
                        "depends_on": [first["id"]],
                    },
                ],
                session_id="session-1",
            )

            team_store = TeamStore(root / "team")
            base_member = {
                "status": "idle",
                "activity": "idle_polling",
                "last_transition_at": time.time(),
                "last_activity_at": time.time(),
                "shutdown_reason": None,
                "current_task_id": None,
                "last_error": None,
                "current_tool_name": None,
                "current_tool_log_id": None,
            }
            team_store.save(
                {
                    "team_name": "default",
                    "members": [
                        {**base_member, "name": "Planner", "role": "planner", "session_id": "session-1"},
                        {**base_member, "name": "Writer", "role": "writer", "session_id": "session-1"},
                    ],
                },
                session_id="session-1",
            )
            bus = MessageBus(InboxStore(root / "inbox"))
            manager = TeammateRuntimeManager(
                runtime=SimpleNamespace(),
                team_store=team_store,
                bus=bus,
                task_store=task_store,
                request_tracker=RequestTracker(root / "requests"),
            )
            runtime = SimpleNamespace(team_manager=manager)
            registry = ToolRegistry()
            register_task_tools(registry, task_store)

            registry.execute(
                ToolExecutionContext(
                    runtime=runtime,
                    session=SimpleNamespace(id="session-1"),
                    actor="lead",
                    trace_id="test",
                ),
                "task_update",
                {"task_id": first["id"], "status": "completed"},
            )

            self.assertEqual(task_store.get(second["id"], session_id="session-1")["owner"], "Planner")
            self.assertEqual(task_store.get(third["id"], session_id="session-1")["owner"], "Writer")
            self.assertEqual(task_store.get(second["id"], session_id="session-1")["status"], "in_progress")
            self.assertEqual(task_store.get(third["id"], session_id="session-1")["status"], "in_progress")
            self.assertEqual(task_store.get(second["id"], session_id="session-1")["blockedBy"], [first["id"]])
            self.assertEqual(task_store.get(third["id"], session_id="session-1")["blockedBy"], [first["id"]])
            planner = manager._find("Planner", session_id="session-1")
            writer = manager._find("Writer", session_id="session-1")
            self.assertEqual(planner["current_task_id"], second["id"])
            self.assertEqual(writer["current_task_id"], third["id"])
            self.assertEqual(len(bus.read_inbox("Planner", session_id="session-1")), 1)
            self.assertEqual(len(bus.read_inbox("Writer", session_id="session-1")), 1)

    def test_completed_dependency_remains_graph_edge_but_does_not_block_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            task_store = TaskStore(Path(tmpdir) / "tasks")
            blocker = task_store.create("Dependency", session_id="session-1")
            blocked = task_store.create("Blocked", blocked_by=[blocker["id"]], session_id="session-1")

            self.assertEqual(task_store.incomplete_blockers(blocked["id"], session_id="session-1"), [blocker["id"]])
            task_store.update(blocker["id"], status="completed", session_id="session-1")

            self.assertEqual(task_store.get(blocked["id"], session_id="session-1")["blockedBy"], [blocker["id"]])
            self.assertEqual(task_store.incomplete_blockers(blocked["id"], session_id="session-1"), [])
            claimed = task_store.claim(blocked["id"], "Worker", session_id="session-1")
            self.assertEqual(claimed["status"], "in_progress")

    def test_creating_task_with_completed_dependency_does_not_create_stale_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            task_store = TaskStore(Path(tmpdir) / "tasks")
            blocker = task_store.create("Already done", session_id="session-1")
            task_store.update(blocker["id"], status="completed", session_id="session-1")

            [created] = task_store.create_many(
                [{"subject": "Ready task", "depends_on": [blocker["id"]]}],
                session_id="session-1",
            )

            self.assertEqual(created["blockedBy"], [blocker["id"]])
            self.assertEqual(task_store.incomplete_blockers(created["id"], session_id="session-1"), [])
            self.assertEqual([item["id"] for item in task_store.list_claimable(session_id="session-1")], [created["id"]])

    def test_dependency_updates_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            task_store = TaskStore(Path(tmpdir) / "tasks")
            blocker = task_store.create("Dependency", session_id="session-1")
            pending = task_store.create("Pending", session_id="session-1")
            started = task_store.create("Started", session_id="session-1")
            done = task_store.create("Done", session_id="session-1")

            task_store.claim(started["id"], "Worker", session_id="session-1")
            task_store.update(done["id"], status="completed", session_id="session-1")

            with self.assertRaisesRegex(ValueError, "dependency updates are not allowed"):
                task_store.update(pending["id"], add_blocked_by=[blocker["id"]], session_id="session-1")
            with self.assertRaisesRegex(ValueError, "dependency updates are not allowed"):
                task_store.update(started["id"], add_blocked_by=[blocker["id"]], session_id="session-1")
            with self.assertRaisesRegex(ValueError, "dependency updates are not allowed"):
                task_store.update(done["id"], add_blocked_by=[blocker["id"]], session_id="session-1")

    def test_task_create_batch_accepts_existing_dependency_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_store = TaskStore(root / "tasks")
            registry = ToolRegistry()
            register_task_tools(registry, task_store)
            ctx = ToolExecutionContext(
                runtime=SimpleNamespace(),
                session=SimpleNamespace(id="session-1"),
                actor="lead",
                trace_id="test",
            )
            blocker = task_store.create("Dependency", session_id="session-1")

            output = registry.execute(
                ctx,
                "task_create_batch",
                {"tasks": [{"subject": "Dependent work", "depends_on": [blocker["id"]]}]},
            )
            created = json.loads(output)["tasks"][0]

            self.assertEqual(created["blockedBy"], [blocker["id"]])
            self.assertEqual(task_store.get(created["id"], session_id="session-1")["blockedBy"], [blocker["id"]])
            self.assertEqual([task["id"] for task in task_store.list_claimable(session_id="session-1")], [blocker["id"]])
            with self.assertRaisesRegex(ValueError, "blocked by"):
                task_store.claim(created["id"], "Worker", session_id="session-1")

    def test_task_tools_only_expose_batch_graph_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            task_store = TaskStore(Path(tmpdir) / "tasks")
            registry = ToolRegistry()
            register_task_tools(registry, task_store)

            names = set(registry.names())

            self.assertIn("task_create_batch", names)
            self.assertNotIn("task_create", names)
            self.assertNotIn("task_pause_auto_assign", names)
            self.assertNotIn("task_release_auto_assign", names)

    def test_task_update_tool_rejects_dependency_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            task_store = TaskStore(Path(tmpdir) / "tasks")
            blocker = task_store.create("Dependency", session_id="session-1")
            task = task_store.create("Task", session_id="session-1")
            registry = ToolRegistry()
            register_task_tools(registry, task_store)

            output = registry.execute(
                ToolExecutionContext(
                    runtime=SimpleNamespace(),
                    session=SimpleNamespace(id="session-1"),
                    actor="lead",
                    trace_id="test",
                ),
                "task_update",
                {"task_id": task["id"], "add_blocked_by": [blocker["id"]]},
            )

            self.assertEqual(output["status"], "error")
            self.assertIn("dependency updates are not allowed", output["message"])
            self.assertEqual(task_store.get(task["id"], session_id="session-1")["blockedBy"], [])

    def test_task_create_batch_creates_graph_and_assigns_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_store = TaskStore(root / "tasks")
            team_store = TeamStore(root / "team")
            team_store.save(
                {
                    "team_name": "default",
                    "members": [
                        {
                            "name": "Planner",
                            "role": "planner",
                            "status": "idle",
                            "activity": "idle_polling",
                            "session_id": "session-1",
                            "last_transition_at": time.time(),
                            "last_activity_at": time.time(),
                            "shutdown_reason": None,
                            "current_task_id": None,
                            "last_error": None,
                            "current_tool_name": None,
                            "current_tool_log_id": None,
                        }
                    ],
                },
                session_id="session-1",
            )
            bus = MessageBus(InboxStore(root / "inbox"))
            manager = TeammateRuntimeManager(
                runtime=SimpleNamespace(),
                team_store=team_store,
                bus=bus,
                task_store=task_store,
                request_tracker=RequestTracker(root / "requests"),
            )
            runtime = SimpleNamespace(team_manager=manager)
            registry = ToolRegistry()
            register_task_tools(registry, task_store)
            ctx = ToolExecutionContext(
                runtime=runtime,
                session=SimpleNamespace(id="session-1"),
                actor="lead",
                trace_id="test",
            )

            output = registry.execute(
                ctx,
                "task_create_batch",
                {
                    "tasks": [
                        {"key": "plan", "subject": "Plan", "preferred_owner": "Planner"},
                        {"key": "write", "subject": "Write", "depends_on": ["plan"]},
                    ],
                },
            )
            payload = json.loads(output)
            first, second = payload["tasks"]

            self.assertEqual(payload["assigned"], 1)
            self.assertFalse(task_store.is_auto_assign_paused("session-1"))
            self.assertEqual(task_store.get(first["id"], session_id="session-1")["owner"], "Planner")
            self.assertEqual(task_store.get(second["id"], session_id="session-1")["blockedBy"], [first["id"]])
            self.assertIsNone(task_store.get(second["id"], session_id="session-1")["owner"])

            registry.execute(ctx, "task_update", {"task_id": first["id"], "status": "completed"})

            self.assertEqual(task_store.get(second["id"], session_id="session-1")["owner"], "Planner")

    def test_task_create_batch_rejects_dependency_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            task_store = TaskStore(Path(tmpdir) / "tasks")

            with self.assertRaisesRegex(ValueError, "cycle"):
                task_store.create_many(
                    [
                        {"key": "a", "subject": "A", "depends_on": ["b"]},
                        {"key": "b", "subject": "B", "depends_on": ["a"]},
                    ],
                    session_id="session-1",
                )

            self.assertEqual(task_store.list_all(session_id="session-1"), [])

    def test_task_store_session_scope_prevents_cross_session_claim_and_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            task_store = TaskStore(Path(tmpdir) / "tasks")
            current = task_store.create("Current session task", session_id="session-1")
            other = task_store.create("Other session task", session_id="session-2")

            visible = task_store.list_all(session_id="session-1")

            self.assertEqual([task["id"] for task in visible], [current["id"]])
            with self.assertRaises(ValueError):
                task_store.get(other["id"], session_id="session-1")
            with self.assertRaises(ValueError):
                task_store.claim(other["id"], "Worker", session_id="session-1")

            claimed = task_store.claim(current["id"], "Worker", session_id="session-1")
            self.assertEqual(claimed["owner"], "Worker")
            self.assertEqual(
                [task["id"] for task in task_store.list_owned_open("Worker", session_id="session-1")],
                [current["id"]],
            )
            self.assertEqual(task_store.list_owned_open("Worker", session_id="session-2"), [])

    def test_spawn_teammate_allows_same_name_in_different_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            class _NoThreadManager(TeammateRuntimeManager):
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
                    self.threads[self._member_key(name, session_id)] = SimpleNamespace(is_alive=lambda: True)

            manager = _NoThreadManager(
                runtime=SimpleNamespace(),
                team_store=TeamStore(root / "team"),
                bus=MessageBus(InboxStore(root / "inbox")),
                task_store=TaskStore(root / "tasks"),
                request_tracker=RequestTracker(root / "requests"),
            )

            first = manager.spawn("Alpha", "worker", "Work in session one.", session_id="session-1")
            second = manager.spawn("Alpha", "worker", "Work in session two.", session_id="session-2")

            self.assertIn("Spawned 'Alpha'", first)
            self.assertIn("Spawned 'Alpha'", second)
            self.assertEqual(manager._find("Alpha", session_id="session-1")["session_id"], "session-1")
            self.assertEqual(manager._find("Alpha", session_id="session-2")["session_id"], "session-2")
            self.assertIn(manager._member_key("Alpha", "session-1"), manager.threads)
            self.assertIn(manager._member_key("Alpha", "session-2"), manager.threads)
            self.assertTrue((root / "team" / "sessions" / "session-1" / "logs" / "Alpha.jsonl").exists())
            self.assertTrue((root / "team" / "sessions" / "session-2" / "logs" / "Alpha.jsonl").exists())

    def test_blocked_task_cannot_be_claimed_started_or_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            task_store = TaskStore(Path(tmpdir) / "tasks")
            blocker = task_store.create("Dependency", session_id="session-1")
            blocked = task_store.create("Blocked task", blocked_by=[blocker["id"]], session_id="session-1")

            with self.assertRaisesRegex(ValueError, "blocked by"):
                task_store.claim(blocked["id"], "Worker", session_id="session-1")
            with self.assertRaisesRegex(ValueError, "blocked by"):
                task_store.update(blocked["id"], status="in_progress", session_id="session-1")
            with self.assertRaisesRegex(ValueError, "blocked by"):
                task_store.update(blocked["id"], status="completed", session_id="session-1")
            started = task_store.create("Already started", session_id="session-1")
            task_store.claim(started["id"], "Worker", session_id="session-1")
            with self.assertRaisesRegex(ValueError, "dependency updates are not allowed"):
                task_store.update(started["id"], add_blocked_by=[blocker["id"]], session_id="session-1")

            task_store.update(blocker["id"], status="completed", session_id="session-1")
            claimed = task_store.claim(blocked["id"], "Worker", session_id="session-1")

            self.assertEqual(claimed["owner"], "Worker")
            self.assertEqual(claimed["status"], "in_progress")

    def test_session_scoped_storage_writes_into_session_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_store = TaskStore(root / "tasks")
            inbox_store = InboxStore(root / "inbox")
            team_store = TeamStore(root / "team")

            task = task_store.create("Session task", session_id="session-1")
            inbox_store.send("lead", {"content": "hello", "session_id": "session-1"})
            team_store.save({"team_name": "default", "members": [{"name": "Lead", "session_id": "session-1"}]}, session_id="session-1")
            team_store.reset_log("Lead", {"type": "session_started", "prompt": "hello", "session_id": "session-1"}, session_id="session-1")
            team_store.append_log("Lead", {"type": "assistant_message", "content": "working", "session_id": "session-1"}, session_id="session-1")

            self.assertTrue((root / "tasks" / "sessions" / "session-1" / f"task_{task['id']}.json").exists())
            self.assertTrue((root / "inbox" / "sessions" / "session-1" / "lead.jsonl").exists())
            self.assertTrue((root / "team" / "sessions" / "session-1" / "team.json").exists())
            self.assertTrue((root / "team" / "sessions" / "session-1" / "logs" / "Lead.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
