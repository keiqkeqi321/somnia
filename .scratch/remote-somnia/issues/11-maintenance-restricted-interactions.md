# 11 — Match maintenance controls and restricted interactions

**What to build:** Expose Session maintenance, model controls, and waiting interactions while enforcing the rule that permissions, sensitive settings, and Yolo require confirmation on the computer.

**Blocked by:** 07 — Match the Desktop Session lifecycle; 09 — Match active Turn control.

**Status:** completed

- [x] Compact and janitor operations expose Desktop-equivalent status and results.
- [x] Provider, model, vision model, and reasoning controls follow the approved remote policy.
- [x] Pending authorization and mode-switch interactions are visible in the correct Session.
- [x] The Web interface clearly identifies actions awaiting confirmation on the computer.
- [x] Remote attempts to persist permissions, change sensitive configuration, or enable Yolo are denied.
- [x] Policy enforcement is tested at the Connector rather than trusted to browser presentation.

## Verification

- `python -m unittest tests.test_remote_connector`
- `npm run typecheck`
- `npm test -- --run`
- `npm run test:e2e -- e2e/remote-tracer.e2e.ts` (desktop and mobile)
