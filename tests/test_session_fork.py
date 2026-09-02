"""Tests for session forking (SessionManager.fork).

Covers message truncation at the fork point, deep-copy isolation between
parent and fork, lineage/provider-pin inheritance, fork-point validation,
and picker visibility of the forked session.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from open_somnia.runtime.session import SessionManager
from open_somnia.storage.sessions import SessionStore
from open_somnia.storage.transcripts import TranscriptStore


def _make_session_manager(tmpdir: Path) -> SessionManager:
    return SessionManager(
        SessionStore(tmpdir / "sessions"),
        TranscriptStore(tmpdir / "transcripts"),
    )


def _make_source(sm: SessionManager):
    source = sm.create()
    source.messages = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": [{"type": "text", "text": "first answer"}]},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": [{"type": "text", "text": "second answer"}]},
    ]
    source.provider_override = "openai"
    source.model_override = "gpt-test"
    sm.save(source)
    return source


class SessionForkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sm = _make_session_manager(self.tmpdir)
        self.source = _make_source(self.sm)

    def test_fork_truncates_messages_at_fork_point(self) -> None:
        forked = self.sm.fork(self.source.id, 2)
        self.assertEqual(forked.messages, self.source.messages[:2])
        self.assertNotEqual(forked.id, self.source.id)

    def test_fork_does_not_mutate_source(self) -> None:
        self.sm.fork(self.source.id, 2)
        reloaded = self.sm.load(self.source.id)
        self.assertEqual(len(reloaded.messages), 4)
        self.assertIsNone(reloaded.forked_from)

    def test_fork_deep_copies_messages(self) -> None:
        forked = self.sm.fork(self.source.id, 2)
        forked.messages[1]["content"][0]["text"] = "edited"
        reloaded = self.sm.load(self.source.id)
        self.assertEqual(reloaded.messages[1]["content"][0]["text"], "first answer")

    def test_fork_records_lineage_and_model_pin(self) -> None:
        forked = self.sm.fork(self.source.id, 3)
        self.assertEqual(forked.forked_from, self.source.id)
        self.assertEqual(forked.provider_override, "openai")
        self.assertEqual(forked.model_override, "gpt-test")
        reloaded = self.sm.load(forked.id)
        self.assertEqual(reloaded.forked_from, self.source.id)
        self.assertEqual(reloaded.provider_override, "openai")
        self.assertEqual(reloaded.model_override, "gpt-test")

    def test_fork_starts_with_fresh_ephemeral_state(self) -> None:
        self.source.token_usage = {"input": 100}
        self.source.todo_items = [{"content": "task", "status": "pending"}]
        self.source.undo_stack = [{"turn_id": "t1"}]
        self.sm.save(self.source)
        forked = self.sm.fork(self.source.id, 2)
        # A fork is a fresh session: token counters match a newly created
        # session (zeroed), and no todo/undo/board state carries over.
        self.assertEqual(forked.token_usage, self.sm.create().token_usage)
        self.assertEqual(forked.todo_items, [])
        self.assertEqual(forked.undo_stack, [])
        self.assertIsNone(forked.task_session_id)

    def test_fork_at_full_length_is_allowed(self) -> None:
        forked = self.sm.fork(self.source.id, 4)
        self.assertEqual(forked.messages, self.source.messages)

    def test_fork_rejects_out_of_range_message_count(self) -> None:
        for bad in (0, -1, 5):
            with self.assertRaises(ValueError):
                self.sm.fork(self.source.id, bad)

    def test_fork_rejects_unknown_session(self) -> None:
        with self.assertRaises(ValueError):
            self.sm.fork("does-not-exist", 1)

    def test_forked_session_appears_in_summaries(self) -> None:
        forked = self.sm.fork(self.source.id, 2)
        summaries = self.sm.list_summaries()
        ids = {summary["id"] for summary in summaries}
        self.assertIn(forked.id, ids)
        self.assertIn(self.source.id, ids)

    def test_fork_survives_parent_deletion(self) -> None:
        forked = self.sm.fork(self.source.id, 2)
        self.sm.delete(self.source.id)
        reloaded = self.sm.load(forked.id)
        self.assertEqual(len(reloaded.messages), 2)
        self.assertEqual(reloaded.forked_from, self.source.id)


if __name__ == "__main__":
    unittest.main()
