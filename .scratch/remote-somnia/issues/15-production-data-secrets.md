# 15 — Lock production data, secrets, and rotation policy

Blocked by: 14

Status: completed

## Question

How will production PostgreSQL, administrator credentials, Relay signing
secrets, Connector device keys, backups, and rotation be provisioned without
leaking secrets through command lines, logs, or database diagnostics?

## Acceptance evidence

- PostgreSQL schema and least-privilege role are provisioned.
- Secret injection and rotation procedures are documented and tested.
- Device key rotation and immediate device revocation are verified.
- Backups contain only explicitly allowed metadata and have a tested restore.

## Resolution

Use PostgreSQL with the existing `psycopg` dependency and a least-privilege
metadata-only database role. Inject `SOMNIA_ADMIN_PASSWORD` and the Relay
browser-token signing secret through the service's secret manager, never as
CLI arguments or checked-in files. Keep Connector Ed25519 private keys only on
their controlled devices. Back up the approved metadata tables with encrypted
storage and test restore without exporting session content.

The Relay currently generates its signing secret at process start and does not
expose a production secret injection or dual-key rotation path. That is a
required implementation follow-up before release.

## Comments

- 2026-07-25: Production policy selected: PostgreSQL, injected secrets, local
  device keys, encrypted metadata-only backups, and explicit key rotation.
