# 22 - One-step remote onboarding

**What to build:** Turn first-time Web remote access into a guided pairing flow that automatically takes a user from “no computer” to “Project ready”.

**Blocked by:** 03, 05

**Status:** ready-for-agent

## Scope

- Web “Add computer” flow creates a named, short-lived pairing grant and shows both QR payload and human-readable fallback code.
- Add a Connector `setup` command that accepts the Relay origin, claims the code, persists the Device identity locally, selects approved Projects, and runs a connectivity self-check.
- Pairing completion is pushed to the authenticated Web client through a presence/device event; no second login or manual refresh is required.
- The final step reports three explicit checks: Device authenticated, Connector online, Project ready.
- Expired, already-used, invalid, and rate-limited codes have actionable messages and a regenerate action.

## Acceptance criteria

- A new user can complete pairing without reading protocol or process documentation.
- QR and code paths result in the same Device identity and never expose the private key to the browser or Relay.
- The browser selects the newly paired Device automatically when it is the only active Device.
- Pairing remains single-use and short-lived; retrying a consumed code cannot create another Device.
- Tests cover successful QR/code pairing, expiry, duplicate claim, browser event delivery, and interrupted setup retry.

## Non-goals

- No multi-user accounts, device sharing, or cloud session storage.
- No arbitrary filesystem registration from the browser.
