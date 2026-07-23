# 01 — Establish the shared Somnia Connection seam

**What to build:** Introduce a shared connection contract and conversation-state core that can serve both Desktop and remote clients, then move one existing Desktop conversation path onto it without changing user-visible behavior.

**Blocked by:** None — can start immediately.

**Status:** completed

- [x] Direct connection behavior is described by a reusable contract suite.
- [x] Conversation events produce deterministic shared state outside the Desktop application shell.
- [x] One complete Desktop open-session and streamed-turn path uses the new seam.
- [x] Existing Desktop and sidecar regression tests remain green.
- [x] The old path remains available only where migration has not yet occurred.

## Comments

Implemented a shared Somnia Connection interface with a direct adapter, a reusable connection contract suite, and a deterministic conversation transition module. Desktop Session loading, Turn start, event subscription, streamed assistant output, active Turn state, and completion snapshots now cross the shared seam.

Verification: frontend typecheck, four Vitest contract/reducer tests, production UI build, and 32 AppService/sidecar regression tests passed. The repository-wide Python suite still has two unrelated baseline failures: a stray `test_utils` dependency on missing `mypackage`, and a provider preset expectation for `kimi-coding-plan` while the implementation exposes `kimi-code`.
