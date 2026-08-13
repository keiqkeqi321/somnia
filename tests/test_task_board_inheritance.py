from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.events import ToolExecutionContext
from open_somnia.runtime.session import AgentSession
from open_somnia.storage.tasks import READY_FOR_AGENT, TaskStore
from open_somnia.tools.registry import ToolRegistry
from open_somnia.tools.tasks import register_task_tools


class _TmpMixin:
    def _tmp(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="somnia-board-test-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return directory


class BoardLayoutTest(_TmpMixin, unittest.TestCase):
    """Writes land in boards/; reads fall back boards/ -> sessions/ -> root."""

    def test_writes_land_in_boards_layout(self) -> None:
        store = TaskStore(self._tmp())
        task = store.create("X", session_id="s1")
        board_file = store.root / "boards" / "s1" / f"task_{task['id']}.json"
        self.assertTrue(board_file.is_file())
        self.assertFalse((store.root / "sessions" / "s1").exists())

    def test_read_falls_back_to_legacy_session_dir(self) -> None:
        store = TaskStore(self._tmp())
        legacy_dir = store.root / "sessions" / "s1"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "task_1.json").write_text(
            json.dumps({"id": 1, "subject": "old", "session_id": "s1", "status": "pending"}),
            encoding="utf-8",
        )
        task = store.get(1, session_id="s1")
        self.assertEqual(task["subject"], "old")
        self.assertIn(task["id"], {t["id"] for t in store.list_all(session_id="s1")})

    def test_read_falls_back_to_stamped_root_file(self) -> None:
        store = TaskStore(self._tmp())
        (store.root / "task_2.json").write_text(
            json.dumps({"id": 2, "subject": "root", "session_id": "s1", "status": "pending"}),
            encoding="utf-8",
        )
        self.assertEqual(store.get(2, session_id="s1")["subject"], "root")

    def test_list_all_none_sees_boards_and_legacy(self) -> None:
        store = TaskStore(self._tmp())
        created = store.create("boarded", session_id="s1")
        legacy_dir = store.root / "sessions" / "s2"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "task_99.json").write_text(
            json.dumps({"id": 99, "subject": "legacy", "session_id": "s2", "status": "pending"}),
            encoding="utf-8",
        )
        ids = {t["id"] for t in store.list_all()}
        self.assertIn(created["id"], ids)
        self.assertIn(99, ids)


class EnsureBoardTest(_TmpMixin, unittest.TestCase):
    def test_migrates_legacy_session_dir(self) -> None:
        store = TaskStore(self._tmp())
        legacy_dir = store.root / "sessions" / "s1"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "task_1.json").write_text(
            json.dumps({"id": 1, "subject": "old", "session_id": "s1", "status": "pending"}),
            encoding="utf-8",
        )
        board_id = store.ensure_board("s1")
        self.assertEqual(board_id, "s1")
        self.assertTrue((store.root / "boards" / "s1" / "task_1.json").is_file())
        self.assertFalse((store.root / "sessions" / "s1").exists())
        self.assertEqual(store.get(1, session_id="s1")["subject"], "old")
        # idempotent
        self.assertEqual(store.ensure_board("s1"), "s1")
        self.assertTrue((store.root / "boards" / "s1" / "task_1.json").is_file())

    def test_migrates_stamped_root_files_only(self) -> None:
        store = TaskStore(self._tmp())
        (store.root / "task_1.json").write_text(
            json.dumps({"id": 1, "subject": "mine", "session_id": "s1", "status": "pending"}),
            encoding="utf-8",
        )
        (store.root / "task_2.json").write_text(
            json.dumps({"id": 2, "subject": "someone-else", "session_id": "s9", "status": "pending"}),
            encoding="utf-8",
        )
        store.ensure_board("s1")
        self.assertTrue((store.root / "boards" / "s1" / "task_1.json").is_file())
        self.assertTrue((store.root / "task_2.json").is_file())  # untouched

    def test_creates_empty_board_when_no_tasks(self) -> None:
        store = TaskStore(self._tmp())
        store.ensure_board("s1")
        self.assertTrue((store.root / "boards" / "s1").is_dir())

    def test_requires_session_id(self) -> None:
        store = TaskStore(self._tmp())
        with self.assertRaises(ValueError):
            store.ensure_board("")


