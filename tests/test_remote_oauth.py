from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlencode, urlparse

from starlette.testclient import TestClient

from open_somnia.remote.auth_store import AuthMetadataStore
from open_somnia.remote.oauth import GITHUB_AUTHORIZE_ENDPOINT, OAuthError, OAuthProfile
from open_somnia.remote.relay import create_relay_app
from tests.remote_auth_support import login


class FakeGitHubProvider:
    """Test double with the GitHubOAuthProvider interface; maps codes to profiles."""

    def __init__(self, profiles: dict[str, OAuthProfile]) -> None:
        self.client_id = "fake-client-id"
        self._profiles = dict(profiles)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": "read:user",
                "state": state,
            }
        )
        return f"{GITHUB_AUTHORIZE_ENDPOINT}?{query}"

    async def fetch_profile(self, *, code: str, redirect_uri: str) -> OAuthProfile:
        del redirect_uri
        try:
            return self._profiles[code]
        except KeyError:
            raise OAuthError("GitHub token exchange did not return an access token.") from None


def authorize_state(client: TestClient, *, mode: str = "login", redirect: str = "/") -> str:
    response = client.get(
        "/api/auth/github/authorize",
        params={"mode": mode, "redirect": redirect},
        follow_redirects=False,
    )
    if response.status_code != 302:
        raise AssertionError(response.text)
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


