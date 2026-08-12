# Task labels — suggested vocabulary

Tasks (`task_create_batch` / `task_update` / `claim_task`) carry free-form
`labels: list[str]`. Labels are **convention, not schema**: any string works, and
nothing enforces this list. These are the labels Somnia's own skills and the
auto-assigner pay attention to.

## The one label that has teeth: `ready-for-agent`

A task is **claimable** when it is `pending`, unowned, and all its blockers are
completed — that is the dependency frontier (`list_claimable`).

A task is **auto-assignable** only when it is claimable **and** carries the
`ready-for-agent` label (`list_ready`). The teammate auto-assigner draws only
from `list_ready`, and `claim_task` refuses a task without the label unless
`force=true`.

The two are deliberately independent gates:

- **claimable** — "the work is unblocked" (deps done, computed).
- **ready-for-agent** — "the work is specified" (specced / grilled into shape,
  stamped by a human or triage).

A task can be unblocked but not yet ready (still being specced), or ready but
still blocked (specced, waiting on a blocker). Only the intersection
auto-assigns. Stamp `ready-for-agent` once a task is fully specified and an agent
can take it AFK.

## Suggested vocabulary

State labels (triage-ish):

- `ready-for-agent` — specified; eligible for auto-assignment (see above).
- `needs-info` — waiting on a reporter / external input.
- `wontfix` — consciously not doing; close rather than leave pending.

Category labels:

- `bug` — something is broken.
- `enhancement` — new feature or improvement.

Add your own as the project needs them. Keep labels lowercase, kebab-case, and
few — they exist to filter and route, not to encode a taxonomy.
