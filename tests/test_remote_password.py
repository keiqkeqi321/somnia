from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from starlette.testclient import TestClient

from open_somnia.remote.auth_store import AuthMetadataStore
from open_somnia.remote.oauth import OAuthProfile
from open_somnia.remote.relay import create_relay_app
from tests.remote_auth_support import login
from tests.test_remote_oauth import FakeGitHubProvider, oauth_login


class OAuthAccountSetPasswordTests(unittest.TestCase):
    def test_oauth_account_sets_password_then_unlinks_github_and_logs_in_with_password(self) -> None:
        profile = OAuthProfile(provider_user_id="5001", username="setcat", display_name="", avatar_url="")
        provider = FakeGitHubProvider({"code-set": profile})
        with TemporaryDirectory() as temp_dir:
            database_url = f"sqlite:///{(Path(temp_dir) / 'relay.db').as_posix()}"
            app = create_relay_app(administrators={}, database_url=database_url, oauth_providers={"github": provider})
            with TestClient(app) as client:
                self.assertEqual(oauth_login(client, "code-set").status_code, 302)

                payload = client.get("/api/auth/identities").json()
                self.assertEqual(len(payload["identities"]), 1)
                self.assertFalse(payload["has_password"])
                # The last-auth-method guard blocks unbind while there is no password.
                self.assertEqual(client.delete("/api/auth/identities/github").status_code, 409)

                response = client.post("/api/auth/password", json={"password": "brand-new-password"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "password_set"})
                self.assertTrue(client.get("/api/auth/identities").json()["has_password"])

                # With a password set, the guard no longer treats GitHub as the last login method.
                removed = client.delete("/api/auth/identities/github")
                self.assertEqual(removed.status_code, 200)
                self.assertEqual(client.get("/api/auth/identities").json()["identities"], [])

                logged_in = client.post(
                    "/api/auth/login",
                    json={"username": "setcat", "password": "brand-new-password"},
                )
                self.assertEqual(logged_in.status_code, 200)

            store = AuthMetadataStore(database_url)
            accounts = store.load_accounts()
            store.close()
            self.assertEqual([account.username for account in accounts], ["setcat"])
            self.assertTrue(accounts[0].password_hash.startswith("$argon2"))

    def test_setting_the_first_password_does_not_require_a_current_password(self) -> None:
        profile = OAuthProfile(provider_user_id="5002", username="freshcat", display_name="", avatar_url="")
        provider = FakeGitHubProvider({"code-fresh": profile})
        app = create_relay_app(administrators={}, oauth_providers={"github": provider})
        with TestClient(app) as client:
            self.assertEqual(oauth_login(client, "code-fresh").status_code, 302)
            response = client.post("/api/auth/password", json={"password": "first-password-1"})
            self.assertEqual(response.status_code, 200)


class PasswordAccountChangePasswordTests(unittest.TestCase):
    def test_change_requires_and_verifies_the_current_password(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password"})
        with TestClient(app) as client:
            login(client)

            missing = client.post("/api/auth/password", json={"password": "replacement-1"})
            self.assertEqual(missing.status_code, 400)

            wrong = client.post(
                "/api/auth/password",
                json={"password": "replacement-1", "current_password": "not-the-password"},
            )
            self.assertEqual(wrong.status_code, 403)

            changed = client.post(
                "/api/auth/password",
                json={"password": "replacement-1", "current_password": "admin-password"},
            )
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json(), {"status": "password_set"})

            stale = client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
            self.assertEqual(stale.status_code, 401)
            fresh = client.post("/api/auth/login", json={"username": "admin", "password": "replacement-1"})
            self.assertEqual(fresh.status_code, 200)


class PasswordPolicyTests(unittest.TestCase):
    def test_policy_violations_are_rejected(self) -> None:
        app = create_relay_app(administrators={"longadminname": "longadminname-password"})
        with TestClient(app) as client:
            login(client, username="longadminname", password="longadminname-password")
            short = client.post(
                "/api/auth/password",
                json={"password": "short", "current_password": "longadminname-password"},
            )
            self.assertEqual(short.status_code, 400)
            same_as_username = client.post(
                "/api/auth/password",
                json={"password": "LongAdminName", "current_password": "longadminname-password"},
            )
            self.assertEqual(same_as_username.status_code, 400)
            # Neither attempt changed the password.
            still_valid = client.post(
                "/api/auth/login",
                json={"username": "longadminname", "password": "longadminname-password"},
            )
            self.assertEqual(still_valid.status_code, 200)

    def test_set_password_requires_authentication(self) -> None:
        app = create_relay_app(administrators={"admin": "admin-password"})
        with TestClient(app) as client:
            response = client.post("/api/auth/password", json={"password": "whatever-123"})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"], "Authentication required.")


if __name__ == "__main__":
    unittest.main()
