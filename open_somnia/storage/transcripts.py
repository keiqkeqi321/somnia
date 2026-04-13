from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from open_somnia.storage.common import append_jsonl, atomic_write_text


class TranscriptStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def transcript_path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.jsonl"

    def snapshot_path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.snapshot.json"

    def append(self, session_id: str, entry: dict[str, Any]) -> None:
        append_jsonl(self.transcript_path(session_id), entry)

    def save_snapshot(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        atomic_write_text(
            self.snapshot_path(session_id),
            json.dumps(messages, indent=2, ensure_ascii=False, default=str),
        )

    def load_snapshot(self, session_id: str) -> list[dict[str, Any]]:
        path = self.snapshot_path(session_id)
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    # ── Checkpoint / Rollback ──────────────────────────────────────────

    def checkpoint_path(self, session_id: str, tag: str) -> Path:
        """Return the path for a checkpoint file.

        Checkpoints are stored as ``{session_id}.checkpoint.{tag}.json``
        inside the transcripts directory.
        """
        encoded_tag = base64.urlsafe_b64encode(tag.encode("utf-8")).decode("ascii").rstrip("=")
        safe_tag = f"b64_{encoded_tag}"
        return self.root / f"{session_id}.checkpoint.{safe_tag}.json"

    def save_checkpoint(self, session_id: str, tag: str, payload: dict[str, Any]) -> None:
        """Persist a checkpoint payload to disk.

        Args:
            session_id: The session this checkpoint belongs to.
            tag: A human-readable tag for the checkpoint.
            payload: The checkpoint data (messages, undo_stack, session state).
        """
        atomic_write_text(
            self.checkpoint_path(session_id, tag),
            json.dumps(payload, ensure_ascii=False, default=str),
        )

    def load_checkpoint(self, session_id: str, tag: str) -> dict[str, Any] | None:
        """Load a checkpoint payload from disk.

        Returns ``None`` if the checkpoint does not exist.
        """
        path = self.checkpoint_path(session_id, tag)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        """Return metadata for all checkpoints of a session, sorted by timestamp ascending.

        Each entry contains: tag, timestamp, message_count.
        """
        prefix = f"{session_id}.checkpoint."
        checkpoints: list[dict[str, Any]] = []
        for path in self.root.glob(f"{prefix}*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            checkpoints.append({
                "tag": data.get("tag", ""),
                "timestamp": data.get("timestamp", 0),
                "message_count": data.get("message_count", 0),
                "file_count": sum(len(entry.get("files", [])) for entry in data.get("undo_stack", [])),
                "last_user_message": data.get("last_user_message", ""),
            })
        checkpoints.sort(key=lambda c: (c.get("timestamp", 0), c.get("tag", "")))
        return checkpoints

    def delete_checkpoints_after(self, session_id: str, tag: str) -> int:
        """Delete all checkpoints whose timestamp is strictly after the given checkpoint's timestamp.

        This is used during rollback to clean up checkpoints that were created
        *after* the rollback target and are therefore orphaned.

        Returns the number of deleted checkpoints.
        """
        target = self.load_checkpoint(session_id, tag)
        if target is None:
            return 0
        target_ts = target.get("timestamp", 0)
        deleted = 0
        for cp in self.list_checkpoints(session_id):
            if cp["tag"] == tag:
                continue
            if cp.get("timestamp", 0) > target_ts:
                path = self.checkpoint_path(session_id, cp["tag"])
                if path.exists():
                    path.unlink()
                    deleted += 1
        return deleted
