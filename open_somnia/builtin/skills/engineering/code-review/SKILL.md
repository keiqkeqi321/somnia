---
name: code-review
description: Two-axis review of the diff since a fixed point - Standards (repo standards + a Fowler smell baseline) and Spec (does it do what the task/spec asked) - run as parallel subagents. Use on 'review this', 'review the branch/PR', 'review since X', or before committing a task.
---

# Code Review

Two-axis review of the diff between `HEAD` and a fixed point the user supplies:

- **Standards** — does the code conform to this repo's documented coding standards?
- **Spec** — does the code faithfully implement the originating spec / the task's
  acceptance criteria?

Both axes run as **parallel subagents** so they don't pollute each other's
context, then this skill aggregates their findings side by side.

## Process

### 1. Pin the fixed point

Whatever the user said — a commit SHA, branch, tag, `main`, `HEAD~5`, etc. If
they didn't specify one, ask via `ask_user_question`. Capture the diff once (run via the shell tool):

- `git diff <fixed-point>...HEAD` (three-dot, against the merge-base)
- `git log <fixed-point>..HEAD --oneline`

Confirm the fixed point resolves (`git rev-parse`) and the diff is non-empty
before spawning anything.

### 2. Identify the spec source

Look for the originating spec, in this order:

1. The current task's `acceptance` criteria (if you're reviewing a task's
   work) — fetch with `task_get`.
2. The spec document under `.scratch/specs/<spec_id>.md` if the task carries a
   `spec_id`, or a path the user passed.
3. If nothing is found, ask via `ask_user_question`. If there genuinely isn't one, the **Spec** subagent
   skips and reports "no spec available".

### 3. Identify the standards sources

Anything documenting how code should be written here (`CODING_STANDARDS.md`,
`CONTRIBUTING.md`, etc.). On top of whatever the repo documents, the Standards
axis always carries the **smell baseline** below — a fixed set of Fowler code
smells that applies even when a repo documents nothing. Two rules bind it:

- **The repo overrides.** A documented standard always wins; where it endorses
  something the baseline would flag, suppress the smell.
- **Always a judgement call.** Each smell is a labelled heuristic ("possible
  Feature Envy"), never a hard violation. Skip anything tooling already enforces.

Each smell reads *what it is* → *how to fix*; match it against the diff:
Mysterious Name, Duplicated Code, Feature Envy, Data Clumps, Primitive
Obsession, Repeated Switches, Shotgun Surgery, Divergent Change, Speculative
Generality, Message Chains, Middle Man, Refused Bequest.

### 4. Spawn both subagents in parallel

**Standards subagent** — include the diff command, commit list, the
standards-source files found in step 3 **plus the smell baseline pasted in
full** (the subagent has no other access to it). Brief: "Report per file/hunk
(a) every place the diff violates a documented standard — cite it; and (b) any
baseline smell — name it and quote the hunk. Distinguish hard violations from
judgement calls. Skip anything tooling enforces. Under 400 words."

**Spec subagent** — include the diff command, commit list, and the spec/acceptance
contents. Brief: "Report (a) requirements missing or partial; (b) behaviour in
the diff not asked for (scope creep); (c) requirements that look wrong. Quote
the spec line for each. Under 400 words."

If the spec is missing, skip the Spec subagent.

### 5. Aggregate

Present both reports under `## Standards` and `## Spec` headings, verbatim or
lightly cleaned. Do **not** merge or rerank findings — the two axes are
deliberately separate. End with a one-line summary: total findings per axis and
the worst issue *within each axis*. Don't pick a single winner across axes —
that's the reranking the separation exists to prevent.

## Why two axes

A change can pass one axis and fail the other: clean code that implements the
wrong thing (Standards pass, Spec fail), or correct behaviour that breaks
conventions (Spec pass, Standards fail). Reporting separately stops one from
masking the other.
