# 02 — Deliver the remote session tracer

**What to build:** Make a narrow end-to-end path in which a hosted browser reaches one registered computer through a Relay and Connector, opens one Project, creates a Session, submits a prompt, and sees the response stream live.

**Blocked by:** 01 — Establish the shared Somnia Connection seam.

**Status:** ready-for-agent

- [ ] Browser, Relay, Connector, and a real Somnia Runtime participate in the tracer.
- [ ] The Connector initiates the outbound connection and the Runtime remains loopback-only.
- [ ] Assistant deltas render incrementally rather than after Turn completion.
- [ ] Completed Session state is reloaded from the computer and matches the streamed result.
- [ ] The tracer has an automated end-to-end test and a documented local launch path.
