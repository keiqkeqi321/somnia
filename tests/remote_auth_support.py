from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.testclient import TestClient


BROWSER_ORIGIN = "http://127.0.0.1:4173"


def login(client: TestClient, username: str = "admin", password: str = "admin-password") -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    if response.status_code != 200:
        raise AssertionError(response.text)


def pair_device(client: TestClient, name: str = "Test Device") -> tuple[Ed25519PrivateKey, str]:
    code_response = client.post("/api/pairings", json={"name": name})
    if code_response.status_code != 201:
        raise AssertionError(code_response.text)
    private_key = Ed25519PrivateKey.generate()
    claim = claim_pairing(client, code_response.json()["code"], name, private_key)
    if claim.status_code != 201:
        raise AssertionError(claim.text)
    return private_key, str(claim.json()["device_id"])


def claim_pairing(client: TestClient, code: str, name: str, private_key: Ed25519PrivateKey):
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return client.post(
        "/api/pairings/claim",
        json={"code": code, "name": name, "public_key": encode_bytes(public_key)},
    )


def authenticate_connector(socket, device_id: str, private_key: Ed25519PrivateKey, *, expect_success: bool = True) -> None:
    challenge = socket.receive_json()
    nonce = str(challenge["nonce"])
    signed = f"somnia-device-auth-v1\n{device_id}\n{nonce}".encode("utf-8")
    socket.send_json({"kind": "auth_response", "signature": encode_bytes(private_key.sign(signed))})
    if expect_success:
        result = socket.receive_json()
        if result != {"kind": "auth_ok", "device_id": device_id}:
            raise AssertionError(result)
    else:
        socket.receive_json()


def encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
