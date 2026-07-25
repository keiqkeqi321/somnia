# Remote Somnia — Launch Readiness Map

Status: planning

## Destination

Produce a production launch plan for Remote Somnia with explicit deployment,
security, privacy, test, and rollback evidence. No production rollout starts
until every decision ticket below is resolved and issue 13's release gates are
green.

## Notes

This map follows the engineering wayfinder skill. Resolve decision tickets one
at a time; implementation work belongs in the ticket that the decision unlocks.
The local launch remains a development/acceptance path, not the production
topology.

## Decisions so far

- [14 — Lock the production edge and origin policy](issues/14-production-edge-origin.md) — Use `https://somnia.top` as one same-origin entry point; route `/`, `/api/`, and `/ws/` through the reverse proxy to loopback services.
- [15 — Lock production data, secrets, and rotation policy](issues/15-production-data-secrets.md) — Use PostgreSQL and injected secrets, keep device keys local, and back up metadata only; persistent Relay secret injection remains an implementation prerequisite.
- [19 — Add persistent Relay secret injection and rotation](issues/19-relay-secret-rotation.md) — Inject the Relay secret from protected configuration; rotation/restart intentionally requires browser re-authentication without adding cloud session persistence.
- [20 — Implement Relay secret configuration](issues/20-implement-relay-secret-config.md) — Production CLI validates `SOMNIA_RELAY_SECRET_KEY` as 32-byte URL-safe Base64; local development retains an ephemeral fallback.
- [16 — Define reproducible privacy evidence](issues/16-privacy-evidence.md) — Use a generated sentinel conversation and inspect every cloud-side persistence and observability location.

## Not yet specified

- Production hosting provider, certificate automation, and service manager are
  not chosen.
- The final observability vendor and retention controls are not chosen.
- The release owner and maintenance window are not assigned.

## Out of scope

- Multi-user accounts, organizations, horizontal Relay scaling, and
  end-to-end payload encryption remain outside this launch plan.

## Open decision tickets

- 21 — Implement the privacy audit harness
- 17 — Resolve baseline tests and define the release test matrix
- 18 — Define release, rollback, and operational acceptance
