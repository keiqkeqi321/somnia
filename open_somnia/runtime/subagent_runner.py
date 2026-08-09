from __future__ import annotations

import uuid
from typing import Any

from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import make_user_text_message
from open_somnia.runtime.round_runner import RoundHooks, SessionlessRoundRunner, ToolCallRecord
from open_somnia.tools.filesystem import (
    GREP_TOOL_DESCRIPTION,
    edit_file,
    find_symbol,
    glob_search,
    grep_search,
    read_image,
    read_file,
    tree_view,
    write_file,
)
from open_somnia.tools.registry import ToolDefinition, ToolRegistry
from open_somnia.tools.shell import register_readonly_shell_tool, register_shell_tool
from open_somnia.tools.tool_errors import serialize_tool_output
from open_somnia.tools.web_fetch import register_web_fetch_tool


class SubagentRunner:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def run_subagent(
        self,
        prompt: str,
        agent_type: str = "Explore",
        *,
        activity_id: str | None = None,
        should_interrupt=None,
    ) -> str:
        activity_id = str(activity_id or f"subagent-{uuid.uuid4().hex[:8]}")
        self._raise_if_interrupted(should_interrupt)
        registry = self._build_registry(agent_type)
        capability_guidance = (
            "You are in Explore mode. Use read-only tools only: `bash`, `tree`, `find_symbol`, `glob`, `grep`, `read_file`, `read_image`, `web_fetch`, and `load_skill`. "
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
        log_store = getattr(self.runtime, "subagent_log_store", None)

        def log(payload: dict[str, Any]) -> None:
            if log_store is not None:
                log_store.append(activity_id, payload)

        def on_assistant_message(assistant_message: dict[str, Any], turn_text: str) -> None:
            if not turn_text:
                return
            self._emit_activity(
                activity_id=activity_id,
                agent_type=agent_type,
                prompt=prompt,
                kind="assistant",
                text=turn_text,
            )
            log({"type": "assistant_message", "content": turn_text})

        def on_tool_record(record: ToolCallRecord) -> None:
            self._emit_activity(
                activity_id=activity_id,
                agent_type=agent_type,
                prompt=prompt,
                kind="tool_result",
                text=self._format_tool_activity(record.tool_call.name, record.tool_call.input, record.persisted_output),
            )
            log(
                {
                    "type": "tool_call",
                    "tool_name": record.tool_call.name,
                    "tool_input": record.tool_call.input,
                    "output_preview": self._compact_text(record.rendered_output, limit=600),
                }
            )

        hooks = RoundHooks(on_assistant_message=on_assistant_message, on_tool_record=on_tool_record)
        runner = SessionlessRoundRunner(self.runtime)
        log({"type": "started", "prompt": prompt, "agent_type": agent_type})
        try:
            for _ in range(self.runtime.settings.runtime.max_subagent_rounds):
                result = runner.run_round(
                    system_prompt=system_prompt,
                    messages=messages,
                    registry=registry,
                    pending_repair_hints=pending_tool_repair_hints,
                    actor="subagent",
                    trace_id=f"subagent-{uuid.uuid4().hex[:8]}",
                    should_interrupt=should_interrupt,
                    hooks=hooks,
                )
                if not result.has_tool_calls:
                    log({"type": "summary", "content": result.turn_text or "(no summary)"})
                    return result.turn_text or "(no summary)"
                final_text = result.turn_text or final_text
            log({"type": "summary", "content": final_text})
            return final_text
        except TurnInterrupted:
            raise
        except Exception as exc:
            log({"type": "error", "error": str(exc)})
            raise

    def _raise_if_interrupted(self, should_interrupt) -> None:
        if should_interrupt is not None and should_interrupt():
            raise TurnInterrupted("Interrupted by user.")

    def _emit_activity(
        self,
        *,
        activity_id: str,
        agent_type: str,
        prompt: str,
        kind: str,
        text: str,
    ) -> None:
        text = self._compact_text(text, limit=180)
        if not text:
            return
        handler = getattr(self.runtime, "subagent_activity_handler", None)
        if not callable(handler):
            return
        try:
            handler(
                {
                    "activity_id": activity_id,
                    "agent_type": agent_type,
                    "prompt": prompt,
                    "kind": kind,
                    "text": text,
                }
            )
        except Exception:
            pass

    def _format_tool_activity(self, tool_name: str, tool_input: dict[str, Any], output: Any) -> str:
        label = self._tool_activity_label(tool_name, tool_input)
        rendered = serialize_tool_output(output)
        summary = self._compact_text(rendered, limit=140)
        if summary:
            return f"{label}: {summary}"
        return label

    def _tool_activity_label(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        if tool_name == "bash":
            command = self._compact_text(str(tool_input.get("command", "")).strip(), limit=64)
            return f"bash {command}" if command else "bash"
        if tool_name in {"read_file", "read_image", "tree"}:
            path = str(tool_input.get("path", "")).strip() or "."
            return f"{tool_name} {self._compact_text(path, limit=64)}"
        if tool_name in {"grep", "find_symbol"}:
            query = str(tool_input.get("pattern", tool_input.get("query", ""))).strip()
            return f"{tool_name} {self._compact_text(query, limit=64)}" if query else tool_name
        return str(tool_name)

    def _compact_text(self, text: str, *, limit: int) -> str:
        compact = " ".join(str(text).split())
        if not compact:
            return ""
        if len(compact) <= limit:
            return compact
        if limit <= 3:
            return compact[:limit]
        return compact[: limit - 3] + "..."

    def _build_registry(self, agent_type: str) -> ToolRegistry:
        registry = ToolRegistry()
        # Explore subagents run in parallel with their siblings, and their only
        # write vector is `bash` (write_file/edit_file are not registered for
        # Explore). To keep parallel Explore subagents free of write races, the
        # Explore `bash` is gated to read-only commands. general-purpose keeps
        # the unrestricted `bash` (it is expected to mutate and runs serially).
        if agent_type == "Explore":
            register_readonly_shell_tool(registry)
        else:
            register_shell_tool(registry)
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
        register_web_fetch_tool(registry)
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
