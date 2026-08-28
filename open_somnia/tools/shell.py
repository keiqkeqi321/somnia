from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from open_somnia.tools.process import run_command
from open_somnia.tools.registry import ToolDefinition

DANGEROUS_SNIPPETS = [
    "rm -rf /",
    "sudo ",
    " shutdown",
    " reboot",
    "mkfs",
]
DANGEROUS_COMMAND_PATTERNS = [
    re.compile(r"(?im)(?:^|[;&|]\s*)format(?:\.com|\.exe)?(?:\s|$)"),
]

# Unix-syntax detectors for `_windows_shell_guidance`, anchored to command
# position (start of string or after a `;`/`|`/`&`/`(`/newline separator).
# Bare word matches also hit arguments and file names: `alembic upgrade head`
# and `python head.py` are not the Unix `head` command.
_UNIX_COMMAND_START = r"(?:^|[;|&(\r\n]\s*)"

_UNIX_HEAD_RE = re.compile(_UNIX_COMMAND_START + r"head(?:\s|$)")
_UNIX_FIND_NAME_RE = re.compile(_UNIX_COMMAND_START + r"find\s+\.\s+-name\b")
_UNIX_LS_RE = re.compile(_UNIX_COMMAND_START + r"ls(?:\s|$)")
_UNIX_GREP_RE = re.compile(_UNIX_COMMAND_START + r"grep(?:\s|$)")

# Read-only shell command prefixes. An Explore subagent runs in parallel with
# its siblings, and its only write vector is `bash` (it has no write_file /
# edit_file). To keep parallel Explore subagents free of write races, the
# subagent's `bash` is gated to these read-only prefixes, mirroring the lead
# loop's exploration-budget classifier (`EXPLORATION_SHELL_PREFIXES` in
# runtime/agent.py). Cross-platform: Unix read commands, Windows PowerShell
# read cmdlets, and the read-only `git` subcommands. Kept as a module-level
# constant so tests and the agent-loop classifier share one source of truth.
READONLY_SHELL_PREFIXES = (
    # Unix read commands
    "cat ",
    "find ",
    "grep ",
    "head ",
    "ls",
    "pwd",
    "rg ",
    "tail ",
    "tree",
    "type ",
    "wc ",
    # Windows PowerShell read cmdlets
    "dir",
    "get-childitem",
    "get-content",
    "select-string",
    # Read-only git subcommands
    "git diff",
    "git log",
    "git show",
    "git status",
)

# Substrings that indicate a write side effect anywhere in the command. Even
# when the leading word is a read-only prefix, these fragments turn the whole
# command into a write (e.g. `grep x > out.txt`, `cat a && rm b`, `git status |
# tee log`). Matched case-insensitively against the normalized command.
WRITE_SYNTAX_SNIPPETS = (
    ">",
    ">>",
    "| tee",
    "| out-file",
    "| set-content",
    "| add-content",
    " rm ",
    "rm ",
    " del ",
    "del ",
    "remove-item",
    " mv ",
    " move ",
    "move-item",
    " cp ",
    " copy ",
    "copy-item",
    " mkdir ",
    "md ",
    " touch ",
    "new-item",
    "set-content",
    "add-content",
    "chmod",
    "chown",
    "tar ",
    "zip ",
    "unzip",
    "curl ",
    "wget ",
    "git checkout",
    "git reset",
    "git clean",
    "git pull",
    "git push",
    "git commit",
    "git add",
    "git stash",
    "git merge",
    "git rebase",
    "git cherry-pick",
    "git rm",
    "git mv",
    "pip install",
    "npm install",
    "yarn add",
)

# Write-syntax detector: matches any WRITE_SYNTAX_SNIPPETS as a token-boundary
# substring of the normalized command. Word-boundary matching prevents false
# positives like "directory" matching "dir".
_WRITE_SYNTAX_PATTERN = re.compile(
    r"(?:^|[^\w-])(?:" + "|".join(re.escape(s.strip()) for s in WRITE_SYNTAX_SNIPPETS) + r")",
    re.IGNORECASE,
)


