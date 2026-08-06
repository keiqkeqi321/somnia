from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from starlette.testclient import TestClient

from open_somnia.remote.auth_store import AuthMetadataStore
from open_somnia.remote.oauth import (
    GiteeOAuthProvider,
    GitHubOAuthProvider,
    OAuthError,
    OAuthProfile,
    oauth_provider_from_env,
    oauth_providers_from_env,
)
from open_somnia.remote.relay import create_relay_app
from tests.remote_auth_support import login

GITEE_AUTHORIZE_ENDPOINT = GiteeOAuthProvider.authorize_endpoint


def patched_transport(handler):
    """Route ``httpx.AsyncClient`` instances through a MockTransport in scope."""
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    return mock.patch.object(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))


class GiteeOAuthProviderTests(unittest.TestCase):
    def test_authorize_url_carries_the_gitee_specific_parameters(self) -> None:
        provider = GiteeOAuthProvider("client-id", "client-secret")
        url = provider.authorize_url(state="state-1", redirect_uri="https://relay.example.com/api/auth/gitee/callback")
        parsed = urlparse(url)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", GITEE_AUTHORIZE_ENDPOINT)
        params = parse_qs(parsed.query)
        self.assertEqual(params["client_id"], ["client-id"])
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["scope"], ["user_info"])
        self.assertEqual(params["redirect_uri"], ["https://relay.example.com/api/auth/gitee/callback"])
        self.assertEqual(params["state"], ["state-1"])

    def test_fetch_profile_posts_grant_type_and_reads_userinfo_via_a_query_token(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                seen["token_form"] = parse_qs(request.content.decode("utf-8"))
                seen["token_accept"] = request.headers.get("accept")
                return httpx.Response(200, json={"access_token": "gitee-token"})
            seen["userinfo_path"] = request.url.path
            seen["userinfo_query"] = parse_qs(request.url.query.decode("ascii"))
            seen["userinfo_headers"] = dict(request.headers)
            return httpx.Response(
                200,
                json={"id": 777, "login": "giteecat", "name": "Gitee Cat", "avatar_url": "https://gitee.com/a.png"},
            )

        provider = GiteeOAuthProvider("client-id", "client-secret")
        with patched_transport(handler):
            profile = asyncio.run(
                provider.fetch_profile(code="code-1", redirect_uri="https://relay.example.com/api/auth/gitee/callback")
            )

        token_form = seen["token_form"]
        assert isinstance(token_form, dict)
        self.assertEqual(token_form["grant_type"], ["authorization_code"])
        self.assertEqual(token_form["code"], ["code-1"])
        self.assertEqual(token_form["client_id"], ["client-id"])
        self.assertEqual(token_form["client_secret"], ["client-secret"])
        self.assertEqual(token_form["redirect_uri"], ["https://relay.example.com/api/auth/gitee/callback"])
        self.assertEqual(seen["token_accept"], "application/json")
        self.assertEqual(seen["userinfo_path"], "/api/v5/user")
        userinfo_query = seen["userinfo_query"]
        assert isinstance(userinfo_query, dict)
        self.assertEqual(userinfo_query["access_token"], ["gitee-token"])
        userinfo_headers = seen["userinfo_headers"]
        assert isinstance(userinfo_headers, dict)
        self.assertNotIn("authorization", userinfo_headers)
        self.assertEqual(
            profile,
            OAuthProfile(
                provider_user_id="777",
                username="giteecat",
                display_name="Gitee Cat",
                avatar_url="https://gitee.com/a.png",
            ),
        )

    def test_fetch_profile_raises_oauth_error_when_the_token_response_lacks_an_access_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            # Gitee answers 200 with an error payload for bad codes.
            return httpx.Response(200, json={"error": "invalid_code"})

        provider = GiteeOAuthProvider("client-id", "client-secret")
        with patched_transport(handler):
            with self.assertRaises(OAuthError):
                asyncio.run(provider.fetch_profile(code="bad-code", redirect_uri="https://relay.example.com/cb"))

    def test_fetch_profile_raises_oauth_error_on_http_failures(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                return httpx.Response(200, json={"access_token": "gitee-token"})
            return httpx.Response(401, json={"message": "unauthorized"})

        provider = GiteeOAuthProvider("client-id", "client-secret")
        with patched_transport(handler):
            with self.assertRaises(OAuthError):
                asyncio.run(provider.fetch_profile(code="code-1", redirect_uri="https://relay.example.com/cb"))

    def test_parse_profile_requires_the_user_id_and_login(self) -> None:
        provider = GiteeOAuthProvider("client-id", "client-secret")
        with self.assertRaises(OAuthError):
            provider.parse_profile({"login": "giteecat"})
        with self.assertRaises(OAuthError):
            provider.parse_profile({"id": 777})


class OAuthProviderFromEnvTests(unittest.TestCase):
    def test_reads_the_channel_credentials_from_the_environment(self) -> None:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("SOMNIA_GITEE_", "SOMNIA_GITHUB_"))}
        env["SOMNIA_GITEE_CLIENT_ID"] = "gitee-id"
        env["SOMNIA_GITEE_CLIENT_SECRET"] = "gitee-secret"
        with mock.patch.dict(os.environ, env, clear=True):
            provider = oauth_provider_from_env("gitee")
            self.assertIsInstance(provider, GiteeOAuthProvider)
            providers = oauth_providers_from_env()
            self.assertEqual(sorted(providers), ["gitee"])
            self.assertIsInstance(providers["gitee"], GiteeOAuthProvider)

    def test_returns_none_for_unknown_or_partially_configured_channels(self) -> None:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("SOMNIA_GITEE_", "SOMNIA_GITHUB_"))}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(oauth_provider_from_env("foo"))
            self.assertIsNone(oauth_provider_from_env("gitee"))
            self.assertEqual(oauth_providers_from_env(), {})
        with mock.patch.dict(os.environ, {**env, "SOMNIA_GITEE_CLIENT_ID": "only-id"}, clear=True):
            self.assertIsNone(oauth_provider_from_env("gitee"))

    def test_github_keeps_its_own_environment_prefix(self) -> None:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("SOMNIA_GITEE_", "SOMNIA_GITHUB_"))}
        env["SOMNIA_GITHUB_CLIENT_ID"] = "github-id"
        env["SOMNIA_GITHUB_CLIENT_SECRET"] = "github-secret"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsInstance(oauth_provider_from_env("github"), GitHubOAuthProvider)
            self.assertIsNone(oauth_provider_from_env("gitee"))


