from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from open_somnia.remote import cli
from open_somnia.remote.runtime_manager import ProjectRegistry


class ConnectorCliTests(unittest.TestCase):
    def test_run_without_projects_starts_all_local_registrations(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            registry_path = root / "projects.json"
            registry = ProjectRegistry(registry_path)
            registry.register("first", first)
            registry.register("second", second)
            manager = _FakeManager()

            with (
                patch.object(cli.DeviceIdentity, "load", return_value=_FakeIdentity()),
                patch.object(cli, "ProjectRuntimeManager", return_value=manager),
                patch.object(cli, "RemoteConnector", _FakeConnector),
                patch("sys.argv", ["somnia-connector", "run", "--registry", str(registry_path)]),
            ):
                self.assertEqual(cli.connector_main(), 0)

            self.assertEqual(manager.requested_projects, ["first", "second"])
            self.assertTrue(manager.stopped)


class _FakeIdentity:
    relay_url = "https://relay.example.com"


class _FakeManager:
    def __init__(self) -> None:
        self.requested_projects: list[str] = []
        self.stopped = False

    def bridges(self, project_ids: list[str]) -> dict[str, object]:
        self.requested_projects = project_ids
        return {project_id: object() for project_id in project_ids}

    def stop_all(self) -> None:
        self.stopped = True


class _FakeConnector:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def run(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
