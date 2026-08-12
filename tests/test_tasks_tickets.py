from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from open_somnia.runtime.events import ToolExecutionContext
from open_somnia.storage.tasks import READY_FOR_AGENT, TaskStore
from open_somnia.tools.registry import ToolRegistry
from open_somnia.tools.tasks import _render_task_list, register_task_tools


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

    # --- L2: mutable dependency edges (add is free, remove is a separate gated tool) ---

    def test_add_blocked_by_via_update(self) -> None:
        store = TaskStore(self._tmp())
        a = store.create("A")
        b = store.create("B")
        updated = store.update(b["id"], add_blocked_by=[a["id"]])
        assert updated is not None
        self.assertEqual(updated["blockedBy"], [a["id"]])

    def test_add_blocked_by_validates_target_and_dedups(self) -> None:
        store = TaskStore(self._tmp())
        a = store.create("A")
        b = store.create("B")
        with self.assertRaises(ValueError):  # target must exist
            store.update(b["id"], add_blocked_by=[999])
        updated = store.update(b["id"], add_blocked_by=[a["id"], a["id"]])
        assert updated is not None
        self.assertEqual(updated["blockedBy"], [a["id"]])  # deduped

    def test_add_blocked_by_rejects_cycle(self) -> None:
        store = TaskStore(self._tmp())
        a = store.create("A")
        b = store.create("B")
        store.update(b["id"], add_blocked_by=[a["id"]])  # B depends on A
        with self.assertRaises(ValueError):  # A depending on B would cycle
            store.update(a["id"], add_blocked_by=[b["id"]])

    def test_remove_blocked_by_via_update(self) -> None:
        store = TaskStore(self._tmp())
        a = store.create("A")
        b = store.create("B")
        store.update(b["id"], add_blocked_by=[a["id"]])
        updated = store.update(b["id"], remove_blocked_by=[a["id"]])
        assert updated is not None
        self.assertEqual(updated["blockedBy"], [])

    def test_remove_blocked_by_unblocks_task(self) -> None:
        store = TaskStore(self._tmp())
        a = store.create("A")
        b = store.create("B", labels=[READY_FOR_AGENT])
        store.update(b["id"], add_blocked_by=[a["id"]])
        self.assertNotIn(b["id"], {t["id"] for t in store.list_claimable()})  # b blocked
        store.update(b["id"], remove_blocked_by=[a["id"]])
        self.assertIn(b["id"], {t["id"] for t in store.list_claimable()})  # b now unblocked

    # --- L2: parent_id grouping (wayfinder map -> child tickets) ---

    def test_parent_id_via_create_and_list_children(self) -> None:
        store = TaskStore(self._tmp())
        parent = store.create("Map")
        children = store.create_many(
            [
                {"key": "c1", "subject": "C1", "parent_id": parent["id"]},
                {"key": "c2", "subject": "C2"},
            ]
        )
        self.assertEqual(children[0]["parent_id"], parent["id"])
        self.assertIsNone(children[1]["parent_id"])
        self.assertEqual({t["id"] for t in store.list_children(parent["id"])}, {children[0]["id"]})
        set_after = store.update(children[1]["id"], parent_id=parent["id"])
        assert set_after is not None
        self.assertEqual({t["id"] for t in store.list_children(parent["id"])}, {children[0]["id"], children[1]["id"]})

    def test_parent_id_via_key_in_batch(self) -> None:
        store = TaskStore(self._tmp())
        tasks = store.create_many(
            [
                {"key": "map", "subject": "Map"},
                {"key": "child", "subject": "Child", "parent_id": "map"},
            ]
        )
        self.assertEqual(tasks[1]["parent_id"], tasks[0]["id"])

    def test_parent_cycle_rejected(self) -> None:
        store = TaskStore(self._tmp())
        a = store.create("A")
        b = store.create("B", parent_id=a["id"])  # B's parent is A
        with self.assertRaises(ValueError):  # A's parent becoming B closes the loop
            store.update(a["id"], parent_id=b["id"])

    def test_self_parent_rejected(self) -> None:
        store = TaskStore(self._tmp())
        a = store.create("A")
        with self.assertRaises(ValueError):
            store.update(a["id"], parent_id=a["id"])

    def test_parent_clear_with_zero(self) -> None:
        store = TaskStore(self._tmp())
        a = store.create("A")
        b = store.create("B", parent_id=a["id"])
        self.assertEqual(b["parent_id"], a["id"])
        cleared = store.update(b["id"], parent_id=0)
        assert cleared is not None
        self.assertIsNone(cleared["parent_id"])

    # --- L2: tool surface (task_claimable, task_close, remove reassigns) ---

    def _ctx(self, team_manager=None, session_id: str | None = None) -> ToolExecutionContext:
        return ToolExecutionContext(
            runtime=SimpleNamespace(team_manager=team_manager),
            session=SimpleNamespace(id=session_id) if session_id else None,
            actor="lead",
            trace_id="test",
        )

    def test_claimable_tasks_tool_splits_ready_and_unspecced(self) -> None:
        store = TaskStore(self._tmp())
        registry = ToolRegistry()
        register_task_tools(registry, store, allow_dep_removal=True)
        ready = store.create("ready", labels=[READY_FOR_AGENT])
        unspecced = store.create("unspecced")
        out = json.loads(registry.execute(self._ctx(), "task_claimable", {}))
        self.assertEqual({t["id"] for t in out["ready_for_agent"]}, {ready["id"]})
        self.assertEqual({t["id"] for t in out["claimable_unspecced"]}, {unspecced["id"]})

    def test_close_task_tool_enforces_acceptance_gate(self) -> None:
        store = TaskStore(self._tmp())
        registry = ToolRegistry()
        register_task_tools(registry, store, allow_dep_removal=True)
        task = store.create("X", acceptance=["a", "b"])
        registry.execute(self._ctx(), "task_close", {"task_id": task["id"], "acceptance_done": [True, False]})
        self.assertEqual(store.get(task["id"])["status"], "pending")  # gate blocked it
        ok = json.loads(
            registry.execute(
                self._ctx(),
                "task_close",
                {"task_id": task["id"], "acceptance_done": [True, True], "result": "done", "commit_ref": "abc"},
            )
        )
        self.assertEqual(ok["status"], "completed")
        self.assertEqual(ok["result"], "done")
        self.assertEqual(ok["commit_ref"], "abc")

    def test_remove_blocked_by_tool_reassigns_after_unblock(self) -> None:
        store = TaskStore(self._tmp())
        a = store.create("A")
        b = store.create("B", labels=[READY_FOR_AGENT])
        store.update(b["id"], add_blocked_by=[a["id"]])
        calls = {"n": 0}

        def fake_assign(session_id=None):
            calls["n"] += 1
            return 0

        registry = ToolRegistry()
        register_task_tools(registry, store, allow_dep_removal=True)
        registry.execute(
            self._ctx(team_manager=SimpleNamespace(assign_claimable_tasks=fake_assign)),
            "task_remove_blocked_by",
            {"task_id": b["id"], "remove": [a["id"]]},
        )
        self.assertGreaterEqual(calls["n"], 1)  # auto-assign re-fired
        self.assertIn(b["id"], {t["id"] for t in store.list_claimable()})


