"""Tests for the checkpoint / rollback system.

Covers TranscriptStore checkpoint persistence, SessionManager checkpoint
creation and rollback (including file revert), and multi-turn scenarios
with complex file modifications.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from copy import deepcopy

from open_somnia.runtime.session import AgentSession, SessionManager
from open_somnia.storage.sessions import SessionStore
from open_somnia.storage.transcripts import TranscriptStore


def _make_session_manager(tmpdir: Path) -> SessionManager:
    sessions_dir = tmpdir / "sessions"
    transcripts_dir = tmpdir / "transcripts"
    return SessionManager(
        SessionStore(sessions_dir),
        TranscriptStore(transcripts_dir),
    )


def _make_session(sm: SessionManager) -> AgentSession:
    return sm.create()


class TranscriptStoreCheckpointTests(unittest.TestCase):
    """Test TranscriptStore checkpoint persistence methods."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.store = TranscriptStore(self.tmpdir)

    def test_save_and_load_checkpoint_roundtrip(self) -> None:
        payload = {
            "tag": "v1",
            "timestamp": 1000.0,
            "message_count": 5,
            "messages_snapshot": [{"role": "user", "content": "hello"}],
            "undo_stack": [{"turn_id": "a1", "files": []}],
        }
        self.store.save_checkpoint("sess1", "v1", payload)
        loaded = self.store.load_checkpoint("sess1", "v1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["tag"], "v1")
        self.assertEqual(loaded["message_count"], 5)
        self.assertEqual(loaded["messages_snapshot"], [{"role": "user", "content": "hello"}])

    def test_load_nonexistent_checkpoint_returns_none(self) -> None:
        result = self.store.load_checkpoint("sess1", "nonexistent")
        self.assertIsNone(result)

    def test_list_checkpoints_returns_sorted_by_timestamp(self) -> None:
        for i, ts in enumerate([1000.0, 3000.0, 2000.0], start=1):
            self.store.save_checkpoint("sess1", f"cp{i}", {
                "tag": f"cp{i}",
                "timestamp": ts,
                "message_count": i * 10,
                "undo_stack": [],
            })
        checkpoints = self.store.list_checkpoints("sess1")
        self.assertEqual(len(checkpoints), 3)
        self.assertEqual(checkpoints[0]["tag"], "cp1")
        self.assertEqual(checkpoints[1]["tag"], "cp3")
        self.assertEqual(checkpoints[2]["tag"], "cp2")

    def test_list_checkpoints_empty_when_none(self) -> None:
        checkpoints = self.store.list_checkpoints("sess1")
        self.assertEqual(checkpoints, [])

    def test_checkpoint_path_sanitizes_special_characters(self) -> None:
        path = self.store.checkpoint_path("sess1", "before/refactor!")
        self.assertIn("sess1.checkpoint.b64_", str(path))

    def test_checkpoint_path_does_not_collide_for_distinct_special_tags(self) -> None:
        first = self.store.checkpoint_path("sess1", "a/b")
        second = self.store.checkpoint_path("sess1", "a?b")
        self.assertNotEqual(first, second)

    def test_delete_checkpoints_after_removes_later_ones(self) -> None:
        self.store.save_checkpoint("sess1", "early", {
            "tag": "early", "timestamp": 1000.0, "message_count": 1, "undo_stack": [],
        })
        self.store.save_checkpoint("sess1", "middle", {
            "tag": "middle", "timestamp": 2000.0, "message_count": 2, "undo_stack": [],
        })
        self.store.save_checkpoint("sess1", "late", {
            "tag": "late", "timestamp": 3000.0, "message_count": 3, "undo_stack": [],
        })
        deleted = self.store.delete_checkpoints_after("sess1", "early")
        self.assertEqual(deleted, 2)
        remaining = self.store.list_checkpoints("sess1")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["tag"], "early")

    def test_delete_checkpoints_after_nonexistent_returns_zero(self) -> None:
        deleted = self.store.delete_checkpoints_after("sess1", "nonexistent")
        self.assertEqual(deleted, 0)

    def test_list_checkpoints_includes_file_count(self) -> None:
        self.store.save_checkpoint("sess1", "v1", {
            "tag": "v1", "timestamp": 1000.0, "message_count": 5,
            "last_user_message": "hello world",
            "undo_stack": [
                {"turn_id": "t1", "files": [
                    {"path": "a.py", "previous_content": "", "existed_before": False},
                    {"path": "b.py", "previous_content": "old", "existed_before": True},
                ]},
            ],
        })
        checkpoints = self.store.list_checkpoints("sess1")
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["file_count"], 2)
        self.assertEqual(checkpoints[0]["last_user_message"], "hello world")

    def test_list_checkpoints_includes_empty_last_user_message_when_absent(self) -> None:
        self.store.save_checkpoint("sess1", "v1", {
            "tag": "v1", "timestamp": 1000.0, "message_count": 3,
            "undo_stack": [],
        })
        checkpoints = self.store.list_checkpoints("sess1")
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["last_user_message"], "")

    def test_save_and_load_distinct_special_tags_roundtrip(self) -> None:
        self.store.save_checkpoint("sess1", "a/b", {
            "tag": "a/b", "timestamp": 1000.0, "message_count": 1, "undo_stack": [],
        })
        self.store.save_checkpoint("sess1", "a?b", {
            "tag": "a?b", "timestamp": 2000.0, "message_count": 2, "undo_stack": [],
        })

        first = self.store.load_checkpoint("sess1", "a/b")
        second = self.store.load_checkpoint("sess1", "a?b")

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first["tag"], "a/b")
        self.assertEqual(second["tag"], "a?b")
        self.assertEqual(len(self.store.list_checkpoints("sess1")), 2)


