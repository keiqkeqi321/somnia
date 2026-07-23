# 03 — Secure account access and Device pairing

**What to build:** Replace tracer credentials with single-administrator authentication, short-lived browser access, QR or short-code Device pairing, Device-specific keys, and immediate revocation.

**Blocked by:** 02 — Deliver the remote session tracer.

**Status:** ready-for-agent

- [ ] An authenticated administrator can pair and name a new Device.
- [ ] Pairing codes are short-lived, single-use, and resistant to guessing.
- [ ] A Connector proves Device identity by signing a server challenge.
- [ ] Browser tokens expire and can be renewed without exposing Device credentials.
- [ ] Revocation disconnects the Device and prevents its old key from reconnecting.
- [ ] Cross-account and cross-Device routing attempts are rejected by integration tests.