class TaskToolGatingTest(unittest.TestCase):
    """The hybrid permission model: add-edge is free, remove-edge needs approval."""

    def test_remove_blocked_by_is_not_in_any_allow_set(self) -> None:
        from open_somnia.runtime.execution_mode import tool_block_message

        self.assertIsNotNone(tool_block_message("shortcuts", "task_remove_blocked_by"))
        self.assertIsNotNone(tool_block_message("accept_edits", "task_remove_blocked_by"))
        self.assertIsNone(tool_block_message("yolo", "task_remove_blocked_by"))

    def test_close_is_mutation_claimable_is_readonly(self) -> None:
        from open_somnia.runtime.execution_mode import tool_block_message

        self.assertIsNone(tool_block_message("accept_edits", "task_close"))
        self.assertIsNotNone(tool_block_message("shortcuts", "task_close"))
        self.assertIsNone(tool_block_message("shortcuts", "task_claimable"))

    def test_worker_registry_excludes_dep_removal(self) -> None:
        lead = ToolRegistry()
        worker = ToolRegistry()
        store = TaskStore(Path(tempfile.mkdtemp(prefix="somnia-task-gate-")))
        register_task_tools(lead, store, allow_dep_removal=True)
        register_task_tools(worker, store, allow_dep_removal=False)
        self.assertIn("task_remove_blocked_by", lead.names())
        self.assertNotIn("task_remove_blocked_by", worker.names())
        self.assertIn("task_claimable", worker.names())
        self.assertIn("task_close", worker.names())


if __name__ == "__main__":
    unittest.main()
