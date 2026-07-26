# 29 - Extract shared ConversationWorkspace

**What to build:** Extract the Desktop conversation panel, session sidebar, message stream, and progress/context surfaces into shared components.

**Blocked by:** 27, 28

**Status:** in-progress

## Scope

- Extract `SessionSidebar`, `ConversationPanel`, `ProgressPanel`, and `ContextPanel` from Desktop.
- Feed rows and runtime items through shared typed view models.
- Keep session archive state, prompt history, and local drafts in client adapters.
- Allow Remote to render the same rich output, tool activity, task/team views, and queue cards.

## Acceptance criteria

- Remote and Desktop use the same message row and progress rendering components.
- Session create/load/archive/delete and active-turn presentation behave identically.
- Mobile layout uses the same component tree with responsive shell differences only.
- Shared component tests cover empty, streaming, completed, interrupted, and resynchronized states.

## Progress

- [x] Shared `ConversationWorkspace` frame introduced and Remote migrated (`093c518`).
- [x] Desktop workspace container migrated while preserving resize refs and context panel (`35becc8`).
- [x] ConversationPanel boundary extracted for Desktop and Remote (`8440815`).
- [x] SessionSidebar boundary extracted for Remote (`5efeb10`).
- [x] Desktop session navigation migrated to the shared boundary (`9163cf3`).
- [x] ProgressPanel boundary extracted for Remote (`84f08c3`).
- [ ] Desktop execution activity migrated to the shared ProgressPanel.
- [x] ContextPanel boundary extracted for Desktop (`888ad38`).
- [ ] Remote diagnostics/context content migrated to the shared ContextPanel.
- [ ] Shared event/render fixtures cover Desktop and Remote.
