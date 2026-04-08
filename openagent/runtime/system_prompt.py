from __future__ import annotations

import platform
import sys
from typing import Any

from openagent.runtime.execution_mode import DEFAULT_EXECUTION_MODE, execution_mode_spec


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

    def build_system_prompt(self, actor: str = "lead", role: str = "lead coding agent") -> str:
        base_prompt = self.base_system_prompt()
        environment_guidance = self.environment_guidance()
        mode_guidance = execution_mode_spec(getattr(self.runtime, "execution_mode", DEFAULT_EXECUTION_MODE)).guidance
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
            "- Use `read_file` instead of shell commands such as `cat`, `head`, `tail`, or `sed` for reading files.\n"
            "- Use `edit_file` instead of shell text replacement via `sed` or `awk`.\n"
            "- Use `write_file` instead of shell redirection or heredocs for file creation.\n"
            "- Use `glob` instead of shell file discovery commands such as `find`, `ls`, or recursive directory listings.\n"
            "- Use `grep` instead of shell content search commands such as `grep` or `rg`.\n"
            "- Do not start with broad `glob` patterns such as `**/*` unless the user explicitly wants a full tree dump.\n"
            "- Prefer precise `glob` patterns such as an exact filename, a suffix filter like `**/*.cs`, or a narrowed directory such as `Runtime/UI/**/*.cs`.\n"
            "- Before `read_file` or `edit_file`, confirm the exact path with a focused `glob`; do not guess file paths from broad directory listings.\n"
            "- Reserve `bash` for system commands and terminal operations that truly require shell execution.\n"
            "- If you are unsure and a dedicated tool exists, use the dedicated tool first."
        )
        workflow_guidance = (
            "Workflow rules:\n"
            "- Use `TodoWrite` to break down meaningful work and keep progress visible to the user.\n"
            "- Mark each todo item complete as soon as it is done; do not batch completions.\n"
            "- When multiple tool calls are independent, prefer emitting them in the same turn.\n"
            "- Do not batch dependent tool calls; sequence them when later inputs depend on earlier results."
        )
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
            f"Available skills:\n{self.runtime.skill_loader.descriptions()}"
        )

    def base_system_prompt(self) -> str:
        configured_prompt = self.runtime.settings.agent.system_prompt
        if configured_prompt:
            return configured_prompt
        return self.runtime.DEFAULT_SYSTEM_PROMPT_TEMPLATE.format(name=self.runtime.settings.agent.name)
