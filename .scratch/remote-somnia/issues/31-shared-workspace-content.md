# 31 - Share Workspace Content Components

**What to build:** Continue the Desktop-first extraction so Remote consumes shared workspace content components rather than only shared frames.

**Blocked by:** 30

**Status:** in-progress

## Scope

- Share the conversation message content tree, including typed part fallback, image grouping, loading state, and row footer slots.
- Move session actions, execution progress details, queue cards, and interaction cards into shared presentational components where their behavior is connection-agnostic.
- Keep Tauri operations, Relay/device operations, and resource fetch callbacks in Desktop and Remote adapters.

## Acceptance criteria

- Desktop and Remote use the same message content component.
- Shared content does not import Tauri, Relay, browser storage, or client-specific connection classes.
- Each extraction preserves the existing client-specific resource and action callbacks.

## Progress

- [x] Shared `ConversationMessageContent` owns row wrapping, typed-part fallback, image grouping, loading state, and footer slots; Desktop and Remote both use it.
- [ ] Extract session action rows and execution-progress details.
- [x] Shared `ConversationPromptQueue` is used by Desktop and Remote; each adapter supplies its scheduling and optional removal callback.
- [ ] Extract interaction cards.
