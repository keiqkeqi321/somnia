# 08 — Match rich output and progress visibility

**What to build:** Render the same conversation content and execution progress available in Desktop, including rich messages, tools, Todos, context, subagents, teammates, tasks, and diagnostic details.

**Blocked by:** 04 — Make real-time delivery recoverable; 07 — Match the Desktop Session lifecycle.

**Status:** ready-for-agent

- [ ] Markdown, code blocks, Mermaid, inline images, and tool images match Desktop semantics.
- [ ] Assistant deltas, thinking state, tool starts and finishes, and Todo changes update live.
- [ ] Context usage, subagent activity, team activity, and task graph state remain coherent after resync.
- [ ] Tool, worker, team, and task detail views are remotely accessible when the Device is online.
- [ ] Identical event fixtures produce identical shared conversation state for Desktop and Web.
- [ ] Large output and long-running activity do not shift or overlap the interface.
