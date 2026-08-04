# 02 — Warn on tool-name collisions in the registry

**What to build:** When a tool registration overwrites an already-registered
name (two MCP servers with the same server name, or an MCP tool shadowing a
built-in), a visible warning is emitted (runtime log and, where a CLI surface
exists, a notice). Registration still succeeds — intentional replacement after
prefix-unregister (MCP refresh) must not break.

**Blocked by:** None — can start immediately.

**Status:** done

- [x] Overwriting an existing tool name produces a recorded warning naming both the tool and, when known, its origin
- [x] MCP server refresh / disable-enable cycles do not emit spurious warnings (they unregister by prefix first)
- [x] A test covers the collision path and the no-warning refresh path
- [x] Full unittest suite passes

## Comments

Completed 2026-08-03. Changes:

- `tools/registry.py` — `ToolRegistry.registration_warnings` records
  `Tool name collision: '<name>' from <incoming origin> overwrites <previous
  origin>.` on overwrite; registration still succeeds (last-write-wins kept).
  Origin = MCP server name parsed from the `mcp__<server>__` prefix, or the
  handler's module for builtins.
- `mcp/registry.py` — `MCPRegistry.warnings`; `register_tools` warns on
  duplicate enabled server names (that case bypasses registry-level detection
  because the later server prefix-unregisters the earlier one's tools first);
  warnings surfaced in `status_lines()` and `describe_servers()`.
- `runtime/agent.py` — reload summary gains `tool_warnings` / `mcp_warnings`.
- `cli/repl.py` — `/reloadplugin` summary renders tool warnings.
- Tests: new `tests/test_tool_registry.py` (4 cases: MCP-name collision,
  builtin-module origin, unregister→re-register silence, distinct-name silence);
  `tests/test_mcp_registry.py` +2 (duplicate server name warning incl. surface
  rendering; double refresh stays silent).

Verification: targeted 8/8 green; full suite 746 tests OK (exit 0). One earlier
full-suite run hit the known `test_sidecar_server` 2s-HTTP-timeout flake —
standalone 18/18 green, unrelated to this change; rerun was green.
Not committed — left for user review.
