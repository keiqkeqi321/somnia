from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openagent.collaboration.bus import MessageBus
from openagent.collaboration.protocols import RequestTracker
from openagent.runtime.messages import AssistantTurn, ToolCall
from openagent.runtime.teammate import TeammateRuntimeManager
from openagent.storage.inbox import InboxStore
from openagent.storage.tasks import TaskStore
from openagent.storage.team import TeamStore
from openagent.tools.registry import ToolDefinition


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def wait_for(predicate, timeout: float = 3.0, interval: float = 0.02) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


class MockRuntime:
    def __init__(self, root: Path, task_store: TaskStore, bus: MessageBus, request_tracker: RequestTracker, complete_fn):
        self.root = root
        self.task_store = task_store
        self.bus = bus
        self.request_tracker = request_tracker
        self.complete_fn = complete_fn
        self.tool_events: list[dict[str, Any]] = []
        self._tool_counter = 0
        self.settings = SimpleNamespace(
            runtime=SimpleNamespace(
                max_agent_rounds=1,
                teammate_idle_timeout_seconds=2,
                teammate_poll_interval_seconds=1,
            )
        )

    def register_worker_tools(self, registry) -> None:
        registry.register(
            ToolDefinition(
                name="idle",
                description="Enter idle state.",
                input_schema={"type": "object", "properties": {}},
                handler=lambda ctx, payload: "Entering idle phase.",
            )
        )
        registry.register(
            ToolDefinition(
                name="claim_task",
                description="Claim a task.",
                input_schema={
                    "type": "object",
                    "properties": {"task_id": {"type": "integer"}},
                    "required": ["task_id"],
                },
                handler=lambda ctx, payload: self.task_store.claim(int(payload["task_id"]), ctx.actor)
                or f"Claimed task #{payload['task_id']}",
            )
        )
        registry.register(
            ToolDefinition(
                name="submit_plan",
                description="Submit a plan for approval.",
                input_schema={
                    "type": "object",
                    "properties": {"plan": {"type": "string"}},
                    "required": ["plan"],
                },
                handler=lambda ctx, payload: self._submit_plan(ctx.actor, payload["plan"]),
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
                    "required": ["task_id"],
                },
                handler=lambda ctx, payload: self.task_store.update(
                    int(payload["task_id"]),
                    status=payload.get("status"),
                )
                or f"Updated task #{payload['task_id']}",
            )
        )
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
                handler=lambda ctx, payload: self._write_file(payload["path"], payload["content"]),
            )
        )

    def _submit_plan(self, actor: str, plan: str) -> str:
        request = self.request_tracker.create_plan_request(actor, plan)
        self.bus.send(actor, "lead", plan, "plan_request", {"request_id": request["request_id"]})
        return f"Submitted plan request {request['request_id']}"

    def _write_file(self, path: str, content: str) -> str:
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {file_path}"

    def build_system_prompt(self, actor: str = "lead", role: str = "lead coding agent") -> str:
        return f"mock system prompt for {actor} ({role})"

    def complete(self, system_prompt, messages, tools, text_callback=None, should_interrupt=None):
        return self.complete_fn(system_prompt, messages, tools, text_callback=text_callback, should_interrupt=should_interrupt)

    def print_tool_event(self, actor: str, tool_name: str, tool_input: dict[str, Any], output: Any) -> str:
        self._tool_counter += 1
        log_id = f"log-{self._tool_counter}"
        self.tool_events.append(
            {
                "log_id": log_id,
                "actor": actor,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "output": str(output),
            }
        )
        return log_id

    def _compact_preview(self, text: str, limit: int = 120) -> str:
        return text[:limit]


def stop_manager(manager: TeammateRuntimeManager) -> None:
    manager.interrupt_active("test_cleanup")
    for name in list(manager.threads.keys()):
        manager._request_stop(name, "test_cleanup")
    for thread in list(manager.threads.values()):
        thread.join(timeout=2)


def task_dependency_scenario() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        tasks = TaskStore(root / "tasks")
        one = tasks.create("Summarize alpha")
        two = tasks.create("Merge report")
        tasks.update(two["id"], add_blocked_by=[one["id"]])
        before = tasks.get(two["id"]).get("blockedBy", [])
        tasks.update(one["id"], status="completed")
        after = tasks.get(two["id"]).get("blockedBy", [])
        ok = before == [one["id"]] and after == []
        return {
            "id": "task_dependency_unblocks_on_completion",
            "ok": ok,
            "details": f"before={before}, after={after}",
        }


