from __future__ import annotations

import json
from threading import Event, Lock, Thread
from typing import Any, Callable
from urllib.parse import quote, urlparse
import urllib.request

from websockets.sync.client import connect

from open_somnia.remote.auth import encode_bytes
from open_somnia.remote.identity import DeviceIdentity


JsonRequest = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


class LocalSidecarBridge:
    """Maps the remote tracer protocol onto one loopback-only sidecar."""

    def __init__(self, base_url: str, *, request: JsonRequest | None = None) -> None:
        self.base_url = str(base_url).strip().rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Somnia Connector requires a loopback sidecar URL.")
        self._request = request or self._request_json

    @property
    def event_url(self) -> str:
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return parsed._replace(scheme=scheme, path="/ws", params="", query="", fragment="").geturl()

    def execute(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "session.create":
            payload = self._request("POST", "/sessions", {})
            return _required_mapping(payload, "session")
        if method == "session.load":
            session_id = _required_text(params, "session_id")
            payload = self._request("GET", f"/sessions/{quote(session_id, safe='')}", None)
            return _required_mapping(payload, "session")
        if method == "turn.start":
            session_id = _required_text(params, "session_id")
            if "user_input" not in params:
                raise ValueError("user_input is required.")
            return self._request(
                "POST",
                "/turns",
                {"session_id": session_id, "user_input": params["user_input"]},
            )
        raise ValueError(f"Unsupported remote method: {method}")

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=30.0) as response:
            body = response.read().decode("utf-8")
        decoded = json.loads(body) if body else {}
        if not isinstance(decoded, dict):
            raise RuntimeError("Sidecar response must be a JSON object.")
        return decoded


class RemoteConnector:
    """Maintains the outbound Relay connection for one tracer Project."""

    def __init__(
        self,
        relay_url: str,
        *,
        identity: DeviceIdentity,
        project_id: str,
        sidecar: LocalSidecarBridge,
    ) -> None:
        self.relay_url = str(relay_url).strip()
        if not identity.is_paired:
            raise ValueError("Device identity must be paired before the Connector can run.")
        self.identity = identity
        self.device_id = identity.device_id
        self.project_id = _nonempty(project_id, "project_id")
        self.sidecar = sidecar

    def run(self, stop_event: Event | None = None) -> None:
        stop = stop_event or Event()
        connector_url = f"{self.relay_url.rstrip('/')}/ws/connector/{quote(self.device_id, safe='')}"
        with connect(self.sidecar.event_url, open_timeout=10, close_timeout=2) as sidecar_events:
            with connect(connector_url, open_timeout=10, close_timeout=2) as relay:
                self._authenticate_relay(relay)
                send_lock = Lock()

                def send(message: dict[str, Any]) -> None:
                    with send_lock:
                        relay.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

                event_thread = Thread(
                    target=self._forward_sidecar_events,
                    args=(sidecar_events, send, stop),
                    name=f"somnia-connector-events-{self.device_id}",
                    daemon=True,
                )
                event_thread.start()
                try:
                    for raw_message in relay:
                        if stop.is_set():
                            break
                        self._handle_relay_message(raw_message, send)
                finally:
                    stop.set()
                    event_thread.join(timeout=2.0)

    def _authenticate_relay(self, relay: Any) -> None:
        raw_challenge = relay.recv(timeout=10.0)
        challenge = json.loads(raw_challenge)
        if not isinstance(challenge, dict) or challenge.get("kind") != "auth_challenge":
            raise RuntimeError("Relay did not provide a Device authentication challenge.")
        nonce = _required_text(challenge, "nonce")
        relay.send(
            json.dumps(
                {
                    "kind": "auth_response",
                    "signature": encode_bytes(self.identity.sign_challenge(nonce)),
                },
                separators=(",", ":"),
            )
        )
        raw_result = relay.recv(timeout=10.0)
        result = json.loads(raw_result)
        if not isinstance(result, dict) or result != {"kind": "auth_ok", "device_id": self.device_id}:
            raise RuntimeError("Relay rejected Device authentication.")

    def _handle_relay_message(self, raw_message: str | bytes, send: Callable[[dict[str, Any]], None]) -> None:
        request_id = ""
        try:
            message = json.loads(raw_message)
            if not isinstance(message, dict) or message.get("kind") != "request":
                return
            request_id = str(message.get("request_id", "")).strip()
            if str(message.get("project_id", "")).strip() != self.project_id:
                raise ValueError("Project is not registered by this Connector.")
            method = _required_text(message, "method")
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("params must be an object.")
            result = self.sidecar.execute(method, params)
            send({"kind": "response", "request_id": request_id, "ok": True, "result": result})
        except Exception as exc:
            send({"kind": "response", "request_id": request_id, "ok": False, "error": str(exc)})

    def _forward_sidecar_events(self, sidecar_events: Any, send: Callable[[dict[str, Any]], None], stop: Event) -> None:
        try:
            for raw_event in sidecar_events:
                if stop.is_set():
                    break
                event = json.loads(raw_event)
                if isinstance(event, dict):
                    send({"kind": "event", "project_id": self.project_id, "event": event})
        except Exception as exc:
            if not stop.is_set():
                send(
                    {
                        "kind": "connector_error",
                        "project_id": self.project_id,
                        "error": f"Sidecar event stream failed: {exc}",
                    }
                )


def _required_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"Sidecar response did not include {key}.")
    return value


def _required_text(payload: dict[str, Any], key: str) -> str:
    return _nonempty(payload.get(key), key)


def _nonempty(value: Any, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} is required.")
    return normalized
