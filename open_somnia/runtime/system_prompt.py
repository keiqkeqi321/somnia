from __future__ import annotations

import platform
import sys
from typing import Any

from open_somnia.runtime.execution_mode import DEFAULT_EXECUTION_MODE, execution_mode_spec


class SystemPromptBuilder:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def environment_guidance(self) -> str:
        os_name = platform.system() or sys.platform
        shell_line = "PowerShell-compatible command runner" if sys.platform == "win32" else "system shell command runner"
        bash_hint = (
            "When using the `bash` tool on Windows, prefer PowerShell commands such as "
            "`Get-ChildItem`, `Get-Content`, `Select-String`, and `Select-Object`. "
            "Do not assume Unix commands like `ls`, `find -name`, `head`, `grep`, or `/dev/null` are available."
            if sys.platform == "win32"
            else "When using the `bash` tool on Unix-like systems, standard shell commands are available."
        )
        return (
            "Execution environment:\n"
            f"- OS: {os_name}\n"
            f"- Shell: {shell_line}\n"
            f"- Workspace: {self.runtime.settings.workspace_root}\n"
            f"- Active provider: {self.runtime.settings.provider.name}\n"
            f"- Active model: {self.runtime.settings.provider.model}\n"
            "Tool behavior:\n"
            f"- {bash_hint}"
        )

    def build_system_prompt(self, actor: str = "lead", role: str = "lead coding agent", session=None) -> str:
        base_prompt = self.base_system_prompt()
        environment_guidance = self.environment_guidance()
        mode_guidance = execution_mode_spec(getattr(self.runtime, "execution_mode", DEFAULT_EXECUTION_MODE)).guidance
        working_file_context_getter = getattr(self.runtime, "current_working_file_context", None)
        working_file_context = working_file_context_getter() if callable(working_file_context_getter) else ""
        identity_guidance = (
            "Identity rules:\n"
            f"- Your configured runtime provider is '{self.runtime.settings.provider.name}'.\n"
            f"- Your configured runtime model is '{self.runtime.settings.provider.model}'.\n"
            "- If the user asks which model or provider you are using, answer with these configured values.\n"
            "- Do not claim to be Claude, ChatGPT, GPT, Gemini, or any other model/vendor unless that exactly matches the configured runtime values above."
        )
        tool_selection_guidance = (
            "Tool selection rules:\n"
            "- Prefer dedicated tools over `bash` whenever a relevant tool exists.\n"
            "- At the start of repository exploration, prefer `project_scan` or a focused `tree` to build a project map.\n"
            "- Use `find_symbol` to locate classes, functions, methods, or interfaces before guessing code paths from docs or memory.\n"
            "- Use `read_file` instead of shell commands such as `cat`, `head`, `tail`, or `sed` for reading files.\n"
            "- Use `edit_file` instead of shell text replacement via `sed` or `awk`.\n"
            "- Use `write_file` instead of shell redirection or heredocs for file creation.\n"
            "- Use `tree` for shallow structure inspection instead of broad file enumeration.\n"
            "- Use `glob` instead of shell file discovery commands such as `find`, `ls`, or recursive directory listings.\n"
            "- Use `grep` instead of shell content search commands such as `grep` or `rg`.\n"
            "- Do not start with broad `glob` patterns such as `**/*` unless the user explicitly wants a full tree dump.\n"
            "- After reading project guidance files such as AGENTS.md or CLAUDE.md, use `project_scan`, `tree`, or `find_symbol` to validate the documented structure against the actual repository.\n"
            "- Prefer precise `glob` patterns such as an exact filename, a suffix filter like `**/*.cs`, or a narrowed directory such as `Runtime/UI/**/*.cs`.\n"
            "- Before `read_file` or `edit_file`, confirm the exact path with a focused `glob`; do not guess file paths from broad directory listings.\n"
            "- For `edit_file`, always wrap replacements as `edits=[{old_text,new_text}, ...]`; do not send top-level `old_text` or `new_text`.\n"
            "- Reserve `bash` for system commands and terminal operations that truly require shell execution.\n"
            "- If you are unsure and a dedicated tool exists, use the dedicated tool first."
        )
        workflow_guidance = (
            "Workflow rules:\n"
            "- Use `TodoWrite` to break down meaningful work and keep progress visible to the user.\n"
            "- Mark each todo item complete as soon as it is done; do not batch completions.\n"
            "- When multiple tool calls are independent, prefer emitting them in the same turn.\n"
            "- Do not batch dependent tool calls; sequence them when later inputs depend on earlier results.\n"
            "- Use `edit_file` with `edits=[...]` for every text replacement, including a single replacement.\n"
            "- When editing one file in several nearby places, prefer a single `edit_file` call with multiple `edits` items over many tiny follow-up patches.\n"
            "- After `write_file` or `edit_file`, use the returned updated snippet or active working file cache before rereading the same file.\n"
            "- Do not claim a root cause until your evidence materially narrows the main alternatives.\n"
            "- If you keep rereading the same file or area, stop and summarize facts, open hypotheses, and the next verification step before another read.\n"
            "- Treat repository exploration as an investigation: gather evidence, update hypotheses, then conclude."
        )
        working_file_guidance = f"\n{working_file_context}" if working_file_context else ""
        if actor == "lead":
            return (
                f"{base_prompt}\n\n"
                f"You are '{actor}', role: {role}, operating inside workspace {self.runtime.settings.workspace_root}.\n"
                "Use tools to solve coding tasks. Prefer task_create/task_update/task_list for longer work.\n"
                "Use TodoWrite for short checklists. Use subagent for isolated subagent work. Use load_skill only when needed.\n"
                "When collaborating, keep teammates informed through inbox messages and respect shutdown and plan protocols.\n"
                f"{identity_guidance}\n"
                f"{mode_guidance}\n"
                f"{tool_selection_guidance}\n"
                f"{workflow_guidance}\n"
                f"{environment_guidance}\n"
                f"{working_file_guidance}\n"
                f"Available skills:\n{self.runtime.skill_loader.descriptions()}"
            )
        return (
            f"{base_prompt}\n\n"
            f"You are '{actor}', role: {role}, operating inside workspace {self.runtime.settings.workspace_root}.\n"
            "You are a persistent teammate following the s11 work/idle loop.\n"
            "Use tools to complete current work, send messages when needed, and call idle when you have finished the current unit of work.\n"
            "While idle you may be resumed by inbox messages or unclaimed tasks.\n"
            f"{identity_guidance}\n"
            f"{mode_guidance}\n"
            f"{tool_selection_guidance}\n"
            f"{workflow_guidance}\n"
            f"{environment_guidance}\n"
            f"{working_file_guidance}\n"
            f"Available skills:\n{self.runtime.skill_loader.descriptions()}"
        )

    def base_system_prompt(self) -> str:
        configured_prompt = self.runtime.settings.agent.system_prompt
        if configured_prompt:
            return configured_prompt
        return self.runtime.DEFAULT_SYSTEM_PROMPT_TEMPLATE.format(name=self.runtime.settings.agent.name)
