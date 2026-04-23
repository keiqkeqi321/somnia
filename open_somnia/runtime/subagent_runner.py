from __future__ import annotations

import uuid
from typing import Any

from open_somnia.runtime.events import ToolExecutionContext
from open_somnia.runtime.messages import make_tool_result_item, make_tool_result_message, make_user_text_message
from open_somnia.tools.filesystem import (
    GREP_TOOL_DESCRIPTION,
    edit_file,
    find_symbol,
    glob_search,
    grep_search,
    project_scan,
    read_image,
    read_file,
    tree_view,
    write_file,
)
from open_somnia.tools.registry import ToolDefinition, ToolRegistry
from open_somnia.tools.shell import register_shell_tool
from open_somnia.tools.tool_errors import (
    extract_transient_repair_hint,
    render_transient_repair_hint_message,
    sanitize_tool_output_for_persistence,
    serialize_tool_output,
    tool_error_from_exception,
)


class SubagentRunner:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def run_subagent(self, prompt: str, agent_type: str = "Explore") -> str:
        registry = self._build_registry(agent_type)
        capability_guidance = (
            "You are in Explore mode. Use read-only tools only: `bash`, `project_scan`, `tree`, `find_symbol`, `glob`, `grep`, `read_file`, `read_image`, and `load_skill`. "
            "Do not attempt workspace edits."
            if agent_type == "Explore"
            else "You are in general-purpose mode. In addition to read-only tools, you may use `write_file` and `edit_file` when needed."
        )
        messages = [make_user_text_message(prompt)]
        system_prompt = (
            f"You are an isolated subagent working in {self.runtime.settings.workspace_root}. "
            "Keep the main context clean. Do the work, then return a concise summary.\n"
            f"{capability_guidance}\n\n"
            f"{self.runtime._environment_guidance()}"
        )
        final_text = "(subagent failed)"
        pending_tool_repair_hints: list[dict[str, Any]] = []
        for _ in range(self.runtime.settings.runtime.max_subagent_rounds):
            if pending_tool_repair_hints:
                repair_message = render_transient_repair_hint_message(pending_tool_repair_hints)
                pending_tool_repair_hints = []
                if repair_message:
                    messages.append(make_user_text_message(repair_message))
            payload_messages = self.runtime._build_payload_messages(messages, session=None)
            turn = self.runtime.complete(system_prompt, payload_messages, registry.schemas())
            messages.append(turn.as_message())
            if not turn.has_tool_calls():
                text = "\n".join(turn.text_blocks).strip()
                return text or "(no summary)"
            results: list[dict[str, Any]] = []
            ctx = ToolExecutionContext(
                runtime=self.runtime,
                session=None,
                actor="subagent",
                trace_id=f"subagent-{uuid.uuid4().hex[:8]}",
            )
            for tool_call in turn.tool_calls:
                try:
                    output = registry.execute(ctx, tool_call.name, tool_call.input)
                except Exception as exc:
                    output = tool_error_from_exception(tool_call.name, exc)
                repair_hint = extract_transient_repair_hint(output)
                if repair_hint is not None:
                    pending_tool_repair_hints.append(repair_hint)
                persisted_output = sanitize_tool_output_for_persistence(output)
                results.append(
                    make_tool_result_item(
                        tool_call.id,
                        persisted_output,
                        rendered_output=serialize_tool_output(persisted_output),
                    )
                )
            messages.append(make_tool_result_message(results))
            final_text = "\n".join(turn.text_blocks).strip() or final_text
        return final_text

    def _build_registry(self, agent_type: str) -> ToolRegistry:
        registry = ToolRegistry()
        register_shell_tool(registry)
        registry.register(
            ToolDefinition(
                name="project_scan",
                description="Build a concise project map before diving into files.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "depth": {"type": "integer"},
                        "limit": {"type": "integer"},
                    },
                },
                handler=project_scan,
            )
        )
        registry.register(
            ToolDefinition(
                name="tree",
                description="Render a shallow directory tree for a focused path.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "depth": {"type": "integer"},
                        "limit": {"type": "integer"},
                    },
                },
                handler=tree_view,
            )
        )
        registry.register(
            ToolDefinition(
                name="find_symbol",
                description="Locate classes, functions, methods, or interfaces by symbol name substring in a directory or a single file.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string"},
                        "kind": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
                handler=find_symbol,
            )
        )
        registry.register(
            ToolDefinition(
                name="glob",
                description="Search for files or directories by glob pattern inside the workspace.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string"},
                        "recursive": {"type": "boolean"},
                        "match": {"type": "string", "enum": ["files", "dirs", "all"]},
                        "limit": {"type": "integer"},
                    },
                    "required": ["pattern"],
                },
                handler=glob_search,
            )
        )
        registry.register(
            ToolDefinition(
                name="grep",
                description=GREP_TOOL_DESCRIPTION,
                input_schema={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string"},
                        "glob": {"type": "string"},
                        "recursive": {"type": "boolean"},
                        "case_sensitive": {"type": "boolean"},
                        "use_regex": {"type": "boolean"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["pattern"],
                },
                handler=grep_search,
            )
        )
        registry.register(
            ToolDefinition(
                name="read_file",
                description="Read file contents.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                handler=read_file,
            )
        )
        registry.register(
            ToolDefinition(
                name="read_image",
                description="Load a local image from the workspace so the model can inspect it on the next turn.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                handler=read_image,
            )
        )
        if agent_type != "Explore":
            registry.register(
                ToolDefinition(
                    name="write_file",
                    description="Write content to a file.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                    handler=write_file,
                )
            )
            registry.register(
                ToolDefinition(
                    name="edit_file",
                    description=(
                        "Replace exact text in one or more files. Always pass "
                        "`edits=[{old_text,new_text}, ...]`, even for a single replacement."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "edits": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "old_text": {"type": "string"},
                                        "new_text": {"type": "string"},
                                    },
                                    "required": ["old_text", "new_text"],
                                },
                            },
                        },
                        "required": ["edits"],
                    },
                    handler=edit_file,
                )
            )
        registry.register(
            ToolDefinition(
                name="load_skill",
                description="Load specialized knowledge by skill name.",
                input_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                handler=lambda ctx, payload: self.runtime.skill_loader.load(payload["name"]),
            )
        )
        return registry