def oauth_callback(client: TestClient, code: str, state: str):
    return client.get(
        "/api/auth/github/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )


def oauth_login(client: TestClient, code: str, *, redirect: str = "/"):
    return oauth_callback(client, code, authorize_state(client, mode="login", redirect=redirect))


class GitHubAuthorizeTests(unittest.TestCase):
    def test_authorize_redirects_to_github_with_a_signed_state(self) -> None:
        provider = FakeGitHubProvider({})
        app = create_relay_app(administrators={}, oauth_providers={"github": provider})
        with TestClient(app) as client:
            response = client.get(
                "/api/auth/github/authorize",
                params={"mode": "login", "redirect": "/dashboard"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)
            location = urlparse(response.headers["location"])
            self.assertEqual(f"{location.scheme}://{location.netloc}{location.path}", GITHUB_AUTHORIZE_ENDPOINT)
            params = parse_qs(location.query)
            self.assertEqual(params["client_id"], ["fake-client-id"])
            self.assertEqual(params["scope"], ["read:user"])
            self.assertEqual(params["redirect_uri"], ["http://testserver/api/auth/github/callback"])
            self.assertTrue(params["state"][0])

    def test_authorize_rejects_invalid_mode_and_disallowed_redirects(self) -> None:
        provider = FakeGitHubProvider({})
        app = create_relay_app(administrators={}, oauth_providers={"github": provider})
        with TestClient(app) as client:
            bad_mode = client.get(
                "/api/auth/github/authorize",
                params={"mode": "steal", "redirect": "/"},
                follow_redirects=False,
            )
            self.assertEqual(bad_mode.status_code, 400)
            for redirect in ("https://evil.example/phish", "//evil.example", "javascript:alert(1)"):
                with self.subTest(redirect=redirect):
                    response = client.get(
                        "/api/auth/github/authorize",
                        params={"mode": "login", "redirect": redirect},
                        follow_redirects=False,
                    )
                    self.assertEqual(response.status_code, 400)

    def test_authorize_allows_an_exact_allowed_origin(self) -> None:
        provider = FakeGitHubProvider({})
        app = create_relay_app(
            administrators={},
            allowed_origins=["https://app.example.com"],
            oauth_providers={"github": provider},
        )
        with TestClient(app) as client:
            response = client.get(
                "/api/auth/github/authorize",
                params={"mode": "login", "redirect": "https://app.example.com/auth/done"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)

    def test_github_endpoints_return_503_when_the_provider_is_not_configured(self) -> None:
        app = create_relay_app(administrators={}, oauth_providers={})
        with TestClient(app) as client:
            authorize = client.get("/api/auth/github/authorize", follow_redirects=False)
            self.assertEqual(authorize.status_code, 503)
            callback = client.get(
                "/api/auth/github/callback",
                params={"code": "x", "state": "y"},
                follow_redirects=False,
            )
            self.assertEqual(callback.status_code, 503)
            bind = client.post("/api/auth/github/bind", json={"code": "x", "state": "y"})
            self.assertEqual(bind.status_code, 503)


class GitHubLoginTests(unittest.TestCase):
    def test_login_creates_an_account_persists_the_identity_and_survives_restart(self) -> None:
        profile = OAuthProfile(provider_user_id="1001", username="octocat", display_name="The Octocat", avatar_url="")
        provider = FakeGitHubProvider({"code-alice": profile})
        with TemporaryDirectory() as temp_dir:
            database_url = f"sqlite:///{(Path(temp_dir) / 'relay.db').as_posix()}"
            app = create_relay_app(administrators={}, database_url=database_url, oauth_providers={"github": provider})
            with TestClient(app) as client:
                response = oauth_login(client, "code-alice")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["location"], "/")
                self.assertTrue(client.cookies.get("somnia_access"))
                self.assertEqual(client.get("/api/devices").status_code, 200)
                identities = client.get("/api/auth/identities").json()["identities"]
                self.assertEqual(len(identities), 1)
                self.assertEqual(identities[0]["provider"], "github")
                self.assertEqual(identities[0]["provider_user_id"], "1001")
                self.assertEqual(identities[0]["provider_username"], "octocat")

            store = AuthMetadataStore(database_url)
            accounts = store.load_accounts()
            stored = store.load_identities()
            store.close()
            self.assertEqual([account.username for account in accounts], ["octocat"])
            self.assertEqual(accounts[0].password_hash, "")
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].account_id, accounts[0].id)

            restarted = create_relay_app(
                administrators={},
                database_url=database_url,
                oauth_providers={"github": provider},
            )
            with TestClient(restarted) as client:
                response = oauth_login(client, "code-alice")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(client.get("/api/devices").status_code, 200)

            store = AuthMetadataStore(database_url)
            usernames = [account.username for account in store.load_accounts()]
            store.close()
            # The second login resolved the persisted identity: no new account.
            self.assertEqual(usernames, ["octocat"])

    def test_login_failure_redirects_back_with_an_error_slug(self) -> None:
        provider = FakeGitHubProvider({})
        app = create_relay_app(administrators={}, oauth_providers={"github": provider})
        with TestClient(app) as client:
            response = oauth_login(client, "unknown-code", redirect="/welcome")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["location"], "/welcome?oauth_error=github_login_failed")
            self.assertFalse(client.cookies.get("somnia_access"))

    def test_callback_rejects_tampered_expired_or_incomplete_state(self) -> None:
        now = [1_000.0]
        profile = OAuthProfile(provider_user_id="1001", username="octocat", display_name="", avatar_url="")
        provider = FakeGitHubProvider({"code-alice": profile})
        app = create_relay_app(
            administrators={},
            clock=lambda: now[0],
            oauth_providers={"github": provider},
        )
        with TestClient(app) as client:
            state = authorize_state(client)
            tampered = oauth_callback(client, "code-alice", f"{state}x")
            self.assertEqual(tampered.status_code, 400)

            missing_code = client.get(
                "/api/auth/github/callback",
                params={"state": state},
                follow_redirects=False,
            )
            self.assertEqual(missing_code.status_code, 400)

            now[0] += 601
            expired = oauth_callback(client, "code-alice", state)
            self.assertEqual(expired.status_code, 400)

    def test_first_login_is_forbidden_when_registration_is_disabled(self) -> None:
        profile = OAuthProfile(provider_user_id="1001", username="octocat", display_name="", avatar_url="")
        provider = FakeGitHubProvider({"code-alice": profile})
        app = create_relay_app(
            administrators={},
            registration_enabled=False,
            oauth_providers={"github": provider},
        )
        with TestClient(app) as client:
            response = oauth_login(client, "code-alice")
            self.assertEqual(response.status_code, 403)
            self.assertFalse(client.cookies.get("somnia_access"))

    def test_oauth_only_account_cannot_log_in_with_a_password(self) -> None:
        profile = OAuthProfile(provider_user_id="1001", username="octocat", display_name="", avatar_url="")
        provider = FakeGitHubProvider({"code-alice": profile})
        app = create_relay_app(administrators={}, oauth_providers={"github": provider})
        with TestClient(app) as client:
            self.assertEqual(oauth_login(client, "code-alice").status_code, 302)
            response = client.post(
                "/api/auth/login",
                json={"username": "octocat", "password": "whatever-password"},
            )
            self.assertEqual(response.status_code, 401)

    def test_oauth_usernames_are_deduplicated_with_numeric_suffixes(self) -> None:
        provider = FakeGitHubProvider(
            {
                "code-one": OAuthProfile(provider_user_id="3001", username="samecat", display_name="", avatar_url=""),
                "code-two": OAuthProfile(provider_user_id="3002", username="samecat", display_name="", avatar_url=""),
            }
        )
        with TemporaryDirectory() as temp_dir:
            database_url = f"sqlite:///{(Path(temp_dir) / 'relay.db').as_posix()}"
            app = create_relay_app(administrators={}, database_url=database_url, oauth_providers={"github": provider})
            with TestClient(app) as client:
                self.assertEqual(oauth_login(client, "code-one").status_code, 302)
                self.assertEqual(oauth_login(client, "code-two").status_code, 302)

            store = AuthMetadataStore(database_url)
            usernames = sorted(account.username for account in store.load_accounts())
            store.close()
            self.assertEqual(usernames, ["samecat", "samecat-2"])
            store.close()


