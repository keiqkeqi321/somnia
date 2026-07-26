# 27 - Shared Conversation UI seams

**What to build:** Define framework-level shared UI boundaries so Desktop and Remote render the same conversation experience while keeping different connection and shell capabilities.

**Blocked by:** 01, 02

**Status:** completed

## Scope

- Create shared presentational components that depend on `SomniaConnection`, conversation state, and typed callbacks only.
- Keep Tauri window controls, local folder selection, Relay authentication, Device/Project selection, and permission policy outside shared components.
- Establish shared props contracts for Composer, ConversationPanel, SessionSidebar, ProgressPanel, and ContextPanel.
- Keep Desktop and Remote adapters responsible for transport errors, local confirmation, and capability restrictions.

## Acceptance criteria

- Shared components can render with both `DirectSomniaConnection` and `RemoteSomniaConnection`.
- No shared component imports Tauri, Sidecar bootstrap, Relay clients, or browser storage directly.
- Desktop and Remote state transition tests use the same conversation event fixtures.
- Visual/layout changes to shared conversation components apply to both clients.

## Progress

- [x] Shared `ConversationComposer` frame extracted and Remote migrated (`5c7b506`).
- [x] Desktop Composer controls migrated to the shared slots (`b7e643b`).
- [x] Shared ConversationPanel and SessionSidebar extracted (`8440815`, `5efeb10`, `9163cf3`).
- [x] Remote uses the shared workspace, panel, sidebar, message-list, row, progress, context, and composer boundaries; transport and access wiring remain in its adapter.
