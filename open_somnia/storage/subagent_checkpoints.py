from __future__ import annotations

from pathlib import Path
from typing import Any

from open_somnia.storage.common import read_json, write_json


class SubagentCheckpoint:
    """Persisted resumable state of a subagent run, keyed by activity id.

    A subagent that ends ``interrupted``/``truncated``/``failed`` is checkpointed
    here so the lead can resume it from where it left off (inheriting the already
    accumulated ``messages`` and ``pending_repair_hints``) instead of restarting
    from scratch. Each execution gets one JSON file under
    ``logs/subagent_checkpoints/``; the file is removed when the subagent reaches
    ``completed``. Resuming is governed by ``resume_count`` (the lead/runtime
    caps how many times a single subagent may be resumed before the round budget
    stops resetting).
    """

    # ``status`` reflects why the checkpoint was written.
    RESUMABLE_STATUSES = ("interrupted", "truncated", "failed")

    def __init__(
        self,
        *,
        activity_id: str,
        prompt: str,
        agent_type: str,
        messages: list[dict[str, Any]],
        pending_repair_hints: list[dict[str, Any]],
        rounds_used: int,
        status: str,
        resume_count: int = 0,
        tool_calls: int = 0,
        created_at: float | None = None,
        session_id: str | None = None,
    ) -> None:
        import time

        self.activity_id = activity_id
        self.prompt = prompt
        self.agent_type = agent_type
        # Defensive copies: the caller's lists keep mutating after save, and a
        # resumed checkpoint must freeze the state at the save point.
        self.messages = list(messages)
        self.pending_repair_hints = [dict(h) for h in pending_repair_hints]
        self.rounds_used = rounds_used
        self.status = status
        self.resume_count = resume_count
        self.tool_calls = tool_calls
        self.created_at = created_at if created_at is not None else time.time()
        # Owning session id. Resume is session-scoped: a new session saying
        # "hi" must never auto-resume a subagent left interrupted by a *different*
        # session. ``None`` covers sessionless/manual invocations (no auto-resume).
        self.session_id = session_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity_id": self.activity_id,
            "prompt": self.prompt,
            "agent_type": self.agent_type,
            "messages": self.messages,
            "pending_repair_hints": self.pending_repair_hints,
            "rounds_used": self.rounds_used,
            "status": self.status,
            "resume_count": self.resume_count,
            "tool_calls": self.tool_calls,
            "created_at": self.created_at,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubagentCheckpoint:
        return cls(
            activity_id=str(data["activity_id"]),
            prompt=str(data.get("prompt", "")),
            agent_type=str(data.get("agent_type", "Explore")),
            messages=list(data.get("messages", [])),
            pending_repair_hints=list(data.get("pending_repair_hints", [])),
            rounds_used=int(data.get("rounds_used", 0)),
            status=str(data.get("status", "interrupted")),
            resume_count=int(data.get("resume_count", 0)),
            tool_calls=int(data.get("tool_calls", 0)),
            created_at=data.get("created_at"),
            session_id=data.get("session_id"),
        )


class SubagentCheckpointStore:
    """File-backed store of :class:`SubagentCheckpoint` records.

    One JSON file per activity id under ``<root>/subagent_checkpoints/``. The
    store is intentionally minimal (save / load / delete / list_pending) so the
    resume policy lives entirely in the runner. ``list_pending`` returns
    checkpoints whose ``status`` is resumable, oldest first, so the lead's
    auto-resume-on-continue drains them deterministically.
    """

    def __init__(self, root: Path) -> None:
        self.root = root / "subagent_checkpoints"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, activity_id: str) -> Path:
        return self.root / f"{activity_id}.json"

    def save(self, checkpoint: SubagentCheckpoint) -> None:
        write_json(self.path(checkpoint.activity_id), checkpoint.to_dict())

    def load(self, activity_id: str) -> SubagentCheckpoint | None:
        data = read_json(self.path(activity_id), None)
        if not isinstance(data, dict):
            return None
        try:
            return SubagentCheckpoint.from_dict(data)
        except (KeyError, TypeError, ValueError):
            return None

    def delete(self, activity_id: str) -> None:
        path = self.path(activity_id)
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    def list_pending(self, session_id: str | None = None) -> list[SubagentCheckpoint]:
        """Return resumable checkpoints, oldest ``created_at`` first.

        When ``session_id`` is given, only checkpoints owned by that session are
        returned. This scopes auto-resume to the current session so a freshly
        created session (e.g. the user just said "hi") never resumes a subagent
        left interrupted by a *different* session. ``session_id=None`` returns
        every pending checkpoint regardless of owner (used by callers that want
        the full set, e.g. diagnostics).
        """
        out: list[SubagentCheckpoint] = []
        for path in self.root.glob("*.json"):
            data = read_json(path, None)
            if not isinstance(data, dict):
                continue
            try:
                cp = SubagentCheckpoint.from_dict(data)
            except (KeyError, TypeError, ValueError):
                continue
            if cp.status not in SubagentCheckpoint.RESUMABLE_STATUSES:
                continue
            if session_id is not None and cp.session_id != session_id:
                continue
            out.append(cp)
        out.sort(key=lambda c: (c.created_at if c.created_at is not None else 0.0))
        return out
