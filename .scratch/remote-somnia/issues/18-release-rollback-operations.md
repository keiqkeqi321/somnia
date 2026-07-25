# 18 — Define release, rollback, and operational acceptance

Blocked by: 15, 16, 17

Status: ready-for-agent

## Question

What is the smallest safe production rollout, how is it monitored, and how is
it rolled back without losing local authoritative Session state or leaving
revoked credentials active?

## Acceptance evidence

- A pre-production runbook covers deploy, migration, smoke test, and approval.
- A rollback runbook covers proxy, Relay, Web, and database compatibility.
- Health checks and alerts cover WSS connectivity, auth failures, reconnects,
  payload-limit violations, resource exhaustion, and storage anomalies.
- A maintenance window, owner, go/no-go checklist, and abort thresholds are
  named.

## Recommended baseline

Use one Linux cloud host with Docker Compose for Web and Relay, Caddy as the
public HTTPS/WSS edge, and managed PostgreSQL for allowed metadata. Keep the
Connector outside the cloud on the controlled device. Store secrets in the
host's secret mechanism or a `0600` environment file outside the repository;
never pass them on the Docker command line. Rollback uses versioned images and
an additive database migration policy, with Caddy routing switched only after
health checks pass.
