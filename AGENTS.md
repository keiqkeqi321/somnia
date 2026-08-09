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

Content/listing tools (`grep`, `glob`, `tree`, `find_symbol`, and read_file auto-resolve)
skip paths ignored by workspace `.gitignore` files (nested files included, deepest rules
win, `!` negation supported) via `open_somnia/tools/gitignore.py::GitignoreMatcher`, in
addition to the hardcoded `EXPLORATION_IGNORED_DIR_NAMES` list. Ignored directories are
pruned during the walk. `grep` classifies files by extension before reading: known-binary
extensions (`BINARY_FILE_EXTENSIONS`) are skipped without opening, known-text extensions
(`TEXT_FILE_EXTENSIONS`) are read directly, and unknown extensions fall back to NUL-byte
sniffing of the first 8 KB. An explicit single-file `path` always bypasses ignore rules. `list_ignored()`
(`open_somnia/tools/filesystem.py`) reports which paths are excluded and by which rule;
it is a diagnostic helper, intentionally not registered as an LLM tool.

### grep acceleration via ripgrep

`grep` (`open_somnia/tools/filesystem.py::grep_search`) delegates to the system
`ripgrep` when available (`open_somnia/tools/ripgrep.py`), falling back to the pure-Python
implementation whenever rg is unsuitable. The Python path is the source of correctness and
is preserved verbatim as the fallback — never delete it. Delegation conditions:

- **rg available** (`shutil.which("rg")` resolves and `--version` parses); disabled by
  `SOMNIA_NO_RG=1` (troubleshooting switch). `find_ripgrep()` caches the result per process.
- **pattern is pure ASCII** — rg encodes the pattern as UTF-8 bytes and cannot match
  GBK/GB18030-encoded files for CJK patterns; non-ASCII patterns go straight to Python
  (`_read_text_with_fallback` decodes gb18030/cp936).
- **ASCII pattern but a matched line carries non-UTF-8 bytes** (e.g. GBK file with Chinese
  content) — `run_ripgrep` decodes stdout with `errors='strict'` and returns `None` on
  `UnicodeDecodeError`, triggering the Python fallback so the Chinese text is rendered
  correctly.
- **rg exit code 2** (unsupported regex, e.g. backreferences `(?P=...)` / `\1`) — returns
  `None`, Python `re` handles it.
- **spawn failure / `base_path` outside workspace** — returns `None`, Python handles it.

