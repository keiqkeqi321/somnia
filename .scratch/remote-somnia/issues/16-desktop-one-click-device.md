# 16 — Desktop one-click controlled device

**What to build:** Let Somnia Desktop turn the local machine into a remote-controllable Device from the UI: a Settings "Remote" section where the user enters Relay address + account credentials + device name once (pair), and from then on a plain enable/disable toggle (no credentials needed — the paired identity persists). While enabled, the machine appears online on the Relay and browsers can connect to its registered projects.

**Blocked by:** none (issues 14 and 15 completed).

**Status:** ready-for-agent

## Product decisions (2026-07-27)

- Pair once, toggle forever: credentials are only needed for the initial pair. The stored device identity (`~/.open_somnia/remote/device-identity.json`) authorizes all later `connector run` launches, so the enable toggle never asks for a password again. Password is used in memory for the pair call and never persisted.
- v1 scope: the machine is controllable **while Desktop is running** (connector lifecycle follows the app, same as managed sidecars). A connector that survives Desktop shutdown (Tauri-level or service-level management) is a follow-up.
- v1 runtime model: the connector bridges the already-running managed sidecar (`--sidecar` legacy mode) so there is exactly one Runtime owner per project — no duplicate runtime hosts. Exposing multiple Desktop projects through one connector (RemoteConnector `sidecars` map) is a follow-up; v1 exposes the active project.

## Backend (desktop sidecar, `desktop/backend/`)

- [x] `POST /remote/setup` — body `{relay_url, username, password, device_name}`: login to the Relay, create a pairing, claim it with the local device identity (`DeviceIdentity.load_or_create` + `pair_device`), register the current workspace as a project in the connector registry. Returns device info. Relay URL validation reuses `identity._relay_http_url` rules (HTTPS required off-loopback).
- [x] `POST /remote/enable` — start an in-process `RemoteConnector` on a daemon thread, bridging the sidecar itself (`LocalSidecarBridge(<own loopback URL>)`, project id = the workspace's stable id). No child process: the connector stops via its stop event when disabled or when the sidecar shuts down. Requires a paired identity, else 409; enabling twice is a no-op. The connector thread must never take the sidecar down — catch and record all failures into status.
- [x] `POST /remote/disable` — signal the connector's stop event and join the thread.
- [x] `GET /remote/status` — `{paired: bool, device_name, relay_url, enabled: bool, connector_running: bool, last_error: str}` so the UI can render setup vs toggle state and surface connector failures.
- [x] Persist remote settings (relay_url, username, device_name, enabled — **never the password**) under the workspace `.open_somnia` store; on sidecar start, if `enabled` and paired, auto-start the connector.
- [x] Shutdown hook: stop the connector thread with the sidecar.
- [x] Unit tests for the endpoint logic (mock relay HTTP + a fake/stopped connector thread).

## Frontend (desktop Settings)

- [x] New Settings section "Remote" (desktop mode only): unpaired state shows relay URL + username + password + device name + "Pair and enable"; paired state shows device name, Relay, online status, and an enable/disable toggle; disable/remove pairing ("unpair" = delete local identity + stop connector) available.
- [x] Status polling while the section is open; i18n en + zh.
- [x] Remote mode (`?remote=1`) is unaffected.

## Follow-ups (not in this issue)

- Connector surviving Desktop shutdown (Tauri-managed or OS service).
- Multi-project exposure via one connector (`sidecars` map bridging each project's managed sidecar).
- Packaged-app bundling of the connector entrypoint (PyInstaller target alongside the sidecar binary).

## Pairing UX revision v2 (2026-07-28): device-flow pairing, no copy-paste

The username/password/device-name form is replaced by a **fully automatic device flow** (GitHub CLI pattern): Desktop shows a single "Pair and enable" button. Clicking it asks the Relay for a short-lived pair session, opens the system browser to a confirmation page on the remote Web app, and polls; the user signs in (if needed) and approves with a device name; the Relay binds a pairing to their account; Desktop's poll returns the code, claims it with the local identity, and auto-enables the connector. No credentials, no device name, no pairing code is ever typed into Desktop.

### Relay API (new, `open_somnia/remote/`)

- `POST /api/pair-sessions` — unauthenticated, per-source rate-limited (10/hour sliding window by default, `pair_session_attempt_limit`); creates a short-lived session `{session_id, secret, expires_at}` (in-memory in `RemoteAuth`, reuses the pairing TTL). 429 with `Retry-After` when limited.
- `GET /api/pair-sessions/{session_id}?secret=...` — `{status: "pending"|"approved"|"expired"}`; 403 on a wrong secret; once approved it also returns the one-time pairing `code` (returned once, then the session is consumed and later polls read as `expired`).
- `POST /api/pair-sessions/{session_id}/approve` — browser-auth (cookie session) + `secret` + `device_name`; creates the account-bound pairing, marks the session approved. 403 on wrong secret, 401 unauthenticated, 410 on an expired/unknown session, 400 on a bad device name.
- Tests in `tests/test_remote_auth.py` (`PairSessionTests`): full flow (create → approve → poll → claim), wrong-secret 403, unauthenticated approve 401, expiry, code returned exactly once, creation rate limit 429.

### Web (`?remote=1`)

- New route `#/pair?session=<id>&secret=<s>`: requires sign-in (redirects to `#/login` and back, or shows the login form inline); shows a device-name input (sensible default) + Approve; on success shows "done, return to Desktop".

### Desktop (`desktop/backend/` + Settings)

- `POST /remote/pair-begin {relay_url}`: creates the pair session via the Relay, opens `<relay origin>/?remote=1#/pair?...` in the system browser, starts a daemon poll thread (1.5s interval); on approval it claims the code (`pair_device`), persists settings (relay_url, device_name, enabled=true), and auto-enables. Calling it again while a flow is pending is a no-op returning the current status. Transient poll/relay errors keep polling until session expiry; expiry (or a browser-open failure) is recorded into `last_error`. The credential-based `/remote/setup` endpoint is removed (it never shipped).
- `GET /remote/status` gains `pair_pending: bool`; `POST /remote/pair-cancel` aborts a pending poll. `unpair`/`shutdown` also stop a pending poll.
- Settings "Remote" unpaired state becomes: Relay URL + "Pair and enable" + pending hint ("confirm in the opened browser tab…"); paired state unchanged.
- Update `tests/test_desktop_remote.py` (mock relay session endpoints).

## Verification

- New backend unit tests pass; `python -m unittest discover -s tests -p "test_*.py"` spot-checked for regressions.
- `cd desktop/ui && npm run typecheck && npm test -- --run && npm run build`
- Manual: in Desktop, pair against the local relay stack, enable, confirm the device shows online in the web `#/connect` page, open a session from the browser, then disable and confirm the device goes offline.
