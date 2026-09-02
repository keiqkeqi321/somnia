"""Unified help system for the Somnia CLI.

This module is the single source of truth for every command an agent (or
human) can drive Somnia with: CLI subcommands, global CLI options, and REPL
slash commands.  ``somnia -help`` prints an overview of all commands and
``somnia -help <topic>`` prints a detailed spec for one command.  Both forms
accept ``--json`` for machine-readable output, so external agents can
discover and invoke the full CLI surface programmatically.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any

from open_somnia import __version__

INTRO = (
    "Somnia is a Python CLI agent framework for building and running AI agents. "
    "It provides a reusable runtime, persistent session storage, tool "
    "registration, MCP integration, background jobs, hooks, skills, and "
    "teammate collaboration primitives. Both humans and other agents can "
    "drive Somnia through the `somnia` CLI."
)

USAGE_LINE = (
    "somnia <command> [options]   |   somnia -help [topic]   |   "
    "somnia chat (interactive REPL)"
)

OPTION_DESCRIPTIONS: dict[str, str] = {
    "--provider": "Override the configured provider for this invocation.",
    "--model": "Override the configured model for this invocation.",
    "--workspace": "Workspace root for the agent (default: current directory).",
    "-r": "Open the interactive session picker and resume a saved chat.",
    "-c": "Continue the latest saved chat in this workspace.",
    "-version": "Show the installed somnia version and exit.",
    "-help": "Show the somnia command overview, or detailed help for one command: -help <topic>.",
    "--json": "Emit machine-readable JSON on stdout (and structured JSON errors on stderr where supported).",
    "--session": "Resume the saved session with this ID (chat/run); for traceviewer, only include provider payloads for this session ID.",
    "--continue-last": "Continue the latest saved session in this workspace.",
    "--plain": "Plain output: no ANSI styling and no bullet prefix (ideal for pipes).",
    "--at": "Fork point: number of leading messages the forked session keeps.",
    "-f": "Read prompt text from this file (combined with the prompt argument and piped stdin).",
    "--global": "Use the global config file (~/.open_somnia/open_somnia.toml).",
    "--project": "Use the workspace config file (.open_somnia/open_somnia.toml).",
    "--limit": "Only include the latest N matching provider payloads.",
    "--output": "Write the HTML report to this path instead of the default provider payload log directory.",
}


@dataclass(frozen=True)
class CommandSpec:
    """One discoverable command: a CLI subcommand, global option, or REPL slash command."""

    name: str
    description: str
    usage: str
    detail: str
    section: str  # "cli" | "option" | "repl"
    options: tuple[str, ...] = field(default_factory=tuple)
    examples: tuple[str, ...] = field(default_factory=tuple)
    hidden: bool = False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CLI_COMMANDS: list[CommandSpec] = [
    CommandSpec(
        name="chat",
        description="Start interactive chat mode.",
        usage="somnia chat [-r | -c | --session <id>] [--provider <name>] [--model <name>]",
        detail=(
            "Launches the interactive REPL. With -r (or -resume) an interactive "
            "session picker lets you resume a saved chat; with -c the latest saved "
            "chat in this workspace is continued automatically; with --session <id> "
            "a saved session is resumed directly by ID (no picker, script-friendly). "
            "Inside the REPL, slash commands (see /help) expose the full runtime: "
            "tools, tasks, teammates, MCP servers, hooks, checkpoints, and "
            "background jobs."
        ),
        section="cli",
        options=("-r", "-c", "--session", "--provider", "--model"),
        examples=(
            "somnia chat",
            "somnia chat -r",
            "somnia chat --session 6b466a6d6a78",
            "somnia chat -c --provider anthropic",
        ),
    ),
    CommandSpec(
        name="run",
        description="Run a single prompt and exit.",
        usage="somnia run [prompt] [-f <path>] [--session <id> | --continue-last] [--json] [--plain] [--provider <name>] [--model <name>]",
        detail=(
            "Executes one prompt non-interactively and streams the assistant reply "
            "to stdout. The prompt comes from the argument, -f/--file, and/or piped "
            "stdin (combined in that order), so `cat file.py | somnia run \"review "
            "this\"` works. --session/--continue-last continue a saved session by ID "
            "without any picker. --json prints one JSON envelope on stdout with "
            "session_id, status, text, per-turn usage (tokens), provider, model, and "
            "duration_ms; failures print a structured error object on stderr. "
            "--plain strips the bullet prefix and all ANSI styling. Exit codes: 0 "
            "ok, 1 internal error, 2 quota exceeded, 3 auth failed, 4 model error, "
            "5 timeout, 6 session not found, 7 config error, 64 usage error."
        ),
        section="cli",
        options=("--session", "--continue-last", "--json", "--plain", "-f", "--provider", "--model"),
        examples=(
            'somnia run "Summarize the open tasks"',
            'somnia run --model gpt-5 "Refactor the parser module"',
            'cat diff.patch | somnia run "Review this diff" --json',
            'somnia run -f prompt.txt --session 6b466a6d6a78 --json',
        ),
    ),
    CommandSpec(
        name="sessions",
        description="Inspect saved sessions non-interactively.",
        usage="somnia sessions list [--json] | somnia sessions fork <session-id> --at <n> [--json]",
        detail=(
            "Lists saved sessions for this workspace (id, timestamps, preview, "
            "token usage) without opening the interactive picker, or forks a "
            "session so the branch keeps only its first N messages. Does not "
            "require a configured provider and never connects to MCP servers. "
            "Pair with `somnia run --session <id>` to continue a session (or a "
            "fresh fork) from a script."
        ),
        section="cli",
        options=("--json", "--at"),
        examples=(
            "somnia sessions list",
            "somnia sessions list --json",
            "somnia sessions fork 6b466a6d6a78 --at 12 --json",
        ),
    ),
    CommandSpec(
        name="config",
        description="Read or modify configuration non-interactively.",
        usage="somnia config get <key> [--global|--project] [--json] | somnia config set <key> <value> [--global|--project]",
        detail=(
            "Gets or sets config values by dotted key (e.g. providers.default, "
            "agent.name). get reads the merged view by default; set writes the "
            "global config unless --project is given. Values are parsed as TOML "
            "literals when possible (true, 42, [\"a\"]) and as strings otherwise. "
            "Array-of-tables sections (hooks) cannot be edited with set."
        ),
        section="cli",
        options=("--global", "--project", "--json"),
        examples=(
            "somnia config get providers.default",
            "somnia config set providers.default myprovider",
            "somnia config set agent.name my-agent --project",
        ),
    ),
    CommandSpec(
        name="capabilities",
        description="List available tools, models, and MCP servers.",
        usage="somnia capabilities [--json] [--provider <name>] [--model <name>]",
        detail=(
            "Reports the somnia version, active provider/model, configured "
            "providers, every registered tool (name + description), and MCP server "
            "status. Lets a calling agent probe what this installation can do "
            "before delegating work."
        ),
        section="cli",
        options=("--json", "--provider", "--model"),
        examples=(
            "somnia capabilities",
            "somnia capabilities --json",
        ),
    ),
    CommandSpec(
        name="tasks",
        description="Inspect persistent tasks.",
        usage="somnia tasks list | somnia tasks get <task_id>",
        detail=(
            "Reads the persistent task store. `somnia tasks list` prints every "
            "task as a JSON object; `somnia tasks get <id>` prints one task. "
            "Output is JSON on stdout, so scripts and agents can consume it "
            "directly."
        ),
        section="cli",
        examples=(
            "somnia tasks list",
            "somnia tasks get 3",
        ),
    ),
    CommandSpec(
        name="compact",
        description="Compact the latest session.",
        usage="somnia compact [--provider <name>] [--model <name>]",
        detail=(
            "Compacts the most recent saved session in this workspace to keep the "
            "context window within budget. Useful before continuing a long-running "
            "agent conversation."
        ),
        section="cli",
        options=("--provider", "--model"),
    ),
    CommandSpec(
        name="doctor",
        description="Validate runtime configuration.",
        usage="somnia doctor [--json] [--provider <name>] [--model <name>]",
        detail=(
            "Runs configuration diagnostics (providers, storage, hooks, MCP, and "
            "runtime wiring). Default output is a human-readable report; --json "
            "emits the same checks as structured data. Exits 0 when healthy, 7 "
            "when no provider or API key is configured, so health checks can be "
            "scripted."
        ),
        section="cli",
        options=("--json", "--provider", "--model"),
    ),
    CommandSpec(
        name="trace",
        description="Start Somnia with provider payload debug tracing enabled.",
        usage="somnia trace [prompt] [-r | -c] [--provider <name>] [--model <name>]",
        detail=(
            "Runs Somnia with provider payload debug tracing enabled. With a "
            "prompt, a single turn is executed; without one, interactive chat "
            "starts. After exit the trace report is opened automatically (see "
            "`somnia traceviewer` to regenerate it)."
        ),
        section="cli",
        options=("-r", "-c", "--provider", "--model"),
        examples=("somnia trace 'explain this error'", "somnia trace"),
    ),
    CommandSpec(
        name="traceviewer",
        description="Generate an HTML viewer for provider payload debug dumps.",
        usage="somnia traceviewer [--session <id>] [--limit <n>] [--output <path>]",
        detail=(
            "Builds an HTML report from saved provider payload dumps. Filters by "
            "session with --session and by count with --limit; --output redirects "
            "the report path. Alias: `somnia trace-viewer`."
        ),
        section="cli",
        options=("--session", "--limit", "--output"),
        examples=("somnia traceviewer", "somnia traceviewer --limit 5 --output trace.html"),
    ),
    CommandSpec(
        name="providers",
        description="Add or edit shared provider profiles.",
        usage="somnia providers | somnia providers list [--json]",
        detail=(
            "Interactively adds or edits shared provider profiles in the global "
            "config (requires a TTY). `somnia providers list` is the "
            "non-interactive counterpart: it prints every profile with API keys "
            "masked to a configured yes/no flag. For scripted edits use "
            "`somnia config set providers.<name>.<key> <value>`."
        ),
        section="cli",
        options=("--json",),
        examples=("somnia providers", "somnia providers list --json"),
    ),
    CommandSpec(
        name="help",
        description="Show the somnia help system (alias: -help).",
        usage="somnia help [topic] [--json]",
        detail=(
            "Without a topic, prints the somnia intro and the full command list "
            "(CLI commands, global options, and REPL slash commands). With a "
            "topic, prints the detailed spec for that command, including usage, "
            "options, and examples. Pass --json for machine-readable output that "
            "other agents can parse."
        ),
        section="cli",
        options=("-help", "--json"),
        examples=(
            "somnia help",
            "somnia help run",
            "somnia -help --json",
        ),
    ),
]

CLI_OPTIONS: list[CommandSpec] = [
    CommandSpec(
        name="-version",
        description="Show the installed somnia version and exit.",
        usage="somnia -version",
        detail="Prints the installed somnia version (also available as --version).",
        section="option",
        examples=("somnia -version",),
    ),
    CommandSpec(
        name="--workspace",
        description="Workspace root for the agent.",
        usage="somnia --workspace <path> <command>",
        detail="Sets the workspace root used for sessions, storage, and tools. Defaults to the current directory.",
        section="option",
        examples=("somnia --workspace ~/projects/repo run 'list open tasks'",),
    ),
    CommandSpec(
        name="-r",
        description="Resume a saved chat via the interactive session picker.",
        usage="somnia -r | somnia chat -r",
        detail="Open the interactive session picker and resume a saved chat (aliases: -resume, --resume).",
        section="option",
        examples=("somnia -r",),
    ),
    CommandSpec(
        name="-c",
        description="Continue the latest saved chat in this workspace.",
        usage="somnia -c | somnia chat -c",
        detail="Continue the latest saved chat in this workspace (alias: --continue).",
        section="option",
        examples=("somnia -c",),
    ),
    CommandSpec(
        name="--provider",
        description="Override the configured provider for this invocation.",
        usage="somnia --provider <name> <command>",
        detail="Overrides the configured provider for this invocation. Combine with --model to pin the model.",
        section="option",
        examples=("somnia --provider anthropic run 'hello'",),
    ),
    CommandSpec(
        name="--model",
        description="Override the configured model for this invocation.",
        usage="somnia --model <name> <command>",
        detail="Overrides the configured model for this invocation. Combine with --provider to pin both.",
        section="option",
        examples=("somnia --model gpt-5 run 'hello'",),
    ),
    CommandSpec(
        name="-help",
        description="Show the help system (alias for the help subcommand).",
        usage="somnia -help [topic] [--json]",
        detail=(
            "Without a topic, prints the somnia intro and the full command list. "
            "With a topic, prints the detailed spec for that command. Add --json "
            "for machine-readable output."
        ),
        section="option",
        examples=("somnia -help", "somnia -help run", "somnia -help --json"),
    ),
]

REPL_COMMANDS: list[CommandSpec] = [
    CommandSpec(
        name="/init",
        description="Generate AGENTS.md project instructions with an agent inspection loop.",
        usage="/init [--force] [extra instructions]",
        detail=(
            "Generates AGENTS.md project instructions for the workspace, including "
            "an agent inspection loop. Skipped if AGENTS.md already exists unless "
            "--force is given. Extra instructions are appended to the generated "
            "file. Requires accept-edits mode."
        ),
        section="repl",
        examples=("/init --force", "/init Focus on the data pipeline."),
    ),
    CommandSpec(
        name="/symbols",
        description="Find symbols and inspect matching source locations.",
        usage="/symbols <query>",
        detail=(
            "Searches the workspace for symbols (classes, functions, methods, "
            "types) whose names match the query. Use `|` to search several "
            "substrings in one pass (up to 10 terms). Matches are listed and can "
            "be inspected one at a time, including a source preview at the "
            "location."
        ),
        section="repl",
        examples=("/symbols parse", "/symbols AuthService|loginUser"),
    ),
    CommandSpec(
        name="/image",
        description="Send a local image to the active multimodal model.",
        usage='/image <path> [prompt]',
        detail=(
            "Sends a local image (png, jpg/jpeg, webp, gif) to the active "
            "multimodal model, optionally with a prompt. Quote the path with "
            "double quotes if it contains spaces."
        ),
        section="repl",
        examples=('/image screenshot.png "What error is shown?"',),
    ),
    CommandSpec(
        name="/paste-image",
        description="Read an image from the system clipboard.",
        usage="/paste-image [prompt]",
        detail=(
            "Reads an image from the system clipboard and sends it to the active "
            "multimodal model, optionally with a prompt. Windows and macOS are "
            "supported."
        ),
        section="repl",
    ),
    CommandSpec(
        name="/model",
        description="Choose the active provider and model.",
        usage="/model",
        detail=(
            "Opens an interactive picker to choose the provider and model used "
            "for subsequent turns."
        ),
        section="repl",
    ),
    CommandSpec(
        name="/vision",
        description="Choose the image understanding model.",
        usage="/vision [<provider> <model>]",
        detail=(
            "Without arguments, opens an interactive picker for the image "
            "understanding (vision) model. With arguments, sets it directly. "
            "`auto`, `none`, or `clear` clears the vision model so the main "
            "model is used for images."
        ),
        section="repl",
        examples=("/vision anthropic claude-3-5-sonnet-20241022", "/vision clear"),
    ),
    CommandSpec(
        name="/reasoning",
        description="Set the active provider reasoning level.",
        usage="/reasoning [auto|low|medium|high|deep]",
        detail=(
            "Sets the reasoning level for subsequent turns. Without an argument, "
            "opens an interactive picker. `auto` (or `none`) restores the "
            "provider default behavior."
        ),
        section="repl",
        examples=("/reasoning deep", "/reasoning auto"),
    ),
    CommandSpec(
        name="/providers",
        description="Add or edit shared provider profiles.",
        usage="/providers",
        detail="Interactively adds or edits shared provider profiles in the global config.",
        section="repl",
    ),
    CommandSpec(
        name="/hooks",
        description="Browse hooks by event and toggle them on or off.",
        usage="/hooks",
        detail=(
            "Interactively browses configured hooks grouped by event "
            "(SessionStart, PreToolUse, PostToolUse, AssistantResponse, "
            "UserChoiceRequested) and toggles individual hooks on or off."
        ),
        section="repl",
    ),
    CommandSpec(
        name="/undo",
        description="Undo the most recent file change set.",
        usage="/undo",
        detail="Reverts the most recent file change set made by the agent, after an interactive confirmation.",
        section="repl",
    ),
    CommandSpec(
        name="/checkpoint",
        description="Save a named checkpoint of the current session state.",
        usage="/checkpoint [tag]",
        detail=(
            "Saves a checkpoint of the current session state (messages and "
            "tracked files) under the given tag, or an auto-generated one. "
            "Requires accept-edits mode."
        ),
        section="repl",
        examples=("/checkpoint before-refactor",),
    ),
    CommandSpec(
        name="/rollback",
        description="Roll back to a previous checkpoint, reverting files and context.",
        usage="/rollback [tag]",
        detail=(
            "Reverts messages and file changes to a previous checkpoint. With a "
            "tag, rolls back to that checkpoint directly; without one, an "
            "interactive picker is shown. Externally modified files are detected "
            "and can be skipped. Requires accept-edits mode."
        ),
        section="repl",
        examples=("/rollback before-refactor",),
    ),
    CommandSpec(
        name="/compact",
        description="Compact the current session context.",
        usage="/compact",
        detail="Compacts the current session context to stay within the context window budget.",
        section="repl",
    ),
    CommandSpec(
        name="/new",
        description="Start a fresh session in place, optionally with handoff text.",
        usage="/new [handoff text]",
        detail=(
            "Discards the current conversation context and starts a brand-new "
            "session without leaving the REPL. With trailing text, that text is "
            "sent immediately as the first prompt of the new session — write a "
            "handoff summary there to carry intent across sessions. The previous "
            "session is preserved and stays resumable via `somnia -r`. The active "
            "provider/model selection carries over to the new session."
        ),
        section="repl",
        examples=("/new", "/new Continue task #3; the spec is in .scratch/spec.md"),
    ),
    CommandSpec(
        name="/fork",
        description="Branch the session at a chosen message and switch to the fork.",
        usage="/fork [message-count]",
        detail=(
            "Creates a new session that keeps the conversation up to a chosen "
            "message and switches to it, so you can explore a different direction "
            "without losing the original. Without an argument, a picker lists the "
            "visible messages to fork after; with a number, forks after exactly "
            "that many messages. The original session is untouched and stays "
            "resumable. The fork inherits the provider/model pin but gets its own "
            "task board."
        ),
        section="repl",
        examples=("/fork", "/fork 12"),
    ),
    CommandSpec(
        name="/cancel",
        description="Cancel a queued prompt by its queue id.",
        usage="/cancel <queue-id>",
        detail=(
            "Cancels a queued prompt. Queue ids are shown next to each queued "
            "prompt when the REPL is busy."
        ),
        section="repl",
        examples=("/cancel 2",),
    ),
    CommandSpec(
        name="/reloadplugin",
        description="Reload MCP tools, skills, and project instructions.",
        usage="/reloadplugin",
        detail=(
            "Reloads MCP server connections and tools, skills, and project "
            "instruction files, printing a summary of what changed."
        ),
        section="repl",
    ),
    CommandSpec(
        name="/skill",
        description="Apply a skill to the prompt.",
        usage="/skill <name> [task]",
        detail=(
            "Runs the prompt with the named skill applied: the skill body is loaded "
            "and the model is instructed to follow it for this task. With no name, "
            "opens the interactive skill picker (same as /skills)."
        ),
        section="repl",
    ),
    CommandSpec(
        name="/skills",
        description="Choose a skill to apply to the next prompt.",
        usage="/skills",
        detail=(
            "Opens an interactive picker of available skills; the chosen skill is "
            "applied to the next prompt you send."
        ),
        section="repl",
    ),
    CommandSpec(
        name="/tasks",
        description="Show persistent tasks.",
        usage="/tasks",
        detail="Prints all persistent tasks as JSON. Same data as `somnia tasks list`.",
        section="repl",
    ),
    CommandSpec(
        name="/team",
        description="Show teammate roster and states.",
        usage="/team",
        detail="Prints the teammate roster, their roles, states, and session scoping.",
        section="repl",
    ),
    CommandSpec(
        name="/mcp",
        description="Browse configured MCP servers and tools.",
        usage="/mcp",
        detail=(
            "Interactively browses configured MCP servers, their tools, and "
            "enables or disables individual tools. Changes are persisted back to "
            "the MCP configuration."
        ),
        section="repl",
    ),
    CommandSpec(
        name="/bg",
        description="Show background jobs.",
        usage="/bg",
        detail="Prints the status of running and finished background jobs.",
        section="repl",
    ),
    CommandSpec(
        name="/help",
        description="Show available REPL commands (or detailed help for one).",
        usage="/help [command]",
        detail=(
            "Without an argument, lists all REPL commands. With a command name "
            "(with or without the leading /), prints its detailed spec."
        ),
        section="repl",
        examples=("/help", "/help rollback"),
    ),
    CommandSpec(
        name="/exit",
        description="Exit chat mode.",
        usage="/exit",
        detail="Exits the interactive REPL. `q` and `exit` are aliases.",
        section="repl",
    ),
    CommandSpec(
        name="/teamlog",
        description="Show the full message and tool history for a teammate.",
        usage="/teamlog [name]",
        detail=(
            "With a teammate name, prints that teammate's full message and tool "
            "history. Without one, lists the active teammates."
        ),
        section="repl",
        hidden=True,
        examples=("/teamlog worker-1",),
    ),
    CommandSpec(
        name="/inbox",
        description="Read the lead inbox.",
        usage="/inbox",
        detail="Prints the lead agent's inbox messages as JSON.",
        section="repl",
        hidden=True,
    ),
    CommandSpec(
        name="/toollog",
        description="Show recent tool logs or expand one by id.",
        usage="/toollog [id]",
        detail=(
            "Without an id, prints the most recent tool logs. With a log id, "
            "prints the full record for that tool call."
        ),
        section="repl",
        hidden=True,
        examples=("/toollog 42",),
    ),
]

ALL_COMMANDS: tuple[CommandSpec, ...] = tuple(
    [*CLI_COMMANDS, *CLI_OPTIONS, *REPL_COMMANDS]
)

# Public alias used by the REPL command registry (open_somnia.cli.prompting).
REPL_COMMAND_SPECS: tuple[CommandSpec, ...] = tuple(REPL_COMMANDS)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def _normalize_topic(topic: str) -> str:
    value = topic.strip()
    if value.lower().startswith("somnia "):
        value = value[len("somnia ") :].strip()
    return value.lower()


def lookup(topic: str) -> CommandSpec | None:
    """Resolve a help topic to its command spec (CLI, option, or REPL)."""
    normalized = _normalize_topic(topic)
    for spec in ALL_COMMANDS:
        if spec.name.lower() == normalized:
            return spec
        if normalized.startswith("/") or spec.section != "repl":
            continue
        if spec.name.lstrip("/").lower() == normalized.lstrip("/"):
            return spec
    return None


def repl_lookup(topic: str) -> CommandSpec | None:
    """Resolve a REPL slash command topic (accepts with or without leading /)."""
    normalized = topic.strip().lower().lstrip("/")
    for spec in REPL_COMMANDS:
        if spec.name.lower().lstrip("/") == normalized:
            return spec
    return None


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def _aligned_rows(rows: list[tuple[str, str]]) -> list[str]:
    if not rows:
        return []
    width = max(len(name) for name, _ in rows)
    return [f"{name:<{width}}  {desc}" for name, desc in rows]


def render_overview() -> str:
    """Human-readable overview: intro + every command, grouped by section."""
    lines: list[str] = [
        f"Somnia v{__version__}",
        INTRO,
        "",
        f"Usage: {USAGE_LINE}",
        "",
        "CLI commands:",
    ]
    lines.extend(_aligned_rows([(spec.name, spec.description) for spec in CLI_COMMANDS]))
    lines.append("")
    lines.append("CLI options:")
    lines.extend(_aligned_rows([(spec.name, spec.description) for spec in CLI_OPTIONS]))
    lines.append("")
    lines.append("REPL commands (available inside `somnia chat`):")
    visible = [spec for spec in REPL_COMMANDS if not spec.hidden]
    hidden = [spec for spec in REPL_COMMANDS if spec.hidden]
    lines.extend(_aligned_rows([(spec.name, spec.description) for spec in visible]))
    if hidden:
        lines.append("")
        lines.append("REPL commands (hidden):")
        lines.extend(_aligned_rows([(spec.name, spec.description) for spec in hidden]))
    lines.append("")
    lines.append("Run `somnia -help <command>` for detailed help on any command.")
    lines.append("Add --json for machine-readable output: `somnia -help --json`.")
    return "\n".join(lines)


def _render_options(spec: CommandSpec) -> list[str]:
    lines: list[str] = []
    for option in spec.options:
        description = OPTION_DESCRIPTIONS.get(option, option)
        lines.append(f"  {option:<12} {description}")
    return lines


def render_detail(spec: CommandSpec) -> str:
    """Human-readable detailed spec for one command."""
    lines: list[str] = [f"{spec.name} - {spec.description}", ""]
    if spec.section != "repl":
        lines.append("Usage:")
        lines.append(f"  {spec.usage}")
        lines.append("")
    lines.append(spec.detail)
    if spec.options:
        lines.append("")
        lines.append("Options:")
        lines.extend(_render_options(spec))
    if spec.examples:
        lines.append("")
        lines.append("Examples:")
        for example in spec.examples:
            lines.append(f"  $ {example}")
    return "\n".join(lines)


def render_repl_help(topic: str | None = None) -> str:
    """Help rendered for the REPL /help command (REPL commands only)."""
    if topic:
        spec = repl_lookup(topic)
        if spec is None:
            return f"[unknown command] {topic}"
        return render_detail(spec)
    lines: list[str] = ["REPL commands:"]
    lines.extend(_aligned_rows([(spec.name, spec.description) for spec in REPL_COMMANDS]))
    lines.append("")
    lines.append("Run `/help <command>` for detailed help on any command.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON rendering
# ---------------------------------------------------------------------------

def _spec_json(spec: CommandSpec, *, full: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "section": spec.section,
        "name": spec.name,
        "description": spec.description,
        "usage": spec.usage,
        "hidden": spec.hidden,
    }
    if full:
        payload["detail"] = spec.detail
        payload["options"] = list(spec.options)
        payload["examples"] = list(spec.examples)
    return payload


def _meta_json() -> dict[str, Any]:
    return {
        "name": "somnia",
        "version": __version__,
        "intro": INTRO,
        "usage": USAGE_LINE,
    }


def overview_json() -> dict[str, Any]:
    return {
        "somnia": _meta_json(),
        "commands": [_spec_json(spec, full=False) for spec in ALL_COMMANDS],
    }


def detail_json(spec: CommandSpec) -> dict[str, Any]:
    return {
        "somnia": _meta_json(),
        "topic": _spec_json(spec, full=True),
    }


def unknown_json(topic: str) -> dict[str, Any]:
    return {
        "somnia": _meta_json(),
        "error": f"unknown command: {topic}",
        "commands": [_spec_json(spec, full=False) for spec in ALL_COMMANDS],
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def cli_help(topic: str | None = None, *, as_json: bool = False) -> int:
    """Entry point for `somnia help [topic]` / `somnia -help [topic]`."""
    if topic:
        spec = lookup(topic)
        if spec is None:
            if as_json:
                print(json.dumps(unknown_json(topic), ensure_ascii=False, indent=2))
            else:
                print(f"Unknown command: {topic}", file=sys.stderr)
                print("Run `somnia -help` to list all available commands.", file=sys.stderr)
            return 2
        if as_json:
            print(json.dumps(detail_json(spec), ensure_ascii=False, indent=2))
        else:
            print(render_detail(spec))
        return 0
    if as_json:
        print(json.dumps(overview_json(), ensure_ascii=False, indent=2))
    else:
        print(render_overview())
    return 0
