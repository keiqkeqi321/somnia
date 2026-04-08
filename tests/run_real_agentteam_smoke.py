from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ALPHA_INPUT = """Alpha feature note

Feature Alpha adds queue-based manual compaction to the REPL. The key behaviors are: show compacting status in the persistent panel, keep the input editable during compaction, and execute queued prompts after compaction completes.

Expected user-visible effect: /compact should no longer block typing, and queued work should resume automatically.
"""

BETA_INPUT = """Beta feature note

The context compaction system now has two major rules. First, old tool results should be compacted only in the payload sent to the model, while the session history remains intact. Second, automatic compaction should trigger when ctx usage reaches roughly seventy-two percent or when the hard token threshold is exceeded.

Expected user-visible effect: compaction is safer, more predictable, and aligned with the ctx percentage shown in the CLI.
"""

CONSTRAINTS_INPUT = """Smoke test constraints

- Keep all writes inside the workspace.
- Use real OpenAgent CLI execution.
- Use persistent tasks, background jobs, teammates, and protocol tools.
- Treat long silent hangs as failures during manual REPL validation.
"""

PHASE1_PROMPT = """
In this workspace, create four persistent tasks:
1. Summarize inputs/alpha.md into artifacts/alpha_summary.md. Set preferred_owner to Planner.
2. Summarize inputs/beta.md into artifacts/beta_summary.md. Set preferred_owner to Writer.
3. Run this exact background command with background_run: python -c "import time, pathlib; time.sleep(3); p=pathlib.Path('artifacts/bg_result.txt'); p.parent.mkdir(exist_ok=True); p.write_text('background complete', encoding='utf-8'); print('background complete')"
4. Merge those outputs into artifacts/team_test_report.md blocked by tasks 1, 2, and 3. Set preferred_owner to Writer.

Start the background job now. Do not spawn teammates yet. End by briefly stating the created task ids and what remains pending.
""".strip()

PHASE2_PROMPT = """
In this workspace, continue the smoke test.
First verify that artifacts/bg_result.txt exists and mark task 3 completed if appropriate.
Then spawn one teammate named Planner with role planner.
After spawning, use a direct send_message to Planner telling it to claim task 1, use submit_plan before any file changes, summarize inputs/alpha.md into artifacts/alpha_summary.md, and mark task 1 completed.
Do not spawn Writer yet.
End by briefly stating whether task 3 was completed, whether Planner was spawned, and that a direct message was sent.
""".strip()

PHASE3_PROMPT = """
Continue the smoke test in this workspace.
Read the lead inbox and, if Planner has a pending plan request, approve it with plan_approval.
Then spawn one teammate named Writer with role writer.
Writer should immediately use idle, auto-claim claimable tasks, prefer task 2 first, summarize inputs/beta.md into artifacts/beta_summary.md, mark task 2 completed, then return to idle and later auto-claim task 4 when it becomes claimable and produce artifacts/team_test_report.md.
After spawning Writer, use broadcast to tell the team that Planner's plan is approved and Writer should idle to auto-claim available work.
End by briefly stating whether plan approval happened, whether Writer was spawned, and whether a broadcast was sent.
""".strip()

CONTINUE_PROMPT = """
Continue the smoke test in this workspace.
Do not recreate tasks or teammates. Prefer restoring and reusing the existing teammates and their in-progress work.
Read the lead inbox and inspect the task board once.
Do not have Planner take task #2 or task #4 if Writer is the preferred owner or already owns them.
If task #2 is done and task #4 is claimable, send one short coordination nudge so Writer continues toward artifacts/team_test_report.md.
Do not wait for completion inside this single run. End immediately after this one coordination pass with a brief status note.
""".strip()

SHUTDOWN_PROMPT = """
In this workspace, perform a minimal graceful shutdown protocol test.
Spawn one teammate named Sleeper with role writer and tell it to call idle immediately and remain available for shutdown.
Then use shutdown_request to request graceful shutdown of Sleeper.
End by briefly stating that the teammate was spawned and a shutdown request was issued.
""".strip()

