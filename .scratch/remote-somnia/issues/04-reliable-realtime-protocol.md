# 04 — Make real-time delivery recoverable

**What to build:** Add ordered streams, acknowledgements, bounded in-memory replay, snapshot fallback, idempotent commands, and slow-client handling so mobile network changes do not corrupt a conversation or repeat an action.

**Blocked by:** 02 — Deliver the remote session tracer.

**Status:** completed

- [x] Every stream has an epoch and monotonically increasing event sequence.
- [x] Browsers acknowledge the highest contiguous event they have applied.
- [x] A reconnect replays available events and otherwise performs an explicit snapshot resync.
- [x] Retried mutating commands with the same request identity execute only once.
- [x] Missing, duplicate, delayed, and reordered frames are covered by fault tests.
- [x] Slow clients are disconnected for resync without blocking Runtime output.

## Verification

- Python remote protocol, Relay, and tracer tests pass.
- Browser tests pass: 9 tests; TypeScript typecheck and production build pass.
- Full Python suite: the existing `mypackage` import failure, `kimi-coding-plan` preset mismatch, and intermittent Windows Hook temp-directory cleanup race remain outside this issue.
