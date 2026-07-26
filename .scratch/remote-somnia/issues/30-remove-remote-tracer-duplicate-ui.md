# 30 - Replace Remote tracer UI with shared workspace

**What to build:** Remove the tracer-specific duplicate workspace after shared components are migrated.

**Blocked by:** 29

**Status:** completed

## Scope

- Reduce `RemoteTracerApp` to Remote shell, access state, target selection, and connection adapter wiring.
- Delete duplicate Remote message, Composer, session, and progress JSX and obsolete styles.
- Preserve Remote-only pairing, Device diagnostics, offline drafts, reconnect state, and local confirmation messaging.
- Add Desktop/Remote Playwright screenshots and interaction checks at laptop and phone sizes.

## Acceptance criteria

- Remote Web has functional and visual parity with Desktop conversation workflows.
- `RemoteTracerApp` contains no independent conversation rendering implementation.
- Desktop behavior and bundle size do not regress.
- Both clients pass typecheck, unit tests, and browser interaction tests.

## Progress

- [x] Remote shell is wired to the shared workspace, session sidebar, conversation panel, message list/row, progress/context, and composer components.
- [x] Remote-only pairing, device diagnostics, offline drafts, reconnect state, and confirmation remain in the Remote adapter.
- [x] Playwright checks the shared Remote workspace at desktop and phone viewports and is configured to record a screenshot artifact after each successful run.
