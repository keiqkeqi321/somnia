# 08 — Match rich output and progress visibility

**What to build:** Render the same conversation content and execution progress available in Desktop, including rich messages, tools, Todos, context, subagents, teammates, tasks, and diagnostic details.

**Blocked by:** 04 — Make real-time delivery recoverable; 07 — Match the Desktop Session lifecycle.

**Status:** completed

- [x] Markdown, code blocks, Mermaid, inline images, and tool images match Desktop semantics.
- [x] Assistant deltas, thinking state, tool starts and finishes, and Todo changes update live.
- [x] Context usage, subagent activity, team activity, and task graph state remain coherent after resync.
- [x] Tool, worker, team, and task detail views are remotely accessible when the Device is online.
- [x] Identical event fixtures produce identical shared conversation state for Desktop and Web.
- [x] Large output and long-running activity do not shift or overlap the interface.

## Verification

- Remote Connector/Tracer protocol tests pass, including diagnostic routes and bounded workspace-image transfer (11 focused tests; 20 broader remote tests).
- Shared conversation fixtures, Direct/Remote connection tests, and TypeScript typecheck pass (10 Vitest tests).
- Playwright rich-output flow passes at desktop and mobile viewports, including Markdown, code, Mermaid, inline images, live deltas, and overflow checks (2 tests).
- Full Python discovery ran 663 tests: 661 passed; the two known unrelated baseline failures are missing `pytest` for `tests/test_utils.py` and the stale `kimi-coding-plan` preset expectation.
