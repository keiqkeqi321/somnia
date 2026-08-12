# Somnia Notes For AI Agents

Higher-priority user instructions, runtime rules, and tool safety rules override this file.

## What This Project Is

Somnia is a Python CLI agent framework (v0.5.2) packaged under `open_somnia/`.
It provides a reusable runtime, persistent session storage, tool registration, MCP integration, background jobs, hooks, skills, and teammate collaboration primitives.

Entrypoints (`pyproject.toml` → `[project.scripts]`):

- `somnia = open_somnia.cli.main:main`
- `somnia-sidecar = desktop.backend.bootstrap:main`

## Main Execution Path

1. `open_somnia.cli.main` → `open_somnia.cli.commands`
2. `open_somnia.cli.repl` (interactive loop)
3. `open_somnia.runtime.agent.OpenAgentRuntime` (owns providers, tools, sessions, MCP, todos, team state)

## Important Directories

| Directory | Purpose |
|-----------|---------|
| `open_somnia/cli/` | CLI entrypoints, REPL, prompt UI, provider management |
| `open_somnia/runtime/` | Agent loop, session, execution modes, permissions, system prompt, subagent/teammate runners |
| `open_somnia/tools/` | Built-in tools: shell, filesystem, todo, tasks, MCP, background jobs, subagent, team |
| `open_somnia/storage/` | Persisted JSON/JSONL stores under `.open_somnia/` |
| `open_somnia/config/` | TOML + env loading, provider profiles, settings models |
| `open_somnia/providers/` | Anthropic and OpenAI-compatible provider adapters |
| `open_somnia/mcp/` | MCP transports (stdio, HTTP) and registry |
| `open_somnia/hooks/` | Hook system: manager, runner, SDK, user notifications |
| `open_somnia/skills/` | Skill loader + builtin skills |
| `open_somnia/collaboration/` | Message bus and collaboration protocols |
| `open_somnia/app_service/` | Desktop-side API service layer (sessions, turns, providers, runtime host) |
| `desktop/backend/` | Sidecar server, IPC, bootstrap for Tauri desktop app |
| `desktop/ui/` | Tauri + React/TypeScript frontend (Vite) |
| `tests/` | Unittest-based regression tests |

## Build and Test Commands

```bash
# Install (editable)
pip install -e .

# Run key regression tests (unittest)
python -m unittest tests.test_cli_resume tests.test_process_output tests.test_repl_todo tests.test_runtime_tool_output

# Run all tests
python -m unittest discover -s tests -p "test_*.py"

# Version is in VERSION file (currently 0.5.2)
```

Dependencies (from `pyproject.toml`): `anthropic>=0.25.0`, `Pillow>=10.3.0`, `prompt_toolkit>=3.0.43`, `tiktoken>=0.8.0`, `pathspec>=0.12,<2` (used by `open_somnia/tools/gitignore.py` for gitignore matching). Requires Python >=3.11.

## Search Tools and Ignore Rules

Content/listing tools (`grep`, `glob`, `tree`, `find_symbol`, read_file auto-resolve)
skip `.gitignore`-ignored paths (nested rules, deepest wins, `!` negation) via
`open_somnia/tools/gitignore.py::GitignoreMatcher`, plus the hardcoded
`EXPLORATION_IGNORED_DIR_NAMES`. Explicit single-file `path` always bypasses ignore
rules. `list_ignored()` (`open_somnia/tools/filesystem.py`) is a diagnostic helper,
not registered as an LLM tool.

### grep acceleration via ripgrep

`grep` (`open_somnia/tools/filesystem.py::grep_search`) delegates to system `ripgrep`
when available (`open_somnia/tools/ripgrep.py`), falling back to the pure-Python
implementation whenever rg is unsuitable — **the Python path is the source of correctness
and is preserved verbatim; never delete it**. Disabled by `SOMNIA_NO_RG=1`.

### Parallel tool dispatch

Independent read-only tool calls in one turn run concurrently; writes and state-changing
calls stay serial. Order-preserving segment parallelism implemented by
`open_somnia/runtime/parallel_dispatch.py`; observable behaviour is identical to serial
(guards, counters, turn-boundary interrupts all preserved). Strict whitelist
(`PARALLEL_SAFE_TOOL_NAMES` ⊂ read-only tools); Explore subagents parallel via a separate
`_SUBAGENT_POOL` (subagent loops also use `_POOL`, so the pools must never mix or they
deadlock). Explore `bash` is gated read-only. Escape hatch: `SOMNIA_NO_PARALLEL_TOOLS=1`.

Full details (algorithm, whitelist, three-stage lead loop, locking, switches):
`Docs/agents/parallel-dispatch.md`. Search/ignore/ripgrep details:
`Docs/agents/search-tools.md`.

## Configuration

Primary config files:

- `.env` — environment variables (not checked in)
- `open_somnia.toml` — project-level settings
- `~/.open_somnia/open_somnia.toml` — global shared settings

Key sections: `[agent]`, `[providers]`, `[providers.<name>]`, `[runtime]`, `[mcp_servers.<name>]` / `[[mcp_servers]]` (per-server `include_tools`/`exclude_tools` subset the registered tools; exclude wins; manageable live from CLI `/mcp` and the desktop MCP panel, persisted back to TOML), `[hooks]`

On first run with no providers, the CLI bootstraps an interactive provider setup flow and saves to global config.

## Persistence Model

State is split between the workspace and a centralized per-project store:

- Workspace `.open_somnia/` keeps project-level config only: `open_somnia.toml`,
  `permissions.json` (workspace-scoped tool authorizations), `skills/`, hooks,
  `temp/` (clipboard staging), `sidecar.lock`, `remote/settings.json`.
