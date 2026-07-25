# 24 - Zero-config Web connection and session restore

**What to build:** Make daily Web use open directly into the last usable Device, Project, and Session.

**Blocked by:** 06, 07, 22

**Status:** ready-for-agent

## Scope

- Production Web uses same-origin `/api/*` and `/ws/*`; remove the Relay URL field from the normal user surface.
- Persist only Device, Project, and Session identifiers in browser-local storage; never persist session content in Relay.
- Automatically select the most recently used online Device/Project; show a selector only when there are multiple viable choices.
- Automatically load the last Session after connection and recover via event replay, then snapshot reload when replay is unavailable.
- Preserve an unsent draft locally and require an explicit retry after an offline period.

## Acceptance criteria

- A returning user with an online computer can reach the composer with at most one click after authentication.
- Refresh, Wi-Fi change, mobile backgrounding, Connector restart, and Relay restart converge to a correct Session view.
- The UI never claims a message was sent when the Device was offline or the request result is unknown.
- Browser-local data contains identifiers/drafts only; privacy tests verify no transcript or payload persistence.
- Playwright covers first visit, returning visit, one-device and multi-device selection, and mobile reconnect.

## Non-goals

- No offline command queue or delayed automatic execution.
