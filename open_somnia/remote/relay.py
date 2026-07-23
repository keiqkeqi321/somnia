from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
import uuid

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect


@dataclass(slots=True)
class _Peer:
    socket: WebSocket
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.socket.send_json(message)


class RelayHub:
    """Routes live frames between browsers and outbound Device connectors."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._connectors: dict[str, _Peer] = {}
        self._clients: dict[str, dict[str, _Peer]] = {}

    async def serve_connector(self, device_id: str, socket: WebSocket) -> None:
        await socket.accept()
        peer = _Peer(socket)
        previous: _Peer | None = None
        async with self._lock:
            previous = self._connectors.get(device_id)
            self._connectors[device_id] = peer
        if previous is not None:
            await previous.socket.close(code=1012, reason="Connector replaced.")
        try:
            while True:
                message = await socket.receive_json()
                if isinstance(message, dict):
                    await self._broadcast_to_clients(device_id, message)
        except WebSocketDisconnect:
            pass
        finally:
            async with self._lock:
                if self._connectors.get(device_id) is peer:
                    self._connectors.pop(device_id, None)

    async def serve_client(self, device_id: str, socket: WebSocket) -> None:
        await socket.accept()
        client_id = uuid.uuid4().hex
        peer = _Peer(socket)
        async with self._lock:
            self._clients.setdefault(device_id, {})[client_id] = peer
        try:
            while True:
                message = await socket.receive_json()
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

    async def _forward_to_connector(self, device_id: str, client: _Peer, message: dict[str, Any]) -> None:
        async with self._lock:
            connector = self._connectors.get(device_id)
        if connector is None:
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
        except (RuntimeError, WebSocketDisconnect):
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
        for client in clients:
            try:
                await client.send(message)
            except (RuntimeError, WebSocketDisconnect):
                continue


def create_relay_app() -> Starlette:
    hub = RelayHub()

    async def health_endpoint(request) -> JSONResponse:
        del request
        return JSONResponse({"status": "ready"})

    async def connector_endpoint(socket: WebSocket) -> None:
        await hub.serve_connector(str(socket.path_params["device_id"]), socket)

    async def client_endpoint(socket: WebSocket) -> None:
        await hub.serve_client(str(socket.path_params["device_id"]), socket)

    app = Starlette(
        routes=[
            Route("/health", health_endpoint),
            WebSocketRoute("/ws/connector/{device_id}", connector_endpoint),
            WebSocketRoute("/ws/client/{device_id}", client_endpoint),
        ]
    )
    app.state.relay_hub = hub
    return app
