"""Shared template for the Relay's OAuth channel strategies.

The base implementations of the hooks match the common GitHub-style
authorization-code flow; a channel module overrides only the pieces that
differ (see :mod:`open_somnia.remote.oauth.gitee`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx


OAUTH_HTTP_TIMEOUT_SECONDS = 10.0


class OAuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OAuthProfile:
    provider_user_id: str
    username: str
    display_name: str
    avatar_url: str


class OAuthProvider:
    """Template for one OAuth channel; subclasses hook in the provider specifics."""

    name = ""
    label = ""
    authorize_endpoint = ""
    token_endpoint = ""
    userinfo_endpoint = ""
    scope = ""

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = str(client_id).strip()
        self._client_secret = str(client_secret).strip()

    # Strategy hooks.
    def authorize_params(self, *, state: str, redirect_uri: str) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": self.scope,
            "state": state,
        }

    def token_form(self, *, code: str, redirect_uri: str) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "client_secret": self._client_secret,
            "code": str(code),
            "redirect_uri": redirect_uri,
        }

    def userinfo_headers(self, access_token: str) -> dict[str, str]:
        return {"Accept": "application/json"}

    def userinfo_params(self, access_token: str) -> dict[str, str]:
        return {}

    def parse_profile(self, payload: dict[str, Any]) -> OAuthProfile:
        user_id = payload.get("id")
        username = payload.get("login")
        if not user_id or not username:
            raise OAuthError(f"{self.label} profile response is missing the user id or login.")
        return OAuthProfile(
            provider_user_id=str(user_id),
            username=str(username),
            display_name=str(payload.get("name") or ""),
            avatar_url=str(payload.get("avatar_url") or ""),
        )

    # Template methods shared by every channel.
    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        query = urlencode(self.authorize_params(state=state, redirect_uri=redirect_uri))
        return f"{self.authorize_endpoint}?{query}"

    async def fetch_profile(self, *, code: str, redirect_uri: str) -> OAuthProfile:
        try:
            async with httpx.AsyncClient(timeout=OAUTH_HTTP_TIMEOUT_SECONDS) as client:
                token_response = await client.post(
                    self.token_endpoint,
                    data=self.token_form(code=code, redirect_uri=redirect_uri),
                    headers={"Accept": "application/json"},
                )
                if token_response.status_code != 200:
                    raise OAuthError(f"{self.label} token exchange failed with status {token_response.status_code}.")
                access_token = token_response.json().get("access_token")
                if not access_token:
                    # Providers may answer 200 with an error payload for bad codes.
                    raise OAuthError(f"{self.label} token exchange did not return an access token.")
                user_response = await client.get(
                    self.userinfo_endpoint,
                    headers=self.userinfo_headers(access_token),
                    params=self.userinfo_params(access_token),
                )
                if user_response.status_code != 200:
                    raise OAuthError(f"{self.label} profile lookup failed with status {user_response.status_code}.")
                payload = user_response.json()
        except OAuthError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError(f"{self.label} sign-in failed: {exc}") from exc
        return self.parse_profile(payload)
