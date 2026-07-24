# 06 — Navigate multiple Devices and Projects

**What to build:** Let an authenticated user see Device presence, switch between online computers, and select locally registered Projects without exposing filesystem paths or queuing content for offline Devices.

**Blocked by:** 03 — Secure account access and Device pairing; 05 — Give the Connector authoritative Runtime ownership.

**Status:** completed

- [x] Device list distinguishes online, reconnecting, revoked, and offline states.
- [x] Project identity and display name are available without a workspace path.
- [x] Switching Device or Project tears down obsolete subscriptions cleanly.
- [x] Content-bearing commands to an offline Device fail immediately and are not persisted.
- [x] Several Devices can remain connected without receiving one another's traffic.
- [x] Phone and desktop browser navigation paths are covered by end-to-end tests.

## Verification

- Python Connector, Relay, and Connector CLI tests pass (16 tests).
- TypeScript typecheck and Vitest suite pass (9 tests).
- Playwright navigation and tracer flow pass at desktop and mobile viewports (2 tests).
