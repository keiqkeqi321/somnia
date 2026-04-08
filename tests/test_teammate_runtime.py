from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from openagent.collaboration.bus import MessageBus
from openagent.collaboration.protocols import RequestTracker
from openagent.runtime.messages import AssistantTurn, ToolCall
from openagent.runtime.teammate import TeammateRuntimeManager
from openagent.storage.inbox import InboxStore
from openagent.storage.tasks import TaskStore
from openagent.storage.team import TeamStore
from openagent.tools.registry import ToolDefinition


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

        self.assertIn("View team logs: /teamlog Analyst", roster)
        self.assertIn("tool grep", roster)
        self.assertIn("[team log Analyst]", log_output)
        self.assertIn("assistant: I will inspect crease generation.", log_output)
        self.assertIn("Tool log: /toollog abc123", log_output)

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

        member = team_store.load()["members"][0]
        self.assertEqual(member["status"], "starting")
        self.assertEqual(member["activity"], "restoring_on_boot")
        self.assertNotIn("current_tool_name", member)
        self.assertNotIn("current_tool_log_id", member)
        self.assertEqual(manager.resume_specs[0][:3], ("Analyst", "analyst", "Stay available."))
        self.assertTrue(manager.resume_specs[0][4])

    def test_restore_state_can_continue_claimed_task_from_persisted_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inputs = root / "inputs"
            artifacts = root / "artifacts"
            inputs.mkdir(parents=True, exist_ok=True)
            artifacts.mkdir(parents=True, exist_ok=True)
            (inputs / "beta.md").write_text("Beta feature note", encoding="utf-8")

            task_store = TaskStore(root / "tasks")
            task = task_store.create("Summarize beta")
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

    def test_list_claimable_for_prefers_task_preferred_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_store = TaskStore(root / "tasks")
            neutral = task_store.create("Neutral task")
            writer_task = task_store.create("Writer task", preferred_owner="Writer")
            planner_task = task_store.create("Planner task", preferred_owner="Planner")

            writer_claimable = task_store.list_claimable_for("Writer")
            planner_claimable = task_store.list_claimable_for("Planner")
            other_claimable = task_store.list_claimable_for("Other")

            self.assertEqual([task["id"] for task in writer_claimable], [writer_task["id"], neutral["id"]])
            self.assertEqual([task["id"] for task in planner_claimable], [planner_task["id"], neutral["id"]])
            self.assertEqual([task["id"] for task in other_claimable], [neutral["id"]])


if __name__ == "__main__":
    unittest.main()
