# 17 — Resolve baseline tests and define the release test matrix

Blocked by: none

Status: in_progress

## Question

What are the two current baseline test failures, which dependency or behavior
causes each, and what exact test matrix is required before release?

## Acceptance evidence

- Full Python dependencies and browser dependencies install from a clean
  environment.
- Both baseline failures have regression tests and documented resolutions.
- Python unit/integration, frontend unit/typecheck, Playwright, privacy,
  restart, replay, revocation, origin, payload-limit, slow-client, and soak
  tests have explicit commands and pass criteria.
- The matrix is runnable in CI and in a pre-production environment.

## Findings

- The required CLI/process/REPL/runtime regression set passes 310 tests.
- Remote tracer, AppService, teammate, and subagent suites pass.
- Two Sidecar failures caused by read-only global builtin-notify hook assets
  are fixed by making optional hook bootstrap permission-tolerant.
- MCP enable/disable now uses the registry's public mutation seam and its
  endpoint regression passes.
- One Sidecar HTTP provider-switch test still times out even though the same
  Runtime operation succeeds directly; the HTTP response/lifecycle path
  remains unresolved.
