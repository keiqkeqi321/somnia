# 13 — Pass the privacy, security, and release gates

**What to build:** Harden and verify the complete remote system for production deployment, with evidence that cloud infrastructure does not retain conversation content and that expected failures recover safely.

**Blocked by:** 03 — Secure account access and Device pairing; 04 — Make real-time delivery recoverable; 06 — Navigate multiple Devices and Projects; 08 — Match rich output and progress visibility; 09 — Match active Turn control; 10 — Match the Desktop composer; 11 — Match maintenance controls and restricted interactions; 12 — Deliver the complete responsive Web experience.

**Status:** ready-for-agent

- [ ] Database, queue, cache, proxy, application log, trace, metric, crash, and temporary-file inspections find no prohibited content.
- [ ] Token expiry, key rotation, revocation, replay, origin, routing, and dangerous-operation tests pass.
- [ ] Relay, Connector, Runtime, and browser restart scenarios preserve authoritative Session state.
- [ ] Long responses, large tool results, multiple observers, slow clients, and concurrency limits pass soak tests.
- [ ] Deployment defaults enforce HTTPS/WSS, payload limits, redaction, backups of allowed metadata, and secret rotation.
- [ ] The release checklist includes reproducible evidence for every acceptance gate in the parent Spec.