def submit_plan_scenario() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        task_store = TaskStore(root / "tasks")
        bus = MessageBus(InboxStore(root / "inbox"))
        tracker = RequestTracker(root / "requests")

        def complete_fn(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            return AssistantTurn(
                stop_reason="tool_use",
                tool_calls=[ToolCall("call-1", "submit_plan", {"plan": "Create alpha summary"})],
            )

        runtime = MockRuntime(root, task_store, bus, tracker, complete_fn)
        manager = TeammateRuntimeManager(runtime, TeamStore(root / "team"), bus, task_store, tracker)
        manager.spawn("Planner", "planner", "Submit a plan.")
        plan_payload = wait_for(lambda: read_json(root / "requests" / "plan_requests.json", {}), timeout=2)
        lead_inbox = bus.read_inbox("lead")
        stop_manager(manager)
        ok = bool(plan_payload) and any(item.get("type") == "plan_request" for item in lead_inbox)
        return {
            "id": "submit_plan_reaches_request_store_and_lead_inbox",
            "ok": ok,
            "details": f"plan_requests={plan_payload}, lead_inbox={lead_inbox}",
        }


def runtime_restart_resume_scenario() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        task_store = TaskStore(root / "tasks")
        bus = MessageBus(InboxStore(root / "inbox"))
        tracker = RequestTracker(root / "requests")
        resumed = threading.Event()
        release = threading.Event()

        def complete_fn(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            resumed.set()
            while not release.is_set():
                if should_interrupt is not None and should_interrupt():
                    break
                time.sleep(0.02)
            return AssistantTurn(stop_reason="end_turn", text_blocks=["Resumed teammate is alive."])

        runtime = MockRuntime(root, task_store, bus, tracker, complete_fn)
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
        manager = TeammateRuntimeManager(runtime, team_store, bus, task_store, tracker)
        try:
            ok = bool(wait_for(lambda: resumed.is_set(), timeout=2))
            member = manager._find("Planner") or {}
            return {
                "id": "runtime_restart_restores_active_teammate",
                "ok": ok
                and member.get("shutdown_reason") != "runtime_restarted"
                and member.get("status") in {"starting", "working", "idle"},
                "details": f"Planner_after_restore={member}",
            }
        finally:
            release.set()
            stop_manager(manager)


def resumed_claimed_task_completion_scenario() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        task_store = TaskStore(root / "tasks")
        task_store.create("Summarize beta into artifacts/beta_summary.md")
        task_store.claim(1, "Writer")
        bus = MessageBus(InboxStore(root / "inbox"))
        tracker = RequestTracker(root / "requests")
        inputs = root / "inputs"
        artifacts = root / "artifacts"
        inputs.mkdir(parents=True, exist_ok=True)
        artifacts.mkdir(parents=True, exist_ok=True)
        write_text(inputs / "beta.md", "Beta feature note")

        def complete_fn(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
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

        runtime = MockRuntime(root, task_store, bus, tracker, complete_fn)
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
                "prompt": "Finish the claimed beta summary task.",
            },
        )
        team_store.append_log(
            "Writer",
            {
                "type": "user_message",
                "timestamp": time.time(),
                "content": "Finish the claimed beta summary task.",
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
        manager = TeammateRuntimeManager(runtime, team_store, bus, task_store, tracker)
        try:
            completed = wait_for(
                lambda: (artifacts / "beta_summary.md").exists() and task_store.get(1).get("status") == "completed",
                timeout=3,
            )
            writer = manager._find("Writer") or {}
            task = task_store.get(1)
            return {
                "id": "restored_teammate_can_finish_claimed_task",
                "ok": bool(completed),
                "details": f"Writer={writer}, task={task}, artifact_exists={(artifacts / 'beta_summary.md').exists()}",
            }
        finally:
            stop_manager(manager)


def shutdown_request_scenario() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        task_store = TaskStore(root / "tasks")
        bus = MessageBus(InboxStore(root / "inbox"))
        tracker = RequestTracker(root / "requests")

        def complete_fn(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
            return AssistantTurn(stop_reason="tool_use", tool_calls=[ToolCall("call-1", "idle", {})])

        runtime = MockRuntime(root, task_store, bus, tracker, complete_fn)
        manager = TeammateRuntimeManager(runtime, TeamStore(root / "team"), bus, task_store, tracker)
        manager.spawn("Sleeper", "writer", "Idle until shutdown.")
        wait_for(lambda: manager._find("Sleeper") and manager._find("Sleeper").get("status") == "idle", timeout=2)
        request = tracker.create_shutdown_request("Sleeper")
        bus.send("lead", "Sleeper", "Stop now.", "shutdown_request", {"request_id": request["request_id"]})
        shutdown_member = wait_for(
            lambda: manager._find("Sleeper")
            and manager._find("Sleeper").get("shutdown_reason") == "shutdown_request"
            and read_json(root / "requests" / "shutdown_requests.json", {}).get(request["request_id"], {}).get("status") == "accepted",
            timeout=3,
        )
        sleeper = manager._find("Sleeper") or {}
        stop_manager(manager)
        ok = bool(shutdown_member)
        return {
            "id": "shutdown_request_is_accepted",
            "ok": ok,
            "details": f"Sleeper={sleeper}, shutdown_requests={read_json(root / 'requests' / 'shutdown_requests.json', {})}",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fast mock regressions for agent-team teammate/task flows.")
    parser.add_argument(
        "--report-dir",
        default=str(Path(__file__).resolve().parent / "artifacts"),
        help="Directory where the repro report will be written.",
    )
    parser.add_argument("--strict", action="store_true", help="Return exit code 1 if any repro fails to reproduce.")
    args = parser.parse_args()

    scenarios = [
        task_dependency_scenario(),
        submit_plan_scenario(),
        runtime_restart_resume_scenario(),
        resumed_claimed_task_completion_scenario(),
        shutdown_request_scenario(),
    ]

    passed = [item for item in scenarios if item["ok"]]
    failed = [item for item in scenarios if not item["ok"]]
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "passed": len(passed),
            "failed": len(failed),
        },
        "scenarios": scenarios,
    }

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "mock_agentteam_bug_repro_report.json"
    md_path = report_dir / "mock_agentteam_bug_repro_report.md"
    write_text(json_path, json.dumps(report, ensure_ascii=False, indent=2))

    lines = [
        "# Mock Agent-Team Regression Report",
        "",
        f"- Passed: `{len(passed)}`",
        f"- Failed: `{len(failed)}`",
        "",
        "## Scenario Results",
        "",
    ]
    for item in scenarios:
        status = "PASS" if item["ok"] else "FAIL"
        lines.append(f"- `{status}` `{item['id']}`: {item['details']}")
    lines.append("")
    write_text(md_path, "\n".join(lines))

    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Report JSON: {json_path}")
    print(f"Report MD: {md_path}")

    if args.strict and failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
