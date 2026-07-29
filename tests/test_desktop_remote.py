from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import urllib.error
import urllib.request

from desktop.backend.remote_device import RemoteDeviceManager, RemoteNotPairedError, workspace_project_id
from desktop.backend.server import SidecarServer
from tests.remote_tracer_support import remote_tracer_settings, wait_until


class _MockRelayHandler(BaseHTTPRequestHandler):
    """Minimal Relay stand-in for the device-flow pair session endpoints."""

    session_mode = "approve"  # "approve" | "pending" | "expired" | "error"
    polls_before_approval = 1
    poll_count = 0
    session_creations = 0
    web_origin = "http://web.test:4173"
    last_pair_session_body: dict = {}

    @classmethod
    def reset(cls) -> None:
        cls.session_mode = "approve"
        cls.polls_before_approval = 1
        cls.poll_count = 0
        cls.session_creations = 0
        cls.web_origin = "http://web.test:4173"
        cls.last_pair_session_body = {}

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        if self.path == "/api/pair-sessions":
            type(self).session_creations += 1
            type(self).last_pair_session_body = body
            if type(self).session_mode == "error":
                self._send_json(500, {"error": "relay exploded"})
                return
            self._send_json(
                201,
                {"session_id": "session-1", "secret": "secret-1", "expires_at": time.time() + 60},
            )
            return
        if self.path == "/api/pairings/claim":
            self._send_json(201, {"device_id": "device-123", "name": "Test Device"})
            return
        self._send_json(404, {"error": "not found"})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/info":
            self._send_json(200, {"web_origin": type(self).web_origin})
            return
        if parsed.path == "/api/pair-sessions/session-1":
            query = parse_qs(parsed.query)
            if query.get("secret", [""])[0] != "secret-1":
                self._send_json(403, {"error": "Pair session secret is invalid."})
                return
            type(self).poll_count += 1
            mode = type(self).session_mode
            if mode == "expired":
                self._send_json(200, {"status": "expired"})
            elif mode == "approve" and type(self).poll_count > type(self).polls_before_approval:
                self._send_json(200, {"status": "approved", "code": "PAIRCODE1"})
            else:
                self._send_json(200, {"status": "pending"})
            return
        self._send_json(404, {"error": "not found"})

    def _send_json(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:
        return None


class _FakeConnector:
    """Short-lived Connector double: blocks in run() until stopped."""

    instances: list["_FakeConnector"] = []
    run_error: Exception | None = None

    def __init__(self, relay_url, *, identity, project_id, sidecar, sidecars=None, project_names=None):
        self.relay_url = relay_url
        self.identity = identity
        self.project_id = project_id
        self.sidecar = sidecar
        self.sidecars = sidecars or {}
        self.project_names = project_names or {}
        self.started = Event()
        type(self).instances.append(self)

    def run(self, stop_event: Event) -> None:
        self.started.set()
        if type(self).run_error is not None:
            raise type(self).run_error
        stop_event.wait(30.0)

    def run_forever(self, stop_event: Event, *, on_retry=None, on_connect=None) -> None:
        del on_retry, on_connect
        self.run(stop_event)


class DesktopRemoteTests(unittest.TestCase):
    def setUp(self) -> None:
        _MockRelayHandler.reset()
        _FakeConnector.instances = []
        _FakeConnector.run_error = None
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.identity_path = self.root / "identity" / "device-identity.json"
        identity_patcher = patch(
            "desktop.backend.remote_device.default_identity_path",
            lambda: self.identity_path,
        )
        identity_patcher.start()
        self.addCleanup(identity_patcher.stop)
        connector_patcher = patch("desktop.backend.remote_device.RemoteConnector", _FakeConnector)
        connector_patcher.start()
        self.addCleanup(connector_patcher.stop)
        interval_patcher = patch("desktop.backend.remote_device.PAIR_POLL_INTERVAL_SECONDS", 0.02)
        interval_patcher.start()
        self.addCleanup(interval_patcher.stop)
        browser_patcher = patch("webbrowser.open")
        self.browser_open = browser_patcher.start()
        self.addCleanup(browser_patcher.stop)
        self.settings = remote_tracer_settings(self.root / "workspace")
        self.server = SidecarServer.from_settings(self.settings, host="127.0.0.1", port=0)
        self.addCleanup(self.server.close)
        self.server.start_background()
        if not self.server.wait_until_ready():
            self.fail("Sidecar did not start.")

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.server.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=5.0) as response:
                return response.status, json.loads(response.read().decode("utf-8") or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or b"{}")

    def _start_mock_relay(self) -> tuple[ThreadingHTTPServer, str]:
        relay = ThreadingHTTPServer(("127.0.0.1", 0), _MockRelayHandler)
        thread = Thread(target=relay.serve_forever, name="mock-relay", daemon=True)
        thread.start()
        self.addCleanup(relay.server_close)
        self.addCleanup(relay.shutdown)
        return relay, f"http://127.0.0.1:{relay.server_address[1]}"

    def _pair(self) -> str:
        """Run the device-flow pairing to completion; returns the relay URL."""
        _, relay_url = self._start_mock_relay()
        status, payload = self._request("POST", "/remote/pair-begin", {"relay_url": relay_url})
        self.assertEqual(status, 200, payload)
        self.assertTrue(
            wait_until(lambda: self.server.remote_status()["paired"]),
            "Pairing did not complete.",
        )
        return relay_url

    def test_status_defaults_to_unpaired(self) -> None:
        status, payload = self._request("GET", "/remote/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {
                "paired": False,
                "device_id": "",
                "device_name": "",
                "relay_url": "",
                "username": "",
                "enabled": False,
                "connector_running": False,
                "pair_pending": False,
                "last_error": "",
                "projects": [],
            },
        )

    def test_pair_begin_opens_browser_polls_and_auto_enables(self) -> None:
        relay_url = self._pair()

        # The pair session carries the machine hostname as the suggested Device
        # name, so the approving browser pre-fills something recognizable.
        self.assertEqual(_MockRelayHandler.last_pair_session_body.get("device_name"), socket.gethostname())

        # The browser was sent to the Web app origin reported by /api/info (split hosting),
        # carrying the session credentials — not to the Relay origin itself.
        opened_url = self.browser_open.call_args[0][0]
        self.assertTrue(opened_url.startswith("http://web.test:4173/?remote=1#/pair?"))
        self.assertIn("session=session-1", opened_url)
        self.assertIn("secret=secret-1", opened_url)

        status, remote_status = self._request("GET", "/remote/status")
        self.assertEqual(status, 200)
        self.assertTrue(remote_status["paired"])
        self.assertEqual(remote_status["device_id"], "device-123")
        self.assertEqual(remote_status["device_name"], "Test Device")
        self.assertEqual(remote_status["relay_url"], relay_url)
        # Enabling happens on the pair poll thread after claiming, so wait for
        # it instead of asserting on the first status read (latent race).
        self.assertTrue(
            wait_until(lambda: self.server.remote_status()["enabled"]),
            "Remote access was not auto-enabled after pairing.",
        )
        self.assertTrue(
            wait_until(lambda: not self.server.remote_status()["pair_pending"]),
            "Pair poll thread did not wind down after approval.",
        )
        self.assertTrue(self.identity_path.exists())

        # Approval auto-enabled the Connector.
        self.assertTrue(
            wait_until(lambda: len(_FakeConnector.instances) == 1),
            "Connector did not auto-start after pairing.",
        )
        self.assertTrue(_FakeConnector.instances[0].started.wait(5.0))
        _, remote_status = self._request("GET", "/remote/status")
        self.assertTrue(remote_status["connector_running"])

        persisted = json.loads(
            (self.settings.storage.data_dir / "remote" / "settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["relay_url"], relay_url)
        self.assertEqual(persisted["device_name"], "Test Device")
        self.assertTrue(persisted["enabled"])
        self.assertNotIn("password", persisted)
        self.assertNotIn("secret", persisted)

    def test_pair_begin_rejects_non_loopback_http(self) -> None:
        status, payload = self._request(
            "POST",
            "/remote/pair-begin",
            {"relay_url": "http://relay.example.com"},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_pair_begin_relay_failure_returns_bad_gateway(self) -> None:
        _MockRelayHandler.session_mode = "error"
        _, relay_url = self._start_mock_relay()
        status, payload = self._request("POST", "/remote/pair-begin", {"relay_url": relay_url})
        self.assertEqual(status, 502)
        self.assertIn("relay exploded", payload["error"])
        self.assertFalse(self.identity_path.exists())

    def test_pair_poll_expiry_records_last_error(self) -> None:
        _MockRelayHandler.session_mode = "expired"
        _, relay_url = self._start_mock_relay()
        status, payload = self._request("POST", "/remote/pair-begin", {"relay_url": relay_url})
        self.assertEqual(status, 200, payload)
        self.assertTrue(
            wait_until(lambda: "expired" in self.server.remote_status()["last_error"]),
            "Pair session expiry was not recorded.",
        )
        status, remote_status = self._request("GET", "/remote/status")
        self.assertEqual(status, 200)
        self.assertFalse(remote_status["paired"])
        self.assertFalse(remote_status["pair_pending"])
        self.assertFalse(self.identity_path.exists())

    def test_pair_cancel_aborts_a_pending_poll(self) -> None:
        _MockRelayHandler.session_mode = "pending"
        _, relay_url = self._start_mock_relay()
        status, payload = self._request("POST", "/remote/pair-begin", {"relay_url": relay_url})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["pair_pending"])

        status, payload = self._request("POST", "/remote/pair-cancel")
        self.assertEqual(status, 200)
        self.assertFalse(payload["pair_pending"])
        self.assertFalse(payload["paired"])

    def test_pair_begin_is_idempotent_while_pending(self) -> None:
        _MockRelayHandler.session_mode = "pending"
        _, relay_url = self._start_mock_relay()
        status, payload = self._request("POST", "/remote/pair-begin", {"relay_url": relay_url})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["pair_pending"])

        status, payload = self._request("POST", "/remote/pair-begin", {"relay_url": relay_url})
        self.assertEqual(status, 200)
        self.assertTrue(payload["pair_pending"])
        self.assertEqual(_MockRelayHandler.session_creations, 1)
        self.assertEqual(self.browser_open.call_count, 1)

        status, payload = self._request("POST", "/remote/pair-cancel")
        self.assertEqual(status, 200)
        self.assertFalse(payload["pair_pending"])

    def test_enable_requires_paired_identity(self) -> None:
        status, payload = self._request("POST", "/remote/enable")
        self.assertEqual(status, 409)
        self.assertIn("error", payload)

    def test_enable_is_idempotent_and_disable_stops_thread(self) -> None:
        relay_url = self._pair()
        # Pairing already auto-enabled the Connector; enabling again is a no-op.
        self.assertTrue(wait_until(lambda: len(_FakeConnector.instances) == 1))
        status, payload = self._request("POST", "/remote/enable")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["enabled"])
        self.assertEqual(len(_FakeConnector.instances), 1)
        connector = _FakeConnector.instances[0]
        self.assertEqual(connector.project_id, workspace_project_id(self.settings.workspace_root))
        self.assertTrue(connector.project_id.startswith("desktop-"))
        self.assertEqual(connector.relay_url, relay_url.replace("http://", "ws://"))

        status, payload = self._request("GET", "/remote/status")
        self.assertTrue(payload["connector_running"])

        status, payload = self._request("POST", "/remote/disable")
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        self.assertFalse(payload["connector_running"])
        persisted = json.loads(
            (self.settings.storage.data_dir / "remote" / "settings.json").read_text(encoding="utf-8")
        )
        self.assertFalse(persisted["enabled"])

        # Manual re-enable after disable starts a fresh Connector.
        status, payload = self._request("POST", "/remote/enable")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["enabled"])
        self.assertEqual(len(_FakeConnector.instances), 2)
        self.assertTrue(_FakeConnector.instances[1].started.wait(5.0))

    def test_enable_with_projects_exposes_all_bridges(self) -> None:
        self._pair()
        self.assertTrue(wait_until(lambda: len(_FakeConnector.instances) == 1))
        status, payload = self._request("POST", "/remote/disable")
        self.assertEqual(status, 200, payload)

        own_id = workspace_project_id(self.settings.workspace_root)
        projects = [
            {"project_id": own_id, "name": "Own Project", "base_url": self.server.base_url},
            {"project_id": "desktop-second", "name": "Second Project", "base_url": "http://127.0.0.1:59001"},
            {"project_id": "desktop-third", "name": "Third Project", "base_url": "http://127.0.0.1:59002"},
        ]
        status, payload = self._request("POST", "/remote/enable", {"projects": projects})
        self.assertEqual(status, 200, payload)

        self.assertEqual(len(_FakeConnector.instances), 2)
        connector = _FakeConnector.instances[1]
        self.assertEqual(connector.project_id, own_id)
        # The own bridge always uses the manager's live base_url (here the
        # caller happens to supply the same value).
        self.assertEqual(connector.sidecar.base_url, self.server.base_url)
        self.assertEqual(set(connector.sidecars), {"desktop-second", "desktop-third"})
        self.assertEqual(connector.sidecars["desktop-second"].base_url, "http://127.0.0.1:59001")
        self.assertEqual(connector.sidecars["desktop-third"].base_url, "http://127.0.0.1:59002")
        self.assertEqual(
            connector.project_names,
            {own_id: "Own Project", "desktop-second": "Second Project", "desktop-third": "Third Project"},
        )
        self.assertTrue(connector.started.wait(5.0))

        self.assertEqual(
            payload["projects"],
            [
                {"project_id": own_id, "name": "Own Project"},
                {"project_id": "desktop-second", "name": "Second Project"},
                {"project_id": "desktop-third", "name": "Third Project"},
            ],
        )
        persisted = json.loads(
            (self.settings.storage.data_dir / "remote" / "settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["projects"], projects)

    def test_enable_overrides_a_stale_own_project_base_url(self) -> None:
        self._pair()
        self.assertTrue(wait_until(lambda: len(_FakeConnector.instances) == 1))
        status, _ = self._request("POST", "/remote/disable")
        self.assertEqual(status, 200)

        own_id = workspace_project_id(self.settings.workspace_root)
        projects = [
            # Persisted by an earlier sidecar generation whose port is now dead.
            {"project_id": own_id, "name": "Own Project", "base_url": "http://127.0.0.1:9"},
        ]
        status, payload = self._request("POST", "/remote/enable", {"projects": projects})
        self.assertEqual(status, 200, payload)

        self.assertEqual(len(_FakeConnector.instances), 2)
        connector = _FakeConnector.instances[1]
        self.assertEqual(connector.sidecar.base_url, self.server.base_url)
        persisted = json.loads(
            (self.settings.storage.data_dir / "remote" / "settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["projects"][0]["base_url"], self.server.base_url)
        self.assertTrue(connector.started.wait(5.0))

    def test_enable_with_projects_deduplicates_and_defaults_name(self) -> None:
        self._pair()
        self.assertTrue(wait_until(lambda: len(_FakeConnector.instances) == 1))
        status, _ = self._request("POST", "/remote/disable")
        self.assertEqual(status, 200)

        own_id = workspace_project_id(self.settings.workspace_root)
        projects = [
            {"project_id": own_id, "name": "Own Project", "base_url": self.server.base_url},
            {"project_id": "desktop-second", "name": "", "base_url": "http://127.0.0.1:59001"},
            {"project_id": "desktop-second", "name": "Duplicate", "base_url": "http://127.0.0.1:59003"},
        ]
        status, payload = self._request("POST", "/remote/enable", {"projects": projects})
        self.assertEqual(status, 200, payload)
        connector = _FakeConnector.instances[1]
        self.assertEqual(set(connector.sidecars), {"desktop-second"})
        self.assertEqual(connector.sidecars["desktop-second"].base_url, "http://127.0.0.1:59001")
        self.assertEqual(connector.project_names["desktop-second"], "desktop-second")

    def test_enable_with_projects_rejects_invalid_entries(self) -> None:
        self._pair()
        self.assertTrue(wait_until(lambda: len(_FakeConnector.instances) == 1))
        status, _ = self._request("POST", "/remote/disable")
        self.assertEqual(status, 200)
        before = len(_FakeConnector.instances)

        own_id = workspace_project_id(self.settings.workspace_root)
        status, payload = self._request(
            "POST",
            "/remote/enable",
            {"projects": [{"project_id": "", "name": "Broken", "base_url": self.server.base_url}]},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

        status, payload = self._request(
            "POST",
            "/remote/enable",
            {
                "projects": [
                    {"project_id": own_id, "name": "Own", "base_url": self.server.base_url},
                    {"project_id": "desktop-remote", "name": "Remote", "base_url": "http://192.168.1.10:8765"},
                ]
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        self.assertEqual(len(_FakeConnector.instances), before)

    def test_autostart_prunes_unreachable_projects(self) -> None:
        self._pair()
        self.assertTrue(wait_until(lambda: len(_FakeConnector.instances) == 1))
        status, _ = self._request("POST", "/remote/disable")
        self.assertEqual(status, 200)

        own_id = workspace_project_id(self.settings.workspace_root)
        projects = [
            {"project_id": own_id, "name": "Own Project", "base_url": self.server.base_url},
            # Answers /health (it is this very sidecar) but stands in for another project.
            {"project_id": "desktop-alive", "name": "Alive Project", "base_url": self.server.base_url},
            {"project_id": "desktop-dead", "name": "Dead Project", "base_url": "http://127.0.0.1:1"},
        ]
        status, payload = self._request("POST", "/remote/enable", {"projects": projects})
        self.assertEqual(status, 200, payload)
        self.assertEqual(set(_FakeConnector.instances[1].sidecars), {"desktop-alive", "desktop-dead"})

        # Simulate a restart: a fresh manager over the same workspace/identity.
        restarted = RemoteDeviceManager(
            workspace_root=self.settings.workspace_root,
            data_dir=self.settings.storage.data_dir,
            sidecar_base_url=self.server.base_url,
        )
        self.addCleanup(restarted.shutdown)
        restarted.autostart_if_enabled()

        self.assertEqual(len(_FakeConnector.instances), 3)
        connector = _FakeConnector.instances[2]
        self.assertTrue(connector.started.wait(5.0))
        self.assertEqual(connector.project_id, own_id)
        self.assertEqual(set(connector.sidecars), {"desktop-alive"})
        restarted_status = restarted.status()
        self.assertIn("Dead Project", restarted_status["last_error"])
        self.assertEqual(
            restarted_status["projects"],
            [
                {"project_id": own_id, "name": "Own Project"},
                {"project_id": "desktop-alive", "name": "Alive Project"},
            ],
        )

    def test_project_id_endpoint_matches_workspace_scheme(self) -> None:
        status, payload = self._request("GET", "/remote/project-id")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"project_id": workspace_project_id(self.settings.workspace_root)})

    def test_connector_failure_is_recorded_not_fatal(self) -> None:
        _FakeConnector.run_error = RuntimeError("relay exploded")
        self._pair()
        self.assertTrue(
            wait_until(lambda: "relay exploded" in self.server.remote_status()["last_error"]),
            "Connector failure was not recorded.",
        )
        status, payload = self._request("GET", "/remote/status")
        self.assertEqual(status, 200)
        self.assertFalse(payload["connector_running"])
        self.assertIn("relay exploded", payload["last_error"])

    def test_autostart_reenables_connector_on_sidecar_start(self) -> None:
        self._pair()
        self.assertTrue(wait_until(lambda: len(_FakeConnector.instances) == 1))
        self.assertTrue(_FakeConnector.instances[0].started.wait(5.0))

        # Simulate a restart: a fresh manager over the same workspace/identity.
        restarted = RemoteDeviceManager(
            workspace_root=self.settings.workspace_root,
            data_dir=self.settings.storage.data_dir,
            sidecar_base_url=self.server.base_url,
        )
        self.addCleanup(restarted.shutdown)
        restarted.autostart_if_enabled()
        self.assertEqual(len(_FakeConnector.instances), 2)
        self.assertTrue(_FakeConnector.instances[1].started.wait(5.0))
        self.assertTrue(restarted.status()["connector_running"])

    def test_unpair_stops_connector_and_deletes_identity(self) -> None:
        self._pair()
        self.assertTrue(wait_until(lambda: len(_FakeConnector.instances) == 1))
        self.assertTrue(_FakeConnector.instances[0].started.wait(5.0))

        status, payload = self._request("POST", "/remote/unpair")
        self.assertEqual(status, 200)
        self.assertFalse(payload["paired"])
        self.assertFalse(payload["enabled"])
        self.assertFalse(payload["connector_running"])
        self.assertFalse(self.identity_path.exists())

        status, payload = self._request("POST", "/remote/enable")
        self.assertEqual(status, 409)

    def test_manager_enable_raises_when_unpaired(self) -> None:
        manager = RemoteDeviceManager(
            workspace_root=self.settings.workspace_root,
            data_dir=self.settings.storage.data_dir,
            sidecar_base_url=self.server.base_url,
        )
        with self.assertRaises(RemoteNotPairedError):
            manager.enable()


if __name__ == "__main__":
    unittest.main()
