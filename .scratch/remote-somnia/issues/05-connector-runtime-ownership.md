# 05 — Give the Connector authoritative Runtime ownership

**What to build:** Make the Connector an independently running manager for registered Project Runtimes and let Desktop use the same ownership model, preventing two processes from controlling one Project store concurrently.

**Blocked by:** 02 — Deliver the remote session tracer.

**Status:** ready-for-agent

- [ ] The Connector can start with the operating system and operate while Desktop is closed.
- [ ] Registered Projects survive Connector restarts without exposing their paths to the Relay.
- [ ] Each Project has at most one managed Runtime owner on a Device.
- [ ] Desktop discovers and connects to Connector-managed Project Runtimes.
- [ ] Start, stop, crash, and stale-owner scenarios have deterministic recovery behavior.
- [ ] Existing direct Desktop workflows continue to work during the ownership migration.