class FakeGiteeProvider:
    """Test double with the GiteeOAuthProvider interface; maps codes to profiles."""

    def __init__(self, profiles: dict[str, OAuthProfile]) -> None:
        self.client_id = "fake-gitee-client-id"
        self._profiles = dict(profiles)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "user_info",
                "state": state,
            }
        )
        return f"{GITEE_AUTHORIZE_ENDPOINT}?{query}"

    async def fetch_profile(self, *, code: str, redirect_uri: str) -> OAuthProfile:
        del redirect_uri
        try:
            return self._profiles[code]
        except KeyError:
            raise OAuthError("Gitee token exchange did not return an access token.") from None


def authorize_state(client: TestClient, *, mode: str = "login", redirect: str = "/") -> str:
    response = client.get(
        "/api/auth/gitee/authorize",
        params={"mode": mode, "redirect": redirect},
        follow_redirects=False,
    )
    if response.status_code != 302:
        raise AssertionError(response.text)
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


def oauth_callback(client: TestClient, code: str, state: str):
    return client.get(
        "/api/auth/gitee/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )


def oauth_login(client: TestClient, code: str, *, redirect: str = "/"):
    return oauth_callback(client, code, authorize_state(client, mode="login", redirect=redirect))


class GiteeAuthorizeTests(unittest.TestCase):
    def test_authorize_redirects_to_gitee_with_a_signed_state(self) -> None:
        provider = FakeGiteeProvider({})
        app = create_relay_app(administrators={}, oauth_providers={"gitee": provider})
        with TestClient(app) as client:
            response = client.get(
                "/api/auth/gitee/authorize",
                params={"mode": "login", "redirect": "/dashboard"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)
            location = urlparse(response.headers["location"])
            self.assertEqual(f"{location.scheme}://{location.netloc}{location.path}", GITEE_AUTHORIZE_ENDPOINT)
            params = parse_qs(location.query)
            self.assertEqual(params["client_id"], ["fake-gitee-client-id"])
            self.assertEqual(params["scope"], ["user_info"])
            self.assertEqual(params["response_type"], ["code"])
            self.assertEqual(params["redirect_uri"], ["http://testserver/api/auth/gitee/callback"])
            self.assertTrue(params["state"][0])

    def test_unknown_channels_answer_404(self) -> None:
        app = create_relay_app(administrators={}, oauth_providers={})
        with TestClient(app) as client:
            authorize = client.get("/api/auth/foo/authorize", follow_redirects=False)
            self.assertEqual(authorize.status_code, 404)
            callback = client.get(
                "/api/auth/foo/callback",
                params={"code": "x", "state": "y"},
                follow_redirects=False,
            )
            self.assertEqual(callback.status_code, 404)
            bind = client.post("/api/auth/foo/bind", json={"code": "x", "state": "y"})
            self.assertEqual(bind.status_code, 404)

    def test_known_but_unconfigured_channels_answer_503(self) -> None:
        profile = OAuthProfile(provider_user_id="1001", username="octocat", display_name="", avatar_url="")
        github = FakeGiteeProvider({"code-alice": profile})
        app = create_relay_app(administrators={}, oauth_providers={"github": github})
        with TestClient(app) as client:
            authorize = client.get("/api/auth/gitee/authorize", follow_redirects=False)
            self.assertEqual(authorize.status_code, 503)
            callback = client.get(
                "/api/auth/gitee/callback",
                params={"code": "x", "state": "y"},
                follow_redirects=False,
            )
            self.assertEqual(callback.status_code, 503)
            bind = client.post("/api/auth/gitee/bind", json={"code": "x", "state": "y"})
            self.assertEqual(bind.status_code, 503)


class GiteeLoginTests(unittest.TestCase):
    def test_login_creates_an_account_and_persists_the_gitee_identity(self) -> None:
        profile = OAuthProfile(provider_user_id="5001", username="giteecat", display_name="Gitee Cat", avatar_url="")
        provider = FakeGiteeProvider({"code-alice": profile})
        with TemporaryDirectory() as temp_dir:
            database_url = f"sqlite:///{(Path(temp_dir) / 'relay.db').as_posix()}"
            app = create_relay_app(administrators={}, database_url=database_url, oauth_providers={"gitee": provider})
            with TestClient(app) as client:
                response = oauth_login(client, "code-alice")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["location"], "/")
                self.assertTrue(client.cookies.get("somnia_access"))
                self.assertEqual(client.get("/api/devices").status_code, 200)
                identities = client.get("/api/auth/identities").json()["identities"]
                self.assertEqual(len(identities), 1)
                self.assertEqual(identities[0]["provider"], "gitee")
                self.assertEqual(identities[0]["provider_user_id"], "5001")
                self.assertEqual(identities[0]["provider_username"], "giteecat")

            store = AuthMetadataStore(database_url)
            accounts = store.load_accounts()
            stored = store.load_identities()
            store.close()
            self.assertEqual([account.username for account in accounts], ["giteecat"])
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].provider, "gitee")
            self.assertEqual(stored[0].account_id, accounts[0].id)

    def test_login_failure_redirects_back_with_a_gitee_error_slug(self) -> None:
        provider = FakeGiteeProvider({})
        app = create_relay_app(administrators={}, oauth_providers={"gitee": provider})
        with TestClient(app) as client:
            response = oauth_login(client, "unknown-code", redirect="/welcome")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["location"], "/welcome?oauth_error=gitee_login_failed")
            self.assertFalse(client.cookies.get("somnia_access"))


