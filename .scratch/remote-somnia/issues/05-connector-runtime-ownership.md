# 05 — Give the Connector authoritative Runtime ownership

**What to build:** Make the Connector an independently running manager for registered Project Runtimes and let Desktop use the same ownership model, preventing two processes from controlling one Project store concurrently.

**Blocked by:** 02 — Deliver the remote session tracer.

**Status:** completed

- [x] The Connector can start with the operating system and operate while Desktop is closed.
- [x] Registered Projects survive Connector restarts without exposing their paths to the Relay.
- [x] Each Project has at most one managed Runtime owner on a Device.
- [x] Desktop discovers and connects to Connector-managed Project Runtimes.
- [x] Start, stop, crash, and stale-owner scenarios have deterministic recovery behavior.
- [x] Existing direct Desktop workflows continue to work during the ownership migration.

## Verification

- Remote Runtime Manager, Connector CLI, Relay, tracer, and Sidecar tests pass (34 tests).
- Full Python suite ran 660 tests; the pre-existing missing `pytest` dependency and `kimi-coding-plan` preset mismatch remain outside this issue.
- Rust checking is blocked locally: the supplied toolchain lacks `rustfmt` and the required Windows GNU/GNULLVM target libraries.