def _normalize_command(command: str) -> str:
    """Collapse whitespace and trim so prefix/snippet matching is stable."""
    return " ".join(str(command or "").split())


def is_readonly_shell_command(command: str) -> tuple[bool, str]:
    """Classify a shell command as read-only for Explore-subagent gating.

    Returns ``(is_readonly, reason)``. ``is_readonly`` is True only when the
    command's leading word matches a :data:`READONLY_SHELL_PREFIXES` entry AND
    no write-syntax fragment appears anywhere in the command. ``reason`` is a
    short human-readable explanation used in the rejection message when the
    command is not read-only.

    This is a conservative static classifier, not a sandbox: its job is to keep
    parallel Explore subagents from racing on workspace writes by refusing
    anything that looks like a mutation. It deliberately errs on the side of
    rejecting ambiguous commands (the model gets a clear error and can switch
    to a read-only command, or the work can move to a general-purpose subagent
    / the lead loop where `bash` is unrestricted).
    """
    normalized = _normalize_command(command)
    if not normalized:
        return False, "empty command"
    lowered = normalized.lower()
    # Reject first if any write-syntax fragment is present, regardless of the
    # leading word -- a command like `cat a && rm b` must be refused even though
    # it starts with the read-only `cat`.
    match = _WRITE_SYNTAX_PATTERN.search(normalized)
    if match is not None:
        return False, f"write operation detected near '{normalized[match.start():match.end()].strip()}'"
    # Then require the leading word to be a recognized read-only prefix.
    if not any(lowered == prefix.rstrip() or lowered.startswith(prefix) for prefix in READONLY_SHELL_PREFIXES):
        first_word = lowered.split(None, 1)[0] if lowered.split() else lowered
        return False, f"'{first_word}' is not in the read-only allowlist"
    return True, ""


def _is_windows() -> bool:
    return os.name == "nt"


def is_dangerous_command(command: str) -> bool:
    lowered = f" {command.lower()} "
    if any(snippet in lowered for snippet in DANGEROUS_SNIPPETS):
        return True
    return any(pattern.search(command) for pattern in DANGEROUS_COMMAND_PATTERNS)


def _translate_windows_command(command: str) -> str | None:
    stripped = command.strip()

    if re.fullmatch(r"ls(?:\s+-[a-zA-Z]+)?", stripped):
        return "Get-ChildItem -Force"
    if stripped == "pwd":
        return "Get-Location"

    cat_match = re.fullmatch(r"cat\s+(.+)", stripped)
    if cat_match:
        return f"Get-Content {cat_match.group(1)}"

    find_match = re.fullmatch(
        r'find\s+\.\s+-name\s+["\']([^"\']+)["\']\s+-type\s+f(?:\s+2>/dev/null)?(?:\s+\|\s+head\s+-?(\d+))?',
        stripped,
    )
    if find_match:
        pattern, limit = find_match.groups()
        translated = f"Get-ChildItem -Recurse -Filter {pattern} -File"
        if limit:
            translated += f" | Select-Object -First {limit}"
        return translated

    return None


def _windows_shell_guidance(command: str) -> str | None:
    stripped = command.strip()
    if "/dev/null" in stripped or _UNIX_HEAD_RE.search(stripped):
        return (
            "Error: Unix shell syntax detected on Windows. The `bash` tool runs PowerShell-compatible commands here. "
            "Try `Get-ChildItem -Recurse -Filter *.py -File | Select-Object -First 20`."
        )
    if _UNIX_FIND_NAME_RE.search(stripped):
        return (
            "Error: `find -name` is a Unix command pattern. On Windows, use "
            "`Get-ChildItem -Recurse -Filter <pattern> -File`."
        )
    if _UNIX_LS_RE.search(stripped):
        return "Error: `ls` is not guaranteed on Windows. Use `Get-ChildItem -Force`."
    if _UNIX_GREP_RE.search(stripped):
        return "Error: `grep` is a Unix command. On Windows, use `Select-String`."
    return None


