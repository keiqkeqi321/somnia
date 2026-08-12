---
name: diagnosing-bugs
description: Disciplined diagnosis loop for hard bugs/perf regressions: build a red-capable feedback loop (via the shell) before anything else, then minimise, hypothesise, instrument, fix, regression-test. No theory before a tight red loop. Use on 'debug this'/'diagnose', or broken/throwing/slow.
---

# Diagnosing Bugs

A discipline for hard bugs and performance regressions. Skip phases only when
explicitly justified. Read the project's domain glossary (`CONTEXT.md` if
present) first to build a mental model of the relevant modules.

## Redact

You will show commands, outputs, captured artifacts. **Redact every secret**
first — write `<REDACTED>` in its place. Build loops against env vars so
credentials stay in the environment. If redacted output isn't enough to
diagnose, say so and ask the user.

## Phase 1 — Build a feedback loop

**This is the skill.** Everything else is mechanical. If you have a **tight**
pass/fail signal for this bug — one command that goes red on *this* bug — you
will find the cause; if you don't, no amount of staring at code will save you.

Spend disproportionate effort here. Be aggressive, be creative, refuse to give
up. Build loops with the shell tool (`bash`), trying roughly in this order: a
failing test at the right seam; a curl/HTTP script against a dev server; a CLI
invocation with a fixture, diffing against a known-good snapshot; a headless
browser script; replaying a captured trace; a throwaway harness exercising one
code path; a property/fuzz loop; a `git bisect run` harness; a differential loop
(old-vs-new); and only as a last resort a HITL bash script that drives a human
click. Build the right loop and the bug is 90% fixed.

**Tighten it:** make it faster (cache setup, narrow scope), sharper (assert the
exact symptom, not "didn't crash"), more deterministic (pin time, seed RNG,
isolate filesystem, freeze network). A 30s flaky loop is barely better than
none; a 2s deterministic one is a superpower. For non-deterministic bugs, raise
the reproduction rate (loop the trigger 100x, parallelise, inject sleeps) until
it's debuggable — a 50% flake is debuggable, 1% is not.

**Completion criterion — a tight loop that goes red.** Phase 1 is done when you
can name **one command** (a script path, a test invocation, a curl) you have
**already run at least once** (show the invocation and its redacted output) that
is: red-capable (drives the real bug path and asserts the user's exact symptom),
deterministic, fast, and agent-runnable. If you catch yourself reading code to
build a theory before this command exists, **stop — jumping straight to a
hypothesis is the exact failure this skill prevents.** No red-capable command,
no Phase 2.

If you genuinely cannot build a loop, stop and say so. List what you tried. Ask
for access to a reproducing environment, a redacted captured artifact (HAR, log
dump, core dump, screen recording with timestamps), or permission to add
temporary instrumentation.

## Phase 2 — Reproduce + minimise

Run the loop, watch it go red. Confirm it produces the **user's** failure (not a
nearby one that happens to be close) and is reproducible across runs (or at a
high enough rate for non-deterministic bugs). Then shrink the repro to the
**smallest scenario that still goes red** — cut inputs, callers, config, data,
and steps one at a time, re-running after each cut. Done when every remaining
element is load-bearing. (A minimal repro shrinks the hypothesis space and
becomes the clean regression test in Phase 5.)

## Phase 3 — Hypothesise

Generate **3-5 ranked hypotheses** before testing any. Single-hypothesis
generation anchors on the first plausible idea. Each must be **falsifiable**:
"If X is the cause, then changing Y will make the bug disappear / changing Z
will make it worse." If you can't state the prediction, it's a vibe — discard or
sharpen it. Show the ranked list to the user before testing (cheap checkpoint,
big saver — they often know hypotheses already ruled out); proceed with your
ranking if they're AFK.

## Phase 4 — Instrument

Each probe maps to a specific prediction from Phase 3. **Change one variable at a
time.** Prefer debugger/REPL inspection (one breakpoint beats ten logs), then
targeted logs at the boundaries that distinguish hypotheses; never "log
everything and grep". Tag every debug log with a unique prefix (`[DEBUG-a4f2]`)
so cleanup is a single grep — untagged logs survive, tagged logs die. Perf
branch: for performance regressions logs are usually wrong — establish a baseline
measurement (timing harness, profiler, query plan) and bisect instead.

## Phase 5 — Fix + regression test

Write the regression test **before** the fix, but only if there is a **correct
seam** for it — one where the test exercises the real bug pattern as it occurs
at the call site. If the only available seam is too shallow, a regression test
there gives false confidence. **If no correct seam exists, that itself is the
finding** — note it (it's an architecture issue for a later deepening pass). If a
correct seam exists: turn the minimised repro into a failing test there, watch it
fail, apply the fix, watch it pass, then re-run the Phase 1 loop against the
original un-minimised scenario.

## Phase 6 — Cleanup + post-mortem

Before declaring done: the original repro no longer reproduces; the regression
test passes (or the no-seam finding is documented); all `[DEBUG-...]`
instrumentation is removed (`grep` the prefix); throwaway prototypes are deleted
or moved to a clearly-marked debug location; the hypothesis that turned out
correct is stated in the commit/PR message so the next debugger learns. Then
ask: **what would have prevented this bug?** If the answer is architectural (no
good test seam, tangled callers, hidden coupling), note it for a later
architecture-deepening pass — after the fix, when you have more information than
when you started.
