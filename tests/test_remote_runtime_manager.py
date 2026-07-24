from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from open_somnia.remote.runtime_manager import ProjectRegistry, ProjectRuntimeManager, RuntimeOwnershipError


class RuntimeManagerTests(unittest.TestCase):
    def test_registry_round_trips_a_registered_project(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            registry_path = root / "projects.json"

            created = ProjectRegistry(registry_path).register("project-a", project_path, name="Local app")
            loaded = ProjectRegistry(registry_path).get("project-a")

            self.assertEqual(loaded, created)
            self.assertEqual(loaded.path, project_path.resolve())
            self.assertEqual(json.loads(registry_path.read_text(encoding="utf-8"))["projects"][0]["name"], "Local app")

    def test_live_owner_blocks_a_second_manager_until_the_first_stops(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            registry = ProjectRegistry(root / "projects.json")
            registry.register("project-a", project_path)
            created: list[FakeRuntime] = []

            def factory(project):
                runtime = FakeRuntime(project.path)
                created.append(runtime)
                return runtime

            first = ProjectRuntimeManager(registry, runtime_factory=factory, owner_dir=root / "owners")
            second = ProjectRuntimeManager(registry, runtime_factory=factory, owner_dir=root / "owners")
            first_runtime = first.start("project-a")

            self.assertIs(first_runtime, first.start("project-a"))
            with self.assertRaises(RuntimeOwnershipError):
                second.start("project-a")

            self.assertTrue(first.stop("project-a"))
            second_runtime = second.start("project-a")
            self.assertIsNot(first_runtime, second_runtime)
            second.stop_all()
            self.assertEqual(len(created), 2)
            self.assertEqual(first_runtime.stop_count, 1)

    def test_stale_owner_and_crashed_runtime_are_recovered(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            registry = ProjectRegistry(root / "projects.json")
            registry.register("project-a", project_path)
            owner_dir = root / "owners"
            owner_dir.mkdir()
            (owner_dir / "project-a.owner").write_text(
                json.dumps({"pid": 2_147_483_647, "project_id": "project-a"}),
                encoding="utf-8",
            )
            created: list[FakeRuntime] = []

            def factory(project):
                runtime = FakeRuntime(project.path)
                created.append(runtime)
                return runtime

            manager = ProjectRuntimeManager(registry, runtime_factory=factory, owner_dir=owner_dir)
            original = manager.start("project-a")
            original.crash()
            recovered = manager.ensure_started("project-a")

            self.assertIsNot(original, recovered)
            self.assertTrue(recovered.started)
            manager.stop_all()
            self.assertEqual(len(created), 2)

    def test_live_runtime_is_published_locally_and_removed_when_stopped(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            registry = ProjectRegistry(root / "projects.json")
            registry.register("project-a", project_path)
            runtime = FakeRuntime(project_path, base_url="http://127.0.0.1:43821")
            manager = ProjectRuntimeManager(registry, runtime_factory=lambda _: runtime, owner_dir=root / "owners")

            manager.start("project-a")

            payload = json.loads(manager.connection_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["connections"][0]["project_id"], "project-a")
            self.assertEqual(payload["connections"][0]["workspace_root"], str(project_path.resolve()))
            self.assertEqual(payload["connections"][0]["base_url"], "http://127.0.0.1:43821")
            self.assertEqual(payload["connections"][0]["ws_url"], "ws://127.0.0.1:43821/ws")

            manager.stop("project-a")

            self.assertEqual(json.loads(manager.connection_path.read_text(encoding="utf-8"))["connections"], [])


class FakeRuntime:
    def __init__(self, workspace: Path, *, base_url: str = "http://127.0.0.1:8765") -> None:
        self.workspace = workspace
        self.base_url = base_url
        self.started = False
        self.is_closed = False
        self.stop_count = 0

    def start(self) -> None:
        self.started = True
        self.is_closed = False

    def stop(self) -> None:
        self.stop_count += 1
        self.started = False
        self.is_closed = True

    def crash(self) -> None:
        self.started = False
        self.is_closed = True


if __name__ == "__main__":
    unittest.main()
