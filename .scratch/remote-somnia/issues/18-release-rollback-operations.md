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

