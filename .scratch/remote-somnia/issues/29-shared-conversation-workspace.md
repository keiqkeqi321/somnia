# 29 - Extract shared ConversationWorkspace

**What to build:** Extract the Desktop conversation panel, session sidebar, message stream, and progress/context surfaces into shared components.

**Blocked by:** 27, 28

**Status:** ready-for-agent

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
