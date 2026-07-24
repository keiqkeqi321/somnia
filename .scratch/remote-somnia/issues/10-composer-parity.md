# 10 — Match the Desktop composer

**What to build:** Provide slash commands, Project path mentions, image input, prompt history, drafts, and submit behavior adapted to browser and mobile constraints.

**Blocked by:** 05 — Give the Connector authoritative Runtime ownership; 07 — Match the Desktop Session lifecycle.

**Status:** completed

- [x] Supported slash commands behave as they do in Desktop.
- [x] Path suggestions come from the selected Project without exposing unrestricted filesystem browsing.
- [x] Image input and preview work without Relay-side durable files or payload logging.
- [x] Prompt history and unsent drafts remain local to the browser.
- [x] Offline submission preserves a local draft but never auto-runs after reconnect.
- [x] Keyboard, touch, paste, multiline, and mobile viewport behaviors have browser tests.

## Verification

- `python -m unittest tests.test_remote_connector tests.test_remote_tracer_e2e`
- `npm run typecheck`
- `npm test -- --run`
- `npm run test:e2e -- e2e/remote-tracer.e2e.ts` (desktop and mobile)