class GitHubBindTests(unittest.TestCase):
    def _bind(self, client: TestClient, code: str, *, redirect: str = "/settings") -> tuple[str, object]:
        state = authorize_state(client, mode="bind", redirect=redirect)
        callback = oauth_callback(client, code, state)
        return state, callback

    def test_bind_two_step_flow_is_idempotent_and_conflicts_across_accounts(self) -> None:
        profile = OAuthProfile(provider_user_id="2002", username="bindcat", display_name="", avatar_url="")
        provider = FakeGitHubProvider({"code-bind": profile})
        app = create_relay_app(administrators={"admin": "admin-password"}, oauth_providers={"github": provider})
        with TestClient(app) as client, TestClient(app) as other:
            login(client)
            state, callback = self._bind(client, "code-bind")
            self.assertEqual(callback.status_code, 302)
            location = callback.headers["location"]
            self.assertTrue(location.startswith("/settings#"))
            # The bind callback must not hand out session cookies.
            self.assertIsNone(callback.headers.get("set-cookie"))
            fragment = parse_qs(urlparse(location).fragment)
            self.assertEqual(fragment["provider"], ["github"])
            self.assertEqual(fragment["code"], ["code-bind"])
            self.assertEqual(fragment["state"], [state])

            bound = client.post("/api/auth/github/bind", json={"code": "code-bind", "state": state})
            self.assertEqual(bound.status_code, 200)
            payload = bound.json()["identity"]
            self.assertEqual(payload["provider"], "github")
            self.assertEqual(payload["provider_user_id"], "2002")
            self.assertEqual(payload["provider_username"], "bindcat")

            listed = client.get("/api/auth/identities").json()["identities"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["provider_user_id"], "2002")

            again = client.post("/api/auth/github/bind", json={"code": "code-bind", "state": state})
            self.assertEqual(again.status_code, 200)
            self.assertEqual(len(client.get("/api/auth/identities").json()["identities"]), 1)

            registered = other.post(
                "/api/auth/register",
                json={"username": "otheruser", "password": "sup3r-secret"},
            )
            self.assertEqual(registered.status_code, 201)
            other_state, _ = self._bind(other, "code-bind")
            conflict = other.post("/api/auth/github/bind", json={"code": "code-bind", "state": other_state})
            self.assertEqual(conflict.status_code, 409)

    def test_bind_requires_authentication_and_a_bind_mode_state(self) -> None:
        profile = OAuthProfile(provider_user_id="2002", username="bindcat", display_name="", avatar_url="")
        provider = FakeGitHubProvider({"code-bind": profile})
        app = create_relay_app(administrators={"admin": "admin-password"}, oauth_providers={"github": provider})
        with TestClient(app) as client:
            anonymous = client.post("/api/auth/github/bind", json={"code": "code-bind", "state": "x"})
            self.assertEqual(anonymous.status_code, 401)
            self.assertEqual(client.get("/api/auth/identities").status_code, 401)
            self.assertEqual(client.delete("/api/auth/identities/github").status_code, 401)

            login(client)
            login_mode_state = authorize_state(client, mode="login")
            wrong_mode = client.post(
                "/api/auth/github/bind",
                json={"code": "code-bind", "state": login_mode_state},
            )
            self.assertEqual(wrong_mode.status_code, 400)

            state = authorize_state(client, mode="bind", redirect="/settings")
            bad_code = client.post("/api/auth/github/bind", json={"code": "unknown", "state": state})
            self.assertEqual(bad_code.status_code, 502)


class GitHubUnbindTests(unittest.TestCase):
    def test_account_with_a_password_can_unbind(self) -> None:
        profile = OAuthProfile(provider_user_id="2002", username="bindcat", display_name="", avatar_url="")
        provider = FakeGitHubProvider({"code-bind": profile})
        app = create_relay_app(administrators={"admin": "admin-password"}, oauth_providers={"github": provider})
        with TestClient(app) as client:
            login(client)
            state = authorize_state(client, mode="bind", redirect="/settings")
            self.assertEqual(oauth_callback(client, "code-bind", state).status_code, 302)
            bound = client.post("/api/auth/github/bind", json={"code": "code-bind", "state": state})
            self.assertEqual(bound.status_code, 200)

            removed = client.delete("/api/auth/identities/github")
            self.assertEqual(removed.status_code, 200)
            self.assertEqual(removed.json()["identity"]["provider_user_id"], "2002")
            self.assertEqual(client.get("/api/auth/identities").json()["identities"], [])
            self.assertEqual(client.delete("/api/auth/identities/github").status_code, 404)

    def test_oauth_only_account_cannot_unbind_its_last_login_method(self) -> None:
        profile = OAuthProfile(provider_user_id="4001", username="solocat", display_name="", avatar_url="")
        provider = FakeGitHubProvider({"code-solo": profile})
        app = create_relay_app(administrators={}, oauth_providers={"github": provider})
        with TestClient(app) as client:
            self.assertEqual(oauth_login(client, "code-solo").status_code, 302)
            self.assertEqual(len(client.get("/api/auth/identities").json()["identities"]), 1)
            removed = client.delete("/api/auth/identities/github")
            self.assertEqual(removed.status_code, 409)
            self.assertEqual(len(client.get("/api/auth/identities").json()["identities"]), 1)


if __name__ == "__main__":
    unittest.main()
