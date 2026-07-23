# 02 — Deliver the remote session tracer

**What to build:** Make a narrow end-to-end path in which a hosted browser reaches one registered computer through a Relay and Connector, opens one Project, creates a Session, submits a prompt, and sees the response stream live.

**Blocked by:** 01 — Establish the shared Somnia Connection seam.

**Status:** completed

- [x] Browser, Relay, Connector, and a real Somnia Runtime participate in the tracer.
- [x] The Connector initiates the outbound connection and the Runtime remains loopback-only.
- [x] Assistant deltas render incrementally rather than after Turn completion.
- [x] Completed Session state is reloaded from the computer and matches the streamed result.
- [x] The tracer has an automated end-to-end test and a documented local launch path.

## Verification

- `python -m unittest tests.test_remote_connector tests.test_remote_relay tests.test_remote_tracer_e2e tests.test_app_service tests.test_sidecar_server`
- `npm test -- --run`
- `npm run typecheck`
- `npm run build`
- `npm run test:e2e` (Playwright, desktop and 390px mobile viewports)
