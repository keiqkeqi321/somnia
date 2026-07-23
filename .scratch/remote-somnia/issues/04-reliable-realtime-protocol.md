# 04 — Make real-time delivery recoverable

**What to build:** Add ordered streams, acknowledgements, bounded in-memory replay, snapshot fallback, idempotent commands, and slow-client handling so mobile network changes do not corrupt a conversation or repeat an action.

**Blocked by:** 02 — Deliver the remote session tracer.

**Status:** ready-for-agent

- [ ] Every stream has an epoch and monotonically increasing event sequence.
- [ ] Browsers acknowledge the highest contiguous event they have applied.
- [ ] A reconnect replays available events and otherwise performs an explicit snapshot resync.
- [ ] Retried mutating commands with the same request identity execute only once.
- [ ] Missing, duplicate, delayed, and reordered frames are covered by fault tests.
- [ ] Slow clients are disconnected for resync without blocking Runtime output.
