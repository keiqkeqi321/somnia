# 21 — Implement the privacy audit harness

Blocked by: 16, 20

Status: ready-for-agent

## Question

How should the sentinel conversation, production-like proxy run, storage/log
inventory, and redaction assertions be implemented so CI and pre-production
produce the same privacy evidence?

## Acceptance evidence

- The harness uses generated sentinel values and never hard-codes real data.
- It exercises HTTP, WSS, reconnect, restart, tool, and error paths.
- It scans every declared persistence location and fails on a sentinel hit.
- It emits a machine-readable and human-readable report without copying the
  conversation payload into the report.

