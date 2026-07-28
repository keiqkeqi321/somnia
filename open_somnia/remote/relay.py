from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import os
import secrets
import time
from typing import Any, Callable, Mapping
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from open_somnia.remote.auth import (
    CredentialPolicyError,
    LoginRateLimited,
    PairingCodeExpired,
    PairingCodeInvalid,
    PairingCodeUsed,
    PairingRateLimited,
    PairSessionExpired,
    PairSessionRateLimited,
    PairSessionSecretInvalid,
    RegistrationRateLimited,
    RemoteAuth,
    UsernameRateLimited,
    UsernameTaken,
    decode_bytes,
    device_challenge_payload,
)
from open_somnia.remote.auth_store import AuthMetadataStore


ACCESS_COOKIE = "somnia_access"
REFRESH_COOKIE = "somnia_refresh"
DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


@dataclass(slots=True)
class _Peer:
    socket: WebSocket
    account_id: str
    access_token: str | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    send_timeout_seconds: float = 2.0

    async def send(self, message: dict[str, Any]) -> None:
        if self.send_lock.locked():
            raise TimeoutError("Client send queue is full.")
        async with self.send_lock:
            await asyncio.wait_for(
                self.socket.send_json(message),
                timeout=self.send_timeout_seconds,
            )


class RelayHub:
    """Routes authenticated live frames without retaining Somnia content."""

    def __init__(
        self,
        auth: RemoteAuth,
        *,
        browser_origins: set[str],
        client_send_timeout_seconds: float = 2.0,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        if client_send_timeout_seconds <= 0:
            raise ValueError("Relay client send timeout must be positive.")
        if max_message_bytes < 1024:
            raise ValueError("Relay max message size must be at least 1024 bytes.")
        self.auth = auth
        self._browser_origins = browser_origins
        self._client_send_timeout_seconds = client_send_timeout_seconds
        self._max_message_bytes = max_message_bytes
        self._lock = asyncio.Lock()
        self._connectors: dict[str, _Peer] = {}
        self._clients: dict[str, dict[str, _Peer]] = {}
        self._project_metadata: dict[str, list[dict[str, str]]] = {}
        self._reconnecting_until: dict[str, float] = {}
        self._delivery_tasks: set[asyncio.Task[None]] = set()

    async def serve_connector(self, device_id: str, socket: WebSocket) -> None:
        await socket.accept()
        device = self.auth.device(device_id)
        nonce = secrets.token_urlsafe(32)
        await socket.send_json({"kind": "auth_challenge", "nonce": nonce})
        try:
            proof = await asyncio.wait_for(socket.receive_json(), timeout=10.0)
            if not isinstance(proof, dict) or proof.get("kind") != "auth_response":
                raise ValueError("Device is not authorized.")
            signature = decode_bytes(proof.get("signature", ""), expected_length=64)
            if device is None or device.revoked_at is not None:
                raise ValueError("Device is not authorized.")
            Ed25519PublicKey.from_public_bytes(device.public_key).verify(
                signature,
                device_challenge_payload(device_id, nonce),
            )
        except (InvalidSignature, ValueError, WebSocketDisconnect, asyncio.TimeoutError):
            await socket.close(code=4403, reason="Device authentication failed.")
            return

        peer = _Peer(
            socket=socket,
            account_id=device.account_id,
            send_timeout_seconds=self._client_send_timeout_seconds,
        )
        previous: _Peer | None = None
        async with self._lock:
            previous = self._connectors.get(device_id)
            self._connectors[device_id] = peer
            self._reconnecting_until.pop(device_id, None)
        if previous is not None:
            await previous.socket.close(code=1012, reason="Connector replaced.")
        await socket.send_json({"kind": "auth_ok", "device_id": device_id})
        try:
            while True:
                message = await socket.receive_json()
                if not _message_within_limit(message, self._max_message_bytes):
                    await socket.close(code=1009, reason="Message exceeds the Relay payload limit.")
                    break
                current = self.auth.device(device_id)
                if current is None or current.revoked_at is not None:
                    await socket.close(code=4403, reason="Device revoked.")
                    break
                if isinstance(message, dict):
                    if message.get("kind") == "connector_presence":
                        await self._record_project_metadata(device_id, message)
                        continue
                    await self._broadcast_to_clients(device_id, message)
        except WebSocketDisconnect:
            pass
        finally:
            async with self._lock:
                if self._connectors.get(device_id) is peer:
                    self._connectors.pop(device_id, None)
                    self._reconnecting_until[device_id] = time.monotonic() + 30.0

    async def serve_client(self, device_id: str, socket: WebSocket) -> None:
        if socket.headers.get("origin") not in self._browser_origins:
            await socket.close(code=4403, reason="Browser origin is not allowed.")
            return
        await socket.accept()
        access_token = socket.cookies.get(ACCESS_COOKIE)
        account = self.auth.resolve_access(access_token)
        if account is None:
            await socket.close(code=4401, reason="Browser authentication required.")
            return
        device = self.auth.device(device_id)
        if device is None or device.revoked_at is not None or device.account_id != account.id:
            await socket.close(code=4403, reason="Device access denied.")
            return

        client_id = uuid.uuid4().hex
        peer = _Peer(
            socket=socket,
            account_id=account.id,
            access_token=access_token,
            send_timeout_seconds=self._client_send_timeout_seconds,
        )
        async with self._lock:
            self._clients.setdefault(device_id, {})[client_id] = peer
        try:
            while True:
                message = await socket.receive_json()
                if not _message_within_limit(message, self._max_message_bytes):
                    await socket.close(code=1009, reason="Message exceeds the Relay payload limit.")
                    break
                if self.auth.resolve_access(peer.access_token) is None:
                    await socket.close(code=4401, reason="Browser authentication expired.")
                    break
                current = self.auth.device(device_id)
                if current is None or current.revoked_at is not None or current.account_id != account.id:
                    await socket.close(code=4403, reason="Device access denied.")
                    break
                if isinstance(message, dict):
                    await self._forward_to_connector(device_id, peer, message)
        except WebSocketDisconnect:
            pass
        finally:
            async with self._lock:
                clients = self._clients.get(device_id)
                if clients is not None:
                    clients.pop(client_id, None)
                    if not clients:
                        self._clients.pop(device_id, None)

    async def revoke_device(self, device_id: str) -> None:
        async with self._lock:
            connector = self._connectors.pop(device_id, None)
            clients = list(self._clients.pop(device_id, {}).values())
            self._project_metadata.pop(device_id, None)
            self._reconnecting_until.pop(device_id, None)
        peers = ([connector] if connector is not None else []) + clients
        for peer in peers:
            try:
                await peer.socket.close(code=4403, reason="Device revoked.")
            except RuntimeError:
                pass

    async def navigation_metadata(self, device) -> dict[str, Any]:
        async with self._lock:
            online = device.id in self._connectors
            projects = list(self._project_metadata.get(device.id, []))
            reconnecting = self._reconnecting_until.get(device.id, 0.0) > time.monotonic()
        return {
            **_serialize_device(device),
            "status": "revoked" if device.revoked_at is not None else "online" if online else "reconnecting" if reconnecting else "offline",
            "projects": projects,
        }

    async def _record_project_metadata(self, device_id: str, message: dict[str, Any]) -> None:
        raw_projects = message.get("projects")
        if not isinstance(raw_projects, list):
            return
        projects: list[dict[str, str]] = []
        for item in raw_projects:
            if not isinstance(item, dict):
                continue
            project_id = str(item.get("project_id", "")).strip()
            name = str(item.get("name", "")).strip()
            if project_id and name and len(project_id) <= 128 and len(name) <= 120:
                projects.append({"project_id": project_id, "name": name})
        async with self._lock:
            self._project_metadata[device_id] = projects

    async def _forward_to_connector(self, device_id: str, client: _Peer, message: dict[str, Any]) -> None:
        claimed_device = str(message.get("device_id", device_id)).strip()
        if claimed_device != device_id:
            await client.send(
                {
                    "kind": "response",
                    "request_id": str(message.get("request_id", "")),
                    "ok": False,
                    "error": "Cross-Device routing is not allowed.",
                }
            )
            return
        async with self._lock:
            connector = self._connectors.get(device_id)
        if connector is None or connector.account_id != client.account_id:
            await client.send(
                {
                    "kind": "response",
                    "request_id": str(message.get("request_id", "")),
                    "ok": False,
                    "error": "Device is offline.",
                }
            )
            return
        try:
            await connector.send(message)
        except (TimeoutError, asyncio.TimeoutError, RuntimeError, WebSocketDisconnect):
            await client.send(
                {
                    "kind": "response",
                    "request_id": str(message.get("request_id", "")),
                    "ok": False,
                    "error": "Device connection failed.",
                }
            )

    async def _broadcast_to_clients(self, device_id: str, message: dict[str, Any]) -> None:
        async with self._lock:
            clients = list(self._clients.get(device_id, {}).values())
            connector = self._connectors.get(device_id)
        for client in clients:
            if connector is None or client.account_id != connector.account_id:
                continue
            if self.auth.resolve_access(client.access_token) is None:
                await self._disconnect_expired_client(device_id, client)
                continue
            task = asyncio.create_task(self._deliver_to_client(device_id, client, message))
            self._delivery_tasks.add(task)
            task.add_done_callback(self._delivery_tasks.discard)

    async def _deliver_to_client(self, device_id: str, client: _Peer, message: dict[str, Any]) -> None:
        try:
            await client.send(message)
        except (TimeoutError, asyncio.TimeoutError):
            await self._disconnect_slow_client(device_id, client)
        except (RuntimeError, WebSocketDisconnect):
            await self._remove_client(device_id, client)

    async def _disconnect_slow_client(self, device_id: str, client: _Peer) -> None:
        await self._remove_client(device_id, client)
        try:
            await asyncio.wait_for(
                client.socket.close(code=4008, reason="Client too slow; resync required."),
                timeout=self._client_send_timeout_seconds,
            )
        except (RuntimeError, WebSocketDisconnect, asyncio.TimeoutError):
            pass

    async def _remove_client(self, device_id: str, client: _Peer) -> None:
        async with self._lock:
            clients = self._clients.get(device_id)
            if clients is None:
                return
            stale_ids = [client_id for client_id, peer in clients.items() if peer is client]
            for client_id in stale_ids:
                clients.pop(client_id, None)
            if not clients:
                self._clients.pop(device_id, None)

    async def _disconnect_expired_client(self, device_id: str, client: _Peer) -> None:
        await self._remove_client(device_id, client)
        try:
            await client.socket.close(code=4401, reason="Browser authentication expired.")
        except RuntimeError:
            pass


def create_relay_app(
    *,
    administrators: Mapping[str, str] | None = None,
    secret_key: bytes | None = None,
    clock: Callable[[], float] = time.time,
    access_ttl_seconds: int = 15 * 60,
    refresh_ttl_seconds: int = 30 * 24 * 60 * 60,
    pairing_ttl_seconds: int = 5 * 60,
    pairing_attempt_limit: int = 10,
    pair_session_attempt_limit: int = 10,
    pair_session_attempt_window_seconds: int = 60 * 60,
    login_attempt_limit: int = 10,
    login_username_attempt_limit: int = 10,
    login_username_attempt_window_seconds: int = 10 * 60,
    registration_enabled: bool = True,
    registration_attempt_limit: int = 5,
    registration_attempt_window_seconds: int = 60 * 60,
    secure_cookies: bool = False,
    allowed_origins: list[str] | None = None,
    database_url: str | None = None,
    client_send_timeout_seconds: float = 2.0,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> Starlette:
    configured = administrators
    if configured is None:
        username = os.environ.get("SOMNIA_ADMIN_USERNAME", "admin")
        password = os.environ.get("SOMNIA_ADMIN_PASSWORD", "")
        configured = {username: password} if password else {}
    metadata_store = AuthMetadataStore(database_url) if database_url else None
    web_origins = allowed_origins or ["http://127.0.0.1:4173", "http://localhost:4173"]
    browser_origins = set(web_origins)
    auth = RemoteAuth(
        configured,
        secret_key=secret_key,
        clock=clock,
        access_ttl_seconds=access_ttl_seconds,
        refresh_ttl_seconds=refresh_ttl_seconds,
        pairing_ttl_seconds=pairing_ttl_seconds,
        pairing_attempt_limit=pairing_attempt_limit,
        pair_session_attempt_limit=pair_session_attempt_limit,
        pair_session_attempt_window_seconds=pair_session_attempt_window_seconds,
        login_attempt_limit=login_attempt_limit,
        login_username_attempt_limit=login_username_attempt_limit,
        login_username_attempt_window_seconds=login_username_attempt_window_seconds,
        registration_attempt_limit=registration_attempt_limit,
        registration_attempt_window_seconds=registration_attempt_window_seconds,
        metadata_store=metadata_store,
    )
    hub = RelayHub(
        auth,
        browser_origins=browser_origins,
        client_send_timeout_seconds=client_send_timeout_seconds,
        max_message_bytes=max_message_bytes,
    )

    async def health_endpoint(request: Request) -> JSONResponse:
        del request
        return JSONResponse({"status": "ready"})

    async def info_endpoint(request: Request) -> JSONResponse:
        del request
        # Clients (e.g. Desktop pairing) use this to find the Web app origin;
        # it differs from the Relay origin whenever the SPA is hosted separately.
        web_origin = web_origins[0] if web_origins else None
        return JSONResponse({"web_origin": web_origin})

    async def login_endpoint(request: Request) -> JSONResponse:
        body = await _json_body(request)
        source = request.client.host if request.client is not None else "unknown"
        try:
            account = auth.authenticate_password(body.get("username", ""), body.get("password", ""), source=source)
        except LoginRateLimited as exc:
            return JSONResponse({"error": str(exc)}, status_code=429, headers={"Retry-After": "60"})
        except UsernameRateLimited as exc:
            return JSONResponse(
                {"error": str(exc)},
                status_code=429,
                headers={"Retry-After": str(auth.login_username_attempt_window_seconds)},
            )
        if account is None:
            return JSONResponse({"error": "Invalid username or password."}, status_code=401)
        response = JSONResponse({"username": account.username})
        _set_browser_cookies(response, auth.issue_browser_tokens(account.id), auth, secure=secure_cookies)
        return response

    async def register_endpoint(request: Request) -> JSONResponse:
        if not registration_enabled:
            return JSONResponse({"error": "Registration is disabled on this Relay."}, status_code=403)
        body = await _json_body(request)
        source = request.client.host if request.client is not None else "unknown"
        try:
            account = auth.register_account(body.get("username", ""), body.get("password", ""), source=source)
        except RegistrationRateLimited as exc:
            return JSONResponse(
                {"error": str(exc)},
                status_code=429,
                headers={"Retry-After": str(auth.registration_attempt_window_seconds)},
            )
        except UsernameTaken as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except CredentialPolicyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        response = JSONResponse({"username": account.username, "account_id": account.id}, status_code=201)
        _set_browser_cookies(response, auth.issue_browser_tokens(account.id), auth, secure=secure_cookies)
        return response

    async def refresh_endpoint(request: Request) -> JSONResponse:
        tokens = auth.rotate_refresh(request.cookies.get(REFRESH_COOKIE))
        if tokens is None:
            return JSONResponse({"error": "Refresh token is invalid or expired."}, status_code=401)
        response = JSONResponse({"status": "renewed"})
        _set_browser_cookies(response, tokens, auth, secure=secure_cookies)
        return response

    async def logout_endpoint(request: Request) -> JSONResponse:
        auth.revoke_browser_tokens(
            request.cookies.get(ACCESS_COOKIE),
            request.cookies.get(REFRESH_COOKIE),
        )
        response = JSONResponse({"status": "signed_out"})
        response.delete_cookie(ACCESS_COOKIE, path="/")
        response.delete_cookie(REFRESH_COOKIE, path="/api/auth")
        return response

    async def create_pairing_endpoint(request: Request) -> JSONResponse:
        account = auth.resolve_access(request.cookies.get(ACCESS_COOKIE))
        if account is None:
            return JSONResponse({"error": "Authentication required."}, status_code=401)
        body = await _json_body(request)
        device_name = str(body.get("name", "")).strip()
        if not device_name or len(device_name) > 80:
            return JSONResponse({"error": "Device name must be between 1 and 80 characters."}, status_code=400)
        code, expires_at = auth.create_pairing(account.id, device_name)
        return JSONResponse({"code": code, "expires_at": expires_at}, status_code=201)

    async def claim_pairing_endpoint(request: Request) -> JSONResponse:
        body = await _json_body(request)
        try:
            public_key = decode_bytes(body.get("public_key", ""), expected_length=32)
            source = request.client.host if request.client is not None else "unknown"
            device = auth.claim_pairing(body.get("code", ""), public_key, source=source)
        except PairingRateLimited as exc:
            return JSONResponse({"error": str(exc)}, status_code=429, headers={"Retry-After": "60"})
        except PairingCodeUsed as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except PairingCodeExpired as exc:
            return JSONResponse({"error": str(exc)}, status_code=410)
        except (PairingCodeInvalid, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        return JSONResponse(_serialize_device(device), status_code=201)

    async def create_pair_session_endpoint(request: Request) -> JSONResponse:
        source = request.client.host if request.client is not None else "unknown"
        try:
            session_id, secret, expires_at = auth.create_pair_session(source=source)
        except PairSessionRateLimited as exc:
            return JSONResponse(
                {"error": str(exc)},
                status_code=429,
                headers={"Retry-After": str(auth.pair_session_attempt_window_seconds)},
            )
        return JSONResponse(
            {"session_id": session_id, "secret": secret, "expires_at": expires_at},
            status_code=201,
        )

    async def pair_session_status_endpoint(request: Request) -> JSONResponse:
        session_id = str(request.path_params["session_id"])
        secret = str(request.query_params.get("secret", ""))
        try:
            return JSONResponse(auth.pair_session_status(session_id, secret))
        except PairSessionSecretInvalid as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)

    async def approve_pair_session_endpoint(request: Request) -> JSONResponse:
        account = auth.resolve_access(request.cookies.get(ACCESS_COOKIE))
        if account is None:
            return JSONResponse({"error": "Authentication required."}, status_code=401)
        body = await _json_body(request)
        device_name = str(body.get("device_name", "")).strip()
        if not device_name or len(device_name) > 80:
            return JSONResponse({"error": "Device name must be between 1 and 80 characters."}, status_code=400)
        session_id = str(request.path_params["session_id"])
        try:
            auth.approve_pair_session(session_id, str(body.get("secret", "")), account.id, device_name)
        except PairSessionSecretInvalid as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except PairSessionExpired as exc:
            return JSONResponse({"error": str(exc)}, status_code=410)
        return JSONResponse({"status": "approved"})

    async def list_devices_endpoint(request: Request) -> JSONResponse:
        account = auth.resolve_access(request.cookies.get(ACCESS_COOKIE))
        if account is None:
            return JSONResponse({"error": "Authentication required."}, status_code=401)
        devices = [await hub.navigation_metadata(device) for device in auth.devices_for_account(account.id)]
        return JSONResponse({"devices": devices})

    async def revoke_device_endpoint(request: Request) -> JSONResponse:
        account = auth.resolve_access(request.cookies.get(ACCESS_COOKIE))
        if account is None:
            return JSONResponse({"error": "Authentication required."}, status_code=401)
        device_id = str(request.path_params["device_id"])
        device = auth.revoke_device(account.id, device_id)
        if device is None:
            return JSONResponse({"error": "Device not found."}, status_code=404)
        await hub.revoke_device(device_id)
        return JSONResponse(_serialize_device(device))

    async def connector_endpoint(socket: WebSocket) -> None:
        await hub.serve_connector(str(socket.path_params["device_id"]), socket)

    async def client_endpoint(socket: WebSocket) -> None:
        await hub.serve_client(str(socket.path_params["device_id"]), socket)

    @asynccontextmanager
    async def lifespan(app_instance):
        del app_instance
        try:
            yield
        finally:
            if metadata_store is not None:
                metadata_store.close()

    app = Starlette(
        lifespan=lifespan,
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=sorted(browser_origins),
                allow_credentials=True,
                allow_methods=["GET", "POST", "DELETE"],
                allow_headers=["Content-Type"],
            )
        ],
        routes=[
            Route("/health", health_endpoint),
            Route("/api/info", info_endpoint),
            Route("/api/auth/login", login_endpoint, methods=["POST"]),
            Route("/api/auth/register", register_endpoint, methods=["POST"]),
            Route("/api/auth/refresh", refresh_endpoint, methods=["POST"]),
            Route("/api/auth/logout", logout_endpoint, methods=["POST"]),
            Route("/api/pairings", create_pairing_endpoint, methods=["POST"]),
            Route("/api/pairings/claim", claim_pairing_endpoint, methods=["POST"]),
            Route("/api/pair-sessions", create_pair_session_endpoint, methods=["POST"]),
            Route("/api/pair-sessions/{session_id}", pair_session_status_endpoint, methods=["GET"]),
            Route("/api/pair-sessions/{session_id}/approve", approve_pair_session_endpoint, methods=["POST"]),
            Route("/api/devices", list_devices_endpoint, methods=["GET"]),
            Route("/api/devices/{device_id}", revoke_device_endpoint, methods=["DELETE"]),
            WebSocketRoute("/ws/connector/{device_id}", connector_endpoint),
            WebSocketRoute("/ws/client/{device_id}", client_endpoint),
        ]
    )
    app.state.relay_hub = hub
    app.state.remote_auth = auth
    return app


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _message_within_limit(message: Any, limit: int) -> bool:
    try:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return False
    return len(encoded) <= limit


def _set_browser_cookies(response: JSONResponse, tokens, auth: RemoteAuth, *, secure: bool) -> None:
    response.set_cookie(
        ACCESS_COOKIE,
        tokens.access_token,
        max_age=auth.access_ttl_seconds,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        tokens.refresh_token,
        max_age=auth.refresh_ttl_seconds,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/api/auth",
    )


def _serialize_device(device) -> dict[str, Any]:
    return {
        "device_id": device.id,
        "name": device.name,
        "created_at": device.created_at,
        "revoked_at": device.revoked_at,
    }
