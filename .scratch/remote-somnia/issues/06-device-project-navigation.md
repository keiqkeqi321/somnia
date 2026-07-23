# 06 — Navigate multiple Devices and Projects

**What to build:** Let an authenticated user see Device presence, switch between online computers, and select locally registered Projects without exposing filesystem paths or queuing content for offline Devices.

**Blocked by:** 03 — Secure account access and Device pairing; 05 — Give the Connector authoritative Runtime ownership.

**Status:** ready-for-agent

- [ ] Device list distinguishes online, reconnecting, revoked, and offline states.
- [ ] Project identity and display name are available without a workspace path.
- [ ] Switching Device or Project tears down obsolete subscriptions cleanly.
- [ ] Content-bearing commands to an offline Device fail immediately and are not persisted.
- [ ] Several Devices can remain connected without receiving one another's traffic.
- [ ] Phone and desktop browser navigation paths are covered by end-to-end tests.
