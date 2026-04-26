from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Empty, Queue
import select
import socket
from threading import Lock, Thread
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
import uuid

from desktop.backend.ipc import (
    build_websocket_close_frame,
    build_websocket_pong_frame,
    build_websocket_text_frame,
    json_dumps,
    make_sidecar_event,
    read_websocket_frame,
    serialize_app_event,
    serialize_interaction,
    serialize_model,
    serialize_provider,
    serialize_session,
    serialize_tool_log_detail,
    serialize_tool_log_index_entry,
    serialize_turn_result,
    websocket_accept_value,
)
from open_somnia import __version__
from open_somnia.app_service import AppService
from open_somnia.config.models import AppSettings
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.execution_mode import execution_mode_spec, normalize_execution_mode


class SidecarAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.message = str(message)


@dataclass(slots=True)
class _WebSocketClient:
    id: str
    queue: Queue


class _SidecarHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, request_handler_class, *, sidecar: "SidecarServer") -> None:
        self.sidecar = sidecar
        super().__init__(server_address, request_handler_class)


class SidecarServer:
    def __init__(self, settings: AppSettings, *, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.settings = settings
        self.runtime = OpenAgentRuntime(settings)
        self.service = AppService(self.runtime)
        self._lock = Lock()
        self._clients: dict[str, _WebSocketClient] = {}
        self._active_turns: dict[str, Any] = {}
        self._turn_threads: dict[str, Thread] = {}
        self._closed = False
        self._server_thread: Thread | None = None
        self.httpd = _SidecarHTTPServer((host, port), _SidecarRequestHandler, sidecar=self)

    @classmethod
    def from_settings(cls, settings: AppSettings, *, host: str = "127.0.0.1", port: int = 8765) -> "SidecarServer":
        return cls(settings, host=host, port=port)

    @property
    def host(self) -> str:
        return str(self.httpd.server_address[0])

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws"

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def ready_payload(self) -> dict[str, Any]:
        execution_mode = getattr(self.runtime, "execution_mode", None)
        return {
            "status": "ready",
            "version": __version__,
            "workspace_root": str(self.settings.workspace_root),
            "base_url": self.base_url,
            "ws_url": self.ws_url,
            "provider": str(self.runtime.settings.provider.name),
            "model": str(self.runtime.settings.provider.model),
            "reasoning_level": self.runtime.settings.provider.reasoning_level,
            "execution_mode": execution_mode,
            "execution_mode_title": execution_mode_spec(execution_mode).title,
        }

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.httpd.serve_forever(poll_interval=poll_interval)

    def start_background(self) -> Thread:
        with self._lock:
            if self._server_thread is not None and self._server_thread.is_alive():
                return self._server_thread
            self._server_thread = Thread(
                target=self.serve_forever,
                name="somnia-sidecar-server",
                daemon=True,
            )
            self._server_thread.start()
            return self._server_thread

    def wait_until_ready(self, timeout: float = 2.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection((self.host, self.port), timeout=0.1):
                    return True
            except OSError:
                time.sleep(0.01)
        return False

    def close(self) -> None:
        clients: list[_WebSocketClient] = []
        with self._lock:
            if self._closed:
                return
            self._closed = True
            clients = list(self._clients.values())
            self._clients = {}
        for client in clients:
            try:
                client.queue.put_nowait(None)
            except Exception:
                pass
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        self.service.close()
        thread = None
        with self._lock:
            thread = self._server_thread
            self._server_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self._serialize_session(session) for session in self.service.list_sessions()]

    def create_session(self) -> dict[str, Any]:
        session = self.service.create_session()
        payload = self._serialize_session(session)
        self.broadcast_event(make_sidecar_event("session_created", payload={"session": payload}, session_id=session.id))
        return payload

    def load_session(self, session_id: str) -> dict[str, Any]:
        try:
            session = self.service.load_session(session_id)
        except FileNotFoundError as exc:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Session '{session_id}' was not found.") from exc
        return self._serialize_session(session)

    def _serialize_session(self, session: Any) -> dict[str, Any]:
        payload = serialize_session(session)
        usage = self._context_usage_payload(session)
        if usage is not None:
            payload["context_window_usage"] = usage
        return payload

    def _context_usage_payload(self, session: Any) -> dict[str, Any] | None:
        usage = None
        for method_name in ("recent_context_window_usage", "context_window_usage"):
            getter = getattr(self.runtime, method_name, None)
            if not callable(getter):
                continue
            try:
                usage = getter(session)
            except Exception:
                usage = None
            if usage is not None:
                break
        if usage is None:
            return None
        used_tokens = int(getattr(usage, "used_tokens", 0) or 0)
        max_tokens = getattr(usage, "max_tokens", None)
        usage_percent = getattr(usage, "usage_percent", None)
        return {
            "used_tokens": used_tokens,
            "max_tokens": int(max_tokens) if max_tokens else None,
            "usage_percent": float(usage_percent) if usage_percent is not None else None,
            "counter_name": str(getattr(usage, "counter_name", "") or "estimate"),
        }

    def list_providers(self) -> list[dict[str, Any]]:
        return [serialize_provider(provider) for provider in self.service.list_providers()]

    def list_models(self, provider_name: str | None = None) -> list[dict[str, Any]]:
        try:
            return [serialize_model(model) for model in self.service.list_models(provider_name)]
        except ValueError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, str(exc)) from exc

    def switch_provider_model(self, provider_name: str, model: str) -> dict[str, Any]:
        try:
            message = self.service.switch_provider_model(provider_name, model)
        except ValueError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        payload = {
            "message": message,
            "provider": str(self.runtime.settings.provider.name),
            "model": str(self.runtime.settings.provider.model),
        }
        self.broadcast_event(make_sidecar_event("provider_switched", payload=payload))
        return payload

    def set_reasoning_level(self, reasoning_level: str | None) -> dict[str, Any]:
        message = self.service.set_reasoning_level(reasoning_level)
        payload = {
            "message": message,
            "provider": str(self.runtime.settings.provider.name),
            "model": str(self.runtime.settings.provider.model),
            "reasoning_level": self.runtime.settings.provider.reasoning_level,
        }
        self.broadcast_event(make_sidecar_event("reasoning_level_updated", payload=payload))
        return payload

    def set_execution_mode(self, mode: str) -> dict[str, Any]:
        normalized_mode = normalize_execution_mode(mode)
        self.runtime.execution_mode = normalized_mode
        payload = {
            "message": f"Execution mode set to {execution_mode_spec(normalized_mode).title}.",
            "execution_mode": normalized_mode,
            "execution_mode_title": execution_mode_spec(normalized_mode).title,
        }
        self.broadcast_event(make_sidecar_event("execution_mode_updated", payload=payload))
        return payload

    def start_turn(self, session_id: str, user_input: str | dict[str, Any]) -> dict[str, Any]:
        try:
            session = self.service.load_session(session_id)
        except FileNotFoundError as exc:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Session '{session_id}' was not found.") from exc
        try:
            handle = self.service.run_turn(session, user_input)
        except RuntimeError as exc:
            raise SidecarAPIError(HTTPStatus.CONFLICT, str(exc)) from exc
        drainer = Thread(
            target=self._drain_turn_events,
            args=(handle,),
            name=f"somnia-sidecar-turn-{handle.turn_id}",
            daemon=True,
        )
        with self._lock:
            self._active_turns[handle.turn_id] = handle
            self._turn_threads[handle.turn_id] = drainer
        drainer.start()
        return {"turn_id": handle.turn_id, "session_id": session.id}

    def interrupt_turn(self, turn_id: str) -> dict[str, Any]:
        interrupted = self.service.interrupt_turn(turn_id)
        return {"turn_id": str(turn_id).strip(), "interrupted": bool(interrupted)}

    def pending_interactions(self) -> list[dict[str, Any]]:
        return [serialize_interaction(interaction) for interaction in self.service.pending_interactions()]

    def runtime_status(self) -> dict[str, Any]:
        payload = self.ready_payload()
        payload["pending_interaction_count"] = len(self.service.pending_interactions())
        payload["open_session_count"] = len(self.service.list_sessions())
        return payload

    def list_tool_logs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        entries = self.runtime.tool_log_store.list_recent(limit=max(1, int(limit)))
        return [serialize_tool_log_index_entry(entry) for entry in entries]

    def get_tool_log(self, log_id: str) -> dict[str, Any]:
        entry = self.runtime.tool_log_store.get(log_id)
        if entry is None:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Tool log '{log_id}' was not found.")
        payload = serialize_tool_log_detail(entry)
        payload["rendered"] = self.runtime.render_tool_log(log_id)
        return payload

    def resolve_authorization(
        self,
        request_id: str,
        *,
        scope: str,
        approved: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        try:
            resolved = self.service.resolve_authorization(
                request_id,
                scope=scope,
                approved=approved,
                reason=reason,
            )
        except ValueError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        if not resolved:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Interaction '{request_id}' was not found.")
        return {"request_id": request_id, "resolved": True}

    def resolve_mode_switch(
        self,
        request_id: str,
        *,
        approved: bool,
        active_mode: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        resolved = self.service.resolve_mode_switch(
            request_id,
            approved=approved,
            active_mode=active_mode,
            reason=reason,
        )
        if not resolved:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Interaction '{request_id}' was not found.")
        return {"request_id": request_id, "resolved": True}

    def register_client(self) -> _WebSocketClient:
        client = _WebSocketClient(id=uuid.uuid4().hex[:8], queue=Queue())
        with self._lock:
            self._clients[client.id] = client
        return client

    def unregister_client(self, client_id: str) -> None:
        with self._lock:
            self._clients.pop(client_id, None)

    def enqueue_client_event(self, client_id: str, event: dict[str, Any] | None) -> None:
        with self._lock:
            client = self._clients.get(client_id)
        if client is None:
            return
        client.queue.put(deepcopy(event) if event is not None else None)

    def broadcast_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            clients = list(self._clients.values())
        for client in clients:
            try:
                client.queue.put_nowait(deepcopy(event))
            except Exception:
                continue

    def _drain_turn_events(self, handle) -> None:
        try:
            while True:
                batch = handle.drain_events(block=not handle.is_done(), timeout=0.05)
                if batch:
                    for event in batch:
                        self.broadcast_event(serialize_app_event(event))
                    continue
                if handle.is_done():
                    trailing = handle.drain_events()
                    if trailing:
                        for event in trailing:
                            self.broadcast_event(serialize_app_event(event))
                        continue
                    break
            if handle.result is not None:
                payload = serialize_turn_result(handle.result)
                if payload.get("session") is not None:
                    payload["session"] = self._serialize_session(handle.result.session)
                self.broadcast_event(
                    make_sidecar_event(
                        "turn_result",
                        session_id=handle.session.id,
                        turn_id=handle.turn_id,
                        payload=payload,
                    )
                )
        finally:
            with self._lock:
                self._active_turns.pop(handle.turn_id, None)
                self._turn_threads.pop(handle.turn_id, None)


class _SidecarRequestHandler(BaseHTTPRequestHandler):
    server_version = "SomniaSidecar/0.1"

    @property
    def sidecar(self) -> SidecarServer:
        return self.server.sidecar

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_common_headers(content_type=None, content_length=0)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/ws":
            self._handle_websocket()
            return
        try:
            payload = self._route_get(parsed)
            self._send_json(HTTPStatus.OK, payload)
        except SidecarAPIError as exc:
            self._send_json(exc.status_code, {"error": exc.message})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json_body()
            payload, status_code = self._route_post(parsed, body)
            self._send_json(status_code, payload)
        except SidecarAPIError as exc:
            self._send_json(exc.status_code, {"error": exc.message})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def log_message(self, format: str, *args) -> None:
        return None

    def _route_get(self, parsed) -> dict[str, Any]:
        path_parts = [part for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)
        if path_parts == ["health"]:
            return self.sidecar.ready_payload()
        if path_parts == ["runtime", "status"]:
            return self.sidecar.runtime_status()
        if path_parts == ["sessions"]:
            return {"sessions": self.sidecar.list_sessions()}
        if len(path_parts) == 2 and path_parts[0] == "sessions":
            return {"session": self.sidecar.load_session(path_parts[1])}
        if path_parts == ["providers"]:
            return {"providers": self.sidecar.list_providers()}
        if path_parts == ["models"]:
            provider_name = (query.get("provider") or [None])[0]
            return {"models": self.sidecar.list_models(provider_name)}
        if path_parts == ["interactions"]:
            return {"interactions": self.sidecar.pending_interactions()}
        if path_parts == ["tool-logs"]:
            raw_limit = (query.get("limit") or [20])[0]
            try:
                limit = max(1, int(raw_limit))
            except (TypeError, ValueError):
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "limit must be an integer.")
            return {"tool_logs": self.sidecar.list_tool_logs(limit=limit)}
        if len(path_parts) == 2 and path_parts[0] == "tool-logs":
            return {"tool_log": self.sidecar.get_tool_log(path_parts[1])}
        raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Unknown route: {parsed.path}")

    def _route_post(self, parsed, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        path_parts = [part for part in parsed.path.split("/") if part]
        if path_parts == ["sessions"]:
            return {"session": self.sidecar.create_session()}, HTTPStatus.CREATED
        if path_parts == ["turns"]:
            session_id = str(body.get("session_id", "")).strip()
            if not session_id:
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "session_id is required.")
            if "user_input" not in body:
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "user_input is required.")
            return self.sidecar.start_turn(session_id, body["user_input"]), HTTPStatus.ACCEPTED
        if len(path_parts) == 3 and path_parts[0] == "turns" and path_parts[2] == "interrupt":
            return self.sidecar.interrupt_turn(path_parts[1]), HTTPStatus.OK
        if path_parts == ["providers", "switch"]:
            provider_name = str(body.get("provider_name", "")).strip()
            model = str(body.get("model", "")).strip()
            if not provider_name or not model:
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "provider_name and model are required.")
            return self.sidecar.switch_provider_model(provider_name, model), HTTPStatus.OK
        if path_parts == ["reasoning"]:
            raw_level = body.get("reasoning_level")
            return self.sidecar.set_reasoning_level(None if raw_level in {"", "auto"} else raw_level), HTTPStatus.OK
        if path_parts == ["execution-mode"]:
            mode = str(body.get("mode", "")).strip()
            if not mode:
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "mode is required.")
            return self.sidecar.set_execution_mode(mode), HTTPStatus.OK
        if len(path_parts) == 3 and path_parts[0] == "interactions" and path_parts[2] == "authorization":
            scope = str(body.get("scope", "")).strip()
            if not scope:
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "scope is required.")
            return (
                self.sidecar.resolve_authorization(
                    path_parts[1],
                    scope=scope,
                    approved=bool(body.get("approved", True)),
                    reason=str(body.get("reason", "")).strip(),
                ),
                HTTPStatus.OK,
            )
        if len(path_parts) == 3 and path_parts[0] == "interactions" and path_parts[2] == "mode-switch":
            return (
                self.sidecar.resolve_mode_switch(
                    path_parts[1],
                    approved=bool(body.get("approved", False)),
                    active_mode=body.get("active_mode"),
                    reason=str(body.get("reason", "")).strip(),
                ),
                HTTPStatus.OK,
            )
        raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Unknown route: {parsed.path}")

    def _read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        if content_length <= 0:
            return {}
        payload = self.rfile.read(content_length)
        if not payload:
            return {}
        try:
            body = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "Request body must be valid JSON.") from exc
        if not isinstance(body, dict):
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        return body

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        encoded = json_dumps(payload).encode("utf-8")
        self.send_response(int(status_code))
        self._send_common_headers(content_length=len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_common_headers(self, *, content_type: str | None = "application/json; charset=utf-8", content_length: int = 0) -> None:
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(int(content_length)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _handle_websocket(self) -> None:
        upgrade = str(self.headers.get("Upgrade", "")).strip().lower()
        connection = str(self.headers.get("Connection", "")).strip().lower()
        key = str(self.headers.get("Sec-WebSocket-Key", "")).strip()
        if upgrade != "websocket" or "upgrade" not in connection or not key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "A valid WebSocket upgrade request is required."})
            return

        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", websocket_accept_value(key))
        self.end_headers()
        self.close_connection = True

        client = self.sidecar.register_client()
        self.sidecar.enqueue_client_event(
            client.id,
            make_sidecar_event(
                "sidecar_ready",
                payload=self.sidecar.ready_payload(),
            ),
        )
        no_message = object()
        try:
            while not self.sidecar.is_closed:
                try:
                    queued_event = client.queue.get(timeout=0.05)
                except Empty:
                    queued_event = no_message
                if queued_event is None:
                    break
                if queued_event is not no_message:
                    self.wfile.write(build_websocket_text_frame(json_dumps(queued_event)))
                    self.wfile.flush()
                readable, _, _ = select.select([self.connection], [], [], 0.01)
                if not readable:
                    continue
                frame = read_websocket_frame(self.rfile)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:
                    try:
                        self.wfile.write(build_websocket_close_frame())
                        self.wfile.flush()
                    except Exception:
                        pass
                    break
                if opcode == 0x9:
                    self.wfile.write(build_websocket_pong_frame(payload))
                    self.wfile.flush()
                    continue
                if opcode == 0x1:
                    continue
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self.sidecar.unregister_client(client.id)
