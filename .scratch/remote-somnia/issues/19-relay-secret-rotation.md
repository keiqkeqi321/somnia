# 19 — Add persistent Relay secret injection and rotation

Blocked by: 15

Status: completed

## Question

What minimal runtime/configuration change exposes a Relay browser-token signing
secret and supports explicit rotation without silently accepting revoked
credentials or leaking the secret through process arguments and logs? The
current browser sessions are process-memory state, so Relay restart already
requires browser re-authentication.

## Acceptance evidence

- Relay accepts its signing secret from a protected environment/configuration
  source and fails closed when production configuration omits it.
- Rotation intentionally invalidates all in-memory browser sessions and
  requires re-authentication; it does not add cloud session persistence.
- Device public keys, revocation state, and local authoritative Sessions remain
  unaffected by browser-token rotation or Relay restart.
- Tests cover startup configuration, expiry, refresh, logout, rotation,
  restart re-authentication, and device revocation.

## Resolution

Add a protected `SOMNIA_RELAY_SECRET_KEY` configuration input, decoded and
validated at startup. Do not support a secret in CLI arguments. A planned
rotation replaces the configured key, restarts the Relay, and explicitly
invalidates browser sessions; operators then require browser login again.
Device identity metadata stays in PostgreSQL and Connector private keys remain
on controlled devices. No browser-session table is introduced for this launch.

## Comments

- 2026-07-25: Rotation semantics selected: restart/re-authentication rather
  than persistent cloud browser sessions.
