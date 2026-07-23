# 10 — Match the Desktop composer

**What to build:** Provide slash commands, Project path mentions, image input, prompt history, drafts, and submit behavior adapted to browser and mobile constraints.

**Blocked by:** 05 — Give the Connector authoritative Runtime ownership; 07 — Match the Desktop Session lifecycle.

**Status:** ready-for-agent

- [ ] Supported slash commands behave as they do in Desktop.
- [ ] Path suggestions come from the selected Project without exposing unrestricted filesystem browsing.
- [ ] Image input and preview work without Relay-side durable files or payload logging.
- [ ] Prompt history and unsent drafts remain local to the browser.
- [ ] Offline submission preserves a local draft but never auto-runs after reconnect.
- [ ] Keyboard, touch, paste, multiline, and mobile viewport behaviors have browser tests.