class SessionManagerCheckpointTests(unittest.TestCase):
    """Test SessionManager checkpoint and rollback without files."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sm = _make_session_manager(self.tmpdir)
        self.session = _make_session(self.sm)

    def test_create_checkpoint_returns_metadata(self) -> None:
        self.session.messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = self.sm.create_checkpoint(self.session, "before_work")
        self.assertEqual(result["tag"], "before_work")
        self.assertEqual(result["message_count"], 2)
        self.assertEqual(result["file_count"], 0)
        self.assertEqual(result["last_user_message"], "hello")

    def test_create_checkpoint_captures_last_user_message(self) -> None:
        self.session.messages = [
            {"role": "assistant", "content": "greeting"},
            {"role": "user", "content": "implement feature X"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "now do Y"},
        ]
        result = self.sm.create_checkpoint(self.session, "cp1")
        self.assertEqual(result["last_user_message"], "now do Y")

    def test_create_checkpoint_empty_when_no_user_messages(self) -> None:
        self.session.messages = [
            {"role": "assistant", "content": "greeting"},
        ]
        result = self.sm.create_checkpoint(self.session, "cp1")
        self.assertEqual(result["last_user_message"], "")

    def test_create_checkpoint_handles_multimodal_content(self) -> None:
        self.session.messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "look at this image"},
                {"type": "image_url", "image_url": {"url": "http://example.com/img.png"}},
            ]},
        ]
        result = self.sm.create_checkpoint(self.session, "cp1")
        self.assertEqual(result["last_user_message"], "look at this image")

    def test_create_checkpoint_truncates_long_user_message(self) -> None:
        long_msg = "x" * 200
        self.session.messages = [
            {"role": "user", "content": long_msg},
        ]
        result = self.sm.create_checkpoint(self.session, "cp1")
        # Full message is stored, truncation is display-only in REPL
        self.assertEqual(result["last_user_message"], long_msg)

    def test_create_checkpoint_empty_tag_preserved(self) -> None:
        self.session.messages = [{"role": "user", "content": "hello"}]
        result = self.sm.create_checkpoint(self.session, "")
        # SessionManager doesn't auto-generate tags; that's agent.py's job.
        # It should accept empty tag as-is.
        self.assertEqual(result["tag"], "")

    def test_create_checkpoint_with_undo_stack(self) -> None:
        self.session.messages = [{"role": "user", "content": "hello"}]
        self.session.undo_stack = [
            {"turn_id": "t1", "files": [
                {"path": "src/main.py", "previous_content": "original", "existed_before": True},
            ]},
        ]
        result = self.sm.create_checkpoint(self.session, "v1")
        self.assertEqual(result["file_count"], 1)

    def test_rollback_restores_messages(self) -> None:
        self.session.messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        self.sm.create_checkpoint(self.session, "v1")

        # Simulate more conversation
        self.session.messages.append({"role": "user", "content": "do work"})
        self.session.messages.append({"role": "assistant", "content": "done"})
        self.assertEqual(len(self.session.messages), 4)

        result = self.sm.rollback_to_checkpoint(self.session, "v1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(self.session.messages), 2)
        self.assertEqual(self.session.messages[0]["content"], "hello")

    def test_rollback_restores_session_state(self) -> None:
        self.session.todo_items = [{"content": "task1", "status": "pending"}]
        self.session.rounds_without_todo = 3
        self.session.token_usage = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
        self.sm.create_checkpoint(self.session, "state_cp")

        # Mutate state
        self.session.todo_items = []
        self.session.rounds_without_todo = 99
        self.session.token_usage = {"input_tokens": 9999}

        self.sm.rollback_to_checkpoint(self.session, "state_cp")
        self.assertEqual(len(self.session.todo_items), 1)
        self.assertEqual(self.session.todo_items[0]["content"], "task1")
        self.assertEqual(self.session.rounds_without_todo, 3)
        self.assertEqual(self.session.token_usage["input_tokens"], 100)

    def test_rollback_nonexistent_checkpoint_returns_error(self) -> None:
        result = self.sm.rollback_to_checkpoint(self.session, "nope")
        self.assertEqual(result["status"], "error")
        self.assertIn("not found", result["message"])

    def test_rollback_clears_pending_file_changes(self) -> None:
        self.session.pending_file_changes = [{"path": "temp.py", "previous_content": ""}]
        self.sm.create_checkpoint(self.session, "v1")
        self.sm.rollback_to_checkpoint(self.session, "v1")
        self.assertEqual(self.session.pending_file_changes, [])

    def test_rollback_deletes_orphaned_later_checkpoints(self) -> None:
        import time
        self.session.messages = [{"role": "user", "content": "step1"}]
        self.sm.create_checkpoint(self.session, "cp1")

        self.session.messages.append({"role": "assistant", "content": "reply1"})
        # Small sleep to ensure different timestamps
        time.sleep(0.01)
        self.sm.create_checkpoint(self.session, "cp2")

        self.session.messages.append({"role": "user", "content": "step2"})
        time.sleep(0.01)
        self.sm.create_checkpoint(self.session, "cp3")

        result = self.sm.rollback_to_checkpoint(self.session, "cp1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["orphaned_checkpoints_deleted"], 2)

        remaining = self.sm.list_checkpoints(self.session)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["tag"], "cp1")

    def test_checkpoint_deepcopies_undo_stack(self) -> None:
        """Verify checkpoint doesn't share references with live undo_stack."""
        self.session.undo_stack = [
            {"turn_id": "t1", "files": [{"path": "a.py", "previous_content": "v1"}]},
        ]
        self.sm.create_checkpoint(self.session, "cp1")

        # Mutate the live undo_stack
        self.session.undo_stack.append({"turn_id": "t2", "files": []})
        self.session.undo_stack[0]["files"][0]["previous_content"] = "mutated"

        # Rollback should restore the original undo_stack
        self.sm.rollback_to_checkpoint(self.session, "cp1")
        self.assertEqual(len(self.session.undo_stack), 1)
        self.assertEqual(self.session.undo_stack[0]["files"][0]["previous_content"], "v1")


