from __future__ import annotations

import platform
import sys
from typing import Any

from open_somnia.runtime.execution_mode import DEFAULT_EXECUTION_MODE, execution_mode_spec
from open_somnia.runtime.project_instructions import ProjectInstructionsLoader
from open_somnia.runtime.prompt_sections import PromptBundle, PromptSection


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

    def build_prompt_bundle(self, actor: str = "lead", role: str = "lead coding agent", session=None) -> PromptBundle:
        del session
        mode_guidance = execution_mode_spec(getattr(self.runtime, "execution_mode", DEFAULT_EXECUTION_MODE)).guidance
        working_file_context_getter = getattr(self.runtime, "current_working_file_context", None)
        working_file_context = working_file_context_getter() if callable(working_file_context_getter) else ""
        working_file_path_getter = getattr(self.runtime, "current_working_file_path", None)
        working_file_path = working_file_path_getter() if callable(working_file_path_getter) else ""
        project_instruction_paths = [working_file_path] if working_file_path else None
        project_instructions = ProjectInstructionsLoader(self.runtime.settings.workspace_root).render(paths=project_instruction_paths)
        identity_guidance = (
            "Identity rules:\n"
            f"- Your configured runtime provider is '{self.runtime.settings.provider.name}'.\n"
            f"- Your configured runtime model is '{self.runtime.settings.provider.model}'.\n"
            "- If the user asks which model or provider you are using, answer with these configured values.\n"
            "- Do not claim to be Claude, ChatGPT, GPT, Gemini, or any other model/vendor unless that exactly matches the configured runtime values above."
        )
        tool_selection_guidance = (
            "Tool selection rules:\n"
            "- If project instructions explicitly require specific tools for tasks that overlap with general tools, "
            "use the project-specified tools first; general tools such as `tree`, `grep`, and `read_file` are fallback options for that overlapping work.\n"
            "- Prefer dedicated tools over `bash` whenever a relevant tool exists.\n"
            "- When project instructions do not specify an overlapping code-intelligence tool, use `project_scan` or a focused `tree` to build a project map.\n"
            "- When project instructions do not specify an overlapping symbol tool, use `find_symbol` to locate classes, functions, methods, or interfaces before guessing code paths from docs or memory.\n"
            "- When project instructions do not specify an overlapping file-reading tool, use `read_file` instead of shell commands such as `cat`, `head`, `tail`, or `sed`.\n"
            "- Use `edit_file` instead of shell text replacement via `sed` or `awk`.\n"
            "- Use `write_file` instead of shell redirection or heredocs for file creation.\n"
            "- Use `tree` for shallow structure inspection instead of broad file enumeration only when no project-specific tool is required for that inspection.\n"
            "- Use `glob` instead of shell file discovery commands such as `find`, `ls`, or recursive directory listings.\n"
            "- Use `grep` instead of shell content search commands such as `grep` or `rg` only when no project-specific search or code-intelligence tool applies.\n"
            "- Do not start with broad `glob` patterns such as `**/*` unless the user explicitly wants a full tree dump.\n"
            "- After reading project guidance files such as AGENTS.md or CLAUDE.md, use their specified tools first; otherwise use `project_scan`, `tree`, or `find_symbol` to validate the documented structure against the actual repository.\n"
            "- Prefer precise `glob` patterns such as an exact filename, a suffix filter like `**/*.cs`, or a narrowed directory such as `Runtime/UI/**/*.cs`.\n"
            "- Before `read_file` or `edit_file`, confirm the exact path with a focused `glob`; do not guess file paths from broad directory listings.\n"
            "- For `edit_file`, always wrap replacements as `edits=[{old_text,new_text}, ...]`; do not send top-level `old_text` or `new_text`.\n"
            "- Reserve `bash` for system commands and terminal operations that truly require shell execution.\n"
            "- If you are unsure and a dedicated tool exists, use the dedicated tool first."
        )
        workflow_guidance = (
            "Problem solving workflow:\n"
            "- For non-trivial coding tasks, follow a compact loop: understand local evidence, plan the smallest coherent change, implement focused edits, verify with checks matched to risk, then close the loop.\n"
            "- Do not treat edits as complete until the user-visible goal is verified. If checks cannot run, say what was reviewed and what remains unverified.\n"
            "Workflow rules:\n"
            "- Use `TodoWrite` to break down meaningful work and keep progress visible to the user.\n"
            "- Mark each todo item complete as soon as it is done; do not batch completions.\n"
            "- When multiple tool calls are independent, prefer emitting them in the same turn.\n"
            "- Do not batch dependent tool calls; sequence them when later inputs depend on earlier results.\n"
            "- When a tool result matters for later context governance, you may set `importance` on the tool input: "
            "`glance`, `investigate`, or `foundation`.\n"
            "- Use `edit_file` with `edits=[...]` for every text replacement, including a single replacement.\n"
            "- When editing one file in several nearby places, prefer a single `edit_file` call with multiple `edits` items over many tiny follow-up patches.\n"
            "- After `write_file` or `edit_file`, use the returned updated snippet or active working file cache before rereading the same file.\n"
            "- Do not claim a root cause until your evidence materially narrows the main alternatives.\n"
            "- If you keep rereading the same file or area, stop and summarize facts, open hypotheses, and the next verification step before another read.\n"
            "- Treat repository exploration as an investigation: gather evidence, update hypotheses, then conclude."
        )
        runtime_identity = (
            f"You are '{actor}', role: {role}, operating inside workspace {self.runtime.settings.workspace_root}.\n"
            f"{identity_guidance}\n"
            f"{mode_guidance}\n"
            f"{self.environment_guidance()}"
        )
        lead_guidance = (
            "Use tools to solve coding tasks. Prefer task_create/task_update/task_list for longer work.\n"
            "Use TodoWrite for short checklists. Use subagent for isolated subagent work. Use load_skill only when needed.\n"
            "When collaborating, keep teammates informed through inbox messages and respect shutdown and plan protocols.\n"
            "After sending work to a teammate, use wait_for_inbox when their reply is needed before continuing."
        )
        teammate_guidance = (
            "You are a persistent teammate following the s11 work/idle loop.\n"
            "Use tools to complete current work, send messages when needed, and call idle when you have finished the current unit of work.\n"
            "While idle you may be resumed by inbox messages or unclaimed tasks."
        )
        runtime_guidance = lead_guidance if actor == "lead" else teammate_guidance
        skill_descriptions = self.runtime.skill_loader.descriptions()
        sections = (
            PromptSection("core", "A. Core System Prompt", self.base_system_prompt(), dynamic=False),
            PromptSection(
                "runtime",
                "B. Runtime Injection",
                f"{runtime_identity}\n{runtime_guidance}\n{tool_selection_guidance}\n{workflow_guidance}\n{working_file_context}",
                dynamic=True,
            ),
            PromptSection("skills", "C. Skill Prompt", f"Available skills:\n{skill_descriptions}", dynamic=True),
            PromptSection(
                "mcp",
                "D. MCP Prompt",
                "MCP tools are supplied through the provider tool schema. Use project- or task-specific MCP tools before overlapping general tools.",
                dynamic=True,
            ),
            PromptSection("repo", "E. Repo Prompt", project_instructions, dynamic=True),
        )
        return PromptBundle(sections)

    def build_system_prompt(self, actor: str = "lead", role: str = "lead coding agent", session=None) -> str:
        return self.build_prompt_bundle(actor=actor, role=role, session=session).render()

    def build_system_prompt_sections(self, actor: str = "lead", role: str = "lead coding agent", session=None) -> list[dict[str, object]]:
        return self.build_prompt_bundle(actor=actor, role=role, session=session).to_payload()

    def base_system_prompt(self) -> str:
        configured_prompt = self.runtime.settings.agent.system_prompt
        if configured_prompt:
            return configured_prompt
        return self.runtime.DEFAULT_SYSTEM_PROMPT_TEMPLATE.format(name=self.runtime.settings.agent.name)
