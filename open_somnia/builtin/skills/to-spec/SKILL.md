---
name: to-spec
description: Turn the current conversation into a spec (markdown PRD) and decide its testing seams - no interview, just synthesis of what was discussed. Use when the user wants to write a spec/PRD/requirements before implementation, or on 'write a spec', 'turn this into a spec', 'what should we build'.
---

# To Spec

Take the current conversation and codebase understanding and produce a **spec** —
a markdown PRD. Do NOT interview the user; synthesize what you already know.

The spec is written to `.scratch/specs/<slug>.md`. The `<slug>` becomes the
`spec_id` that `to-tickets` later stamps on every ticket it splits out of this
spec, so pick a short, stable slug (e.g. `orders-v2`).

## Process

1. Explore the repo to understand the current state, if you haven't already. Use
   the project's domain vocabulary throughout the spec, and respect any existing
   decisions (ADRs, recorded choices) in the area you're touching.

2. **Sketch the testing seams** at which the feature will be tested. Prefer
   existing seams to new ones; use the highest seam possible; the fewer seams
   across the codebase, the better (the ideal is one). If new seams are needed,
   propose them at the highest point you can. Check with the user that these
   seams match their expectations. (This is the single most important decision
   the spec makes — `tdd` will only ever test at these pre-agreed seams.)

3. Write the spec using the template below.

<spec-template>

## Problem Statement

The problem the user is facing, from the user's perspective.

## Solution

The solution, from the user's perspective.

## User Stories

A long, numbered list. Each: `As a <actor>, I want <feature>, so that <benefit>.`
Be exhaustive — cover all aspects of the feature.

## Implementation Decisions

The modules to build/modify, their interfaces, technical clarifications,
architectural decisions, schema changes, API contracts, key interactions.

Do NOT include specific file paths or code snippets — they go stale fast.
Exception: if a prototype produced a snippet that encodes a decision more
precisely than prose (state machine, reducer, schema, type shape), inline it
within the relevant decision and note it came from a prototype.

## Testing Decisions

What makes a good test here (test external behaviour, not implementation
details); which modules will be tested; the pre-agreed seams (from step 2);
prior art (similar existing tests to mirror).

## Out of Scope

What is explicitly out of scope for this spec.

## Further Notes

Anything else relevant.

</spec-template>

## After the spec

The natural next step is `/+to-tickets`: break this spec into tracer-bullet
tickets in the task engine, each stamped with `spec_id` = this spec's slug,
`acceptance` criteria drawn from the User Stories, and blocking edges declared up
front. Do not start implementing straight from the spec for anything bigger than
a single context window — go through tickets first.