class SessionManagerFileRollbackTests(unittest.TestCase):
    """Test file-level rollback with actual filesystem operations."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.workspace = self.tmpdir / "workspace"
        self.workspace.mkdir()
        self.sm = _make_session_manager(self.tmpdir)
        self.session = _make_session(self.sm)

    def test_rollback_reverts_modified_file(self) -> None:
        """Create a file, checkpoint, modify it, then rollback."""
        # Create initial file
        file_path = self.workspace / "src" / "main.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("original content", encoding="utf-8")

        # Set up session state with undo_stack as if AI modified the file
        self.session.messages = [{"role": "user", "content": "hello"}]
        self.session.undo_stack = []
        self.sm.create_checkpoint(self.session, "before_edit")

        # Simulate AI editing the file
        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{
                "path": "src/main.py",
                "previous_content": "original content",
                "existed_before": True,
            }],
        })
        file_path.write_text("modified content", encoding="utf-8")
        self.session.messages.append({"role": "assistant", "content": "edited"})

        # Rollback
        result = self.sm.rollback_to_checkpoint(
            self.session, "before_edit", workspace_root=self.workspace,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files_reverted"], 1)
        self.assertEqual(file_path.read_text(encoding="utf-8"), "original content")

    def test_rollback_deletes_newly_created_file(self) -> None:
        """File created after checkpoint should be deleted on rollback."""
        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "v1")

        # Simulate AI creating a new file
        new_file = self.workspace / "new_module.py"
        new_file.write_text("new content", encoding="utf-8")
        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{
                "path": "new_module.py",
                "previous_content": "",
                "existed_before": False,
            }],
        })

        self.assertTrue(new_file.exists())
        result = self.sm.rollback_to_checkpoint(
            self.session, "v1", workspace_root=self.workspace,
        )
        self.assertEqual(result["status"], "ok")
        self.assertFalse(new_file.exists())

    def test_rollback_multiple_files_across_multiple_turns(self) -> None:
        """Complex scenario: 3 turns, each modifying different files, then rollback."""
        # Initial files
        (self.workspace / "a.py").write_text("a_v1", encoding="utf-8")
        (self.workspace / "b.py").write_text("b_v1", encoding="utf-8")
        (self.workspace / "c.py").write_text("c_v1", encoding="utf-8")

        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "start")

        # Turn 1: modify a.py
        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "a.py", "previous_content": "a_v1", "existed_before": True}],
        })
        (self.workspace / "a.py").write_text("a_v2", encoding="utf-8")
        self.session.messages.append({"role": "assistant", "content": "edited a"})

        # Turn 2: modify b.py and create d.py
        self.session.undo_stack.append({
            "turn_id": "t2",
            "files": [
                {"path": "b.py", "previous_content": "b_v1", "existed_before": True},
                {"path": "d.py", "previous_content": "", "existed_before": False},
            ],
        })
        (self.workspace / "b.py").write_text("b_v2", encoding="utf-8")
        (self.workspace / "d.py").write_text("d_new", encoding="utf-8")
        self.session.messages.append({"role": "user", "content": "edit b and create d"})

        # Turn 3: modify a.py again and c.py
        self.session.undo_stack.append({
            "turn_id": "t3",
            "files": [
                {"path": "a.py", "previous_content": "a_v2", "existed_before": True},
                {"path": "c.py", "previous_content": "c_v1", "existed_before": True},
            ],
        })
        (self.workspace / "a.py").write_text("a_v3", encoding="utf-8")
        (self.workspace / "c.py").write_text("c_v2", encoding="utf-8")
        self.session.messages.append({"role": "assistant", "content": "edited a,c"})

        # Verify pre-rollback state
        self.assertEqual((self.workspace / "a.py").read_text(), "a_v3")
        self.assertEqual((self.workspace / "b.py").read_text(), "b_v2")
        self.assertEqual((self.workspace / "c.py").read_text(), "c_v2")
        self.assertTrue((self.workspace / "d.py").exists())
        self.assertEqual(len(self.session.messages), 4)

        # Rollback to "start"
        result = self.sm.rollback_to_checkpoint(
            self.session, "start", workspace_root=self.workspace,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files_reverted"], 5)  # 1 + 2 + 2
        self.assertEqual(result["undo_entries_removed"], 3)

        # Verify file contents restored
        self.assertEqual((self.workspace / "a.py").read_text(), "a_v1")
        self.assertEqual((self.workspace / "b.py").read_text(), "b_v1")
        self.assertEqual((self.workspace / "c.py").read_text(), "c_v1")
        self.assertFalse((self.workspace / "d.py").exists())

        # Verify messages truncated
        self.assertEqual(len(self.session.messages), 1)
        self.assertEqual(self.session.messages[0]["content"], "start")

    def test_rollback_without_workspace_root_skips_file_revert(self) -> None:
        """Rollback without workspace_root should not touch files."""
        file_path = self.workspace / "test.py"
        file_path.write_text("original", encoding="utf-8")

        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "v1")

        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "test.py", "previous_content": "original", "existed_before": True}],
        })
        file_path.write_text("modified", encoding="utf-8")

        result = self.sm.rollback_to_checkpoint(self.session, "v1")
        # File revert skipped (no workspace_root)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files_reverted"], 0)
        # File on disk is still modified
        self.assertEqual(file_path.read_text(), "modified")

    def test_rollback_does_not_escape_workspace(self) -> None:
        """Paths outside workspace should be skipped during revert."""
        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "v1")

        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "../../etc/passwd", "previous_content": "bad", "existed_before": True}],
        })

        result = self.sm.rollback_to_checkpoint(
            self.session, "v1", workspace_root=self.workspace,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files_reverted"], 0)  # path escapes workspace, skipped


class MultiTurnRollbackScenarioTests(unittest.TestCase):
    """End-to-end multi-turn scenarios simulating real usage patterns."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.workspace = self.tmpdir / "project"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
        (self.workspace / "utils.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
        self.sm = _make_session_manager(self.tmpdir)
        self.session = _make_session(self.sm)

    def test_checkpoint_rollback_then_continue_and_rollback_again(self) -> None:
        """Create cp1 -> work -> create cp2 -> work -> rollback to cp1 -> continue -> rollback to new cp."""
        # Initial state
        self.session.messages = [
            {"role": "user", "content": "implement feature A"},
        ]
        self.sm.create_checkpoint(self.session, "cp1")

        # Turn 1: modify app.py
        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "app.py", "previous_content": "def main():\n    pass\n", "existed_before": True}],
        })
        (self.workspace / "app.py").write_text("def main():\n    print('feature A')\n", encoding="utf-8")
        self.session.messages.append({"role": "assistant", "content": "implemented A"})

        import time
        time.sleep(0.01)
        self.sm.create_checkpoint(self.session, "cp2")

        # Turn 2: modify utils.py
        self.session.undo_stack.append({
            "turn_id": "t2",
            "files": [{"path": "utils.py", "previous_content": "def helper():\n    return 1\n", "existed_before": True}],
        })
        (self.workspace / "utils.py").write_text("def helper():\n    return 42\n", encoding="utf-8")
        self.session.messages.append({"role": "user", "content": "optimize helper"})

        # Rollback to cp1
        result = self.sm.rollback_to_checkpoint(
            self.session, "cp1", workspace_root=self.workspace,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files_reverted"], 2)
        self.assertEqual((self.workspace / "app.py").read_text(), "def main():\n    pass\n")
        self.assertEqual((self.workspace / "utils.py").read_text(), "def helper():\n    return 1\n")
        self.assertEqual(len(self.session.messages), 1)
        # cp2 should be deleted (orphaned)
        remaining = self.sm.list_checkpoints(self.session)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["tag"], "cp1")

        # Continue from cp1 with new direction
        self.session.messages.append({"role": "user", "content": "implement feature B"})
        self.session.undo_stack.append({
            "turn_id": "t3",
            "files": [{"path": "app.py", "previous_content": "def main():\n    pass\n", "existed_before": True}],
        })
        (self.workspace / "app.py").write_text("def main():\n    print('feature B')\n", encoding="utf-8")
        self.session.messages.append({"role": "assistant", "content": "implemented B"})

        time.sleep(0.01)
        self.sm.create_checkpoint(self.session, "cp3")

        # Verify new state
        self.assertEqual((self.workspace / "app.py").read_text(), "def main():\n    print('feature B')\n")
        self.assertEqual(len(self.session.messages), 3)

        # Rollback to cp1 again
        result = self.sm.rollback_to_checkpoint(
            self.session, "cp1", workspace_root=self.workspace,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files_reverted"], 1)
        self.assertEqual((self.workspace / "app.py").read_text(), "def main():\n    pass\n")
        self.assertEqual(len(self.session.messages), 1)

    def test_rollback_to_intermediate_checkpoint(self) -> None:
        """cp1 -> work -> cp2 -> work -> rollback to cp2 (not cp1)."""
        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "cp1")

        # Turn 1
        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "app.py", "previous_content": "def main():\n    pass\n", "existed_before": True}],
        })
        (self.workspace / "app.py").write_text("v2", encoding="utf-8")
        self.session.messages.append({"role": "assistant", "content": "step 1"})

        import time
        time.sleep(0.01)
        self.sm.create_checkpoint(self.session, "cp2")

        # Turn 2
        self.session.undo_stack.append({
            "turn_id": "t2",
            "files": [{"path": "utils.py", "previous_content": "def helper():\n    return 1\n", "existed_before": True}],
        })
        (self.workspace / "utils.py").write_text("v2", encoding="utf-8")
        self.session.messages.append({"role": "user", "content": "step 2"})

        # Rollback to cp2 (not cp1) — should only revert turn 2
        result = self.sm.rollback_to_checkpoint(
            self.session, "cp2", workspace_root=self.workspace,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files_reverted"], 1)
        self.assertEqual(result["undo_entries_removed"], 1)

        # app.py should still be v2 (from turn 1), utils.py reverted
        self.assertEqual((self.workspace / "app.py").read_text(), "v2")
        self.assertEqual((self.workspace / "utils.py").read_text(), "def helper():\n    return 1\n")
        self.assertEqual(len(self.session.messages), 2)

    def test_multiple_checkpoints_same_tag_overwrites(self) -> None:
        """Creating checkpoint with same tag should overwrite."""
        self.session.messages = [{"role": "user", "content": "v1"}]
        self.sm.create_checkpoint(self.session, "my_tag")

        self.session.messages.append({"role": "assistant", "content": "v2"})
        self.sm.create_checkpoint(self.session, "my_tag")

        # Should only have one checkpoint with that tag
        checkpoints = self.sm.list_checkpoints(self.session)
        tags = [cp["tag"] for cp in checkpoints]
        self.assertEqual(tags.count("my_tag"), 1)

    def test_rollback_preserves_checkpoint_itself(self) -> None:
        """After rollback, the target checkpoint should still exist."""
        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "keep_me")
        self.session.messages.append({"role": "assistant", "content": "more"})

        self.sm.rollback_to_checkpoint(self.session, "keep_me")
        checkpoints = self.sm.list_checkpoints(self.session)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["tag"], "keep_me")

    def test_sequential_rollback_chain(self) -> None:
        """cp1 -> work -> cp2 -> work -> cp3 -> work -> rollback to cp1."""
        (self.workspace / "log.py").write_text("v0", encoding="utf-8")

        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "cp1")

        # Turn 1
        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "log.py", "previous_content": "v0", "existed_before": True}],
        })
        (self.workspace / "log.py").write_text("v1", encoding="utf-8")
        self.session.messages.append({"role": "assistant", "content": "step1"})

        import time
        time.sleep(0.01)
        self.sm.create_checkpoint(self.session, "cp2")

        # Turn 2
        self.session.undo_stack.append({
            "turn_id": "t2",
            "files": [{"path": "log.py", "previous_content": "v1", "existed_before": True}],
        })
        (self.workspace / "log.py").write_text("v2", encoding="utf-8")
        self.session.messages.append({"role": "user", "content": "step2"})

        time.sleep(0.01)
        self.sm.create_checkpoint(self.session, "cp3")

        # Turn 3
        self.session.undo_stack.append({
            "turn_id": "t3",
            "files": [{"path": "log.py", "previous_content": "v2", "existed_before": True}],
        })
        (self.workspace / "log.py").write_text("v3", encoding="utf-8")
        self.session.messages.append({"role": "assistant", "content": "step3"})

        # Rollback all the way to cp1
        result = self.sm.rollback_to_checkpoint(
            self.session, "cp1", workspace_root=self.workspace,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files_reverted"], 3)
        self.assertEqual((self.workspace / "log.py").read_text(), "v0")
        self.assertEqual(len(self.session.messages), 1)

        # cp2 and cp3 should be deleted
        remaining = self.sm.list_checkpoints(self.session)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["tag"], "cp1")


