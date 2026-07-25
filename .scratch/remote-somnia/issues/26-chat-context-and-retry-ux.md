# 26 - Safer chat context, queue, and retry controls

**What to build:** Reduce mistakes during active Turns and device switching while preserving existing execution and idempotency semantics.

**Blocked by:** 24, 25, 09

**Status:** ready-for-agent

## Scope

- Display the active Device, Project, Session, and connection state beside the composer and send control.
- Show queued prompt count and provide per-item remove plus “stop current Turn”.
- On reconnect, show how many events were replayed or whether a local snapshot was loaded.
- Keep draft text and attachments local; require explicit retry when delivery was not confirmed.
- Provide user-facing diagnostic IDs and a privacy-safe “copy diagnostics” action.

## Acceptance criteria

- A user cannot submit to an offline Device, wrong Project, or stale Session without an explicit confirmation path.
- Retrying a timed-out request uses the same request identity when safe and never duplicates a completed Turn.
- Queue, interruption, reconnect, and ambiguous-result states are covered at phone and desktop widths.
- Diagnostic exports exclude prompt text, response text, file contents, tool arguments, and secrets.

## Non-goals

- No changes to Runtime concurrency policy or Relay content retention rules.
