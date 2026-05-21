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

Dependencies (from `pyproject.toml`): `anthropic>=0.25.0`, `Pillow>=10.3.0`, `prompt_toolkit>=3.0.43`, `tiktoken>=0.8.0`. Requires Python >=3.11.

## Configuration

Primary config files:

- `.env` — environment variables (not checked in)
- `open_somnia.toml` — project-level settings
- `~/.open_somnia/open_somnia.toml` — global shared settings

Key sections: `[agent]`, `[providers]`, `[providers.<name>]`, `[runtime]`, `[mcp_servers.<name>]` / `[[mcp_servers]]`, `[hooks]`

On first run with no providers, the CLI bootstraps an interactive provider setup flow and saves to global config.

## Persistence Model

All state lives under `.open_somnia/` in the workspace root:

- `sessions/`, `transcripts/`, `tasks/`, `inbox/`, `team/`, `jobs/`, `logs/`
- `permissions.json` — workspace-scoped tool authorizations

Do not change storage shape without updating load/save paths.

## REPL Execution Modes

Four modes ordered by risk (`Shift+Tab` cycles):

1. `? shortcuts` — read-only workspace access
2. `⏸ plan mode` — read-only + planning-first
3. `⏵⏵ accept edits` — file edits, task mutations, team collaboration
4. `! Yolo` — full autonomy

Blocked tools trigger `request_authorization`. Agents may request non-Yolo mode switches via `request_mode_switch`. "Allow in this workspace" persists to `.open_somnia/permissions.json`.

## TodoWrite Behavior

- Session-scoped; shown in REPL status while any item is open (☐ pending, ⏳ in progress, ✅ completed)
- Runtime injects a transient reminder on every turn while todos remain open; this reminder is NOT persisted
- Tool event box is suppressed in terminal output; internal logs still recorded

## Shell Tool (`bash`)

Platform-aware: Unix uses system shell; Windows uses PowerShell. Common Unix commands are auto-translated on Windows. Untranslatable commands return guidance instead of cryptic errors.

## Session Resume

`somnia -r` opens a session picker. Incomplete sessions (missing user message or assistant reply) are filtered out. `somnia -c` continues the latest session.

## Editing Guidance

- Keep tool behavior consistent with REPL UX.
- Fix in runtime/tool layers, not UI docs.
- Check both Unix and Windows for prompt/shell changes.
- Verify both tool output and REPL status rendering for todo changes.
- Preserve the session filter for resume behavior.
- The runtime appends execution-environment guidance to the system prompt — preserve that when modifying prompt construction.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **somnia** (8173 symbols, 16806 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/somnia/context` | Codebase overview, check index freshness |
| `gitnexus://repo/somnia/clusters` | All functional areas |
| `gitnexus://repo/somnia/processes` | All execution flows |
| `gitnexus://repo/somnia/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
