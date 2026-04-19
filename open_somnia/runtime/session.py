from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from typing import Any

from open_somnia.storage.common import atomic_write_text
from open_somnia.storage.sessions import SessionStore
from open_somnia.storage.transcripts import TranscriptStore


@dataclass(slots=True)
class AgentSession:
    id: str
    created_at: float | None = None
    updated_at: float | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)
    todo_items: list[dict[str, Any]] = field(default_factory=list)
    rounds_without_todo: int = 0
    read_file_overlap_state: dict[str, Any] = field(default_factory=dict, repr=False)
    latest_turn_id: str | None = None
    last_turn_file_changes: list[dict[str, Any]] = field(default_factory=list)
    undo_stack: list[dict[str, Any]] = field(default_factory=list)
    pending_file_changes: list[dict[str, Any]] = field(default_factory=list, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AgentSession":
        return cls(
            id=payload["id"],
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
            messages=list(payload.get("messages", [])),
            token_usage=dict(payload.get("token_usage", {})),
            todo_items=list(payload.get("todo_items", [])),
            rounds_without_todo=int(payload.get("rounds_without_todo", 0)),
            read_file_overlap_state=deepcopy(payload.get("read_file_overlap_state", {})),
            latest_turn_id=payload.get("latest_turn_id"),
            last_turn_file_changes=list(payload.get("last_turn_file_changes", [])),
            undo_stack=list(payload.get("undo_stack", [])),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": self.messages,
            "token_usage": self.token_usage,
            "todo_items": self.todo_items,
            "rounds_without_todo": self.rounds_without_todo,
            "read_file_overlap_state": self.read_file_overlap_state,
            "latest_turn_id": self.latest_turn_id,
            "last_turn_file_changes": self.last_turn_file_changes,
            "undo_stack": self.undo_stack,
        }


class SessionManager:
    def __init__(self, session_store: SessionStore, transcript_store: TranscriptStore):
        self.session_store = session_store
        self.transcript_store = transcript_store

    def create(self) -> AgentSession:
        return AgentSession.from_payload(self.session_store.create())

    def latest_or_create(self) -> AgentSession:
        latest = self.session_store.latest()
        if latest is None:
            return self.create()
        return AgentSession.from_payload(latest)

    def load(self, session_id: str) -> AgentSession:
        payload = self.session_store.load(session_id)
        if not payload.get("messages"):
            payload["messages"] = self.transcript_store.load_snapshot(session_id)
        return AgentSession.from_payload(payload)

    def list_all(self) -> list[AgentSession]:
        sessions: list[AgentSession] = []
        for payload in self.session_store.list_all():
            if not payload.get("messages"):
                payload["messages"] = self.transcript_store.load_snapshot(payload["id"])
            sessions.append(AgentSession.from_payload(payload))
        return sessions

    def save(self, session: AgentSession) -> None:
        self.session_store.save(session.to_payload())
        self.transcript_store.save_snapshot(session.id, session.messages)

    def detect_external_modifications(
        self,
        session: AgentSession,
        tag: str,
        workspace_root,
    ) -> list[dict[str, Any]]:
        """Detect files that were modified outside the agent since checkpoint.

        Compares each file touched by the agent after the checkpoint against
        the content the agent expects to be on disk. If the actual disk
        content differs, the file is flagged as externally modified.

        Args:
            session: The current session.
            tag: The checkpoint tag to compare against.
            workspace_root: Workspace root path for reading files.

        Returns:
            A list of dicts, each with:
            - path: relative file path
            - reason: "content_mismatch" | "unexpectedly_present" | "unexpectedly_missing"
        """
        from pathlib import Path as _Path

        checkpoint = self.transcript_store.load_checkpoint(session.id, tag)
        if checkpoint is None:
            return []

        checkpoint_undo_len = len(checkpoint.get("undo_stack", []))
        current_undo_len = len(session.undo_stack)
        if current_undo_len <= checkpoint_undo_len:
            return []

        workspace_root = _Path(workspace_root).resolve()

        # Gather all file records across the relevant undo entries.
        file_records_by_path: dict[str, list[dict[str, Any]]] = {}
        for idx in range(checkpoint_undo_len, current_undo_len):
            entry = session.undo_stack[idx]
            for file_record in entry.get("files", []):
                rp = str(file_record.get("path", "")).strip()
                if rp:
                    file_records_by_path.setdefault(rp, []).append(file_record)

        modified: list[dict[str, Any]] = []

        for relative_path, records in file_records_by_path.items():
            file_path = (workspace_root / relative_path).resolve()
            if not file_path.is_relative_to(workspace_root):
                continue

            first_existed = bool(records[0].get("existed_before"))
            file_exists = file_path.exists()

            if not first_existed and not file_exists:
                # Agent created the file, but it's been deleted externally
                modified.append({
                    "path": relative_path,
                    "reason": "unexpectedly_missing",
                    "detail": "Agent created this file but it no longer exists on disk",
                })
            elif first_existed and not file_exists:
                # File existed before but was deleted externally
                modified.append({
                    "path": relative_path,
                    "reason": "unexpectedly_missing",
                    "detail": "File existed at checkpoint but has been deleted",
                })
            elif first_existed and file_exists:
                try:
                    disk_content = file_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                previous_versions = [str(record.get("previous_content", "")) for record in records]
                if len(previous_versions) == 1 and disk_content == previous_versions[0]:
                    modified.append({
                        "path": relative_path,
                        "reason": "content_mismatch",
                        "detail": "File content matches pre-agent state; agent writes may have been externally reverted",
                    })
                    continue
                if len(previous_versions) > 1 and disk_content == previous_versions[-1]:
                    modified.append({
                        "path": relative_path,
                        "reason": "content_mismatch",
                        "detail": "File content matches the state before the agent's last write; the latest agent write may have been externally reverted",
                    })

        return modified

    def create_checkpoint(self, session: AgentSession, tag: str) -> dict[str, Any]:
        """Create a named checkpoint of the current session state.

        Saves messages, undo_stack, and session metadata to a checkpoint file.
        The undo_stack is deep-copied so subsequent mutations don't affect it.

        Args:
            session: The session to checkpoint.
            tag: A human-readable tag (e.g. "before_refactor").

        Returns:
            A dict with checkpoint metadata (tag, message_count, file_count).
        """
        import time

        try:
            undo_snapshot = deepcopy(session.undo_stack)
        except Exception:
            undo_snapshot = json.loads(json.dumps(session.undo_stack, ensure_ascii=False, default=str))

        # Extract a preview: the last user message before this checkpoint
        last_user_msg = ""
        for msg in reversed(session.messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    last_user_msg = content
                elif isinstance(content, list):
                    # Multimodal: concatenate text parts
                    parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            parts.append(part.get("text", ""))
                        elif isinstance(part, str):
                            parts.append(part)
                    last_user_msg = " ".join(parts)
                break

        payload = {
            "tag": tag,
            "timestamp": time.time(),
            "message_count": len(session.messages),
            "messages_snapshot": list(session.messages),
            "last_user_message": last_user_msg,
            "session_state": {
                "token_usage": dict(session.token_usage),
                "todo_items": list(session.todo_items),
                "rounds_without_todo": session.rounds_without_todo,
                "read_file_overlap_state": deepcopy(session.read_file_overlap_state),
                "latest_turn_id": session.latest_turn_id,
                "last_turn_file_changes": list(session.last_turn_file_changes),
            },
            "undo_stack": undo_snapshot,
        }
        self.transcript_store.save_checkpoint(session.id, tag, payload)
        return {
            "tag": tag,
            "message_count": payload["message_count"],
            "file_count": sum(len(entry.get("files", [])) for entry in undo_snapshot),
            "last_user_message": last_user_msg,
        }

    def list_checkpoints(self, session: AgentSession) -> list[dict[str, Any]]:
        """List all checkpoints for a session."""
        return self.transcript_store.list_checkpoints(session.id)

    def rollback_to_checkpoint(
        self,
        session: AgentSession,
        tag: str,
        *,
        workspace_root=None,
        skip_externally_modified: bool = False,
    ) -> dict[str, Any]:
        """Roll back a session to a previously created checkpoint.

        This operation:
        1. Reverts all file changes recorded in the undo_stack *after* the
           checkpoint by walking backwards from the current undo_stack top
           to the checkpoint's undo_stack length.
        2. Truncates the messages list to the checkpoint's message_count.
        3. Restores session state (todo_items, token_usage, etc.).
        4. Deletes orphaned checkpoints created after the target.

        Args:
            session: The session to roll back.
            tag: The checkpoint tag to roll back to.
            workspace_root: Workspace root Path for file revert operations.
                If None, file revert is skipped.
            skip_externally_modified: If True, files that were externally
                modified after the agent's last write are skipped during
                file revert. The messages/session state are still restored.

        Returns:
            A result dict with rollback statistics. Includes
            ``external_modifications`` list if any were detected.
        """
        from pathlib import Path as _Path

        checkpoint = self.transcript_store.load_checkpoint(session.id, tag)
        if checkpoint is None:
            return {"status": "error", "message": f"Checkpoint '{tag}' not found."}

        # 0. Detect external modifications if we have a workspace
        externally_modified: list[dict[str, str]] = []
        if workspace_root is not None:
            externally_modified = self.detect_external_modifications(
                session, tag, workspace_root,
            )
        skipped_paths: set[str] = set()
        if skip_externally_modified and externally_modified:
            skipped_paths = {em["path"] for em in externally_modified}

        # 1. Revert file changes: undo everything after the checkpoint's undo_stack
        checkpoint_undo_len = len(checkpoint.get("undo_stack", []))
        current_undo_len = len(session.undo_stack)
        files_reverted = 0
        files_skipped = 0

        if workspace_root is not None and current_undo_len > checkpoint_undo_len:
            workspace_root = _Path(workspace_root).resolve()
            # Walk backwards through undo entries after the checkpoint
            for entry_index in range(current_undo_len - 1, checkpoint_undo_len - 1, -1):
                entry = session.undo_stack[entry_index]
                for file_record in reversed(entry.get("files", [])):
                    relative_path = str(file_record.get("path", "")).strip()
                    if not relative_path:
                        continue
                    file_path = (workspace_root / relative_path).resolve()
                    if not file_path.is_relative_to(workspace_root):
                        continue
                    if relative_path in skipped_paths:
                        files_skipped += 1
                        continue
                    existed_before = bool(file_record.get("existed_before"))
                    previous_content = str(file_record.get("previous_content", ""))
                    if existed_before:
                        atomic_write_text(file_path, previous_content)
                    elif file_path.exists():
                        file_path.unlink()
                    files_reverted += 1

        # 2. Restore session state from checkpoint
        session.messages = list(checkpoint.get("messages_snapshot", []))
        checkpoint_state = checkpoint.get("session_state", {})
        session.token_usage = dict(checkpoint_state.get("token_usage", {}))
        session.todo_items = list(checkpoint_state.get("todo_items", []))
        session.rounds_without_todo = int(checkpoint_state.get("rounds_without_todo", 0))
        session.read_file_overlap_state = deepcopy(checkpoint_state.get("read_file_overlap_state", {}))
        session.latest_turn_id = checkpoint_state.get("latest_turn_id")
        session.last_turn_file_changes = list(checkpoint_state.get("last_turn_file_changes", []))
        try:
            session.undo_stack = deepcopy(checkpoint.get("undo_stack", []))
        except Exception:
            session.undo_stack = json.loads(
                json.dumps(checkpoint.get("undo_stack", []), ensure_ascii=False, default=str)
            )
        session.pending_file_changes = []

        # 3. Save the rolled-back session
        self.save(session)

        # 4. Delete orphaned checkpoints created after the target
        deleted_checkpoints = self.transcript_store.delete_checkpoints_after(session.id, tag)

        return {
            "status": "ok",
            "tag": tag,
            "messages_restored": len(session.messages),
            "files_reverted": files_reverted,
            "files_skipped": files_skipped,
            "undo_entries_removed": current_undo_len - checkpoint_undo_len,
            "orphaned_checkpoints_deleted": deleted_checkpoints,
            "external_modifications": externally_modified,
        }
