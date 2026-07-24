# 07 — Match the Desktop Session lifecycle

**What to build:** Provide remote Session listing, creation, loading, deletion, archive, restore, selection, and authoritative recovery with the same behavior users rely on in Desktop.

**Blocked by:** 04 — Make real-time delivery recoverable; 05 — Give the Connector authoritative Runtime ownership.

**Status:** completed

- [x] Session summaries and complete Session history are loaded only from the selected computer.
- [x] Create, select, delete, archive, and restore behavior matches Desktop.
- [x] Archive state remains browser-local and is never sent to Relay persistence.
- [x] Session selection survives a transient reconnect when the Device and Project remain valid.
- [x] Destructive actions are idempotent and present explicit conflict or not-found results.
- [x] Direct and remote adapters pass the same Session lifecycle contract tests.

## Verification

- Connector, tracer, and Sidecar Python tests pass (26 tests).
- TypeScript typecheck and shared Direct/Remote lifecycle contract tests pass.
- Playwright lifecycle flow, including archive/restore, passes at desktop and mobile viewports (2 tests).
