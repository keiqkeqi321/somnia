from __future__ import annotations

import platform
import sys
from typing import Any

from open_somnia.integrations.gitnexus import prompt_guidance as gitnexus_prompt_guidance
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
            "- Follow project instructions first. If they specify a tool or workflow for a task, use that before overlapping generic tools.\n"
            "- Prefer MCP and project-specific tools over generic filesystem/search tools when their capabilities overlap.\n"
            "- Treat generic workspace tools as fallbacks for overlapping work; use them when no more specific tool applies, the specific tool is unavailable, or its result needs focused confirmation.\n"
            "- Prefer dedicated tools over `bash` whenever a relevant tool exists; reserve `bash` for system commands and terminal operations that truly require shell execution.\n"
            "- Use `edit_file` instead of shell text replacement via `sed` or `awk`.\n"
            "- Use `write_file` instead of shell redirection or heredocs for file creation.\n"
            "- Avoid broad repository sweeps unless the user explicitly asks for them; narrow discovery by task, directory, symbol, or file type.\n"
            "- Before reading or editing a file, establish the exact path through the most specific available evidence; do not guess paths from broad listings.\n"
            "- For `edit_file`, always wrap replacements as `edits=[{old_text,new_text}, ...]`; do not send top-level `old_text` or `new_text`.\n"
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
        skill_prompt_getter = getattr(self.runtime.skill_loader, "prompt_index", None)
        skill_descriptions = skill_prompt_getter() if callable(skill_prompt_getter) else self.runtime.skill_loader.descriptions()
        gitnexus_guidance = gitnexus_prompt_guidance(self.runtime)
        mcp_guidance = (
            "MCP tools are provided through the tool schema and are not repeated here.\n"
            "Use MCP tools when they are more specific to the task than generic filesystem or shell tools.\n"
            "If repository instructions require an MCP-backed workflow, follow that workflow before using overlapping generic tools.\n"
            "If a relevant MCP tool is unavailable, fall back to the closest safe generic tool and mention the limitation when it matters."
        )
        if gitnexus_guidance:
            mcp_guidance = f"{mcp_guidance}\n{gitnexus_guidance}"
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
                mcp_guidance,
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