- Session-like state lives under `~/.open_somnia/projects/<project-key>/`
  (`config/settings.py::central_state_dir`, key = readable slug + sha1 of the
  normalized workspace path): `sessions/`, `transcripts/`, `tasks/`, `inbox/`,
  `team/`, `jobs/`, `requests/`, `logs/`, `repl_history.txt`, plus a
  `project.json` marker recording the workspace path.
- `StorageSettings.data_dir` is the workspace dir; `StorageSettings.state_dir`
  is the centralized dir. The 8 store sub-dirs derive from `state_dir`.

Do not change storage shape without updating load/save paths.

## REPL Execution Modes

Three modes ordered by risk (`Shift+Tab` cycles):

1. `? shortcuts` — read-only workspace access
2. `⏵⏵ accept edits` — file edits, task mutations, team collaboration
3. `! Yolo` — full autonomy

(The former `⏸ plan mode` was removed: its tool gating was identical to
shortcuts, so it carried no real constraint. Legacy persisted `plan` values
normalize to `shortcuts` — degraded, never escalated. Planning discipline is
expected to come from skills and tracker artifacts, not from a permission mode.)

Blocked tools trigger `request_authorization`. Agents may request non-Yolo mode switches via `request_mode_switch`. "Allow in this workspace" persists to `.open_somnia/permissions.json`.

`ask_user_question` lets the agent ask the user a multiple-choice question (options + optional custom answer) mid-turn. It is read-only in every mode and silent in the tool-event renderer. Interactive answers travel the same channels as authorization: the REPL main-thread prompt drain (CLI) or `InteractionService` → sidecar `POST /interactions/<id>/question` → desktop `InteractionDecisionCard` (kind `ask_user_question`, event `question_requested`). Non-interactive sessions (`somnia run`) auto-resolve it as `cancelled`.

## TodoWrite Behavior

- Session-scoped; shown in REPL status while any item is open (☐ pending, ⏳ in progress, ✅ completed)
- Runtime injects a transient reminder on every turn while todos remain open; this reminder is NOT persisted
- Tool event box is suppressed in terminal output; internal logs still recorded

## Shell Tool (`bash`)

Platform-aware: Unix uses system shell; Windows uses PowerShell. Common Unix commands are auto-translated on Windows. Untranslatable commands return guidance instead of cryptic errors.

## Session Resume

`somnia -r` opens a session picker. Incomplete sessions (missing user message or assistant reply) are filtered out. `somnia -c` continues the latest session.

## Scriptable CLI (non-interactive)

Every operation has a non-interactive path: given input → deterministic output →
immediate exit, never waiting for a keypress.

- `somnia sessions list [--json]` — list resumable sessions (no provider needed,
  no MCP connections). Pair with `somnia run --session <id> "..."` or
  `--continue-last` to continue a session by ID, skipping the picker.
  `somnia chat --session <id>` does the same for the REPL.
- `somnia run [prompt] [-f file]` — prompt may also come from piped stdin
  (`cat f.py | somnia run "review this"`); argument, file, and stdin text are
  combined in that order.
- `somnia providers list [--json]` — list profiles with `api_key_configured`
  booleans (keys are never printed). `somnia config get/set <key> [value]`
  (`--global` default for set, `--project` for the workspace file) replaces the
  interactive `somnia providers` editor; `config set` cannot edit
  array-of-tables sections (`hooks`).
- `--json` is accepted by `run`, `doctor`, `sessions list`, `providers list`,
  `config get/set`, `capabilities`, and `help`. Success JSON goes to stdout;
  failures print `{"error": {"code": ..., "message": ...}}` on stderr.
  `run --json` wraps the reply in an envelope with `session_id`, `status`,
  `text`, per-turn `usage` (tokens), `provider`, `model`, `duration_ms`.
  `run --plain` strips the bullet prefix and all ANSI styling.
- `somnia capabilities [--json]` — version, active provider/model, registered
  tools, and MCP server status, for probing before delegating.
- `somnia doctor [--json]` — exits 0 when healthy, 7 when no provider/API key.

Exit codes (defined in `open_somnia/cli/scripting.py`, keep in sync):

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | internal / unclassified error |
| 2 | quota exceeded / rate limited |
| 3 | authentication failed |
| 4 | model error (bad request / not found) |
| 5 | timeout |
| 6 | session not found |
| 7 | config error (no provider, bad TOML, unknown key) |
| 64 | usage error (bad CLI arguments) |

Provider errors carry a `kind` (`auth`/`quota`/`model`/`timeout`/`other`) from
the SDK adapters through `ProviderError`; the runtime re-raise preserves it and
`TurnRunResult.error_kind` exposes it to the CLI. Tracebacks are suppressed
unless `SOMNIA_DEBUG=1`.

## Editing Guidance

- Keep tool behavior consistent with REPL UX.
- Fix in runtime/tool layers, not UI docs.
- Check both Unix and Windows for prompt/shell changes.
- Verify both tool output and REPL status rendering for todo changes.
- Preserve the session filter for resume behavior.
- The runtime appends execution-environment guidance to the system prompt — preserve that when modifying prompt construction.

## Agent skills

### Issue tracker

Issues and specs are tracked as local Markdown files under `.scratch/`.
See `Docs/agents/issue-tracker.md`.

### Triage labels

Use the default engineering skill triage labels.
See `Docs/agents/triage-labels.md`.

### Domain docs

This repository uses a single-context domain documentation layout.
See `Docs/agents/domain.md`.
