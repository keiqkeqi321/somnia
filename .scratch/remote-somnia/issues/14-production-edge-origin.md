# 14 — Lock the production edge and origin policy

Blocked by: none

Status: completed

## Question

Which reverse proxy, certificate automation, hostnames, routing paths, and
Relay `--web-origin`/secure-cookie settings will be the supported production
configuration? The answer must include an executable proxy configuration and
checks proving HTTPS, WSS upgrade, strict origin validation, loopback Relay
binding, and absence of payload logging.

The registered production domain is `somnia.top`. Use one same-origin entry
point: the proxy serves the Web app at `/`, forwards `/api/` to the Relay HTTP
API, and forwards `/ws/` with WebSocket upgrade headers to the Relay. The Relay
must bind loopback and use `--web-origin https://somnia.top --secure-cookies`.

## Acceptance evidence

- One documented production topology and one chosen proxy configuration.
- DNS for `somnia.top` points to the proxy, and certificate renewal is tested.
- Relay is started with `--web-origin https://somnia.top` and secure cookies.
- HTTPS and WSS smoke tests pass through the proxy.
- An origin allowlist rejects an untrusted browser origin.
- Proxy access logs contain metadata only, never request bodies or envelopes.

## Resolution

Use `https://somnia.top` as the only browser origin. DNS points the domain at
the reverse proxy, TLS terminates there, and the proxy routes `/`, `/api/`, and
`/ws/` to the local Web and Relay services. No public Relay port is exposed.

## Comments

- 2026-07-25: Same-origin path design selected by the user.
- 2026-07-25: Recommended deployment baseline is a Linux host with Docker
  Compose and Caddy; Relay and Web remain private container services.
