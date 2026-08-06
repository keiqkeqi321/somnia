"""Framework-agnostic OAuth channels for the Relay (GitHub, Gitee, ...).

Each channel is a small strategy object (:class:`OAuthProvider` subclass)
registered in :data:`OAUTH_PROVIDER_TYPES`; the Relay mounts one set of
endpoints parameterized by the channel name.

Configuration (environment variables, same style as ``SOMNIA_ADMIN_PASSWORD``,
with ``{NAME}`` the uppercased channel name, e.g. ``GITHUB``):

- ``SOMNIA_{NAME}_CLIENT_ID`` / ``SOMNIA_{NAME}_CLIENT_SECRET``: both are
  required; when either is missing the Relay keeps the channel's endpoints
  mounted but answers 503.
- ``SOMNIA_{NAME}_REDIRECT_URI``: optional override for the callback URL
  registered with the provider's OAuth App. Defaults to
  ``{request.base_url}api/auth/{name}/callback`` and must be identical for
  the authorize step and the code exchange.

SPA contract:

- ``GET /api/auth/{provider}/authorize?mode=login|bind&redirect=<target>``
  302-redirects to the provider. ``redirect`` must be a same-origin relative
  path (a ``//`` prefix is rejected) or an absolute URL whose origin exactly
  matches one of the Relay's allowed browser origins; it is carried inside
  the signed state.
- Login mode: the callback sets the session cookies and 302-redirects to
  ``redirect`` (or ``{redirect}?oauth_error=<slug>`` on failure).
- Bind mode is two-step because the ``somnia_access`` cookie is
  ``SameSite=strict``: the browser does not send it when the provider
  redirects back to the callback, so the callback cannot know which account
  is being bound. Instead it 302-redirects to
  ``{redirect}#provider=<name>&code=...&state=...`` (URL fragment, so the
  code never hits SPA server logs), and the SPA then calls
  ``POST /api/auth/{provider}/bind`` with the session cookie attached to
  finish.

State is an HMAC-SHA256 self-signed token (``base64url(json).base64url(sig)``)
derived from the Relay secret key with domain separation. It is verified but
not consumed server-side: the provider's authorization code itself is
single-use, which bounds replay to the state TTL. The ``provider`` claim
keeps states from different channels isolated.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Callable, Collection
from urllib.parse import urlencode, urlparse

import httpx


Clock = Callable[[], float]

GITHUB_AUTHORIZE_ENDPOINT = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_ENDPOINT = "https://github.com/login/oauth/access_token"
GITHUB_USERINFO_ENDPOINT = "https://api.github.com/user"
OAUTH_HTTP_TIMEOUT_SECONDS = 10.0
OAUTH_STATE_TTL_SECONDS = 600

_OAUTH_STATE_DOMAIN = b"somnia-oauth-state-v1"


class OAuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OAuthProfile:
    provider_user_id: str
    username: str
    display_name: str
    avatar_url: str


class OAuthProvider:
    """Template for one OAuth channel; subclasses hook in the provider specifics.

    The base implementations of the hooks match the common GitHub-style
    authorization-code flow; a channel overrides only the pieces that differ
    (see :class:`GiteeOAuthProvider`).
    """

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


class GitHubOAuthProvider(OAuthProvider):
    """Exchanges GitHub authorization codes for user profiles."""

    name = "github"
    label = "GitHub"
    authorize_endpoint = GITHUB_AUTHORIZE_ENDPOINT
    token_endpoint = GITHUB_TOKEN_ENDPOINT
    userinfo_endpoint = GITHUB_USERINFO_ENDPOINT
    scope = "read:user"

    def userinfo_headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }


class GiteeOAuthProvider(OAuthProvider):
    """Gitee channel — same profile shape as GitHub, different wire conventions."""

    name = "gitee"
    label = "Gitee"
    authorize_endpoint = "https://gitee.com/oauth/authorize"
    token_endpoint = "https://gitee.com/oauth/token"
    userinfo_endpoint = "https://gitee.com/api/v5/user"
    scope = "user_info"

    def authorize_params(self, *, state: str, redirect_uri: str) -> dict[str, str]:
        # Gitee requires an explicit response_type on the authorize request.
        return {**super().authorize_params(state=state, redirect_uri=redirect_uri), "response_type": "code"}

    def token_form(self, *, code: str, redirect_uri: str) -> dict[str, str]:
        # Gitee requires an explicit grant_type on the code exchange.
        return {**super().token_form(code=code, redirect_uri=redirect_uri), "grant_type": "authorization_code"}

    def userinfo_params(self, access_token: str) -> dict[str, str]:
        # The v5 API convention is the token as a query parameter.
        return {"access_token": access_token}


OAUTH_PROVIDER_TYPES: dict[str, type[OAuthProvider]] = {
    "github": GitHubOAuthProvider,
    "gitee": GiteeOAuthProvider,
}


def oauth_provider_from_env(name: str) -> OAuthProvider | None:
    """Build the channel instance when its client credentials are configured."""
    provider_type = OAUTH_PROVIDER_TYPES.get(str(name).strip().lower())
    if provider_type is None:
        return None
    prefix = f"SOMNIA_{provider_type.name.upper()}"
    client_id = os.environ.get(f"{prefix}_CLIENT_ID", "").strip()
    client_secret = os.environ.get(f"{prefix}_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    return provider_type(client_id, client_secret)


def oauth_providers_from_env() -> dict[str, OAuthProvider]:
    """The instances for every channel whose credentials are configured."""
    providers: dict[str, OAuthProvider] = {}
    for name in OAUTH_PROVIDER_TYPES:
        provider = oauth_provider_from_env(name)
        if provider is not None:
            providers[name] = provider
    return providers


def derive_state_signing_key(secret_key: bytes) -> bytes:
    """Derive the OAuth state signing key from the Relay secret (domain-separated)."""
    return hmac.new(secret_key, _OAUTH_STATE_DOMAIN, hashlib.sha256).digest()


def issue_oauth_state(
    signing_key: bytes,
    *,
    provider: str,
    mode: str,
    redirect: str,
    clock: Clock = time.time,
) -> str:
    payload = {
        "provider": provider,
        "mode": mode,
        "redirect": redirect,
        "nonce": secrets.token_urlsafe(16),
        "exp": clock() + OAUTH_STATE_TTL_SECONDS,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).rstrip(b"=")
    signature = base64.urlsafe_b64encode(hmac.new(signing_key, body, hashlib.sha256).digest()).rstrip(b"=")
    return f"{body.decode('ascii')}.{signature.decode('ascii')}"


def verify_oauth_state(
    signing_key: bytes,
    token: str,
    *,
    provider: str,
    clock: Clock = time.time,
) -> dict[str, Any] | None:
    """Return the state payload only when signature, expiry, and provider all check out."""
    try:
        body, signature = str(token).split(".")
        expected = hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest()
        decoded = base64.b64decode(signature + "=" * (-len(signature) % 4), altchars=b"-_", validate=True)
        if not hmac.compare_digest(expected, decoded):
            return None
        payload = json.loads(base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True))
        if not isinstance(payload, dict):
            return None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if float(payload.get("exp", 0)) <= clock():
        return None
    if payload.get("provider") != provider:
        return None
    return payload


def is_allowed_redirect(value: str, allowed_origins: Collection[str]) -> bool:
    """Allow same-origin relative paths (never ``//``) or exact allowed origins."""
    text = str(value).strip()
    if not text:
        return False
    if text.startswith("/"):
        return not text.startswith("//")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    return f"{parsed.scheme}://{parsed.netloc}" in allowed_origins