def prepare_shell_command(command: str) -> tuple[str | list[str], bool, str | None]:
    """Adapt a raw command string for the platform's shell.

    Returns ``(run_args, use_shell, guidance)`` for ``run_command``. On
    Windows, known Unix commands are translated to PowerShell and executed
    with ``use_shell=False``; untranslatable Unix syntax yields guidance text
    instead (mirroring the ``bash`` tool's error response), in which case the
    caller must surface the guidance and not execute the command.
    """
    if not _is_windows():
        return command, True, None
    translated = _translate_windows_command(command)
    if translated is None:
        guidance = _windows_shell_guidance(command)
        if guidance is not None:
            return command, True, guidance
        translated = command
    return ["powershell", "-NoLogo", "-NoProfile", "-Command", translated], False, None


def run_shell(ctx: Any, payload: dict[str, Any]) -> str:
    command = str(payload["command"])
    if is_dangerous_command(command):
        return "Error: Dangerous command blocked"

    run_args, use_shell, guidance = prepare_shell_command(command)
    if guidance is not None:
        return guidance

    try:
        completed = run_command(
            run_args,
            shell=use_shell,
            cwd=ctx.runtime.settings.workspace_root,
            timeout=int(payload.get("timeout", ctx.runtime.settings.runtime.command_timeout_seconds)),
            stop_checker=getattr(ctx, "should_interrupt", None),
        )
    except subprocess.TimeoutExpired:
        return f"Error: Timeout ({ctx.runtime.settings.runtime.command_timeout_seconds}s)"
    output = completed.combined_output().strip() or "(no output)"
    return output[: ctx.runtime.settings.runtime.max_tool_output_chars]


def register_shell_tool(registry) -> None:
    registry.register(
        ToolDefinition(
            name="bash",
            description="Run a shell command inside the workspace. On Unix this uses the system shell; on Windows commands should be PowerShell-compatible.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
            handler=run_shell,
        )
    )


_READONLY_REJECTION_HEADER = (
    "Error: Explore 模式仅允许只读命令（cat/grep/find/rg/ls/head/tail/wc/tree/type/"
    "git status/git log/git diff/git show，Windows 下 Get-Content/Get-ChildItem/Select-String）。"
)
_READONLY_REJECTION_GUIDE = (
    "若需修改文件，请改用 general-purpose subagent，或在 lead 主循环直接执行 bash。"
)


def run_readonly_shell(ctx: Any, payload: dict[str, Any]) -> str:
    """Explore-subagent bash: execute read-only commands, refuse writes.

    Mirrors :func:`run_shell` for read-only commands but rejects anything
    :func:`is_readonly_shell_command` classifies as a write, returning a clear
    error with the specific reason and read-only alternatives. The rejection is
    surfaced as a tool error (not raised) so the model can react and switch
    commands within the same subagent run.
    """
    command = str(payload.get("command", ""))
    is_readonly, reason = is_readonly_shell_command(command)
    if not is_readonly:
        return f"{_READONLY_REJECTION_HEADER}\n拒绝原因：{reason}。\n{_READONLY_REJECTION_GUIDE}"
    return run_shell(ctx, payload)


def register_readonly_shell_tool(registry) -> None:
    """Register the Explore-subagent `bash` tool, gated to read-only commands.

    Registered under the same name/schema/description as the unrestricted
    :func:`register_shell_tool` so the model sees an identical interface; only
    the handler (:func:`run_readonly_shell`) differs. Used by Explore
    subagents whose only write vector is `bash`; the lead loop and
    general-purpose subagents keep the unrestricted tool.
    """
    registry.register(
        ToolDefinition(
            name="bash",
            description="Run a shell command inside the workspace. On Unix this uses the system shell; on Windows commands should be PowerShell-compatible.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
            handler=run_readonly_shell,
        )
    )
