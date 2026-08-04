# 01 — Remove the `project_scan` tool

**What to build:** `project_scan` no longer exists anywhere in the product. The
model's tool menu, the Explore subagent's toolset, read-only mode allowances,
REPL listings, tool-event rendering, and project-init guidance all behave as if
the tool was never there; repository orientation is done with `tree` + `glob`.
Its dedicated constants and handler are gone, not just unregistered.

**Blocked by:** None — can start immediately (pre-approved by user).

**Status:** done

- [x] `project_scan` is absent from the lead registry, worker/subagent registries, and the Explore-mode prompt
- [x] `project_scan` is absent from read-only/exploration tool name sets, tool-event rendering, REPL lists, and project-init guidance text
- [x] Handler and its exclusive constants (guidance/manifest/entry/source-root name tables) are deleted
- [x] A repo-wide search for `project_scan` returns no remaining references (code, tests, docs)
- [x] Tests referencing `project_scan` are removed or adjusted; full unittest suite passes

## Comments

Touchpoints identified during planning (verify by search, do not trust blindly):
tool registration + handler + constants in the filesystem tool module;
`EXPLORATION_TOOL_NAMES` and a classification set in the runtime agent module;
read-only list in execution modes; Explore registry + prompt in the subagent
runner; tool event rendering; project-init guidance; REPL tool lists; tests
`test_filesystem_tool.py`, `test_subagent_tool.py`, `test_runtime_tool_output.py`,
`test_project_init.py`.

Completed 2026-08-03. 21 files changed. Beyond the planned touchpoints, removal
also covered the `/scan` REPL slash command (a UI over `project_scan`:
handler, dispatch, completion spec, README/Docs rows) — discovered during
implementation. Full suite: 739 tests green (excluding two pre-existing
environmental/order flakes unrelated to this change; see session notes).
Work not committed — left for the user to review and commit.
