# 16 — Define reproducible privacy evidence

Blocked by: 14, 15

Status: completed

## Question

What automated audit will prove that conversation payloads do not persist in
the Relay database, queues, caches, proxy logs, application logs, traces,
metrics, crash reports, or temporary directories after representative traffic?

## Acceptance evidence

- A unique sentinel payload is sent through login, pairing, streaming,
  reconnect, tool output, and error paths.
- Every permitted persistence location is enumerated and scanned.
- The sentinel is absent from cloud-side storage and operational artifacts.
- Retention, cleanup, and failure-reporting settings are recorded.

## Resolution

Build one automated privacy audit around a unique sentinel value that appears
in a user prompt, assistant response, tool input, tool output, and an error
path. Run login, pairing, Session creation, streaming, reconnect, and Relay
restart through the production-like proxy. After the run, inspect the Relay
metadata database, proxy/application logs, WebSocket diagnostics, trace and
metric exports, crash/reporting output, service working directories, and
temporary/cache directories. The sentinel must be absent everywhere except
the controlled device's authoritative local Session store and explicitly
scoped test artifacts.

The audit must also assert that proxy body logging, request sampling, payload
exception formatting, and durable queues/caches are disabled. It produces a
timestamped report listing each inspected location, retention setting, and
search result so the release gate is reproducible rather than anecdotal.

## Comments

- 2026-07-25: Sentinel-based privacy audit selected as the release evidence.
