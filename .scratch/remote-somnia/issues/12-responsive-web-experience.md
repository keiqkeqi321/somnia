# 12 — Deliver the complete responsive Web experience

**What to build:** Assemble the approved capabilities into a polished hosted Web interface with information-dense desktop navigation and ergonomic phone and tablet workflows.

**Blocked by:** 06 — Navigate multiple Devices and Projects; 08 — Match rich output and progress visibility; 09 — Match active Turn control; 10 — Match the Desktop composer; 11 — Match maintenance controls and restricted interactions.

**Status:** completed

- [x] Device, Project, Session, conversation, progress, and settings navigation works at all target sizes.
- [x] Mobile uses a full conversation view, stable bottom composer, and accessible progress drawers.
- [x] Desktop provides efficient multi-column scanning without nesting page sections as decorative cards.
- [x] Dynamic output cannot overlap controls or resize fixed interaction surfaces unexpectedly.
- [x] Playwright screenshots cover phone, tablet, laptop, and wide desktop viewports.
- [x] Capability parity is documented against the approved Desktop inventory with no unexplained gaps.

## Capability parity

| Desktop capability | Remote Web surface | Deliberate restriction |
| --- | --- | --- |
| Device / Project / Session navigation | Header selectors, Session list, mobile Sessions drawer | Archive state remains browser-local |
| Conversation and active Turn control | Shared rows, Send/Queue, Inject, Interrupt | None |
| Rich output and progress | Markdown, code, Mermaid, images, progress drawers | None |
| Composer | Slash commands, path mentions, history, drafts, image paste/upload | Offline drafts require explicit resend |
| Maintenance and model controls | Compact, janitor, provider/model, vision, reasoning, safe modes | Yolo, permissions, and sensitive configuration require local confirmation |

## Verification

- `npm run typecheck`
- `npm test -- --run`
- `npm run test:e2e -- e2e/remote-tracer.e2e.ts` — 4 passed: phone, tablet, laptop, wide desktop; screenshots captured for each project
