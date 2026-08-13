---
name: implement
description: Implement a task-engine task end to end - claim it, drive TDD at the pre-agreed seams, run tests, finish with code-review, then close with task_close and commit. Use on 'implement', 'do this task', 'build task #N', or to execute a ready-for-agent task.
---

# Implement

Implement a single task from the task engine, then close it out cleanly.

## Process

1. **Get a task.** If you don't already have one, call `task_claimable` to see
   the frontier (split into `ready_for_agent` vs unspecced-but-unblocked), then
   `claim_task` the one you're doing. Read it with `task_get` — note its
   `acceptance` criteria (these define done) and any `spec_id` (the spec under
   `.scratch/specs/<spec_id>.md` has the detail and the pre-agreed testing seams).

2. **Build test-first at the pre-agreed seams.** Drive the `tdd` skill — red →
   green, one vertical slice at a time. The seams were agreed in the spec; don't
   invent new ones without checking with the user (use `ask_user_question`).

3. **Run checks regularly** via the shell tool: typecheck often, the single
   relevant test every cycle, and the full test suite once at the end.

4. **Review before committing.** Run the `code-review` skill on the diff since
   the branch point. Address what it finds (or consciously defer).

5. **Close the task.** Call `task_close`: check off every `acceptance` item
   (the engine won't let it close otherwise) and record `result` (what was done,
   anything notable) and `commit_ref`.

6. **Commit** your work to the current branch. Use a message that names the
   task and states the key finding (so the next reader learns).

## Context hygiene

**One task per fresh context window** — that's the point of slicing. Call
`request_new_session` between tasks; each task's context is disposable and
rebuilds from artifacts, not recall — right after claiming, `task_get` each
completed blocker and read its `result` note (the task board is the
cross-session memory). If one task needs more than one session, it was sized
too coarsely — split it with `to-tasks` first.
