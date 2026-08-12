---
name: wayfinder
description: Plan a huge, foggy effort (more than one session, path not yet visible) as a map of decision tasks on the task engine - resolve one decision per session until the way is clear, then hand to to-spec. Use only for big+foggy work, never a well-scoped feature.
---

# Wayfinder

A loose idea has arrived — too big for one session, wrapped in fog: the way from
here to the **destination** isn't visible yet. Wayfinding is about finding that
way, not charging at the destination. Chart the way as a **shared map** of
**decision tasks** in the task engine, then work them one at a time until the
route is clear.

The destination varies per effort, and naming it is the first act of charting —
it might be a spec to hand to `to-spec`, a decision to lock before planning, or a
change to make in place.

## Plan, don't do

Wayfinder is **planning** by default: each task resolves a decision, and the map
is done when the way is clear — nothing left to decide before someone does the
thing. The pull to just do the work is usually the signal you've reached the edge
of the map. When the map clears, **hand off to `to-spec`** — don't build from the
map directly.

## Refer by name

Every map and decision task has a **subject** — its name. In everything you
narrate, refer to tasks by subject, never by a bare id. A wall of `#42, #43, #44`
is illegible; names read at a glance.

## The map

The map is a **single task** labelled `wayfinder:map` — the canonical artifact.
Its decision tasks are its **children** (`parent_id` = the map's id). The map's
`description` is a living index with these sections (update it as the effort
evolves via `task_update`):

```
## Destination
<what reaching the end looks like — the spec / decision / change this effort finds its way to>

## Notes
<domain; skills every session should consult; standing preferences>

## Decisions so far
<!-- one line per closed decision task: gist + the task id -->
- <subject of closed task> (#<id>) — <one-line gist of the answer>

## Not yet specified
<!-- in-scope fog you can't yet make into a task; graduates as the frontier advances -->

## Out of scope
<!-- work ruled beyond the destination; closed, never graduates -->
```

The map is an **index, not a store**: a decision lives in exactly one place — its
task — so the map only gists it and points.

## Decision tasks

Each decision task is a **child** of the map (`parent_id` = map id). Its
`description` is the question, sized to one ~100k-token session. Each carries a
`wayfinder:<type>` label — `research`, `prototype`, `grilling`, or `task` (see
Types). A task is **unblocked** when every id in its `blocked_by` is completed;
the **frontier** is the open, unblocked, unclaimed children — the edge of the
known.

Use `task_claimable` to see the frontier; take one with `claim_task` (claiming
sets `owner`, so concurrent sessions skip it).

## Types

Every task is either **HITL** (worked with a human) or **AFK** (driven by the
agent alone). A HITL task only resolves through that live exchange.

- **`wayfinder:research`** (AFK) — read docs/APIs/resources to surface a fact a
  decision waits on. Resolve with a research subagent.
- **`wayfinder:prototype`** (HITL) — raise fidelity with a cheap concrete artifact
  to react to (use `prototype`). Link the prototype as the answer's primary
  source.
- **`wayfinder:grilling`** (HITL) — conversation. The default. Use `grilling` and
  `domain-modeling`.
- **`wayfinder:task`** (HITL or AFK) — manual work that must happen before a
  *decision* can be made (sign up for a service, move data, provision access). The
  one type that *does* rather than decides; it earns its place by unblocking a
  decision.

## Fog of war

The map is **deliberately incomplete**: don't chart what you can't yet see.
Beyond the live tasks lies the **fog of war** — decisions you can tell are coming
but can't pin down because they hang on open questions. Resolving a task clears
the fog ahead of it, graduating whatever's now specifiable into fresh tasks.

The map's **Not yet specified** section holds that dim view: the suspected
question, the area to revisit. It's the undiscovered frontier *toward* the
destination — in scope, just not sharp enough to make into a task.

**Fog or task?** The test is whether you can state the question *precisely now* —
not whether you can answer it now. Sharp now → task. Can't phrase it sharply →
Not yet specified.

## Out of scope

Fog only gathers *toward* the destination. Work beyond it is **out of scope** —
not fog, and not in Not yet specified; the destination fixes the scope. When a
task turns out to sit past the destination, **close it** (don't leave it on the
frontier) and leave one line in **Out of scope** with why.

## Invocation

Two modes. Either way, **never resolve more than one decision task per session** —
with the exception of research tasks.

### Chart the map

1. **Name the destination.** Run `grilling` + `domain-modeling` to pin down what
   this map is finding its way to.
2. **Map the frontier, breadth-first.** Grill again, fanning out across the whole
   space rather than deep on one thread, surfacing the open decisions and the
   first takeable steps. **If this surfaces no fog** — the way is already clear
   and the whole thing fits one session — you don't need a map. Stop and ask the
   user how to proceed.
3. **Create the map** (label `wayfinder:map`): Destination and Notes filled,
   Decisions empty, fog sketched into Not yet specified.
4. **Create the tasks you can specify now** as children (`parent_id` = map id),
   then wire `blocked_by` edges in a second pass (they need ids first). Everything
   you can't yet specify stays in the fog.
5. **Fire the research subagents** for each `wayfinder:research` task.
6. Stop — charting is one session's work and resolves nothing.

### Work through the map

1. Load the map (read its `description`). Choose the next frontier task (via
   `task_claimable`) — or the one the user named. **Claim it** with `claim_task`.
2. Resolve it — zoom as needed: invoke the skills the map's Notes name; in doubt,
   `grilling` + `domain-modeling`.
3. **Record the resolution**: close with `task_close`, putting the answer in
   `result`, and append a one-line gist to the map's **Decisions so far** (via
   `task_update` on the map's description).
4. Add newly-surfaced tasks (create-then-wire); graduate any fog the answer made
   specifiable, clearing it from Not yet specified. If the answer reveals a task
   sits beyond the destination, rule it out of scope. If it invalidates other
   tasks, update or delete them.

When the way is clear — no fog, no open decisions — hand the map to `to-spec`,
which collapses the linked decisions into a buildable plan. Don't loop the map
straight into `implement`; that skips the collapse and throws the linked detail
away.
