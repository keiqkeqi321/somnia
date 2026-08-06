"""HMAC-signed OAuth state tokens (CSRF protection for the callback flow).

State is a self-signed token (``base64url(json).base64url(sig)``) derived
from the Relay secret key with domain separation. It is verified but not
consumed server-side: the provider's authorization code itself is
single-use, which bounds replay to the state TTL. The ``provider`` claim
keeps states from different channels isolated.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Callable


Clock = Callable[[], float]

OAUTH_STATE_TTL_SECONDS = 600

_OAUTH_STATE_DOMAIN = b"somnia-oauth-state-v1"


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
