# Real CLI Agent-Team Bug List

This file tracks the current findings from real `openagent` CLI validation against the task, background job, team, protocol, and autonomous teammate flows described in `s07` to `s11`.

Automation entrypoint:

```bash
python tests/run_mock_agentteam_bug_repro.py
python tests/run_mock_agentteam_bug_repro.py --strict
python tests/run_real_agentteam_smoke.py
python tests/run_real_agentteam_smoke.py --strict
```

Recommended default:

- Use `tests/run_mock_agentteam_bug_repro.py` for fast local reproduction without a real provider.
- Use `tests/run_real_agentteam_smoke.py` only when you specifically need end-to-end provider validation.

## Confirmed Working Paths

- Persistent task creation and dependency edges work in real CLI runs.
- `background_run` can start and complete a real background process.
- `submit_plan` creates a real request and `plan_approval` resolves it.
- `send_message` and `broadcast` both reach teammates.
- `shutdown_request` works end-to-end in a dedicated real CLI run.

## Confirmed Bugs

### BUG-01: REPL smoke runs can stall after teammate setup

- Status: open
- Severity: high
- Scope: interactive `openagent` chat / REPL orchestration
- Symptom: after `spawn_teammate`, the REPL can stop producing visible output for more than 120 seconds even though the process is still alive.
- Impact: a real end-to-end manual smoke run can hang with no user feedback.
- Repro status: observed in the monitored REPL run; not yet covered by the automated `run`-based script because the script intentionally avoids the fragile REPL path.

### BUG-02: Existing teammates do not survive a later `openagent run`

- Status: fixed
- Severity: high
- Scope: teammate persistence across separate CLI invocations
- Symptom: after a teammate is spawned in one `openagent run`, a later `openagent run` can mark that teammate as `stale_on_boot` with `shutdown_reason = runtime_restarted`.
- Impact: multi-step lead coordination across separate CLI invocations breaks; a previously active teammate can be shut down before finishing its assigned task.
- Root cause: `TeammateRuntimeManager._repair_state()` unconditionally rewrote active teammates to `shutdown/runtime_restarted` on manager startup, even when enough persisted state existed to resume them.
- Fix: manager startup now restores active teammates from persisted team logs and restarts their work loop instead of immediately killing them.
- Regression coverage:
  - `tests.test_teammate_runtime.TeammateRuntimeTests.test_restore_state_resumes_active_teammate_instead_of_marking_runtime_restarted`
  - `python tests/run_mock_agentteam_bug_repro.py`

### BUG-03: Idle auto-claim can stop after `claim_task`

- Status: mitigated
- Severity: high
- Scope: autonomous teammate work loop
- Symptom: a writer teammate can successfully `idle`, inspect the task board, and auto-claim task 2, but then remain stuck in `waiting_for_model` without reading the file, writing the artifact, or completing the task.
- Impact: `s11` autonomous pickup is only partially working; tasks become owned but not completed.
- Root cause: when a teammate was left mid-task and a new runtime started, the previous implementation could not resume from persisted context, so claimed work effectively died with the old process.
- Fix: startup restore now rebuilds teammate conversation state from the team log and lets the teammate continue from persisted tool-result context.
- Regression coverage:
  - `tests.test_teammate_runtime.TeammateRuntimeTests.test_restore_state_can_continue_claimed_task_from_persisted_context`
  - `python tests/run_mock_agentteam_bug_repro.py`

### BUG-04: End-to-end report generation remains blocked

- Status: partially addressed
- Severity: medium
- Scope: full `s07` to `s11` smoke flow
- Symptom: because claimed teammate tasks do not finish, `artifacts/team_test_report.md` is never produced.
- Impact: the complete agent-team workflow does not converge to the final merged deliverable.
- Notes: this was downstream of BUG-02 and BUG-03 during the observed real CLI run. A fresh real end-to-end revalidation is still needed after the teammate restore fix.

## Notes

- The automated script is intentionally phase-based and uses real `openagent run` invocations because that path is reliable enough to reproduce protocol and teammate state transitions.
- The mock repro script covers the currently most important product bugs without any provider cost.
- The dedicated shutdown check is isolated in its own workspace so the shutdown protocol result is not polluted by the teammate-stall issues above.