class AgentRuntimeCheckpointMethodTests(unittest.TestCase):
    """Test the convenience methods on OpenAgentRuntime."""

    def test_checkpoint_session_auto_tag(self) -> None:
        from open_somnia.runtime.agent import OpenAgentRuntime

        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        tmpdir = Path(tempfile.mkdtemp())
        runtime.session_manager = _make_session_manager(tmpdir)
        session = runtime.session_manager.create()
        session.messages = [{"role": "user", "content": "hi"}]

        result = runtime.checkpoint_session(session, "")
        self.assertTrue(result["tag"].startswith("checkpoint_"))
        self.assertEqual(result["message_count"], 1)

    def test_rollback_session_passes_workspace_root(self) -> None:
        from open_somnia.runtime.agent import OpenAgentRuntime

        tmpdir = Path(tempfile.mkdtemp())
        workspace = tmpdir / "ws"
        workspace.mkdir()

        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.session_manager = _make_session_manager(tmpdir)
        runtime.settings = SimpleNamespace(workspace_root=workspace)

        session = runtime.session_manager.create()
        session.messages = [{"role": "user", "content": "start"}]

        # Create a file and checkpoint
        (workspace / "f.py").write_text("original", encoding="utf-8")
        runtime.checkpoint_session(session, "v1")

        # Simulate edit
        session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "f.py", "previous_content": "original", "existed_before": True}],
        })
        (workspace / "f.py").write_text("modified", encoding="utf-8")

        result = runtime.rollback_session(session, "v1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual((workspace / "f.py").read_text(), "original")

    def test_list_checkpoints_delegates(self) -> None:
        from open_somnia.runtime.agent import OpenAgentRuntime

        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        tmpdir = Path(tempfile.mkdtemp())
        runtime.session_manager = _make_session_manager(tmpdir)
        session = runtime.session_manager.create()
        session.messages = [{"role": "user", "content": "hi"}]

        runtime.checkpoint_session(session, "test_cp")
        checkpoints = runtime.list_checkpoints(session)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["tag"], "test_cp")


