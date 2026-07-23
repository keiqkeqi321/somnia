# 09 — Match active Turn control

**What to build:** Support Desktop-equivalent prompt queuing, loop injection, interruption, concurrency limits, conflict handling, and simultaneous observation while a Turn is active.

**Blocked by:** 04 — Make real-time delivery recoverable; 07 — Match the Desktop Session lifecycle.

**Status:** ready-for-agent

- [ ] A prompt submitted during an active Turn follows the same queue or injection behavior as Desktop.
- [ ] Injection acknowledgement and visible user-message insertion are idempotent.
- [ ] A user can interrupt the correct Turn and see interruption progress and completion.
- [ ] One active Turn per Session and the existing per-Project limit remain enforced.
- [ ] Conflicting commands return explicit actionable errors.
- [ ] Multiple browsers can observe an active Turn without multiplying Runtime work.
