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
    "--json": "Emit help output as JSON (machine readable).",
    "--session": "Only include provider payloads for this session ID.",
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
        usage="somnia chat [-r | -c] [--provider <name>] [--model <name>]",
        detail=(
            "Launches the interactive REPL. With -r (or -resume) an interactive "
            "session picker lets you resume a saved chat; with -c the latest saved "
            "chat in this workspace is continued automatically. Inside the REPL, "
            "slash commands (see /help) expose the full runtime: tools, tasks, "
            "teammates, MCP servers, hooks, checkpoints, and background jobs."
        ),
        section="cli",
        options=("-r", "-c", "--provider", "--model"),
        examples=(
            "somnia chat",
            "somnia chat -r",
            "somnia chat -c --provider anthropic",
        ),
    ),
    CommandSpec(
        name="run",
        description="Run a single prompt.",
        usage="somnia run <prompt> [--provider <name>] [--model <name>]",
        detail=(
            "Executes one prompt non-interactively and streams the assistant reply "
            "to stdout. Exit code 0 means the turn completed; a non-zero exit code "
            "signals a setup or runtime failure. This is the primary entry point "
            "for scripting and for other agents that want to delegate a single "
            "question to Somnia."
        ),
        section="cli",
        options=("--provider", "--model"),
        examples=(
            'somnia run "Summarize the open tasks"',
            'somnia run --model gpt-5 "Refactor the parser module"',
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
        usage="somnia doctor [--provider <name>] [--model <name>]",
        detail=(
            "Runs configuration diagnostics (providers, storage, hooks, MCP, and "
            "runtime wiring) and prints a human-readable validation report."
        ),
        section="cli",
        options=("--provider", "--model"),
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
        usage="somnia providers",
        detail=(
            "Interactively adds or edits shared provider profiles in the global "
            "config. Requires a TTY; in non-interactive contexts edit the global "
            "config file directly (see `somnia doctor`) instead."
        ),
        section="cli",
        examples=("somnia providers",),
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
        name="/janitor",
        description="Run semantic janitor on the current payload.",
        usage="/janitor",
        detail="Runs the semantic janitor over the current payload to trim or clean context.",
        section="repl",
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
