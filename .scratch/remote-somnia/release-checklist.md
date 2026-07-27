# Remote Somnia release checklist

This checklist is the release evidence for the remote system. The Relay is a
router for transient envelopes; the Connector and local Runtime remain the
authoritative owners of Session content.

## Privacy and retention

- [x] `AuthMetadataStore` contains only administrator and Device metadata.
- [x] Forwarded conversation content is not written to the Relay database:
  `python -m unittest tests.test_remote_relay.RemoteRelayTests.test_metadata_database_does_not_persist_forwarded_conversation_content`
- [x] Relay forwarding does not log, inspect, or enqueue envelope payloads:
  `python -m unittest tests.test_remote_relay.RemoteRelayTests.test_relay_forwards_requests_responses_and_events_without_interpreting_payloads`
- [ ] Before deployment, inspect the configured database, application logs,
  reverse-proxy logs, trace exports, metrics labels, crash dumps, cache/queue
  stores, and temporary directories for a canary marker. The marker must occur
  only in the local Connector/Runtime evidence, never in Relay infrastructure.

## Authentication and routing

- [x] Access expiry, refresh rotation, logout, rate limits, pairing expiry and
  single-use pairing:
  `python -m unittest tests.test_remote_auth`
- [x] Device key authentication, rotation-by-repairing, revocation, origin
  checks, account isolation, cross-Device routing and replay handling:
  `python -m unittest tests.test_remote_auth tests.test_remote_relay tests.test_remote_connector`
- [x] Oversized content frames are closed with WebSocket code 1009 and are not
  forwarded:
  `python -m unittest tests.test_remote_relay.RemoteRelayTests.test_relay_closes_oversized_content_frames_before_forwarding`
- [x] Remote operations are fully authorized through verified (paired, non-revoked)
  Devices: tool-authorization and mode-switch interactions (including Yolo) may be
  approved remotely, and all configuration sections — hooks included — are
  readable/writable through the Connector:
  `python -m unittest tests.test_remote_connector`

## Recovery and load

- [x] Relay, Connector and Runtime restart persistence/recovery coverage:
  `python -m unittest tests.test_remote_auth tests.test_remote_runtime_manager tests.test_remote_tracer_e2e`
- [x] Replay window, snapshot fallback, duplicate request idempotency and slow
  client resynchronization:
  `python -m unittest tests.test_remote_connector tests.test_remote_relay`
- [ ] Run the production soak profile with long responses, large tool results,
  multiple observers, slow clients and concurrent turns. Record peak memory,
  disconnect/reconnect counts, and the absence of content in infrastructure
  logs before signing the release.

## Deployment defaults

- [x] `somnia relay` requires `SOMNIA_ADMIN_PASSWORD` and a metadata database
  URL; it marks cookies Secure for non-loopback hosts.
- [x] Remote pairing rejects plaintext HTTP for non-loopback Relays; Connector
  URLs must be HTTPS/WSS remotely.
- [x] Browser WebSocket origins are explicit (`--web-origin`) and cookies are
  HttpOnly/SameSite=Strict; the default payload limit is 16 MiB.
- [ ] Terminate TLS at the deployment proxy, set an explicit production
  `--web-origin`, provision database backups containing only allowed metadata,
  and rotate administrator/device secrets according to the deployment runbook.

## Final verification

```powershell
python -m unittest tests.test_remote_auth tests.test_remote_relay tests.test_remote_connector tests.test_remote_runtime_manager
python -m unittest tests.test_remote_tracer_e2e
```

The full repository suite must also be run in CI with its declared test
dependencies installed. A local environment without `pytest`, or a checkout
whose provider preset inventory is stale, is not release evidence.
