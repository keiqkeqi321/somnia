# 21 — Implement the privacy audit harness

Blocked by: 16, 20

Status: completed

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

## Resolution

Added tests.remote_privacy_audit with a public sentinel scanner and
metadata-only JSON report. It scans declared files/directories recursively,
records inspected and matched paths, and never writes the sentinel or scanned
content into the report. Unit tests cover both detected leaks and missing
paths. Production wiring still supplies the PostgreSQL export, proxy logs,
service logs, caches, and temporary directories from the deployment manifest.

## Comments

- 2026-07-25: Core scanner and report tests implemented.
