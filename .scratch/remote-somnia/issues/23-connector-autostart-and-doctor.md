# 23 - Connector automatic startup and diagnostics

**What to build:** Remove the need to manually start multiple local processes after pairing.

**Blocked by:** 05, 22

**Status:** ready-for-agent

## Scope

- Provide a supported install/register path for Windows startup service or tray/background process; document the equivalent Linux service path.
- Connector supervises its managed Sidecar/Runtime children and applies bounded restart backoff after crashes.
- Add `somnia-connector doctor` with checks for identity file permissions, Relay reachability, authentication, registered Projects, Sidecar health, and provider readiness.
- Surface a stable diagnostic code and remediation text; redact paths, prompts, responses, provider secrets, and tool payloads.
- Expose a local status endpoint/command for the Web onboarding flow to consume without exposing a listening service externally.

## Acceptance criteria

- After installation and reboot, a paired Device reconnects without opening a terminal.
- A crashed child is restarted without creating a second Runtime owner or corrupting local Session state.
- `doctor` returns pass/warn/fail results with an exit code suitable for scripts.
- Failure output identifies the next action (reauthenticate, register Project, fix provider, or inspect network).
- Tests cover reboot-equivalent startup, child crash/backoff, stale owner recovery, and redaction.

## Non-goals

- No remote shell, remote filesystem browser, or cloud-side process control.
