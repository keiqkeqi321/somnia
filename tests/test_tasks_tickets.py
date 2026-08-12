from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from open_somnia.storage.tasks import READY_FOR_AGENT, TaskStore
from open_somnia.tools.tasks import _render_task_list


class TaskTicketsTest(unittest.TestCase):
    def _tmp(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="somnia-task-test-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return directory

    # --- creation persists the ticket fields ---

    def test_create_persists_ticket_fields(self) -> None:
        store = TaskStore(self._tmp())
        task = store.create(
            "Build X",
            description="d",
            acceptance=["a", "b"],
            spec_id="orders-v2",
            labels=["ready-for-agent", "bug"],
        )
        self.assertEqual(task["acceptance"], ["a", "b"])
        self.assertEqual(task["acceptance_done"], [False, False])
        self.assertEqual(task["spec_id"], "orders-v2")
        self.assertEqual(task["labels"], ["ready-for-agent", "bug"])
        self.assertIsNone(task["result"])
        self.assertIsNone(task["commit_ref"])
        # round-trips through disk
        reloaded = store.get(task["id"])
        self.assertEqual(reloaded["acceptance"], ["a", "b"])
        self.assertEqual(reloaded["acceptance_done"], [False, False])

    def test_create_many_ticket_fields_and_blocking(self) -> None:
        store = TaskStore(self._tmp())
        tasks = store.create_many(
            [
                {"key": "a", "subject": "A", "acceptance": ["do A"], "spec_id": "feat", "labels": ["ready-for-agent"]},
                {"key": "b", "subject": "B", "blocked_by": ["a"], "acceptance": ["do B1", "do B2"]},
            ]
        )
        self.assertEqual(tasks[0]["labels"], ["ready-for-agent"])
        self.assertEqual(tasks[1]["acceptance"], ["do B1", "do B2"])
        self.assertEqual(tasks[1]["acceptance_done"], [False, False])
        self.assertEqual(tasks[1]["blockedBy"], [tasks[0]["id"]])

    # --- close discipline gate ---

    def test_close_gate_blocks_unchecked_acceptance(self) -> None:
        store = TaskStore(self._tmp())
        task = store.create("X", acceptance=["a", "b"])
        with self.assertRaises(ValueError):
            store.update(task["id"], status="completed")
        # on-disk status untouched because save() never ran
        self.assertEqual(store.get(task["id"])["status"], "pending")

    def test_close_gate_passes_when_all_checked_with_closure_notes(self) -> None:
        store = TaskStore(self._tmp())
        task = store.create("X", acceptance=["a", "b"])
        result = store.update(
            task["id"],
            status="completed",
            acceptance_done=[True, True],
            result="shipped slice 1",
            commit_ref="abc123",
        )
        assert result is not None
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["acceptance_done"], [True, True])
        self.assertEqual(result["result"], "shipped slice 1")
        self.assertEqual(result["commit_ref"], "abc123")

    def test_close_gate_empty_acceptance_allowed(self) -> None:
        store = TaskStore(self._tmp())
        task = store.create("X")  # no acceptance criteria
        result = store.update(task["id"], status="completed")
        assert result is not None
        self.assertEqual(result["status"], "completed")

    def test_acceptance_done_length_mismatch_raises(self) -> None:
        store = TaskStore(self._tmp())
        task = store.create("X", acceptance=["a", "b", "c"])
        with self.assertRaises(ValueError):
            store.update(task["id"], acceptance_done=[True, False])

    # --- legacy data is normalized on read ---

    def test_normalize_legacy_task(self) -> None:
        root = self._tmp()
        store = TaskStore(root)
        legacy = {
            "id": 1,
            "subject": "old",
            "description": "",
            "status": "pending",
            "owner": None,
            "preferred_owner": None,
            "session_id": None,
            "blockedBy": [],
            "created_at": 0,
            "updated_at": 0,
        }
        (root / "task_1.json").write_text(json.dumps(legacy), encoding="utf-8")
        reloaded = store.get(1)
        self.assertEqual(reloaded["acceptance"], [])
        self.assertEqual(reloaded["acceptance_done"], [])
        self.assertIsNone(reloaded["spec_id"])
        self.assertEqual(reloaded["labels"], [])
        self.assertIsNone(reloaded["result"])
        self.assertIsNone(reloaded["commit_ref"])
        listed = store.list_all()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["labels"], [])
        # a legacy task (empty acceptance) can still be closed
        closed = store.update(1, status="completed")
        assert closed is not None
        self.assertEqual(closed["status"], "completed")

    def test_normalize_pads_short_acceptance_done(self) -> None:
        root = self._tmp()
        store = TaskStore(root)
        # acceptance has 3 items but acceptance_done only has 1 -> padded
        weird = {
            "id": 1, "subject": "x", "description": "", "status": "pending", "owner": None,
            "preferred_owner": None, "session_id": None, "blockedBy": [],
            "acceptance": ["a", "b", "c"], "acceptance_done": [True],
            "created_at": 0, "updated_at": 0,
        }
        (root / "task_1.json").write_text(json.dumps(weird), encoding="utf-8")
        reloaded = store.get(1)
        self.assertEqual(reloaded["acceptance_done"], [True, False, False])

    # --- ready-for-agent frontier ---

    def test_list_ready_filters_by_label(self) -> None:
        store = TaskStore(self._tmp())
        ready = store.create("ready", labels=[READY_FOR_AGENT])
        unready = store.create("not-ready")  # claimable but not specced
        claimable_ids = {t["id"] for t in store.list_claimable()}
        self.assertEqual(claimable_ids, {ready["id"], unready["id"]})
        ready_ids = {t["id"] for t in store.list_ready()}
        self.assertEqual(ready_ids, {ready["id"]})

    # --- claim gate ---

    def test_claim_requires_ready_label(self) -> None:
        store = TaskStore(self._tmp())
        task = store.create("not-ready")
        # the gate is opt-in on the primitive (the claim_task tool turns it on)
        with self.assertRaises(ValueError):
            store.claim(task["id"], "alice", require_ready_label=True)
        # status untouched because save() never ran
        self.assertEqual(store.get(task["id"])["status"], "pending")
        # default primitive claim is policy-free and succeeds
        claimed = store.claim(task["id"], "alice")
        self.assertEqual(claimed["owner"], "alice")
        self.assertEqual(claimed["status"], "in_progress")

    def test_claim_allows_ready_label(self) -> None:
        store = TaskStore(self._tmp())
        task = store.create("ready", labels=[READY_FOR_AGENT])
        claimed = store.claim(task["id"], "alice")
        self.assertEqual(claimed["owner"], "alice")
        self.assertEqual(claimed["status"], "in_progress")

    # --- field update semantics ---

    def test_update_clears_and_replaces_fields(self) -> None:
        store = TaskStore(self._tmp())
        task = store.create("X", spec_id="feat", labels=["ready-for-agent", "bug"])
        # empty string clears a string field
        cleared = store.update(task["id"], spec_id="")
        assert cleared is not None
        self.assertIsNone(cleared["spec_id"])
        # labels replace the whole list
        replaced = store.update(task["id"], labels=["enhancement"])
        assert replaced is not None
        self.assertEqual(replaced["labels"], ["enhancement"])

    # --- rendering ---

    def test_render_task_list_shows_acceptance_and_ready(self) -> None:
        tasks = [
            {
                "id": 1, "subject": "X", "status": "in_progress", "owner": "alice",
                "preferred_owner": None, "blockedBy": [],
                "acceptance": ["a", "b"], "acceptance_done": [True, False],
                "labels": ["ready-for-agent", "bug"],
            }
        ]
        out = _render_task_list(tasks)
        self.assertIn("[1/2]", out)
        self.assertIn("(ready)", out)
        self.assertIn("{bug}", out)
        # the ready-for-agent label is shown as the (ready) marker, not duplicated in braces
        self.assertNotIn("ready-for-agent}", out)


if __name__ == "__main__":
    unittest.main()
