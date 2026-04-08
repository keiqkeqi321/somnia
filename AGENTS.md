# OpenAgent Notes For AI Agents

## What This Project Is

OpenAgent is a Python CLI agent framework packaged under `openagent/`. It is not just a demo script: it has a reusable runtime, persistent session storage, tool registration, MCP integration, background jobs, and teammate collaboration primitives.

The package entrypoint is:

- `openagent = openagent.cli.main:main`

## Main Execution Path

For interactive use, the important call path is:

1. `openagent.cli.main`
2. `openagent.cli.commands`
3. `openagent.cli.repl`
4. `openagent.runtime.agent.OpenAgentRuntime`

The runtime owns:

- provider selection
- tool registry
- session persistence
- background job manager
- MCP registry
- todo manager
- team collaboration state

## Important Directories

- `openagent/cli/`: CLI entrypoints, REPL, prompt UI
- `openagent/runtime/`: agent loop, session manager, runtime composition
- `openagent/tools/`: built-in tools such as `bash`, filesystem, todo, MCP, background jobs
- `openagent/storage/`: persisted JSON/JSONL-backed stores under `.openagent/`
- `openagent/config/`: TOML and env loading
- `openagent/providers/`: Anthropic and OpenAI-compatible provider adapters
- `openagent/mcp/`: MCP transports and registry
- `tests/`: package-level regression tests

## Current User-Facing Behaviors

- Running `openagent --workspace .` starts interactive chat directly.
- Running `openagent -r` opens a session picker and resumes a selected session.
- The REPL has four execution modes ordered by risk:
  - `? for shortcuts`: read-only workspace access
  - `⏸ plan mode on`: read-only plus planning-first behavior
  - `⏵⏵ accept edits on`: file edits, persistent task mutations, and agent-team collaboration allowed; broader tools still blocked
  - `! Yolo`: full autonomy
- `Shift+Tab` cycles execution modes in the REPL.
- The active execution mode is shown under `openagent >>` with color-coded risk.
- When a needed tool is blocked by the current mode, the agent can call `request_authorization`.
- The agent can call `request_mode_switch` to ask the user to switch to `? for shortcuts`, `⏸ plan mode on`, or `⏵⏵ accept edits on`.
- The agent must not use `request_mode_switch` to request `! Yolo`.
- Authorization prompts should offer:
  - allow once
  - allow in this workspace
  - deny
- `Allow in this workspace` should persist under `.openagent/permissions.json` so the workspace-scoped approval survives restarting OpenAgent.
- Mode-switch prompts should let the user either switch to the requested non-Yolo mode or stay in the current mode.
- After the user answers an authorization prompt, the agent should continue the same task without requiring the user to restate it.
- Empty or incomplete sessions should not appear in resume history. A session must include both a visible user message and a visible assistant reply.
- `TodoWrite` updates session-scoped todos.
- Todos are shown persistently in the REPL status area above `openagent >>` while any item is still open.
- When all todos are completed, the todo status block disappears.
- Todo status markers are:
  - `☐` pending
  - `⏳` in progress
  - `✅` completed
- `TodoWrite` should not print the normal tool event box to the terminal, but tool logs are still recorded internally.

## Shell Tool Expectations

The tool name remains `bash`, but behavior is platform-aware.

- On Unix-like systems, it uses the system shell.
- On Windows, it runs PowerShell-compatible commands.
- The runtime system prompt explicitly tells the model which OS it is on.
- The `bash` tool description also explains the platform behavior.
- On Windows, common Unix commands are translated when safe:
  - `ls -la` -> `Get-ChildItem -Force`
  - `pwd` -> `Get-Location`
  - `cat ...` -> `Get-Content ...`
  - `find . -name "*.py" -type f | head -20` -> PowerShell equivalent
- For Unix-only commands that are not safely translated, the tool should return a clear guidance message instead of a cryptic shell error.

## Configuration

Primary config files:

- `.env`
- `openagent.toml`
- `openagent.toml.example`

Key config sections in `openagent.toml`:

- `[agent]`
- `[providers]`
- `[providers.<name>]`
- `[runtime]`
- `[mcp_servers.<name>]` or `[[mcp_servers]]`

The runtime appends execution-environment guidance to the system prompt, so changes to prompt construction should preserve that.

## Persistence Model

State lives under `.openagent/` in the workspace root. Important subfolders:

- `.openagent/sessions`
- `.openagent/transcripts`
- `.openagent/tasks`
- `.openagent/inbox`
- `.openagent/team`
- `.openagent/jobs`
- `.openagent/logs`
- `.openagent/permissions.json`

Do not casually change storage shape unless you also update load/save paths and compatibility expectations.

## Tests That Matter

Useful regression tests for recent behavior:

- `tests/test_cli_resume.py`
- `tests/test_process_output.py`
- `tests/test_repl_todo.py`
- `tests/test_runtime_tool_output.py`

Run them with:

```bash
python -m unittest tests.test_cli_resume tests.test_process_output tests.test_repl_todo tests.test_runtime_tool_output
```

## Editing Guidance

- Keep tool behavior consistent with the current REPL UX.
- Prefer fixing behavior in runtime/tool layers instead of papering over it in docs.
- If changing prompt or shell behavior, check both Unix and Windows assumptions.
- If changing todo behavior, verify both tool output and REPL status rendering.
- If changing resume/session behavior, preserve the filter that hides non-conversation sessions.