class ExternalModificationDetectionTests(unittest.TestCase):
    """Test detect_external_modifications and its interaction with rollback."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.workspace = self.tmpdir / "workspace"
        self.workspace.mkdir()
        self.sm = _make_session_manager(self.tmpdir)
        self.session = self.sm.create()

    # ── Detection tests ────────────────────────────────────────────────

    def test_detect_no_modifications_when_no_undo_entries(self) -> None:
        """No undo entries after checkpoint → no external modifications."""
        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "cp1")
        mods = self.sm.detect_external_modifications(self.session, "cp1", self.workspace)
        self.assertEqual(mods, [])

    def test_detect_no_modifications_when_file_unchanged_after_agent_edit(self) -> None:
        """Agent edited file, user didn't touch it → agent writes still present on disk."""
        file_path = self.workspace / "a.py"
        file_path.write_text("original", encoding="utf-8")

        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "cp1")

        # Agent modifies file (undo records previous_content="original")
        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "a.py", "previous_content": "original", "existed_before": True}],
        })
        # Agent wrote "modified" to disk (simulated)
        file_path.write_text("modified", encoding="utf-8")

        # Disk content != previous_content("original"), so NOT flagged as externally reverted
        mods = self.sm.detect_external_modifications(self.session, "cp1", self.workspace)
        # The agent wrote "modified" which differs from previous_content "original"
        # so this should NOT be flagged (the write is still present)
        self.assertEqual(len(mods), 0)

    def test_detect_external_revert_of_agent_write(self) -> None:
        """Agent edited file, user reverted it to pre-agent state → detected."""
        file_path = self.workspace / "a.py"
        file_path.write_text("original", encoding="utf-8")

        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "cp1")

        # Agent modifies file
        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "a.py", "previous_content": "original", "existed_before": True}],
        })
        file_path.write_text("modified", encoding="utf-8")

        # User externally reverts the file back to "original"
        file_path.write_text("original", encoding="utf-8")

        mods = self.sm.detect_external_modifications(self.session, "cp1", self.workspace)
        self.assertEqual(len(mods), 1)
        self.assertEqual(mods[0]["path"], "a.py")
        self.assertEqual(mods[0]["reason"], "content_mismatch")

    def test_detect_externally_deleted_agent_created_file(self) -> None:
        """Agent created a file, user deleted it → detected as unexpectedly_missing."""
        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "cp1")

        # Agent creates file
        new_file = self.workspace / "new.py"
        new_file.write_text("agent content", encoding="utf-8")
        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "new.py", "previous_content": "", "existed_before": False}],
        })

        # User deletes it
        new_file.unlink()

        mods = self.sm.detect_external_modifications(self.session, "cp1", self.workspace)
        self.assertEqual(len(mods), 1)
        self.assertEqual(mods[0]["path"], "new.py")
        self.assertEqual(mods[0]["reason"], "unexpectedly_missing")

    def test_detect_externally_deleted_existing_file(self) -> None:
        """File existed before agent edited it, user deleted it → detected."""
        file_path = self.workspace / "b.py"
        file_path.write_text("v1", encoding="utf-8")

        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "cp1")

        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "b.py", "previous_content": "v1", "existed_before": True}],
        })
        file_path.write_text("v2", encoding="utf-8")

        # User deletes the file
        file_path.unlink()

        mods = self.sm.detect_external_modifications(self.session, "cp1", self.workspace)
        self.assertEqual(len(mods), 1)
        self.assertEqual(mods[0]["reason"], "unexpectedly_missing")

    def test_detect_nonexistent_checkpoint_returns_empty(self) -> None:
        mods = self.sm.detect_external_modifications(self.session, "nope", self.workspace)
        self.assertEqual(mods, [])

    def test_detect_no_modification_when_agent_restores_original_content(self) -> None:
        """Multiple agent writes ending at the original content should not be flagged."""
        file_path = self.workspace / "a.py"
        file_path.write_text("v1", encoding="utf-8")

        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "cp1")

        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "a.py", "previous_content": "v1", "existed_before": True}],
        })
        file_path.write_text("v2", encoding="utf-8")

        self.session.undo_stack.append({
            "turn_id": "t2",
            "files": [{"path": "a.py", "previous_content": "v2", "existed_before": True}],
        })
        file_path.write_text("v1", encoding="utf-8")

        mods = self.sm.detect_external_modifications(self.session, "cp1", self.workspace)
        self.assertEqual(mods, [])

    # ── Rollback with skip_externally_modified tests ───────────────────

    def test_rollback_with_skip_preserves_externally_modified_files(self) -> None:
        """skip_externally_modified=True should not revert externally changed files."""
        file_a = self.workspace / "a.py"
        file_b = self.workspace / "b.py"
        file_a.write_text("a_original", encoding="utf-8")
        file_b.write_text("b_original", encoding="utf-8")

        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "cp1")

        # Agent modifies both files
        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [
                {"path": "a.py", "previous_content": "a_original", "existed_before": True},
                {"path": "b.py", "previous_content": "b_original", "existed_before": True},
            ],
        })
        file_a.write_text("a_agent", encoding="utf-8")
        file_b.write_text("b_agent", encoding="utf-8")

        # User externally reverts only a.py back to original
        file_a.write_text("a_original", encoding="utf-8")
        # b.py still has agent content

        result = self.sm.rollback_to_checkpoint(
            self.session, "cp1",
            workspace_root=self.workspace,
            skip_externally_modified=True,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files_reverted"], 1)  # only b.py reverted
        self.assertEqual(result["files_skipped"], 1)  # a.py skipped
        # a.py keeps its externally-modified content
        self.assertEqual(file_a.read_text(), "a_original")
        # b.py reverted to pre-checkpoint state
        self.assertEqual(file_b.read_text(), "b_original")

    def test_rollback_without_skip_overwrites_externally_modified_files(self) -> None:
        """skip_externally_modified=False (default) overwrites external changes."""
        file_a = self.workspace / "a.py"
        file_a.write_text("original", encoding="utf-8")

        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "cp1")

        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "a.py", "previous_content": "original", "existed_before": True}],
        })
        file_a.write_text("agent_edit", encoding="utf-8")

        # User externally modifies
        file_a.write_text("user_manual_edit", encoding="utf-8")

        result = self.sm.rollback_to_checkpoint(
            self.session, "cp1",
            workspace_root=self.workspace,
            skip_externally_modified=False,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files_reverted"], 1)
        self.assertEqual(result["files_skipped"], 0)
        # File reverted to checkpoint's previous_content (overwriting user edit)
        self.assertEqual(file_a.read_text(), "original")

    def test_rollback_result_includes_external_modifications_list(self) -> None:
        """Result dict should include external_modifications even when not skipping."""
        file_a = self.workspace / "a.py"
        file_a.write_text("original", encoding="utf-8")

        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "cp1")

        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "a.py", "previous_content": "original", "existed_before": True}],
        })
        file_a.write_text("agent_edit", encoding="utf-8")

        # External revert
        file_a.write_text("original", encoding="utf-8")

        result = self.sm.rollback_to_checkpoint(
            self.session, "cp1",
            workspace_root=self.workspace,
        )
        self.assertIn("external_modifications", result)
        self.assertEqual(len(result["external_modifications"]), 1)
        self.assertEqual(result["external_modifications"][0]["path"], "a.py")

    def test_rollback_with_skip_on_agent_created_then_externally_deleted_file(self) -> None:
        """Agent created file, user deleted it → skip should not try to delete again."""
        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "cp1")

        new_file = self.workspace / "new.py"
        new_file.write_text("agent content", encoding="utf-8")
        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [{"path": "new.py", "previous_content": "", "existed_before": False}],
        })

        # User deletes the file externally
        new_file.unlink()
        self.assertFalse(new_file.exists())

        result = self.sm.rollback_to_checkpoint(
            self.session, "cp1",
            workspace_root=self.workspace,
            skip_externally_modified=True,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files_skipped"], 1)
        # File should remain deleted (skipped)
        self.assertFalse(new_file.exists())

    def test_multiple_files_mixed_external_modifications(self) -> None:
        """Some files externally modified, some not → selective skipping."""
        for name in ["a.py", "b.py", "c.py"]:
            (self.workspace / name).write_text(f"{name}_original", encoding="utf-8")

        self.session.messages = [{"role": "user", "content": "start"}]
        self.sm.create_checkpoint(self.session, "cp1")

        # Agent modifies all three files
        self.session.undo_stack.append({
            "turn_id": "t1",
            "files": [
                {"path": "a.py", "previous_content": "a.py_original", "existed_before": True},
                {"path": "b.py", "previous_content": "b.py_original", "existed_before": True},
                {"path": "c.py", "previous_content": "c.py_original", "existed_before": True},
            ],
        })
        for name in ["a.py", "b.py", "c.py"]:
            (self.workspace / name).write_text(f"{name}_agent", encoding="utf-8")

        # User externally reverts only a.py and c.py
        (self.workspace / "a.py").write_text("a.py_original", encoding="utf-8")
        (self.workspace / "c.py").write_text("c.py_original", encoding="utf-8")
        # b.py still has agent content

        # Detect
        mods = self.sm.detect_external_modifications(self.session, "cp1", self.workspace)
        self.assertEqual(len(mods), 2)
        mod_paths = {m["path"] for m in mods}
        self.assertIn("a.py", mod_paths)
        self.assertIn("c.py", mod_paths)
        self.assertNotIn("b.py", mod_paths)

        # Rollback with skip
        result = self.sm.rollback_to_checkpoint(
            self.session, "cp1",
            workspace_root=self.workspace,
            skip_externally_modified=True,
        )
        self.assertEqual(result["files_reverted"], 1)  # only b.py
        self.assertEqual(result["files_skipped"], 2)  # a.py and c.py
        self.assertEqual((self.workspace / "a.py").read_text(), "a.py_original")  # externally reverted kept
        self.assertEqual((self.workspace / "b.py").read_text(), "b.py_original")  # agent reverted
        self.assertEqual((self.workspace / "c.py").read_text(), "c.py_original")  # externally reverted kept


if __name__ == "__main__":
    unittest.main()