SHUTDOWN_CONTINUE_PROMPT = """
Continue the graceful shutdown protocol test in this workspace.
Do not spawn any new teammates.
Read the lead inbox, inspect the existing teammate state, and give the existing teammate time to process any pending shutdown request.
End with a brief status note after checking whether the shutdown request has been accepted.
""".strip()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_config_source() -> Path | None:
    candidate = repo_root().parent / ".openagent" / "openagent.toml"
    return candidate if candidate.exists() else None


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            rows.append({"raw": line})
    return rows


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def reset_workspace(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    (path / "inputs").mkdir(parents=True, exist_ok=True)
    (path / "artifacts").mkdir(parents=True, exist_ok=True)
    (path / ".openagent").mkdir(parents=True, exist_ok=True)
    write_text(path / "inputs" / "alpha.md", ALPHA_INPUT)
    write_text(path / "inputs" / "beta.md", BETA_INPUT)
    write_text(path / "inputs" / "constraints.md", CONSTRAINTS_INPUT)


def configure_workspace(path: Path, config_source: Path | None) -> None:
    permissions = {"authorized_tools": ["bash", "background_run", "submit_plan"]}
    write_text(path / ".openagent" / "permissions.json", json.dumps(permissions, ensure_ascii=False, indent=2))
    if config_source is not None:
        shutil.copyfile(config_source, path / ".openagent" / "openagent.toml")
    else:
        fallback = "\n".join(
            [
                "[runtime]",
                "background_poll_interval_seconds = 2",
                "teammate_idle_timeout_seconds = 30",
                "teammate_poll_interval_seconds = 2",
                "",
                "[mcp_servers.unityMCP]",
                "enabled = false",
                "",
            ]
        )
        write_text(path / ".openagent" / "openagent.toml", fallback)


def run_openagent(
    workspace: Path,
    args: list[str],
    timeout: int,
    stdout_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "openagent.cli.main", "--workspace", str(workspace), *args]
    env = dict(**__import__("os").environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            cmd,
            cwd=repo_root(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        result = subprocess.CompletedProcess(cmd, 124, stdout, stderr)
    if stdout_path is not None:
        write_text(stdout_path, result.stdout)
        write_text(stdout_path.with_suffix(".stderr.txt"), result.stderr)
    return result


def wait_for(label: str, predicate, timeout: int, interval: float = 2.0) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def task_path(workspace: Path, task_id: int) -> Path:
    return workspace / ".openagent" / "tasks" / f"task_{task_id}.json"


def task_data(workspace: Path, task_id: int) -> dict[str, Any]:
    return read_json(task_path(workspace, task_id), {}) or {}


def team_data(workspace: Path) -> dict[str, Any]:
    return read_json(workspace / ".openagent" / "team" / "team.json", {"members": []}) or {"members": []}


def team_member(workspace: Path, name: str) -> dict[str, Any] | None:
    for member in team_data(workspace).get("members", []):
        if member.get("name") == name:
            return member
    return None


def tool_index(workspace: Path) -> list[dict[str, Any]]:
    return read_jsonl(workspace / ".openagent" / "logs" / "tool_logs" / "index.jsonl")


def tool_seen(workspace: Path, actor: str | None, tool_name: str) -> bool:
    for row in tool_index(workspace):
        if row.get("tool_name") != tool_name:
            continue
        if actor is not None and row.get("actor") != actor:
            continue
        return True
    return False


def log_contains(path: Path, pattern: str) -> bool:
    if not path.exists():
        return False
    return pattern in path.read_text(encoding="utf-8")


def run_continue_cycle(workspace: Path, timeout: int, cycle: int) -> subprocess.CompletedProcess[str]:
    return run_openagent(
        workspace,
        ["run", CONTINUE_PROMPT],
        timeout=timeout,
        stdout_path=workspace / "artifacts" / f"phase_continue_{cycle}_stdout.txt",
    )


def run_shutdown_continue_cycle(workspace: Path, timeout: int, cycle: int) -> subprocess.CompletedProcess[str]:
    return run_openagent(
        workspace,
        ["run", SHUTDOWN_CONTINUE_PROMPT],
        timeout=timeout,
        stdout_path=workspace / "artifacts" / f"shutdown_continue_{cycle}_stdout.txt",
    )


def append_check(results: list[dict[str, Any]], check_id: str, ok: bool, details: str) -> None:
    results.append({"id": check_id, "ok": ok, "details": details})


def append_bug(findings: list[dict[str, Any]], bug_id: str, observed: bool, details: str) -> None:
    findings.append({"id": bug_id, "observed": observed, "details": details})


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a real OpenAgent CLI smoke test for task/team flows.")
    parser.add_argument(
        "--workspace-root",
        default=str(repo_root().parent / "agentteam_real_cli_auto"),
        help="Workspace root for the main smoke test.",
    )
    parser.add_argument(
        "--shutdown-workspace-root",
        default=str(repo_root().parent / "agentteam_shutdown_cli_auto"),
        help="Workspace root for the isolated shutdown smoke test.",
    )
    parser.add_argument(
        "--config-source",
        default=str(default_config_source()) if default_config_source() else "",
        help="Optional source openagent.toml to copy into the test workspace.",
    )
    parser.add_argument("--run-timeout", type=int, default=150, help="Timeout in seconds for each openagent run.")
    parser.add_argument("--wait-timeout", type=int, default=45, help="Timeout in seconds for post-run polling checks.")
    parser.add_argument("--strict", action="store_true", help="Return exit code 1 when any validation fails.")
    args = parser.parse_args()

    config_source = Path(args.config_source) if args.config_source else None
    main_workspace = Path(args.workspace_root)
    shutdown_workspace = Path(args.shutdown_workspace_root)

    reset_workspace(main_workspace)
    configure_workspace(main_workspace, config_source)

    checks: list[dict[str, Any]] = []
    bugs: list[dict[str, Any]] = []

    doctor = run_openagent(
        main_workspace,
        ["doctor"],
        timeout=30,
        stdout_path=main_workspace / "artifacts" / "doctor.txt",
    )
    doctor_text = doctor.stdout
    append_check(
        checks,
        "doctor_provider_ready",
        doctor.returncode == 0 and "api_key_configured: yes" in doctor_text,
        doctor_text.strip() or "doctor produced no output",
    )

    phase1 = run_openagent(
        main_workspace,
        ["run", PHASE1_PROMPT],
        timeout=args.run_timeout,
        stdout_path=main_workspace / "artifacts" / "phase1_stdout.txt",
    )
    task4 = task_data(main_workspace, 4)
    jobs = read_json(main_workspace / ".openagent" / "jobs" / "jobs.json", {}) or {}
    append_check(
        checks,
        "task_board_created",
        phase1.returncode == 0 and all(task_path(main_workspace, idx).exists() for idx in (1, 2, 3, 4)),
        f"phase1_returncode={phase1.returncode}",
    )
    append_check(
        checks,
        "task_dependencies_created",
        {1, 2}.issubset(set(task4.get("blockedBy", [])))
        and (3 in set(task4.get("blockedBy", [])) or task_data(main_workspace, 3).get("status") == "completed"),
        f"task_4.blockedBy={task4.get('blockedBy')}, task_3.status={task_data(main_workspace, 3).get('status')}",
    )
    append_check(
        checks,
        "background_job_completed",
        any(job.get("status") == "completed" for job in jobs.values())
        and (main_workspace / "artifacts" / "bg_result.txt").exists(),
        f"jobs={list(jobs.keys())}, bg_result_exists={(main_workspace / 'artifacts' / 'bg_result.txt').exists()}",
    )

    phase2 = run_openagent(
        main_workspace,
        ["run", PHASE2_PROMPT],
        timeout=args.run_timeout,
        stdout_path=main_workspace / "artifacts" / "phase2_stdout.txt",
    )
    planner_plan = wait_for(
        "planner_plan_request",
        lambda: read_json(main_workspace / ".openagent" / "requests" / "plan_requests.json", {}),
        timeout=args.wait_timeout,
    )
    append_check(
        checks,
        "task3_completed",
        task_data(main_workspace, 3).get("status") == "completed",
        f"task_3.status={task_data(main_workspace, 3).get('status')}",
    )
    append_check(
        checks,
        "planner_spawned",
        phase2.returncode == 0 and team_member(main_workspace, "Planner") is not None,
        f"phase2_returncode={phase2.returncode}",
    )
    append_check(
        checks,
        "direct_message_sent",
        tool_seen(main_workspace, "lead", "send_message"),
        "send_message found in tool log index" if tool_seen(main_workspace, "lead", "send_message") else "send_message missing",
    )
    phase3 = run_openagent(
        main_workspace,
        ["run", PHASE3_PROMPT],
        timeout=args.run_timeout,
        stdout_path=main_workspace / "artifacts" / "phase3_stdout.txt",
    )
    approved_plan = wait_for(
        "approved_plan",
        lambda: (
            read_json(main_workspace / ".openagent" / "requests" / "plan_requests.json", {})
            or {}
        ),
        timeout=args.wait_timeout,
    )
    writer_claimed = wait_for(
        "writer_claimed_task2",
        lambda: task_data(main_workspace, 2).get("owner") == "Writer" and task_data(main_workspace, 2).get("status") == "in_progress",
        timeout=args.wait_timeout,
    )
    beta_summary = main_workspace / "artifacts" / "beta_summary.md"
    team_report = main_workspace / "artifacts" / "team_test_report.md"
    continue_runs: list[int] = []
    for cycle in range(1, 7):
        task2 = task_data(main_workspace, 2)
        task4 = task_data(main_workspace, 4)
        if team_report.exists() and task2.get("status") == "completed" and task4.get("status") == "completed":
            break
        continue_result = run_continue_cycle(main_workspace, args.run_timeout, cycle)
        continue_runs.append(continue_result.returncode)
        wait_for(
            f"post_continue_cycle_{cycle}",
            lambda: team_report.exists()
            or task_data(main_workspace, 2).get("status") == "completed"
            or beta_summary.exists(),
            timeout=min(args.wait_timeout, 20),
        )
    append_check(
        checks,
        "plan_approval_sent",
        any(req.get("status") == "approved" for req in (approved_plan or {}).values()),
        f"plan_requests={approved_plan}",
    )
    append_check(
        checks,
        "submit_plan_seen",
        tool_seen(main_workspace, "Planner", "submit_plan")
        or bool(planner_plan)
        or any(req.get("from") == "Planner" for req in (approved_plan or {}).values()),
        f"submit_plan_tool_seen={tool_seen(main_workspace, 'Planner', 'submit_plan')}, plan_requests={approved_plan}",
    )
    append_check(
        checks,
        "writer_spawned",
        phase3.returncode == 0 and team_member(main_workspace, "Writer") is not None,
        f"phase3_returncode={phase3.returncode}",
    )
    append_check(
        checks,
        "broadcast_sent",
        tool_seen(main_workspace, "lead", "broadcast"),
        "broadcast found in tool log index" if tool_seen(main_workspace, "lead", "broadcast") else "broadcast missing",
    )
    append_check(
        checks,
        "idle_auto_claim_seen",
        (bool(writer_claimed) or task_data(main_workspace, 2).get("owner") == "Writer")
        and tool_seen(main_workspace, "Writer", "idle")
        and (
            tool_seen(main_workspace, "Writer", "claim_task")
            or log_contains(main_workspace / ".openagent" / "team" / "logs" / "Writer.jsonl", '"source": "auto_claimed"')
        ),
        f"writer_claimed={bool(writer_claimed)}, task_2.owner={task_data(main_workspace, 2).get('owner')}, writer_claim_tool_seen={tool_seen(main_workspace, 'Writer', 'claim_task')}",
    )
    append_check(
        checks,
        "beta_summary_generated",
        beta_summary.exists() and task_data(main_workspace, 2).get("status") == "completed",
        f"beta_summary_exists={beta_summary.exists()}, task_2.status={task_data(main_workspace, 2).get('status')}, continue_runs={continue_runs}",
    )
    append_check(
        checks,
        "final_team_report_generated",
        team_report.exists() and task_data(main_workspace, 4).get("status") == "completed",
        f"team_report_exists={team_report.exists()}, task_4.status={task_data(main_workspace, 4).get('status')}, continue_runs={continue_runs}",
    )

    planner = team_member(main_workspace, "Planner") or {}
    writer = team_member(main_workspace, "Writer") or {}
    append_bug(
        bugs,
        "BUG-02_runtime_restarted_kills_existing_teammate",
        planner.get("status") == "shutdown" and planner.get("shutdown_reason") == "runtime_restarted",
        f"Planner={planner}",
    )
    append_bug(
        bugs,
        "BUG-03_auto_claim_stalls_after_claim_task",
        writer.get("current_task_id") == 2
        and writer.get("status") == "working"
        and not beta_summary.exists()
        and task_data(main_workspace, 2).get("status") != "completed",
        f"Writer={writer}, task_2={task_data(main_workspace, 2)}",
    )
    append_bug(
        bugs,
        "BUG-04_final_report_not_generated",
        not team_report.exists() or task_data(main_workspace, 4).get("status") != "completed",
        f"team_test_report_exists={team_report.exists()}, task_4={task_data(main_workspace, 4)}",
    )

    reset_workspace(shutdown_workspace)
    configure_workspace(shutdown_workspace, config_source)
    shutdown_run = run_openagent(
        shutdown_workspace,
        ["run", SHUTDOWN_PROMPT],
        timeout=args.run_timeout,
        stdout_path=shutdown_workspace / "artifacts" / "shutdown_stdout.txt",
    )
    shutdown_requests = read_json(shutdown_workspace / ".openagent" / "requests" / "shutdown_requests.json", {}) or {}
    sleeper = team_member(shutdown_workspace, "Sleeper") or {}
    if not any(req.get("status") == "accepted" for req in shutdown_requests.values()):
        for cycle in range(1, 3):
            run_shutdown_continue_cycle(shutdown_workspace, args.run_timeout, cycle)
            wait_for(
                f"shutdown_continue_{cycle}",
                lambda: any(
                    req.get("status") == "accepted"
                    for req in (read_json(shutdown_workspace / ".openagent" / "requests" / "shutdown_requests.json", {}) or {}).values()
                ),
                timeout=min(args.wait_timeout, 20),
            )
            shutdown_requests = read_json(shutdown_workspace / ".openagent" / "requests" / "shutdown_requests.json", {}) or {}
            sleeper = team_member(shutdown_workspace, "Sleeper") or {}
            if any(req.get("status") == "accepted" for req in shutdown_requests.values()):
                break
    append_check(
        checks,
        "shutdown_request_flow",
        shutdown_run.returncode == 0
        and any(req.get("status") == "accepted" for req in shutdown_requests.values())
        and sleeper.get("shutdown_reason") == "shutdown_request",
        f"shutdown_requests={shutdown_requests}, Sleeper={sleeper}",
    )

    passed = sum(1 for item in checks if item["ok"])
    failed = len(checks) - passed
    observed_bugs = [item for item in bugs if item["observed"]]

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "main_workspace": str(main_workspace),
        "shutdown_workspace": str(shutdown_workspace),
        "checks": checks,
        "bugs": bugs,
        "summary": {
            "passed_checks": passed,
            "failed_checks": failed,
            "observed_bugs": len(observed_bugs),
        },
    }
    write_text(main_workspace / "artifacts" / "real_cli_report.json", json.dumps(report, ensure_ascii=False, indent=2))

    lines = [
        "# Real CLI Agent-Team Smoke Report",
        "",
        f"- Main workspace: `{main_workspace}`",
        f"- Shutdown workspace: `{shutdown_workspace}`",
        f"- Passed checks: `{passed}`",
        f"- Failed checks: `{failed}`",
        f"- Observed bugs: `{len(observed_bugs)}`",
        "",
        "## Checks",
        "",
    ]
    for item in checks:
        status = "PASS" if item["ok"] else "FAIL"
        lines.append(f"- `{status}` `{item['id']}`: {item['details']}")
    lines.extend(["", "## Observed Bugs", ""])
    if observed_bugs:
        for item in observed_bugs:
            lines.append(f"- `{item['id']}`: {item['details']}")
    else:
        lines.append("- None observed.")
    lines.append("")
    write_text(main_workspace / "artifacts" / "real_cli_report.md", "\n".join(lines))

    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Report JSON: {main_workspace / 'artifacts' / 'real_cli_report.json'}")
    print(f"Report MD: {main_workspace / 'artifacts' / 'real_cli_report.md'}")

    if args.strict and (failed or observed_bugs):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
