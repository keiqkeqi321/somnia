# 28 - Migrate Desktop and Remote Composer

**What to build:** Make Desktop and Remote use one Composer layout and preserve client-specific controls through typed slots.

**Blocked by:** 27

**Status:** in-progress

## Scope

- Migrate Desktop provider/model/reasoning controls into the shared Composer control slot.
- Migrate Remote Device/Project target context, queue, attachment, command picker, and interruption controls.
- Share textarea sizing, suggestion placement, attachment rendering, keyboard behavior, and disabled states.
- Preserve Desktop translations and Remote connection-state copy without duplicating layout markup.

## Acceptance criteria

- Composer has identical structure and responsive behavior at desktop, tablet, and phone widths.
- Provider/model controls remain Desktop-only; target context and reconnect status remain Remote-only.
- Slash command, path mention, image attachment, queue, interrupt, and keyboard tests pass for both clients.
- No duplicate Composer JSX remains in `App.tsx` and `RemoteTracerApp.tsx`.

## Progress

- [x] Shared `ConversationComposer` frame introduced.
- [x] Desktop Composer migrated to the shared frame (`b99b270`).
- [x] Remote Composer migrated to the shared frame (`5c7b506`).
- [x] Remote Composer migrated from children compatibility to typed slots (`pending commit`).
- [x] Desktop and Remote Composer inputs, attachments, suggestions, and actions use typed slots; legacy children compatibility remains only for external callers (`pending commit`).
- [ ] Align shared responsive Composer styling and browser screenshots.
