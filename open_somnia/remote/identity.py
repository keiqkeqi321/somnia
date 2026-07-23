from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from open_somnia.remote.auth import decode_bytes, device_challenge_payload, encode_bytes


IDENTITY_VERSION = 1


@dataclass(frozen=True, slots=True)
class PairingResult:
    device_id: str
    device_name: str


class DeviceIdentity:
    """A Device-specific private key and its Relay registration metadata."""

    def __init__(
        self,
        path: Path,
        private_key: Ed25519PrivateKey,
        *,
        device_id: str = "",
        device_name: str = "",
        relay_url: str = "",
    ) -> None:
        self.path = Path(path)
        self._private_key = private_key
        self.device_id = str(device_id).strip()
        self.device_name = str(device_name).strip()
        self.relay_url = str(relay_url).strip().rstrip("/")

    @classmethod
    def load_or_create(cls, path: str | Path) -> "DeviceIdentity":
        identity_path = Path(path).expanduser()
        if identity_path.exists():
            return cls.load(identity_path)
        identity = cls(identity_path, Ed25519PrivateKey.generate())
        identity._save()
        return identity

    @classmethod
    def load(cls, path: str | Path) -> "DeviceIdentity":
        identity_path = Path(path).expanduser()
        try:
            payload = json.loads(identity_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or int(payload.get("version", 0)) != IDENTITY_VERSION:
                raise ValueError("Unsupported Device identity version.")
            private_key = Ed25519PrivateKey.from_private_bytes(
                decode_bytes(payload.get("private_key", ""), expected_length=32)
            )
            return cls(
                identity_path,
                private_key,
                device_id=str(payload.get("device_id", "")),
                device_name=str(payload.get("device_name", "")),
                relay_url=str(payload.get("relay_url", "")),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Unable to load Device identity from {identity_path}: {exc}") from exc

    @property
    def is_paired(self) -> bool:
        return bool(self.device_id and self.relay_url)

    def public_key_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def sign_challenge(self, nonce: str) -> bytes:
        if not self.device_id:
            raise ValueError("Device identity has not been paired.")
        return self._private_key.sign(device_challenge_payload(self.device_id, nonce))

    def complete_pairing(self, *, device_id: str, device_name: str, relay_url: str) -> None:
        self.device_id = _required(device_id, "device_id")
        self.device_name = _required(device_name, "device_name")
        self.relay_url = _relay_http_url(relay_url)
        self._save()

    def _save(self) -> None:
        private_key = self._private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        payload: dict[str, Any] = {
            "version": IDENTITY_VERSION,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "relay_url": self.relay_url,
            "private_key": encode_bytes(private_key),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()


def pair_device(identity: DeviceIdentity, *, relay_url: str, code: str) -> PairingResult:
    base_url = _relay_http_url(relay_url)
    payload = json.dumps(
        {
            "code": _required(code, "code").upper(),
            "public_key": encode_bytes(identity.public_key_bytes()),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/api/pairings/claim",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15.0) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            message = str(error_payload.get("error", exc.reason))
        except Exception:
            message = str(exc.reason)
        raise RuntimeError(f"Device pairing failed: {message}") from exc
    if not isinstance(body, dict):
        raise RuntimeError("Device pairing response must be an object.")
    device_id = _required(body.get("device_id"), "device_id")
    device_name = _required(body.get("name"), "device_name")
    identity.complete_pairing(device_id=device_id, device_name=device_name, relay_url=base_url)
    return PairingResult(device_id=device_id, device_name=device_name)


def default_identity_path() -> Path:
    return Path.home() / ".open_somnia" / "remote" / "device-identity.json"


def _relay_http_url(value: str) -> str:
    normalized = _required(value, "relay_url").rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Relay pairing URL must be an http or https origin without embedded credentials.")
    if parsed.scheme == "http" and parsed.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Relay HTTP is permitted only for loopback development; use HTTPS remotely.")
    return normalized


def _required(value: Any, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} is required.")
    return normalized
