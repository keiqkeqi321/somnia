"""Gitee OAuth channel strategy — same profile shape as GitHub, different wire conventions."""

from __future__ import annotations

from open_somnia.remote.oauth.base import OAuthProvider


GITEE_AUTHORIZE_ENDPOINT = "https://gitee.com/oauth/authorize"
GITEE_TOKEN_ENDPOINT = "https://gitee.com/oauth/token"
GITEE_USERINFO_ENDPOINT = "https://gitee.com/api/v5/user"


class GiteeOAuthProvider(OAuthProvider):
    """Exchanges Gitee authorization codes for user profiles."""

    name = "gitee"
    label = "Gitee"
    authorize_endpoint = GITEE_AUTHORIZE_ENDPOINT
    token_endpoint = GITEE_TOKEN_ENDPOINT
    userinfo_endpoint = GITEE_USERINFO_ENDPOINT
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
