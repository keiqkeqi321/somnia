"""Channel registry: which OAuth strategies exist and how env config builds them."""

from __future__ import annotations

import os

from open_somnia.remote.oauth.base import OAuthProvider
from open_somnia.remote.oauth.gitee import GiteeOAuthProvider
from open_somnia.remote.oauth.github import GitHubOAuthProvider


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