argv mapping: `-H` (always print filename), `--null` (NUL-separated filename defeats the
Windows `D:\...` colon ambiguity), `-e <pattern>` (flag form avoids PATTERN/PATH positional
ambiguity), `-F` for literal / raw regex passthrough, `-i` when case-insensitive,
`--max-depth 1` when non-recursive, `--sort path` for deterministic order, and
`--no-require-git` so `.gitignore` applies even without a `.git` directory. The hardcoded
`EXPLORATION_IGNORED_DIR_NAMES` / prefixes and `BINARY_FILE_EXTENSIONS` are translated to
`-g '!<name>/'` / `-g '!*.<ext>'` globs so projects without a `.gitignore` still skip
`.venv`/`node_modules`/binaries. `cwd` is set to `base_path` so glob variants are evaluated
relative to `base_path` (matching the Python path's base-relative label matching); output
paths are re-prefixed to workspace-relative form on parse.

### Parallel tool dispatch (order-preserving segment parallelism)

Independent read-only tool calls in a single turn run concurrently; writes and
state-changing calls stay serial. `open_somnia/runtime/parallel_dispatch.py`
implements the policy; both call sites — the Lead main loop
(`open_somnia/runtime/agent.py`) and `SessionlessRoundRunner.run_round`
(`open_somnia/runtime/round_runner.py`) — dispatch through it.

**Algorithm.** `segment_tool_calls(tool_calls)` scans the calls in input order
and yields maximal runs of consecutive **parallel-safe** call indices. Each
segment runs on a process-lifetime singleton `ThreadPoolExecutor`
(`max_workers = min(8, runtime.parallel_tool_max_workers)`). Results are
re-collected and **reordered to input order** before being paired back to the
provider's `tool_use` blocks (Anthropic/OpenAI pair results positionally/by id).
Segments are concatenated in input order, so the observable behaviour — result
order, transcript order, counters, guards, turn-boundary interrupts — is
identical to serial execution. Serial fast path: segments of length ≤ 1 (a lone
safe call, or any unsafe call) execute inline exactly as before.

**Whitelist** (`PARALLEL_SAFE_TOOL_NAMES`, a strict subset of the read-only
tools): `read_file`, `read_image`, `grep`, `glob`, `tree`, `find_symbol`,
`web_fetch`, `task_get`, `task_list`, `list_teammates`, `check_background`.
Everything else is conservatively serial: `TodoWrite` (unlocked `session.todo_items`
write), `request_authorization`/`request_mode_switch` (blocking handshake +
control flow), `submit_plan`/`compress`/`load_skill`/`request_original_context`
(context mutation), `write_file`/`edit_file`/`bash`/`background_run` (writes /
side effects), `subagent`/`spawn_teammate` (nested agent loops), all task-mutation
and team-collaboration tools, and all `mcp__*` tools (no read-only marker). GIL
releases during I/O in the whitelisted tools make the threading worthwhile.

**Explore-subagent parallelism.** An `agent_type=Explore` `subagent` call is
also parallel-safe (via `is_explore_subagent_safe`, *not* membership in
`PARALLEL_SAFE_TOOL_NAMES` — that set stays a pure read-only *tool* list).
Consecutive Explore-subagent calls in one turn run concurrently so the lead
can fan out three explorations and pay max(latency), not sum(latency).
`general-purpose` subagents (which carry `write_file`/`edit_file`) stay serial.
Two dispatch pools, deliberately separate: read-only tools run on `_POOL`
(`dispatch_parallel_segment`); Explore subagents run on `_SUBAGENT_POOL`
(`run_parallel_explore_subagents`). A subagent is a nested agent loop whose
internal rounds submit read-only tools to `_POOL`, so the subagent calls
themselves must **not** consume `_POOL` workers or they deadlock (all workers
busy holding subagent loops waiting for a worker). The lead loop bounds a
maximal parallel run by *kind* (`_parallel_safe_kind`: `tool` vs `subagent`)
so the two pools never mix in one segment. `SkillLoader` is guarded by an
`RLock` (its `reload` reassigns `self.skills` wholesale; parallel subagents
calling `load_skill` could otherwise observe a half-rebuilt dict).

**Explore-subagent read-only bash.** An Explore subagent's only write vector
is `bash` (it has no `write_file`/`edit_file`). To keep parallel Explore
subagents free of write races, the subagent runner registers a **gated** `bash`
(`register_readonly_shell_tool`) that refuses mutating commands via
`is_readonly_shell_command` (allow-list of read-only prefixes mirroring
`EXPLORATION_SHELL_PREFIXES`, plus a write-syntax deny-list for `>`/`|tee`/
`rm`/`git checkout`/`git reset`/`git pull`/`git push`/`git commit`/`npm install`/etc.).
Non-read-only commands return an error naming the write op and suggesting a
read-only alternative or a general-purpose subagent / lead-loop `bash`. The
lead loop's `bash` and general-purpose subagents' `bash` are **unrestricted**.

**Lead loop (three-stage).** Stage A is a deterministic, I/O-free pre-scan
(`_plan_lead_tool_calls`) that reproduces the serial guard/counter sequence
(flood guard, malformed/unknown-name dedup with drop-on-repeat, exploration
budget streak/total) and yields a plan with a decision per call plus the
computed `is_parallel_safe`/`is_exploration`/`end_turn_after` flags. Stages B
and C are **fused into a single cursor loop**: each iteration determines the
segment (maximal parallel-safe run, or a single call), executes it
(`dispatch_parallel_segment` or inline), then applies all side effects for that
segment in input order — `print_tool_started`, repair hints, `print_tool_event`,
transcript append, `reported_tool_calls`/exploration counters, `used_todo`/
`manual_compact`, and the stateful interrupts (`is_turn_boundary`,
`prepare_next_loop_user_message`, `end_turn_after`) which break the loop exactly
as the serial loop did. Fusion is required because `prepare_next_loop_user_message`
moves pending→ready context injections and cannot be pre-scanned.

**Locking.** `PermissionManager` wraps its worker-once / lead-once counter
mutations in an `RLock` as defensive insurance (the safe set never needs a once
grant). UI rendering, transcript, and `tool_results` appends all happen in stage
C / the round-runner post-segment hooks, which are single-threaded and
order-preserving — no extra locks needed.

**Switches.** `runtime.parallel_tool_dispatch` (default `True`) and
`runtime.parallel_tool_max_workers` (default `8`) live in
`RuntimeSettings` (`open_somnia/config/models.py`). The escape hatch
`SOMNIA_NO_PARALLEL_TOOLS=1` forces fully serial execution regardless of config
(troubleshooting). The system prompt tells the model that independent read-only
calls run concurrently and sequencing only matters across result dependencies.

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
