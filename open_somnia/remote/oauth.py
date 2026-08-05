"""Framework-agnostic GitHub OAuth channel for the Relay.

Configuration (environment variables, same style as ``SOMNIA_ADMIN_PASSWORD``):

- ``SOMNIA_GITHUB_CLIENT_ID`` / ``SOMNIA_GITHUB_CLIENT_SECRET``: both are
  required; when either is missing the Relay keeps its GitHub endpoints
  mounted but answers 503.
- ``SOMNIA_GITHUB_REDIRECT_URI``: optional override for the callback URL
  registered with the GitHub OAuth App. Defaults to
  ``{request.base_url}api/auth/github/callback`` and must be identical for
  the authorize step and the code exchange.

SPA contract:

- ``GET /api/auth/github/authorize?mode=login|bind&redirect=<target>``
  302-redirects to GitHub. ``redirect`` must be a same-origin relative path
  (a ``//`` prefix is rejected) or an absolute URL whose origin exactly
  matches one of the Relay's allowed browser origins; it is carried inside
  the signed state.
- Login mode: the callback sets the session cookies and 302-redirects to
  ``redirect`` (or ``{redirect}?oauth_error=<slug>`` on failure).
- Bind mode is two-step because the ``somnia_access`` cookie is
  ``SameSite=strict``: the browser does not send it when GitHub redirects
  back to the callback, so the callback cannot know which account is being
  bound. Instead it 302-redirects to
  ``{redirect}#provider=github&code=...&state=...`` (URL fragment, so the
  code never hits SPA server logs), and the SPA then calls
  ``POST /api/auth/github/bind`` with the session cookie attached to finish.

State is an HMAC-SHA256 self-signed token (``base64url(json).base64url(sig)``)
derived from the Relay secret key with domain separation. It is verified but
not consumed server-side: the GitHub authorization code itself is single-use,
which bounds replay to the state TTL.
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


class GitHubOAuthProvider:
    """Exchanges GitHub authorization codes for user profiles."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = str(client_id).strip()
        self._client_secret = str(client_secret).strip()

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
        try:
            async with httpx.AsyncClient(timeout=OAUTH_HTTP_TIMEOUT_SECONDS) as client:
                token_response = await client.post(
                    GITHUB_TOKEN_ENDPOINT,
                    data={
                        "client_id": self.client_id,
                        "client_secret": self._client_secret,
                        "code": str(code),
                        "redirect_uri": redirect_uri,
                    },
                    headers={"Accept": "application/json"},
                )
                if token_response.status_code != 200:
                    raise OAuthError(f"GitHub token exchange failed with status {token_response.status_code}.")
                access_token = token_response.json().get("access_token")
                if not access_token:
                    # GitHub answers 200 with an error payload for bad codes.
                    raise OAuthError("GitHub token exchange did not return an access token.")
                user_response = await client.get(
                    GITHUB_USERINFO_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                if user_response.status_code != 200:
                    raise OAuthError(f"GitHub profile lookup failed with status {user_response.status_code}.")
                payload = user_response.json()
        except OAuthError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError(f"GitHub sign-in failed: {exc}") from exc
        user_id = payload.get("id")
        username = payload.get("login")
        if not user_id or not username:
            raise OAuthError("GitHub profile response is missing the user id or login.")
        return OAuthProfile(
            provider_user_id=str(user_id),
            username=str(username),
            display_name=str(payload.get("name") or ""),
            avatar_url=str(payload.get("avatar_url") or ""),
        )


def github_provider_from_env() -> GitHubOAuthProvider | None:
    client_id = os.environ.get("SOMNIA_GITHUB_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SOMNIA_GITHUB_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    return GitHubOAuthProvider(client_id, client_secret)


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
