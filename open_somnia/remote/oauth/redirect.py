"""Validation for the SPA redirect target carried inside OAuth state."""

from __future__ import annotations

from typing import Collection
from urllib.parse import urlparse


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
