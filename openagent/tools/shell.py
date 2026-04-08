from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from openagent.tools.process import run_command
from openagent.tools.registry import ToolDefinition

DANGEROUS_SNIPPETS = [
    "rm -rf /",
    "sudo ",
    " shutdown",
    " reboot",
    "mkfs",
    "format ",
]


def _is_windows() -> bool:
    return os.name == "nt"


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
    if "/dev/null" in stripped or re.search(r"\bhead\b", stripped):
        return (
            "Error: Unix shell syntax detected on Windows. The `bash` tool runs PowerShell-compatible commands here. "
            "Try `Get-ChildItem -Recurse -Filter *.py -File | Select-Object -First 20`."
        )
    if re.search(r"\bfind\s+\.\s+-name\b", stripped):
        return (
            "Error: `find -name` is a Unix command pattern. On Windows, use "
            "`Get-ChildItem -Recurse -Filter <pattern> -File`."
        )
    if re.search(r"(^|\s)ls(\s|$)", stripped):
        return "Error: `ls` is not guaranteed on Windows. Use `Get-ChildItem -Force`."
    if re.search(r"\bgrep\b", stripped):
        return "Error: `grep` is a Unix command. On Windows, use `Select-String`."
    return None


def run_shell(ctx: Any, payload: dict[str, Any]) -> str:
    command = str(payload["command"])
    lowered = f" {command.lower()} "
    if any(snippet in lowered for snippet in DANGEROUS_SNIPPETS):
        return "Error: Dangerous command blocked"

    run_args: str | list[str]
    use_shell = True
    if _is_windows():
        translated = _translate_windows_command(command)
        if translated is None:
            guidance = _windows_shell_guidance(command)
            if guidance is not None:
                return guidance
            translated = command
        run_args = ["powershell", "-NoLogo", "-NoProfile", "-Command", translated]
        use_shell = False
    else:
        run_args = command

    try:
        completed = run_command(
            run_args,
            shell=use_shell,
            cwd=ctx.runtime.settings.workspace_root,
            timeout=int(payload.get("timeout", ctx.runtime.settings.runtime.command_timeout_seconds)),
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
