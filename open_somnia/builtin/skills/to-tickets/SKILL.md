---
name: to-tickets
description: Break work into tracer-bullet tickets in the native task engine - vertical slices with acceptance + blocking edges, stamped ready-for-agent. Use when splitting a multi-step feature into tickets before implementing, or on 'break this down' / 'make tickets' / 'plan the work'.
---

# To Tickets

Break a plan, spec, or conversation into a set of **tickets** — tracer-bullet
vertical slices, each declaring the tickets that **block** it — as records in the
native task engine.

Tickets live in the task engine (the `task_*` tools), not in files or an external
tracker. One `task_create_batch` call builds the whole dependency graph at once.

## Process

### 1. Gather context

Work from whatever is already in the conversation. If the user passes a reference
(a spec, an earlier decision), read it in full first.

### 2. Explore the codebase (optional)

If you have not already, explore to understand the current state. Ticket titles
should use the project's domain vocabulary and respect decisions already made.
Look for chances to **prefactor** — "make the change easy, then make the easy
change" — and make the prefactor its own first ticket.

### 3. Draft vertical slices

Break the work into **tracer bullet** tickets.

<vertical-slice-rules>

- Each slice cuts a narrow but COMPLETE path through every layer (schema, API,
  UI, tests) — vertical, NOT a horizontal slice of one layer.
- A completed slice is demoable or verifiable on its own.
- Each slice is sized to fit in a single fresh context window.
- Any prefactoring is its own ticket, blocked-by nothing, blocking the rest.

</vertical-slice-rules>

Give each ticket its **blocking edges** — the other tickets that must complete
before it can start. A ticket with no blockers can start immediately.

**Wide refactors are the exception to vertical slicing.** A wide refactor is one
mechanical change — rename a column, retype a shared symbol — whose blast radius
fans across the whole codebase, so a single edit breaks thousands of call sites
and no vertical slice can land green. Don't force it into a tracer bullet;
sequence it as **expand-contract**: expand (add the new form beside the old so
nothing breaks), migrate call sites in batches (each batch its own ticket,
blocked-by the expand, green batch to batch), then contract (delete the old form,
blocked-by every migrate batch).

### 4. Quiz the user

Present the breakdown as a numbered list. For each ticket show: **title**,
**blocked-by**, **what it delivers** (end-to-end behaviour). Ask: granularity
right? blocking edges correct — each ticket only depends on tickets that
genuinely gate it? merge or split further? Iterate until the user approves.

### 5. Create the graph

Build the approved tickets with **one `task_create_batch` call**. References
between tickets use earlier `key`s. Each ticket:

- **`subject`** — short name.
- **`acceptance`** — the done-definition checklist. Every item must be checked
  off before the task can close; the engine enforces this gate, so write items
  you can actually verify.
- **`blocked_by`** — the keys/ids of tickets that gate this one; omit if it can
  start immediately.
- **`labels: ["ready-for-agent"]`** — stamp every ticket that is fully specified
  and an agent can take AFK. Only `ready-for-agent` tickets auto-assign; leave
  the label off anything still being specced.
- **`spec_id`** (optional) — a slug grouping these tickets under the originating
  spec/feature.
- **`parent_id`** (optional) — point tickets at a "map"/epic ticket to group them.

Avoid specific file paths or code snippets — they go stale fast. Exception: if a
prototype produced a snippet that encodes a decision more precisely than prose
(state machine, reducer, schema, type shape), inline it within the relevant
ticket's description and note it came from a prototype.

### 6. Work the frontier

After `task_create_batch`, unblocked `ready-for-agent` tickets are auto-assigned
to idle teammates (if any). To see what is takeable yourself, call
`task_claimable` — it splits the frontier into `ready_for_agent` (auto-claimable)
and `claimable_unspecced` (unblocked but not yet stamped ready). Take one with
`claim_task`. Work **one ticket per fresh context window**, `/clear` between —
each ticket is self-contained, so the last one's context is disposable. Close
with `task_close`, checking off every acceptance item and recording `result`
(what was done, anything notable) and `commit_ref`.

### Re-edges

Adding a dependency discovered later is free: `task_update` with
`add_blocked_by`. **Removing or re-planning edges needs the user's approval** —
it uses `task_remove_blocked_by`, which re-runs auto-assignment after unblocking
but prompts for authorization because it reorganizes the plan.