class GiteeBindTests(unittest.TestCase):
    def test_bind_round_trip_uses_the_gitee_channel(self) -> None:
        profile = OAuthProfile(provider_user_id="6002", username="bindcat", display_name="", avatar_url="")
        provider = FakeGiteeProvider({"code-bind": profile})
        app = create_relay_app(administrators={"admin": "admin-password"}, oauth_providers={"gitee": provider})
        with TestClient(app) as client:
            login(client)
            state = authorize_state(client, mode="bind", redirect="/settings")
            callback = oauth_callback(client, "code-bind", state)
            self.assertEqual(callback.status_code, 302)
            location = callback.headers["location"]
            self.assertTrue(location.startswith("/settings#"))
            self.assertIsNone(callback.headers.get("set-cookie"))
            fragment = parse_qs(urlparse(location).fragment)
            self.assertEqual(fragment["provider"], ["gitee"])
            self.assertEqual(fragment["code"], ["code-bind"])
            self.assertEqual(fragment["state"], [state])

            bound = client.post("/api/auth/gitee/bind", json={"code": "code-bind", "state": state})
            self.assertEqual(bound.status_code, 200)
            payload = bound.json()["identity"]
            self.assertEqual(payload["provider"], "gitee")
            self.assertEqual(payload["provider_user_id"], "6002")
            self.assertEqual(payload["provider_username"], "bindcat")


class InfoEndpointTests(unittest.TestCase):
    def test_info_lists_the_configured_oauth_channels(self) -> None:
        providers = {
            "github": FakeGiteeProvider({}),
            "gitee": FakeGiteeProvider({}),
        }
        app = create_relay_app(administrators={}, oauth_providers=providers)
        with TestClient(app) as client:
            payload = client.get("/api/info").json()
            self.assertEqual(payload["oauth_providers"], ["gitee", "github"])

    def test_info_reports_an_empty_channel_list_when_nothing_is_configured(self) -> None:
        app = create_relay_app(administrators={}, oauth_providers={})
        with TestClient(app) as client:
            payload = client.get("/api/info").json()
            self.assertEqual(payload["oauth_providers"], [])


if __name__ == "__main__":
    unittest.main()
