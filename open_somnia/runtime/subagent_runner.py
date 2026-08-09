from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import (
    make_tool_result_message,
    make_user_text_message,
)
from open_somnia.runtime.round_runner import RoundHooks, SessionlessRoundRunner, ToolCallRecord
from open_somnia.storage.subagent_checkpoints import SubagentCheckpoint
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


SubagentStatus = Literal["completed", "truncated", "failed", "interrupted"]

# A text-only turn (no tool call) is not valid completion for a subagent --
# only the submit_summary tool is. When the model emits such a turn anyway,
# inject this nudge so it either calls the next tool or calls submit_summary,
# rather than exiting before the work is done. This closes the "premature exit
# on round 1" hole where a bare-text summary used to be accepted as done.
SUBAGENT_NO_TOOL_NUDGE = (
    "You ended this turn without calling any tool and without calling submit_summary. "
    "A text-only response is not completion. If your investigation is finished, call "
    "submit_summary with your final summary now. Otherwise, call the next tool "
    "(grep / read_file / glob / etc.) and keep working -- do not merely describe what "
    "you intend to do."
)


@dataclass(slots=True)
class SubagentResult:
    """Structured outcome of a subagent run.

    Replaces the old bare-string return. ``completed`` carries the summary the
    subagent produced; the other statuses are recoverable — the lead resumes the
    subagent from its checkpoint (``resume_from=<activity_id>``) inheriting the
    accumulated context, or accepts the partial result and moves on. Restarting
    the subagent from scratch is never the intended path: it discards the tokens
    already spent.
    """

    status: SubagentStatus
    summary: str = ""
    rounds_used: int = 0
    tool_calls: int = 0
    error: str | None = None
    activity_id: str | None = None

    def as_tool_output(self) -> dict[str, Any]:
        """Render as the ``subagent`` tool's structured output.

        ``completed`` flows through ``tool_result_text`` so the lead sees the
        clean summary text (backward compatible with the old string return).
        Non-completed statuses become a structured error dict so the lead can
        branch on ``status`` and is guided toward ``resume_from`` (never retry).
        """
        if self.status == "completed":
            return {"status": "completed", "tool_result_text": self.summary or "(no summary)"}
        return {
            "status": self.status,
            "error_type": "subagent_" + self.status,
            "tool_name": "subagent",
            "message": self._resume_guide(),
            "activity_id": self.activity_id,
            "rounds_used": self.rounds_used,
            "tool_calls": self.tool_calls,
            "is_error": True,
        }

    def _resume_guide(self) -> str:
        aid = self.activity_id or "?"
        base = "不要从头重新派 subagent——那会丢弃已花费的 token。"
        if self.status == "interrupted":
            return (
                f"Subagent 被中断（已落盘 checkpoint，上下文已保留）。"
                f"用 resume_from=\"{aid}\" 恢复它从中断点继续，继承已读文件与推理（低成本）；"
                f"或接受现状继续。{base}"
            )
        if self.status == "truncated":
            return (
                f"Subagent 轮次耗尽（rounds_used={self.rounds_used}），未输出完整总结。已落盘 checkpoint。"
                f"用 resume_from=\"{aid}\" 恢复，从中断处继续（低成本）；"
                f"或接受现状继续。{base}"
            )
        # failed
        err = self.error or "unknown error"
        return (
            f"Subagent 出错：{err}。已落盘 checkpoint。"
            f"可先尝试用 resume_from=\"{aid}\" 恢复继续（继承上下文）；"
            f"或评估是否换一个更聚焦的 prompt 恢复，而非从零开始。"
        )


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
        resume_from: SubagentCheckpoint | None = None,
        session_id: str | None = None,
        extra_prompt: str | None = None,
    ) -> SubagentResult:
        """Run an isolated subagent loop and return a structured result.

        When ``resume_from`` is given, the subagent continues from the
        checkpoint's accumulated ``messages``/``pending_repair_hints`` rather
        than starting over. The round budget is reset on each resume up to
        ``runtime.max_subagent_resumes`` times; beyond that the remaining
        rounds from the checkpoint are used, capping total work and preventing
        unbounded round inflation via repeated interrupt+resume cycles.

        ``session_id`` is stamped onto any checkpoint written here so the lead
        can scope resume decisions to the owning session. When resuming, the
        checkpoint's existing ``session_id`` is preserved.

        ``extra_prompt`` may carry an additional instruction the lead wants to
        inject when resuming (e.g. a refined direction). It is appended as a
        user message to the subagent's accumulated context BEFORE the resume
        loop continues, so the subagent sees both its prior work and the new
        guidance. Only meaningful together with ``resume_from``.
        """
        activity_id = str(activity_id or f"subagent-{uuid.uuid4().hex[:8]}")
        self._raise_if_interrupted(should_interrupt)
        registry = self._build_registry(agent_type)
        capability_guidance = (
            "You are in Explore mode. Use read-only tools only: `bash`, `tree`, `find_symbol`, `glob`, `grep`, `read_file`, `read_image`, `web_fetch`, and `load_skill`. "
            "Do not attempt workspace edits."
            if agent_type == "Explore"
            else "You are in general-purpose mode. In addition to read-only tools, you may use `write_file` and `edit_file` when needed."
        )
        max_rounds = int(getattr(self.runtime.settings.runtime, "max_subagent_rounds", 50))
        max_resumes = int(getattr(self.runtime.settings.runtime, "max_subagent_resumes", 3))
        checkpoint_store = getattr(self.runtime, "subagent_checkpoint_store", None)

        # ---- State initialization (fresh run vs resume) ----
        resuming = resume_from is not None
        if resuming:
            assert resume_from is not None  # narrowing for type checkers
            cp = resume_from
            # Copy so the loaded checkpoint is not mutated by run_round below.
            messages: list[dict[str, Any]] = [dict(m) for m in cp.messages]
            pending_tool_repair_hints: list[dict[str, Any]] = [dict(h) for h in cp.pending_repair_hints]
            resume_count = cp.resume_count + 1
            tool_calls_seen = cp.tool_calls
            # Reset the round budget on each resume (the subagent gets a fresh
            # max_rounds budget), but only up to max_resumes times. Past that
            # limit, keep accumulating from cp.rounds_used so repeated
            # interrupt+resume cannot inflate the total round budget unboundedly.
            if resume_count <= max_resumes:
                rounds_used = 0
            else:
                rounds_used = cp.rounds_used
            log_prompt = cp.prompt or prompt
            # Repair the orphaned assistant message left by an interrupt that
            # fired between appending the assistant turn and appending the
            # matching tool_result message (see _sanitize_messages_for_resume).
            self._sanitize_messages_for_resume(messages)
            resume_note = f" (resumed, attempt {resume_count})"
            # Preserve the owning session across resumes.
            effective_session_id = cp.session_id if cp.session_id else session_id
            # Inject an optional additional instruction from the lead (e.g. a
            # refined direction). Appended as a user message so the subagent
            # sees both its prior work and the new guidance on the next round.
            if extra_prompt and str(extra_prompt).strip():
                messages.append(make_user_text_message(str(extra_prompt).strip()))
        else:
            messages = [make_user_text_message(prompt)]
            pending_tool_repair_hints = []
            resume_count = 0
            tool_calls_seen = 0
            rounds_used = 0
            log_prompt = prompt
            resume_note = ""
            effective_session_id = session_id

        system_prompt = (
            f"You are an isolated subagent working in {self.runtime.settings.workspace_root}. "
            "Keep the main context clean. Do the work, then return a concise summary.\n"
            f"{capability_guidance}\n\n"
            "## Completion protocol (IMPORTANT)\n"
            "When -- and ONLY when -- your task is fully done, call the `submit_summary` tool "
            "with your final summary. That tool is the sole way to finish the task. Do NOT end "
            "the task by writing a plain text response with no tool call: a text-only turn is "
            "not completion and will not end the run -- the loop will keep going (with a reminder) "
            "until you either call a tool to continue the work or call `submit_summary`. Use tools "
            "to investigate/verify first; then submit a concise, self-contained summary once "
            "complete.\n\n"
            f"{self.runtime._environment_guidance()}"
        )
        final_text = ""
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
                prompt=log_prompt,
                kind="assistant",
                text=turn_text,
            )
            log({"type": "assistant_message", "content": turn_text})

        def on_tool_record(record: ToolCallRecord) -> None:
            nonlocal tool_calls_seen
            tool_calls_seen += 1
            # submit_summary is the explicit completion act; capture its summary
            # so the loop uses it as SubagentResult.summary (preferred over any
            # accompanying text). Prefer the canonical field the handler set,
            # fall back to the raw input.
            if record.tool_call.name == "submit_summary":
                persisted = record.persisted_output
                summary = None
                if isinstance(persisted, dict):
                    summary = persisted.get("submitted_summary")
                if not summary:
                    summary = str(record.tool_call.input.get("summary", "")).strip()
                if summary:
                    submitted_summary_holder["value"] = summary
            self._emit_activity(
                activity_id=activity_id,
                agent_type=agent_type,
                prompt=log_prompt,
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

        # submit_summary marks the round as the final one (mirrors the teammate
        # ``idle`` pattern via should_stop_after_round). submitted_summary_holder
        # carries the captured summary out to the loop.
        submitted_summary_holder: dict[str, Any] = {"value": None}

        def should_stop_after_round(record: ToolCallRecord) -> bool:
            return record.tool_call.name == "submit_summary"

        hooks = RoundHooks(
            on_assistant_message=on_assistant_message,
            on_tool_record=on_tool_record,
            should_stop_after_round=should_stop_after_round,
        )
        runner = SessionlessRoundRunner(self.runtime)
        log({"type": "started", "prompt": log_prompt, "agent_type": agent_type, "resume": resuming})

        def _checkpoint(status: SubagentStatus) -> None:
            """Persist current state so the lead can resume this subagent."""
            if checkpoint_store is None:
                return
            try:
                    checkpoint_store.save(
                        SubagentCheckpoint(
                            activity_id=activity_id,
                            prompt=log_prompt,
                            agent_type=agent_type,
                            messages=messages,
                            pending_repair_hints=pending_tool_repair_hints,
                            rounds_used=rounds_used,
                            status=status,
                            resume_count=resume_count,
                            tool_calls=tool_calls_seen,
                            session_id=effective_session_id,
                        )
                    )
            except Exception:
                # Checkpointing is best-effort: a failure here must not mask the
                # real outcome or crash the loop. The run still reports its status.
                pass

        def _clear_checkpoint() -> None:
            if checkpoint_store is not None:
                try:
                    checkpoint_store.delete(activity_id)
                except Exception:
                    pass

        try:
            while rounds_used < max_rounds:
                self._raise_if_interrupted(should_interrupt)
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
                rounds_used += 1
                if result.turn_text:
                    final_text = result.turn_text
                # Completion is an explicit act: the model called submit_summary
                # (signaled via should_stop_after_round). The submitted summary
                # is the authoritative answer; fall back to the round's text if
                # a non-standard handler did not populate the holder.
                if result.stop_after_round:
                    summary = submitted_summary_holder["value"] or final_text or ""
                    log({"type": "summary", "content": summary or "(no summary)"})
                    _clear_checkpoint()
                    return SubagentResult(
                        status="completed",
                        summary=summary,
                        rounds_used=rounds_used,
                        tool_calls=tool_calls_seen,
                        activity_id=activity_id,
                    )
                if not result.has_tool_calls:
                    # A text-only turn is NOT completion -- the model must call
                    # submit_summary to finish. Append the nudge as a plain user
                    # message and continue, so the subagent either calls a tool
                    # or formally submits its summary, instead of exiting early.
                    # It is appended after run_round returns (and after it has
                    # drained pending_tool_repair_hints for this round), so it
                    # appears in the next round's payload.
                    messages.append(make_user_text_message(SUBAGENT_NO_TOOL_NUDGE))
                    continue
            # Round budget exhausted without a submit_summary completion.
            _checkpoint("truncated")
            log({"type": "summary", "content": final_text or "(no summary)", "truncated": True})
            return SubagentResult(
                status="truncated",
                summary=final_text,
                rounds_used=rounds_used,
                tool_calls=tool_calls_seen,
                activity_id=activity_id,
            )
        except TurnInterrupted:
            # Checkpoint first so the lead's continue-auto-resume can pick it
            # up, then re-raise to preserve the existing interrupt semantics in
            # the lead loop (it saves the session, etc.).
            _checkpoint("interrupted")
            log({"type": "interrupted"})
            raise
        except Exception as exc:
            _checkpoint("failed")
            log({"type": "error", "error": str(exc)})
            return SubagentResult(
                status="failed",
                summary=final_text,
                rounds_used=rounds_used,
                tool_calls=tool_calls_seen,
                error=str(exc),
                activity_id=activity_id,
            )

    def _sanitize_messages_for_resume(self, messages: list[dict[str, Any]]) -> None:
        """Repair orphaned assistant tool_use blocks left by an interrupt.

        ``run_round`` appends the assistant message (with ``tool_call`` blocks)
        before it appends the matching ``tool_result`` user message. If an
        interrupt fires in that window, ``messages`` ends with an assistant
        turn whose tool calls have no paired results, which providers reject as
        a malformed payload on resume. For each unpaired trailing tool call we
        synthesize a placeholder ``tool_result`` ("[interrupted before
        completion]") so the conversation is well-formed. We keep the assistant
        turn (rather than rolling it back) to preserve the model's reasoning
        and any tool inputs it already produced.
        """
        if not messages:
            return
        # Find the index of the last assistant message.
        last_assistant_idx = None
        for idx in range(len(messages) - 1, -1, -1):
            if str(messages[idx].get("role", "")) == "assistant":
                last_assistant_idx = idx
                break
        if last_assistant_idx is None:
            return
        assistant = messages[last_assistant_idx]
        content = assistant.get("content")
        # Normalize to a block list. A bare-string assistant turn has no tool
        # calls and therefore nothing to repair.
        if isinstance(content, str):
            return
        if not isinstance(content, list):
            return
        tool_call_ids: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type", "")) == "tool_call":
                call_id = str(block.get("id", "")).strip()
                if call_id:
                    tool_call_ids.append(call_id)
        if not tool_call_ids:
            return
        # Collect tool_call_ids that already have a matching tool_result in any
        # user message after the last assistant turn.
        answered: set[str] = set()
        for msg in messages[last_assistant_idx + 1 :]:
            if str(msg.get("role", "")) != "user":
                continue
            user_content = msg.get("content")
            items = user_content if isinstance(user_content, list) else []
            for item in items:
                if isinstance(item, dict) and str(item.get("type", "")) == "tool_result":
                    tc_id = str(item.get("tool_call_id", "")).strip()
                    if tc_id:
                        answered.add(tc_id)
        unpaired = [cid for cid in tool_call_ids if cid not in answered]
        if not unpaired:
            return
        # Synthesize placeholder tool_results for the unpaired calls. Reuse the
        # existing tool_result message if the last message is one (its content
        # is a list of items), otherwise append a fresh user tool_result message.
        placeholder_text = "[interrupted before completion]"
        new_items = [
            {"type": "tool_result", "tool_call_id": cid, "content": placeholder_text, "is_error": False}
            for cid in unpaired
        ]
        if messages and str(messages[-1].get("role", "")) == "user" and isinstance(messages[-1].get("content"), list):
            messages[-1]["content"].extend(new_items)
        else:
            messages.append(make_tool_result_message(new_items))

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
        # The ONLY way a subagent signals "done". submit_summary carries the
        # final summary; the runner returns when it sees a submit_summary tool
        # call (via should_stop_after_round). Unlike a bare text turn -- which
        # might be a half-formed plan or a premature guess -- submit_summary is
        # an explicit completion act, so a subagent cannot exit before doing the
        # work. The handler just echoes the payload so the round completes with
        # a well-formed tool_result; the runner ignores the tool output and uses
        # the summary field directly.
        registry.register(
            ToolDefinition(
                name="submit_summary",
                description=(
                    "Signal that your work is complete and deliver the final summary to the "
                    "calling agent. This is the ONLY way to finish a subagent task -- a plain "
                    "text response without any tool call does NOT complete the task. "
                    "Call this exactly once when done, with a concise, self-contained summary "
                    "of what you found, decided, or changed."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "The final summary delivered to the calling agent.",
                        }
                    },
                    "required": ["summary"],
                },
                handler=lambda ctx, payload: {
                    "status": "completed",
                    "tool_result_text": "Summary submitted.",
                    "submitted_summary": str(payload.get("summary", "")).strip(),
                },
            )
        )
        return registry
