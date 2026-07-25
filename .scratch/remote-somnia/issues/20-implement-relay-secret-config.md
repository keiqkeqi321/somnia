# 20 — Implement Relay secret configuration

Blocked by: 19

Status: completed

## Question

What code and regression tests are required to expose
`SOMNIA_RELAY_SECRET_KEY` through the Relay CLI, validate it at startup, and
ensure rotation/restart requires browser re-authentication without persisting
browser session content?

## Acceptance evidence

- Relay CLI reads and validates the protected environment value.
- Missing or malformed production secret fails closed with a safe error.
- The secret never appears in command output, logs, or exception text.
- Tests cover configured startup, invalid configuration, restart, token
  expiry/refresh, logout, and device revocation.

## Resolution

`somnia-relay` now reads `SOMNIA_RELAY_SECRET_KEY` as URL-safe Base64 and
requires it when `SOMNIA_ENV=production`. The value must decode to exactly 32
bytes; malformed or missing production configuration fails before the server
starts. Local development keeps the existing ephemeral fallback when
`SOMNIA_ENV` is unset. The decoded value is passed through the existing Relay
factory and is never included in errors or logs.

## Comments

- 2026-07-25: Implemented with CLI and regression tests.