class ToolContextResolutionTest(_TmpMixin, unittest.TestCase):
    """_context_session_id prefers the inherited board over the session's own id."""

    def _ctx(self, session) -> ToolExecutionContext:
        return ToolExecutionContext(
            runtime=SimpleNamespace(team_manager=None),
            session=session,
            actor="lead",
            trace_id="test",
        )

    def _registry(self, store: TaskStore) -> ToolRegistry:
        registry = ToolRegistry()
        register_task_tools(registry, store, allow_dep_removal=True)
        return registry

    def test_inherited_session_sees_board(self) -> None:
        store = TaskStore(self._tmp())
        registry = self._registry(store)
        task = store.create("ready", labels=[READY_FOR_AGENT], session_id="s1")
        # s2 was created via /new and inherited s1's board.
        session = SimpleNamespace(id="s2", task_session_id="s1")
        out = json.loads(registry.execute(self._ctx(session), "task_claimable", {}))
        self.assertIn(task["id"], {t["id"] for t in out["ready_for_agent"]})
        got = json.loads(registry.execute(self._ctx(session), "task_get", {"task_id": task["id"]}))
        self.assertEqual(got["subject"], "ready")

    def test_inherited_session_writes_to_board(self) -> None:
        store = TaskStore(self._tmp())
        registry = self._registry(store)
        session = SimpleNamespace(id="s2", task_session_id="s1")
        registry.execute(
            self._ctx(session),
            "task_create_batch",
            {"tasks": [{"subject": "from-s2"}]},
        )
        created = store.list_all(session_id="s1")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["session_id"], "s1")
        self.assertTrue((store.root / "boards" / "s1" / f"task_{created[0]['id']}.json").is_file())

    def test_fresh_session_without_inheritance_is_isolated(self) -> None:
        store = TaskStore(self._tmp())
        registry = self._registry(store)
        task = store.create("boarded", labels=[READY_FOR_AGENT], session_id="s1")
        session = SimpleNamespace(id="s3", task_session_id=None)
        out = json.loads(registry.execute(self._ctx(session), "task_claimable", {}))
        self.assertNotIn(task["id"], {t["id"] for t in out["ready_for_agent"]})
        # task_get crosses boards: the registry converts the store's ValueError
        # into an error payload instead of returning the task.
        result = registry.execute(self._ctx(session), "task_get", {"task_id": task["id"]})
        self.assertIn("not found", str(result).lower())


class InheritTaskBoardTest(_TmpMixin, unittest.TestCase):
    """OpenAgentRuntime.inherit_task_board drives migration + binding at swap."""

    def _runtime_stub(self, store: TaskStore):
        saved: list[str] = []
        return SimpleNamespace(
            task_store=store,
            session_manager=SimpleNamespace(save=lambda session: saved.append(session.id)),
        ), saved

    def test_first_swap_migrates_and_binds(self) -> None:
        store = TaskStore(self._tmp())
        store.create("old-work", session_id="s1")  # written to boards/ already
        runtime_stub, saved = self._runtime_stub(store)
        old = AgentSession(id="s1")
        fresh = AgentSession(id="s2")
        OpenAgentRuntime.inherit_task_board(runtime_stub, fresh, old)
        self.assertEqual(fresh.task_session_id, "s1")
        self.assertEqual(saved, ["s2"])
        # round-trips through payload persistence
        reloaded = AgentSession.from_payload(fresh.to_payload())
        self.assertEqual(reloaded.task_session_id, "s1")

    def test_first_swap_migrates_legacy_layout(self) -> None:
        store = TaskStore(self._tmp())
        legacy_dir = store.root / "sessions" / "s1"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "task_1.json").write_text(
            json.dumps({"id": 1, "subject": "legacy", "session_id": "s1", "status": "pending"}),
            encoding="utf-8",
        )
        runtime_stub, _ = self._runtime_stub(store)
        OpenAgentRuntime.inherit_task_board(runtime_stub, AgentSession(id="s2"), AgentSession(id="s1"))
        self.assertTrue((store.root / "boards" / "s1" / "task_1.json").is_file())

    def test_chain_keeps_founding_board(self) -> None:
        store = TaskStore(self._tmp())
        runtime_stub, _ = self._runtime_stub(store)
        s2 = AgentSession(id="s2", task_session_id="s1")  # already bound to s1's board
        s3 = AgentSession(id="s3")
        OpenAgentRuntime.inherit_task_board(runtime_stub, s3, s2)
        self.assertEqual(s3.task_session_id, "s1")  # founding board, not s2
        self.assertFalse((store.root / "boards" / "s2").exists())


if __name__ == "__main__":
    unittest.main()
