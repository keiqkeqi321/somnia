from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from open_somnia.remote.auth import PairingCodeInvalid, RemoteAuth
from open_somnia.remote.relay import create_relay_app
from tests.remote_auth_support import BROWSER_ORIGIN, authenticate_connector, claim_pairing, login, pair_device


class RemoteAuthenticationTests(unittest.TestCase):
    def test_device_identity_and_revocation_survive_relay_restart(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_url = f"sqlite:///{(Path(temp_dir) / 'relay.db').as_posix()}"
            first_app = create_relay_app(
                administrators={"admin": "admin-password"},
                database_url=database_url,
            )
            with TestClient(first_app) as first_client:
                login(first_client)
                private_key, device_id = pair_device(first_client, "Persistent Device")

            second_app = create_relay_app(
                administrators={"admin": "admin-password"},
                database_url=database_url,
            )
            with TestClient(second_app) as second_client:
                login(second_client)
                devices = second_client.get("/api/devices").json()["devices"]
                self.assertEqual([device["device_id"] for device in devices], [device_id])
                self.assertEqual(second_client.delete(f"/api/devices/{device_id}").status_code, 200)

            third_app = create_relay_app(
                administrators={"admin": "admin-password"},
                database_url=database_url,
            )
            with TestClient(third_app) as third_client:
                login(third_client)
                persisted = third_client.get("/api/devices").json()["devices"][0]
                self.assertIsNotNone(persisted["revoked_at"])
                with self.assertRaises(WebSocketDisconnect):
                    with third_client.websocket_connect(f"/ws/connector/{device_id}") as connector:
                        authenticate_connector(connector, device_id, private_key, expect_success=False)

    def test_administrator_login_is_rate_limited_after_repeated_failures(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password"}, login_attempt_limit=3)
        with TestClient(app) as client:
            for _ in range(3):
                response = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
                self.assertEqual(response.status_code, 401)

            blocked = client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
            self.assertEqual(blocked.status_code, 429)

    def test_concurrent_login_attempts_cannot_bypass_the_rate_limit(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password"}, login_attempt_limit=1)
        with TestClient(app) as client:
            def attempt_login(_: int) -> int:
                return client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "wrong"},
                ).status_code

            with ThreadPoolExecutor(max_workers=4) as executor:
                statuses = list(executor.map(attempt_login, range(4)))

        self.assertEqual(statuses.count(401), 1)
        self.assertEqual(statuses.count(429), 3)

    def test_browser_session_and_pairing_capacity_are_bounded(self) -> None:
        now = [1_000.0]
        auth = RemoteAuth(
            {"admin": "admin-password"},
            clock=lambda: now[0],
            max_browser_sessions=1,
            max_pairings=1,
        )
        account = auth.authenticate_password("admin", "admin-password", source="test")
        assert account is not None

        first_tokens = auth.issue_browser_tokens(account.id)
        second_tokens = auth.issue_browser_tokens(account.id)
        self.assertIsNone(auth.resolve_access(first_tokens.access_token))
        self.assertIsNotNone(auth.resolve_access(second_tokens.access_token))

        first_code, _ = auth.create_pairing(account.id, "First")
        auth.create_pairing(account.id, "Second")
        with self.assertRaises(PairingCodeInvalid):
            auth.claim_pairing(first_code, b"0" * 32, source="test")

    def test_browser_access_expires_and_refresh_rotates_without_device_credentials(self) -> None:
        now = [1_000.0]
        app = create_relay_app(
            administrators={"admin": "correct horse battery staple"},
            clock=lambda: now[0],
            access_ttl_seconds=60,
            refresh_ttl_seconds=600,
        )
        with TestClient(app) as client:
            login = client.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery staple"})
            self.assertEqual(login.status_code, 200)
            original_refresh = client.cookies.get("somnia_refresh")
            self.assertTrue(client.cookies.get("somnia_access"))
            self.assertTrue(original_refresh)
            self.assertEqual(client.get("/api/devices").status_code, 200)

            now[0] += 61
            self.assertEqual(client.get("/api/devices").status_code, 401)
            refreshed = client.post("/api/auth/refresh")

            self.assertEqual(refreshed.status_code, 200)
            self.assertNotEqual(client.cookies.get("somnia_refresh"), original_refresh)
            self.assertEqual(client.get("/api/devices").status_code, 200)
            self.assertEqual(client.post("/api/auth/logout").status_code, 200)
            self.assertEqual(client.get("/api/devices").status_code, 401)

    def test_expired_browser_access_stops_an_existing_websocket(self) -> None:
        now = [1_000.0]
        app = create_relay_app(
            administrators={"admin": "admin-password"},
            clock=lambda: now[0],
            access_ttl_seconds=60,
        )
        with TestClient(app) as client:
            login(client)
            private_key, device_id = pair_device(client, "Workstation")

            with (
                client.websocket_connect(f"/ws/connector/{device_id}") as connector,
                client.websocket_connect(
                    f"/ws/client/{device_id}", headers={"origin": BROWSER_ORIGIN}
                ) as browser,
            ):
                authenticate_connector(connector, device_id, private_key)
                now[0] += 61
                connector.send_json({"kind": "event", "payload": "must-not-be-forwarded"})

                with self.assertRaises(WebSocketDisconnect) as expired:
                    browser.receive_json()
                self.assertEqual(expired.exception.code, 4401)

    def test_pairing_code_is_high_entropy_short_lived_and_single_use(self) -> None:
        now = [2_000.0]
        app = create_relay_app(
            administrators={"admin": "admin-password"},
            clock=lambda: now[0],
            pairing_ttl_seconds=300,
        )
        with TestClient(app) as client:
            login(client, "admin", "admin-password")
            pairing = client.post("/api/pairings", json={"name": "Workstation"})
            self.assertEqual(pairing.status_code, 201)
            code = pairing.json()["code"]
            self.assertRegex(code, re.compile(r"^[A-Z2-9]{10}$"))

            first_key = Ed25519PrivateKey.generate()
            claimed = claim_pairing(client, code, "Connector Override", first_key)
            self.assertEqual(claimed.status_code, 201)
            self.assertEqual(claimed.json()["name"], "Workstation")
            self.assertEqual(claim_pairing(client, code, "Replay", Ed25519PrivateKey.generate()).status_code, 409)

            expiring = client.post("/api/pairings", json={"name": "Late Device"}).json()["code"]
            now[0] += 301
            self.assertEqual(claim_pairing(client, expiring, "Late", Ed25519PrivateKey.generate()).status_code, 410)

    def test_pairing_claims_are_rate_limited_against_online_guessing(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password"}, pairing_attempt_limit=3)
        with TestClient(app) as client:
            for _ in range(3):
                self.assertEqual(claim_pairing(client, "AAAAAAAAAA", "Guess", Ed25519PrivateKey.generate()).status_code, 401)

            blocked = claim_pairing(client, "BBBBBBBBBB", "Guess", Ed25519PrivateKey.generate())
            self.assertEqual(blocked.status_code, 429)

    def test_signed_connector_and_authenticated_browser_are_isolated_by_account(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password", "other": "other-password"})
        with TestClient(app) as admin, TestClient(app) as other:
            login(admin, "admin", "admin-password")
            login(other, "other", "other-password")
            admin_key, admin_device = pair_device(admin, "Admin PC")
            _, other_device = pair_device(other, "Other PC")

            with admin.websocket_connect(f"/ws/connector/{admin_device}") as connector:
                authenticate_connector(connector, admin_device, admin_key)
                with admin.websocket_connect(
                    f"/ws/client/{admin_device}", headers={"origin": BROWSER_ORIGIN}
                ) as browser:
                    request = {
                        "kind": "request",
                        "request_id": "request-1",
                        "project_id": "project-1",
                        "method": "session.create",
                        "params": {},
                    }
                    browser.send_json(request)
                    self.assertEqual(connector.receive_json(), request)

                with self.assertRaises(WebSocketDisconnect) as cross_account:
                    with other.websocket_connect(
                        f"/ws/client/{admin_device}", headers={"origin": BROWSER_ORIGIN}
                    ) as forbidden:
                        forbidden.receive_json()
                self.assertEqual(cross_account.exception.code, 4403)

            wrong_key = Ed25519PrivateKey.generate()
            with self.assertRaises(WebSocketDisconnect) as cross_device:
                with admin.websocket_connect(f"/ws/connector/{other_device}") as forbidden_connector:
                    authenticate_connector(forbidden_connector, other_device, wrong_key, expect_success=False)
            self.assertEqual(cross_device.exception.code, 4403)

    def test_browser_websocket_rejects_an_unapproved_origin(self) -> None:
        app = create_relay_app(
            administrators={"admin": "admin-password"},
            allowed_origins=[BROWSER_ORIGIN],
        )
        with TestClient(app) as client:
            login(client)
            _, device_id = pair_device(client, "Workstation")

            with self.assertRaises(WebSocketDisconnect) as forbidden:
                with client.websocket_connect(
                    f"/ws/client/{device_id}", headers={"origin": "https://attacker.example"}
                ) as browser:
                    browser.receive_json()
            self.assertEqual(forbidden.exception.code, 4403)

    def test_browser_cannot_route_a_request_to_another_device(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password"})
        with TestClient(app) as client:
            login(client)
            private_key, first_device = pair_device(client, "First")
            _, second_device = pair_device(client, "Second")

            with (
                client.websocket_connect(f"/ws/connector/{first_device}") as connector,
                client.websocket_connect(
                    f"/ws/client/{first_device}", headers={"origin": BROWSER_ORIGIN}
                ) as browser,
            ):
                authenticate_connector(connector, first_device, private_key)
                browser.send_json(
                    {
                        "kind": "request",
                        "request_id": "cross-device",
                        "device_id": second_device,
                        "method": "session.create",
                    }
                )

                rejected = browser.receive_json()
                self.assertEqual(rejected["request_id"], "cross-device")
                self.assertFalse(rejected["ok"])
                self.assertEqual(rejected["error"], "Cross-Device routing is not allowed.")

    def test_revocation_disconnects_device_and_rejects_its_old_key(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password"})
        with TestClient(app) as client:
            login(client, "admin", "admin-password")
            private_key, device_id = pair_device(client, "Laptop")

            with client.websocket_connect(f"/ws/connector/{device_id}") as connector:
                authenticate_connector(connector, device_id, private_key)
                revoked = client.delete(f"/api/devices/{device_id}")
                self.assertEqual(revoked.status_code, 200)
                with self.assertRaises(WebSocketDisconnect) as disconnected:
                    connector.receive_json()
                self.assertEqual(disconnected.exception.code, 4403)

            with self.assertRaises(WebSocketDisconnect) as reconnect:
                with client.websocket_connect(f"/ws/connector/{device_id}") as old_connector:
                    authenticate_connector(old_connector, device_id, private_key, expect_success=False)
            self.assertEqual(reconnect.exception.code, 4403)


class PairSessionTests(unittest.TestCase):
    def test_info_reports_the_configured_web_origin(self) -> None:
        app = create_relay_app(
            administrators={"admin": "admin-password"},
            allowed_origins=["https://somnia.example.com", "https://alt.example.com"],
        )
        with TestClient(app) as client:
            response = client.get("/api/info")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"web_origin": "https://somnia.example.com"})

    def _create_session(self, client: TestClient) -> dict:
        response = client.post("/api/pair-sessions")
        if response.status_code != 201:
            raise AssertionError(response.text)
        return response.json()

    def test_device_flow_full_round_trip(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password"})
        with TestClient(app) as client:
            session = self._create_session(client)
            self.assertTrue(session["session_id"])
            self.assertTrue(session["secret"])
            self.assertGreater(session["expires_at"], 0)

            status_url = f"/api/pair-sessions/{session['session_id']}?secret={session['secret']}"
            self.assertEqual(client.get(status_url).json(), {"status": "pending", "suggested_name": ""})

            login(client)
            approved = client.post(
                f"/api/pair-sessions/{session['session_id']}/approve",
                json={"secret": session["secret"], "device_name": "Desktop PC"},
            )
            self.assertEqual(approved.status_code, 200)
            self.assertEqual(approved.json(), {"status": "approved"})

            polled = client.get(status_url)
            self.assertEqual(polled.status_code, 200)
            self.assertEqual(polled.json()["status"], "approved")
            code = polled.json()["code"]
            self.assertRegex(code, re.compile(r"^[A-Z2-9]{10}$"))

            claimed = claim_pairing(client, code, "ignored", Ed25519PrivateKey.generate())
            self.assertEqual(claimed.status_code, 201)
            self.assertEqual(claimed.json()["name"], "Desktop PC")

    def test_pair_session_carries_the_suggested_device_name(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password"})
        with TestClient(app) as client:
            response = client.post("/api/pair-sessions", json={"device_name": "DESKTOP-Office"})
            self.assertEqual(response.status_code, 201)
            session = response.json()
            status = client.get(f"/api/pair-sessions/{session['session_id']}?secret={session['secret']}")
            self.assertEqual(status.json(), {"status": "pending", "suggested_name": "DESKTOP-Office"})

    def test_pair_session_rejects_a_wrong_secret(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password"})
        with TestClient(app) as client:
            session = self._create_session(client)
            wrong_status = client.get(f"/api/pair-sessions/{session['session_id']}?secret=wrong")
            self.assertEqual(wrong_status.status_code, 403)

            login(client)
            wrong_approve = client.post(
                f"/api/pair-sessions/{session['session_id']}/approve",
                json={"secret": "wrong", "device_name": "Desktop PC"},
            )
            self.assertEqual(wrong_approve.status_code, 403)

    def test_pair_session_approve_requires_authentication(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password"})
        with TestClient(app) as client:
            session = self._create_session(client)
            response = client.post(
                f"/api/pair-sessions/{session['session_id']}/approve",
                json={"secret": session["secret"], "device_name": "Desktop PC"},
            )
            self.assertEqual(response.status_code, 401)

    def test_pair_session_expires_with_the_pairing_ttl(self) -> None:
        now = [3_000.0]
        app = create_relay_app(
            administrators={"admin": "admin-password"},
            clock=lambda: now[0],
            pairing_ttl_seconds=60,
        )
        with TestClient(app) as client:
            session = self._create_session(client)
            now[0] += 61
            status = client.get(f"/api/pair-sessions/{session['session_id']}?secret={session['secret']}")
            self.assertEqual(status.json(), {"status": "expired"})

            login(client)
            approved = client.post(
                f"/api/pair-sessions/{session['session_id']}/approve",
                json={"secret": session["secret"], "device_name": "Desktop PC"},
            )
            self.assertEqual(approved.status_code, 410)

    def test_approved_code_is_returned_exactly_once(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password"})
        with TestClient(app) as client:
            session = self._create_session(client)
            login(client)
            client.post(
                f"/api/pair-sessions/{session['session_id']}/approve",
                json={"secret": session["secret"], "device_name": "Desktop PC"},
            )
            status_url = f"/api/pair-sessions/{session['session_id']}?secret={session['secret']}"
            first = client.get(status_url)
            self.assertEqual(first.json()["status"], "approved")
            second = client.get(status_url)
            self.assertEqual(second.json(), {"status": "expired"})

    def test_pair_session_creation_is_rate_limited_per_source(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password"}, pair_session_attempt_limit=3)
        with TestClient(app) as client:
            for _ in range(3):
                self.assertEqual(client.post("/api/pair-sessions").status_code, 201)
            blocked = client.post("/api/pair-sessions")
            self.assertEqual(blocked.status_code, 429)
            self.assertTrue(blocked.headers.get("Retry-After"))


class RegistrationTests(unittest.TestCase):
    def test_registration_issues_a_session_and_persists_the_account(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_url = f"sqlite:///{(Path(temp_dir) / 'relay.db').as_posix()}"
            app = create_relay_app(administrators={}, database_url=database_url)
            with TestClient(app) as client:
                response = client.post(
                    "/api/auth/register",
                    json={"username": "new_user", "password": "sup3r-secret"},
                )
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json()["username"], "new_user")
                self.assertTrue(response.json()["account_id"])
                self.assertTrue(client.cookies.get("somnia_access"))
                self.assertTrue(client.cookies.get("somnia_refresh"))
                self.assertEqual(client.get("/api/devices").status_code, 200)

            restarted = create_relay_app(administrators={}, database_url=database_url)
            with TestClient(restarted) as restarted_client:
                login(restarted_client, "new_user", "sup3r-secret")
                self.assertEqual(restarted_client.get("/api/devices").status_code, 200)

    def test_registration_rejects_a_case_variant_of_an_existing_username(self) -> None:
        app = create_relay_app(administrators={})
        with TestClient(app) as client:
            first = client.post("/api/auth/register", json={"username": "Alice_1", "password": "sup3r-secret"})
            self.assertEqual(first.status_code, 201)
            duplicate = client.post("/api/auth/register", json={"username": "ALICE_1", "password": "an0ther-secret"})
            self.assertEqual(duplicate.status_code, 409)

    def test_registration_enforces_the_credential_policy(self) -> None:
        app = create_relay_app(administrators={})
        with TestClient(app) as client:
            cases = [
                {"username": "ab", "password": "sup3r-secret"},
                {"username": "bad name!", "password": "sup3r-secret"},
                {"username": "x" * 33, "password": "sup3r-secret"},
                {"username": "valid_user", "password": "short"},
                {"username": "ValidName1", "password": "validname1"},
            ]
            for body in cases:
                with self.subTest(body=body):
                    response = client.post("/api/auth/register", json=body)
                    self.assertEqual(response.status_code, 400)

    def test_registration_is_rate_limited_per_source(self) -> None:
        app = create_relay_app(administrators={}, registration_attempt_limit=3)
        with TestClient(app) as client:
            for index in range(3):
                response = client.post(
                    "/api/auth/register",
                    json={"username": f"user_{index}", "password": "sup3r-secret"},
                )
                self.assertEqual(response.status_code, 201)

            blocked = client.post("/api/auth/register", json={"username": "user_3", "password": "sup3r-secret"})
            self.assertEqual(blocked.status_code, 429)
            self.assertTrue(blocked.headers.get("Retry-After"))

    def test_per_username_login_throttle_slides_without_lockout(self) -> None:
        now = [1_000.0]
        app = create_relay_app(
            administrators={"admin": "admin-password"},
            clock=lambda: now[0],
            login_username_attempt_limit=3,
            login_username_attempt_window_seconds=60,
        )
        with TestClient(app) as client:
            for _ in range(3):
                response = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
                self.assertEqual(response.status_code, 401)

            throttled = client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
            self.assertEqual(throttled.status_code, 429)
            self.assertTrue(throttled.headers.get("Retry-After"))

            now[0] += 61
            recovered = client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
            self.assertEqual(recovered.status_code, 200)

            for _ in range(3):
                response = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
                self.assertEqual(response.status_code, 401)

    def test_registration_can_be_disabled(self) -> None:
        app = create_relay_app(administrators={}, registration_enabled=False)
        with TestClient(app) as client:
            response = client.post("/api/auth/register", json={"username": "new_user", "password": "sup3r-secret"})
            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
