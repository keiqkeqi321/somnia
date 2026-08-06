"""Framework-agnostic OAuth channels for the Relay (GitHub, Gitee, ...).

Each channel is a small strategy object (:class:`OAuthProvider` subclass)
registered in :data:`OAUTH_PROVIDER_TYPES`; the Relay mounts one set of
endpoints parameterized by the channel name. Channel strategies live one
per module (:mod:`.github`, :mod:`.gitee`); shared pieces live in
:mod:`.base` (template flow), :mod:`.state` (signed state), and
:mod:`.redirect` (SPA redirect validation).

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

from open_somnia.remote.oauth.base import (
    OAUTH_HTTP_TIMEOUT_SECONDS,
    OAuthError,
    OAuthProfile,
    OAuthProvider,
)
from open_somnia.remote.oauth.gitee import GiteeOAuthProvider
from open_somnia.remote.oauth.github import (
    GITHUB_AUTHORIZE_ENDPOINT,
    GITHUB_TOKEN_ENDPOINT,
    GITHUB_USERINFO_ENDPOINT,
    GitHubOAuthProvider,
)
from open_somnia.remote.oauth.redirect import is_allowed_redirect
from open_somnia.remote.oauth.registry import (
    OAUTH_PROVIDER_TYPES,
    oauth_provider_from_env,
    oauth_providers_from_env,
)
from open_somnia.remote.oauth.state import (
    OAUTH_STATE_TTL_SECONDS,
    derive_state_signing_key,
    issue_oauth_state,
    verify_oauth_state,
)

__all__ = [
    "GITHUB_AUTHORIZE_ENDPOINT",
    "GITHUB_TOKEN_ENDPOINT",
    "GITHUB_USERINFO_ENDPOINT",
    "OAUTH_HTTP_TIMEOUT_SECONDS",
    "OAUTH_PROVIDER_TYPES",
    "OAUTH_STATE_TTL_SECONDS",
    "GiteeOAuthProvider",
    "GitHubOAuthProvider",
    "OAuthError",
    "OAuthProfile",
    "OAuthProvider",
    "derive_state_signing_key",
    "is_allowed_redirect",
    "issue_oauth_state",
    "oauth_provider_from_env",
    "oauth_providers_from_env",
    "verify_oauth_state",
]
