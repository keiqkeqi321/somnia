# 04 — Per-MCP-server tool subset configuration

**What to build:** Per-tool enable/disable for MCP servers, manageable from BOTH
the CLI (`/mcp` tool list) and the desktop app (Settings → MCP server row → tool
list), with every change persisted to the TOML config (line-level upsert,
comments/unknown keys preserved) and applied via runtime reload — mirroring the
existing server-level enabled flow. Config: optional `include_tools` /
`exclude_tools` per server (exclude wins on overlap); neither = all enabled.
Toggling from a UI writes whichever list the server already uses, defaulting to
`exclude_tools`. Disabled tools are not registered (absent from the tools array)
but stay visible in management UIs with their state.

**Blocked by:** None.

**Status:** done

- [x] `include_tools` / `exclude_tools` parse from both `[mcp_servers.<name>]` and `[[mcp_servers]]` forms
- [x] Registration filter applies at connect and runtime refresh (single point: server tool definition registration)
- [x] Server/tool summaries carry per-tool enabled state for UIs
- [x] Toggle persists to TOML (project-first file location) and reloads the runtime; no spurious registration warnings
- [x] CLI: `/mcp` tool detail offers enable/disable; desktop: per-tool switch in the MCP settings panel; remote-relay RPC parity
- [x] Tests cover include-only, exclude-only, overlap (exclude wins), absent-filter, toggle persist round-trip, sidecar endpoint; full unittest suite passes

## Comments

History: originally proposed 2026-08-03, closed wontfix in favor of deferred
loading (ticket 05), reopened same day after 05 was cancelled — static subset
selection has zero cache-compat risk across providers, unlike deferred loading.
Scope extended: management surfaces on CLI + desktop with config sync.

Completed 2026-08-03. Implementation:

- `config/models.py` — `MCPServerSettings.include_tools/exclude_tools`
  (`None` = key absent; explicit `[]` include = all disabled) + `tool_enabled()`.
- `config/settings.py` — parse both TOML forms; `persist_mcp_tool_enabled()`
  line-level upsert (project file first, global fallback, appends a minimal
  table when the server is absent), preserving comments/unknown keys.
- `mcp/registry.py` — filter at `_register_server_tool_definitions` (covers
  connect + refresh); `tool_summaries` carries per-tool `enabled`,
  `server_summaries` adds `enabled_tool_count`.
- `runtime/agent.py` — `set_mcp_tool_enabled()` (persist + reload), used by CLI.
- `cli/repl.py` — `/mcp` shows `[x]/[ ]` states and enabled/total counts; tool
  detail view toggles enable/disable with live refresh.
- `desktop/backend/server.py` — `POST /mcp/servers/{name}/tools/{tool}/enabled`.
- `open_somnia/remote/connector.py` — `mcp.set_tool_enabled` RPC parity.
- `desktop/ui` — per-tool switch in Settings → MCP (dimmed disabled state,
  in-flight guard, i18n EN/CN, contract tests for both transports).
- Tests: `test_mcp_registry.py` +6, `test_settings_overrides.py` +4,
  `test_sidecar_server.py` +1; desktop vitest 47 pass, typecheck/build clean.
- Full Python suite: 757 tests OK.

Verification note: toggling rewrites only the target key's line; unknown keys
and comments survive (line-level upsert, `write_config_text` makes a timestamped
backup before writing). Not committed — left for user review.
