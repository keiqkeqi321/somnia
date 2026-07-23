# 07 — Match the Desktop Session lifecycle

**What to build:** Provide remote Session listing, creation, loading, deletion, archive, restore, selection, and authoritative recovery with the same behavior users rely on in Desktop.

**Blocked by:** 04 — Make real-time delivery recoverable; 05 — Give the Connector authoritative Runtime ownership.

**Status:** ready-for-agent

- [ ] Session summaries and complete Session history are loaded only from the selected computer.
- [ ] Create, select, delete, archive, and restore behavior matches Desktop.
- [ ] Archive state remains browser-local and is never sent to Relay persistence.
- [ ] Session selection survives a transient reconnect when the Device and Project remain valid.
- [ ] Destructive actions are idempotent and present explicit conflict or not-found results.
- [ ] Direct and remote adapters pass the same Session lifecycle contract tests.
