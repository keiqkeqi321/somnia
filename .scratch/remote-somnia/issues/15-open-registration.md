# 15 — Open registration with brute-force protection

**What to build:** Self-service account registration for the remote Relay so new users can create an account and pair their own Devices without an operator provisioning credentials. Registration is open (no invite codes, no email), protected by rate limiting and credential policy. Every account remains an isolated tenant — no roles, no cross-account visibility.

**Blocked by:** none.

**Status:** ready-for-agent

## Product decisions (2026-07-27)

- **Open registration**: anyone who can reach the Relay may create an account. No invite codes, no email verification, no admin approval.
- **No roles**: all accounts stay equal isolated tenants (device/pairing scoping by `account_id` unchanged). Site-admin capabilities are out of scope.
- **Username-only**: no email field; password recovery does not exist (lost password = new account + re-pair).
- The existing env-provisioned administrator account becomes an ordinary account; env provisioning remains bootstrap-only (never overwrites an existing account's password).

## Brute-force protections (required)

- Login: keep the existing per-source rate limit (429) and add a **per-username** failed-attempt sliding window (e.g. 10 failures / 10 min → 429 with `Retry-After`). Throttle, not lockout — an attacker must not be able to permanently lock a victim's account.
- Registration: per-source sliding window (e.g. 5 registrations / hour → 429).
- Credential policy: username 3–32 chars, `[a-zA-Z0-9_.-]`, case-insensitively unique; password min 8 chars and must not equal the username. Constant-shape generic error for login failure (already the case); "username taken" on register is acceptable and documented.
- Pairing endpoint already rate-limited; unchanged.

## API contract

- `POST /api/auth/register` — body `{username, password}`; success `201` with the same cookie session issuance as `/api/auth/login` (auto-login). Errors: `400` invalid username/weak password, `409` username taken, `429` rate limited.
- Relay CLI gains `--disable-registration` (default: registration enabled) so private relays can opt out.

## Frontend

- [x] New `#/register` route + page (same visual system as login: brand icon, username, password, confirm password), linked from `#/login` ("no account? register"); successful register lands on `#/connect` via the issued session.
- [x] Router: `#/register` and `#/login` are both legal while unauthenticated; authenticated users hitting either are redirected to `#/connect`.
- [x] `useRemoteAccess` gains `signUp`; i18n keys en + zh.
- [x] e2e covers register → auto-login → device picker visible, plus duplicate-username and weak-password error paths.

## Backend

- [x] `register_account` in `open_somnia/remote/auth.py` with the credential policy and per-username login throttle + per-source registration throttle (reuse the `_recent_attempts` sliding-window pattern).
- [x] `POST /api/auth/register` route in `relay.py`; `--disable-registration` flag in `remote/cli.py` (relay command).
- [x] Tests in `tests/test_remote_auth.py`: register happy path (cookies issued, account persisted), duplicate username casefold → 409, weak password/invalid username → 400, registration rate limit → 429, per-username login throttle → 429 without permanent lockout, disabled registration flag → 403/404.
- [x] Existing admin accounts require no migration.

## Verification

- `python -m unittest tests.test_remote_auth tests.test_remote_connector`
- `cd desktop/ui && npm run typecheck && npm test -- --run && npm run build`
- `cd desktop/ui && npm run test:e2e` (4173 default, or PLAYWRIGHT_UI_PORT=4174 if occupied)
- Manual: register a new account through the Web UI, pair a device, confirm the old admin account and the new account cannot see each other's devices.

## Follow-ups (not in this issue)

- Change-password API + UI (currently no self-service password change at all).
- Account disable/deletion once a site-admin role exists.
