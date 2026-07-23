# 03 — Secure account access and Device pairing

**What to build:** Replace tracer credentials with single-administrator authentication, short-lived browser access, QR or short-code Device pairing, Device-specific keys, and immediate revocation.

**Blocked by:** 02 — Deliver the remote session tracer.

**Status:** completed

- [x] An authenticated administrator can pair and name a new Device.
- [x] Pairing codes are short-lived, single-use, and resistant to guessing.
- [x] A Connector proves Device identity by signing a server challenge.
- [x] Browser tokens expire and can be renewed without exposing Device credentials.
- [x] Revocation disconnects the Device and prevents its old key from reconnecting.
- [x] Cross-account and cross-Device routing attempts are rejected by integration tests.

## Verification

- `python -m unittest tests.test_remote_auth tests.test_remote_connector tests.test_remote_relay tests.test_remote_tracer_e2e tests.test_app_service tests.test_sidecar_server` — 51 passed.
- `npm run test -- --run` — 7 passed; `npm run typecheck` and `npm run build` passed.
- Full Python suite: 649 tests; the two pre-existing failures remain in `tests.test_utils` (`mypackage` is absent) and `tests.test_provider_presets` (`kimi-coding-plan` vs `kimi-code`).
- Playwright browser execution was attempted but the local Chromium download timed out; no browser executable was available in this environment.
