# 17 — Resolve baseline tests and define the release test matrix

Blocked by: none

Status: ready-for-agent

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

