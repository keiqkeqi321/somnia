from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import base64

from open_somnia.remote import cli
from open_somnia.remote.runtime_manager import ProjectRegistry


class ConnectorCliTests(unittest.TestCase):
    def test_setup_pairs_and_registers_project(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            registry_path = root / "projects.json"
            with (
                patch.object(cli.DeviceIdentity, "load_or_create", return_value=_FakeIdentity()),
                patch.object(cli, "pair_device", return_value=type("Pairing", (), {"device_name": "Laptop", "device_id": "device-1"})()),
                patch("sys.argv", ["somnia-connector", "setup", "--relay", "https://relay.example.com", "--code", "ABC", "--project", "work", "--path", str(project), "--registry", str(registry_path)]),
            ):
                self.assertEqual(cli.connector_main(), 0)
            self.assertEqual(ProjectRegistry(registry_path).list()[0].project_id, "work")

    def test_doctor_reports_ready_connector(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            registry_path = root / "projects.json"
            ProjectRegistry(registry_path).register("work", project)
            identity = _FakeIdentity()
            identity.device_id = "device-1"
            identity.relay_url = "https://relay.example.com"
            with (
                patch.object(cli.DeviceIdentity, "load", return_value=identity),
                patch.object(cli, "_check_http_health"),
                patch("sys.argv", ["somnia-connector", "doctor", "--identity", str(root / "identity.json"), "--registry", str(registry_path)]),
            ):
                self.assertEqual(cli.connector_main(), 0)

    def test_install_autostart_writes_user_service_without_payloads(self) -> None:
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
            identity = _FakeIdentity()
            identity.device_id = "device-1"
            identity.relay_url = "https://relay.example.com"
            output = root / "somnia-connector.service"
            with (
                patch.object(cli.DeviceIdentity, "load", return_value=identity),
                patch("sys.argv", ["somnia-connector", "install-autostart", "--identity", str(root / "identity.json"), "--registry", str(registry_path), "--output", str(output)]),
            ):
                self.assertEqual(cli.connector_main(), 0)
            service = output.read_text(encoding="utf-8")
            self.assertIn("--project first", service)
            self.assertIn("--project second", service)
            self.assertNotIn("relay.example.com", service)

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


class RelaySecretConfigurationTests(unittest.TestCase):
    def test_loads_urlsafe_32_byte_secret(self) -> None:
        secret = bytes(range(32))
        encoded = base64.urlsafe_b64encode(secret).decode("ascii").rstrip("=")

        self.assertEqual(cli.load_relay_secret_key(encoded, required=True), secret)

    def test_local_mode_allows_missing_secret(self) -> None:
        self.assertIsNone(cli.load_relay_secret_key(None, required=False))

    def test_production_mode_rejects_missing_or_malformed_secret(self) -> None:
        with self.assertRaises(ValueError):
            cli.load_relay_secret_key(None, required=True)
        with self.assertRaises(ValueError):
            cli.load_relay_secret_key("not-a-32-byte-secret", required=True)

    def test_production_relay_passes_configured_secret_to_app(self) -> None:
        secret = bytes(range(32))
        encoded = base64.urlsafe_b64encode(secret).decode("ascii")
        with (
            patch.dict(
                "os.environ",
                {
                    "SOMNIA_ADMIN_PASSWORD": "test-password",
                    "SOMNIA_RELAY_DATABASE_URL": "sqlite://",
                    "SOMNIA_ENV": "production",
                    "SOMNIA_RELAY_SECRET_KEY": encoded,
                },
                clear=False,
            ),
            patch("sys.argv", ["somnia-relay"]),
            patch.object(cli, "create_relay_app", return_value=object()) as create_app,
            patch.object(cli.uvicorn, "run"),
        ):
            self.assertEqual(cli.relay_main(), 0)

        self.assertEqual(create_app.call_args.kwargs["secret_key"], secret)


class _FakeIdentity:
    device_id = ""
    relay_url = "https://relay.example.com"

    @property
    def is_paired(self) -> bool:
        return bool(self.device_id and self.relay_url)


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
