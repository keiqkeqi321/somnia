"""GitHub OAuth channel strategy."""

from __future__ import annotations

from open_somnia.remote.oauth.base import OAuthProvider


GITHUB_AUTHORIZE_ENDPOINT = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_ENDPOINT = "https://github.com/login/oauth/access_token"
GITHUB_USERINFO_ENDPOINT = "https://api.github.com/user"


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
