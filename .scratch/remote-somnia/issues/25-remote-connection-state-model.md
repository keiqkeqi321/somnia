# 25 - Actionable remote connection states

**What to build:** Use one state model and user-action contract for Device, Project, Runtime, transport, and local confirmation states.

**Blocked by:** 22, 24

**Status:** ready-for-agent

## Scope

- Define states for unpaired, paired-offline, device-online/project-starting, ready, reconnecting, resynchronizing, waiting-local-confirmation, and offline-draft.
- Map each state to one concise message, a safe primary action, and whether a command may be submitted.
- Distinguish “Device online” from “Project/Runtime ready” in the Web UI and presence protocol.
- Show reconnect progress and resync outcome without exposing payloads or internal stack traces.
- Keep state transitions shared by desktop and remote connection adapters where behavior is equivalent.

## Acceptance criteria

- Every transport/runtime failure has a deterministic state and next action.
- Dangerous or ambiguous operations are disabled while disconnected or awaiting local confirmation.
- State transitions are covered by reducer/contract tests and browser assertions for desktop and mobile layouts.
- Copy explicitly states whether execution occurred, whether a draft was retained, and what the user should do next.
