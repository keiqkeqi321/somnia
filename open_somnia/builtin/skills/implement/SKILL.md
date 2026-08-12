---
name: implement
description: Implement a task-engine ticket end to end - claim it, drive TDD at the pre-agreed seams, run tests, finish with code-review, then close with task_close and commit. Use on 'implement', 'do this ticket', 'build task #N', or to execute a ready-for-agent ticket.
---

# Implement

Implement a single ticket from the task engine, then close it out cleanly.

## Process

1. **Get a ticket.** If you don't already have one, call `task_claimable` to see
   the frontier (split into `ready_for_agent` vs unspecced-but-unblocked), then
   `claim_task` the one you're doing. Read it with `task_get` — note its
   `acceptance` criteria (these define done) and any `spec_id` (the spec under
   `.scratch/specs/<spec_id>.md` has the detail and the pre-agreed testing seams).

2. **Build test-first at the pre-agreed seams.** Drive the `tdd` skill — red →
   green, one vertical slice at a time. The seams were agreed in the spec; don't
   invent new ones without checking with the user.

3. **Run checks regularly** via the shell tool: typecheck often, the single
   relevant test every cycle, and the full test suite once at the end.

4. **Review before committing.** Run the `code-review` skill on the diff since
   the branch point. Address what it finds (or consciously defer).

5. **Close the ticket.** Call `task_close`: check off every `acceptance` item
   (the engine won't let it close otherwise) and record `result` (what was done,
   anything notable) and `commit_ref`.

6. **Commit** your work to the current branch. Use a message that names the
   ticket and states the key finding (so the next reader learns).

## Context hygiene

Work **one ticket per fresh context window** — that is the whole point of
slicing into tickets. `/clear` between tickets; the next ticket's context is
disposable. If a single ticket turns out to need more than one session, it was
sized too coarsely — split it with `to-tickets` first.
