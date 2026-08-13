---
name: tdd
description: Test-driven development - the red-green loop at pre-agreed seams. Use when building a feature or fixing a bug test-first, or on 'TDD', 'write the test first', 'red-green', 'make it pass'. Works one vertical slice at a time; refactoring belongs to code-review, not the loop.
---

# Test-Driven Development

TDD is the red → green loop. This skill is the reference that makes that loop
produce tests worth keeping: what a good test is, where tests go, the
anti-patterns, and the rules of the loop. Every section applies on every cycle —
consult them before and during the loop, not after.

Read the project's domain glossary (`CONTEXT.md` if present) so test names and
interface vocabulary match the project's language, and respect any decisions
already made in the area you're touching.

## What a good test is

Tests verify behaviour through public interfaces, not implementation details.
Code can change entirely; tests shouldn't. A good test reads like a
specification — "user can checkout with a valid cart" tells you exactly what
capability exists — and survives refactors because it doesn't care about
internal structure.

## Seams — where tests go

A **seam** is the public boundary you test at: the interface where you observe
behaviour without reaching inside. Tests live at seams, never against internals.

**Test only at pre-agreed seams.** Before writing any test, write down the seams
under test and confirm them (for a spec'd feature these were agreed in `to-spec`;
for ad-hoc work, agree them with the user now via `ask_user_question`). No test is written at an
unconfirmed seam. You can't test everything — agreeing the seams up front is how
testing effort lands on the critical paths and complex logic instead of every
edge case.

Ask: "What's the public interface, and which seams should we test?" (use `ask_user_question`).

When the shape of the interface is itself in question — how deep the module is,
where the seam belongs, what to expose — use the `codebase-design` skill for the
vocabulary (module, interface, depth, seam, adapter, leverage, locality). It is
a reference to consult, not a session to run.

## Anti-patterns

- **Implementation-coupled** — mocks internal collaborators, tests private
  methods, or verifies through a side channel (querying the DB instead of using
  the interface). The tell: the test breaks when you refactor but behaviour
  hasn't changed.
- **Tautological** — the assertion recomputes the expected value the way the
  code does (`expect(add(a, b)).toBe(a + b)`), so it passes by construction and
  can never disagree. Expected values must come from an independent source of
  truth — a known-good literal, a worked example, the spec.
- **Horizontal slicing** — writing all tests first, then all implementation.
  Bulk tests verify *imagined* behaviour and go insensitive to real changes.
  Work in **vertical slices** instead — one test → one implementation → repeat,
  each test a tracer bullet that responds to what the last cycle taught you.

## Rules of the loop

- **Red before green.** Write the failing test first, then only enough code to
  pass it. Don't anticipate future tests or add speculative features.
- **One slice at a time.** One seam, one test, one minimal implementation per
  cycle. Run the single test (via the shell tool) every cycle.
- **Refactoring is not part of the loop.** It belongs to the review stage (see
  the `code-review` skill), not the red → green implementation cycle.
