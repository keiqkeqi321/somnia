# 09 — Match active Turn control

**What to build:** Support Desktop-equivalent prompt queuing, loop injection, interruption, concurrency limits, conflict handling, and simultaneous observation while a Turn is active.

**Blocked by:** 04 — Make real-time delivery recoverable; 07 — Match the Desktop Session lifecycle.

**Status:** completed

- [x] A prompt submitted during an active Turn follows the same queue or injection behavior as Desktop.
- [x] Injection acknowledgement and visible user-message insertion are idempotent.
- [x] A user can interrupt the correct Turn and see interruption progress and completion.
- [x] One active Turn per Session and the existing per-Project limit remain enforced.
- [x] Conflicting commands return explicit actionable errors.
- [x] Multiple browsers can observe an active Turn without multiplying Runtime work.

## Verification

- Remote Connector and Sidecar control routes cover interrupt and loop injection, including explicit conflict/not-found responses.
- AppService and Runtime tests verify duplicate `injection_id` requests are acknowledged without duplicate Runtime injection.
- Shared conversation-state tests verify interruption progress and idempotent visible injected user messages.
- Playwright Desktop/Mobile flow verifies active prompt queueing, next-loop injection, continuation, and layout overflow safety.
- TypeScript typecheck and all frontend tests pass.
