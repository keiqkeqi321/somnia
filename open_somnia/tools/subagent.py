from __future__ import annotations

from typing import Any

from open_somnia.tools.registry import ToolDefinition


def register_subagent_tool(registry) -> None:
    def handler(ctx: Any, payload: dict[str, Any]) -> dict[str, Any]:
        # Resume path: when resume_from is given, load the checkpoint and hand
        # it to run_subagent so it continues from the saved context instead of
        # restarting. The prompt is still required by schema for traceability.
        resume_aid = str(payload.get("resume_from") or "").strip()
        resume_from = None
        if resume_aid:
            store = getattr(ctx.runtime, "subagent_checkpoint_store", None)
            if store is not None:
                resume_from = store.load(resume_aid)
        # Stamp the owning session so auto-resume stays session-scoped. ctx.session
        # is set on the lead path; teammates/subagents have session=None.
        session = getattr(ctx, "session", None)
        session_id = None
        if session is not None:
            session_id = getattr(session, "id", None)
        # On resume, keep the checkpoint's activity_id so the resumed subagent
        # updates the SAME checkpoint file (otherwise a new tool_call id would
        # orphan the original checkpoint and effectively restart from scratch).
        if resume_from is not None:
            activity_id = getattr(resume_from, "activity_id", None) or resume_aid
        else:
            activity_id = getattr(ctx, "trace_id", None)
        result = ctx.runtime.run_subagent(
            payload["prompt"],
            payload.get("agent_type", "Explore"),
            activity_id=activity_id,
            should_interrupt=getattr(ctx, "should_interrupt", None),
            resume_from=resume_from,
            session_id=session_id,
            extra_prompt=payload.get("extra_prompt"),
        )
        return result.as_tool_output()

    registry.register(
        ToolDefinition(
            name="subagent",
            description=(
                "Delegate exploration or implementation to an isolated subagent that runs in a fresh "
                "context and returns a concise summary, keeping your main context clean. "
                "Use this FIRST for broad codebase exploration, research, 'how does X work' questions, "
                "or any task that would require several read/grep/glob steps. "
                "If a task needs more than ~3-5 exploratory tool calls, delegate it to a subagent "
                "instead of doing the exploration yourself. "
                "If a prior subagent call returned status interrupted/truncated/failed, resume it with "
                "resume_from=<activity_id> (inherits its accumulated context at low cost) rather than "
                "starting over."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "agent_type": {
                        "type": "string",
                        "enum": ["Explore", "general-purpose"],
                    },
                    "resume_from": {
                        "type": "string",
                        "description": (
                            "activity_id of a previously interrupted/truncated/failed subagent to resume "
                            "from its saved context instead of starting over."
                        ),
                    },
                    "extra_prompt": {
                        "type": "string",
                        "description": (
                            "Only with resume_from: an additional instruction appended to the resumed "
                            "subagent's context (e.g. a refined direction or a follow-up question). The "
                            "subagent sees both its prior work and this new guidance."
                        ),
                    },
                },
                "required": ["prompt"],
            },
            handler=handler,
        )
    )
