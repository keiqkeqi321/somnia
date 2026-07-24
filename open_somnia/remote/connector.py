from __future__ import annotations

import json
from collections import OrderedDict, deque
from threading import Event, Lock, Thread
from typing import Any, Callable
from urllib.parse import quote, urlparse
import urllib.request
import uuid

from websockets.sync.client import connect

from open_somnia.remote.auth import encode_bytes
from open_somnia.remote.identity import DeviceIdentity


JsonRequest = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]
PROTOCOL_VERSION = 1


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
        if method == "stream.snapshot":
            sessions = self._request("GET", "/sessions", None)
            runtime = self._request("GET", "/runtime/status", None)
            return {
                "sessions": sessions.get("sessions", []),
                "runtime": runtime,
            }
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
        replay_limit: int = 256,
        deduplication_limit: int = 256,
    ) -> None:
        self.relay_url = str(relay_url).strip()
        if not identity.is_paired:
            raise ValueError("Device identity must be paired before the Connector can run.")
        self.identity = identity
        self.device_id = identity.device_id
        self.project_id = _nonempty(project_id, "project_id")
        self.sidecar = sidecar
        if replay_limit < 1 or deduplication_limit < 1:
            raise ValueError("Connector replay and deduplication limits must be positive.")
        self.replay_limit = replay_limit
        self.deduplication_limit = deduplication_limit
        self._stream_epoch = ""
        self._next_sequence = 0
        self._event_ring: deque[dict[str, Any]] = deque(maxlen=replay_limit)
        self._highest_acknowledged = 0
        self._deduplicated: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
        self._state_lock = Lock()

    def run(self, stop_event: Event | None = None) -> None:
        stop = stop_event or Event()
        self._begin_stream()
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
                        self.handle_relay_message(raw_message, send)
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

    def handle_relay_message(self, raw_message: str | bytes, send: Callable[[dict[str, Any]], None]) -> None:
        """Handle one Relay frame; this is the Connector's protocol seam."""
        request_id = ""
        fingerprint = ""
        cache_response = False
        try:
            message = json.loads(raw_message)
            if not isinstance(message, dict):
                return
            kind = str(message.get("kind", "")).strip()
            if kind == "stream_ack":
                self._handle_stream_ack(message)
                return
            if kind == "stream_resume":
                self._handle_stream_resume(message, send)
                return
            if kind != "request":
                return
            request_id = str(message.get("request_id", "")).strip()
            if not request_id:
                raise ValueError("request_id is required.")
            if str(message.get("project_id", "")).strip() != self.project_id:
                raise ValueError("Project is not registered by this Connector.")
            method = _required_text(message, "method")
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("params must be an object.")
            fingerprint = _request_fingerprint(method, params)
            cached = self._deduplicated.get(request_id)
            if cached is not None:
                previous_fingerprint, response = cached
                if previous_fingerprint != fingerprint:
                    raise ValueError("request_id was reused for a different command.")
                self._deduplicated.move_to_end(request_id)
                send(dict(response))
                return
            cache_response = True
            result = self.sidecar.execute(method, params)
            response = self._response(request_id, ok=True, result=result)
        except Exception as exc:
            response = self._response(request_id, ok=False, error=str(exc))
        if request_id and cache_response:
            self._remember_request(request_id, fingerprint, response)
        send(response)

    def _forward_sidecar_events(self, sidecar_events: Any, send: Callable[[dict[str, Any]], None], stop: Event) -> None:
        try:
            for raw_event in sidecar_events:
                if stop.is_set():
                    break
                event = json.loads(raw_event)
                if isinstance(event, dict):
                    send(self.publish_sidecar_event(event))
        except Exception as exc:
            if not stop.is_set():
                send(
                    {
                        "kind": "connector_error",
                        "project_id": self.project_id,
                        "error": f"Sidecar event stream failed: {exc}",
                    }
                )

    def publish_sidecar_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Add stream identity and ordering to one local Runtime event."""
        return self._record_event(event)

    def _begin_stream(self) -> None:
        with self._state_lock:
            self._stream_epoch = uuid.uuid4().hex
            self._next_sequence = 0
            self._highest_acknowledged = 0
            self._event_ring.clear()

    def _record_event(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._state_lock:
            if not self._stream_epoch:
                self._stream_epoch = uuid.uuid4().hex
                self._next_sequence = 0
                self._highest_acknowledged = 0
                self._event_ring.clear()
            self._next_sequence += 1
            envelope = {
                "kind": "event",
                "protocol_version": PROTOCOL_VERSION,
                "device_id": self.device_id,
                "project_id": self.project_id,
                "stream_epoch": self._stream_epoch,
                "sequence": self._next_sequence,
                "event": event,
            }
            self._event_ring.append(envelope)
            return envelope

    def _handle_stream_ack(self, message: dict[str, Any]) -> None:
        if str(message.get("project_id", "")).strip() != self.project_id:
            return
        with self._state_lock:
            if str(message.get("stream_epoch", "")).strip() != self._stream_epoch:
                return
            try:
                sequence = int(message.get("sequence", -1))
            except (TypeError, ValueError):
                return
            if sequence > self._highest_acknowledged:
                self._highest_acknowledged = min(sequence, self._next_sequence)

    def _handle_stream_resume(self, message: dict[str, Any], send: Callable[[dict[str, Any]], None]) -> None:
        if str(message.get("project_id", "")).strip() != self.project_id:
            return
        try:
            after_sequence = int(message.get("after_sequence", 0))
        except (TypeError, ValueError):
            after_sequence = -1
        requested_epoch = str(message.get("stream_epoch", "")).strip()
        with self._state_lock:
            epoch = self._stream_epoch
            events = list(self._event_ring)
            latest_sequence = self._next_sequence
            oldest_sequence = events[0]["sequence"] if events else latest_sequence + 1
            replay_available = (
                requested_epoch == epoch
                and after_sequence >= oldest_sequence - 1
                and after_sequence <= latest_sequence
            )
        if replay_available:
            send(
                {
                    "kind": "stream_replay",
                    "protocol_version": PROTOCOL_VERSION,
                    "device_id": self.device_id,
                    "project_id": self.project_id,
                    "stream_epoch": epoch,
                    "after_sequence": after_sequence,
                    "events": [event for event in events if event["sequence"] > after_sequence],
                }
            )
            return
        with self._state_lock:
            try:
                snapshot = self.sidecar.execute("stream.snapshot", {})
            except Exception as exc:
                send(
                    {
                        "kind": "snapshot_required",
                        "protocol_version": PROTOCOL_VERSION,
                        "device_id": self.device_id,
                        "project_id": self.project_id,
                        "stream_epoch": epoch,
                        "sequence": latest_sequence,
                        "reason": f"Snapshot resync failed: {exc}",
                    }
                )
                return
            latest_sequence = self._next_sequence
            send(
                {
                    "kind": "stream_snapshot",
                    "protocol_version": PROTOCOL_VERSION,
                    "device_id": self.device_id,
                    "project_id": self.project_id,
                    "stream_epoch": epoch,
                    "sequence": latest_sequence,
                    "snapshot": snapshot,
                }
            )

    def _response(self, request_id: str, *, ok: bool, result: Any = None, error: str = "") -> dict[str, Any]:
        response: dict[str, Any] = {
            "kind": "response",
            "protocol_version": PROTOCOL_VERSION,
            "device_id": self.device_id,
            "project_id": self.project_id,
            "request_id": request_id,
            "ok": ok,
        }
        if ok:
            response["result"] = result
        else:
            response["error"] = error
        return response

    def _remember_request(self, request_id: str, fingerprint: str, response: dict[str, Any]) -> None:
        self._deduplicated[request_id] = (fingerprint, dict(response))
        self._deduplicated.move_to_end(request_id)
        while len(self._deduplicated) > self.deduplication_limit:
            self._deduplicated.popitem(last=False)

    @property
    def stream_epoch(self) -> str:
        return self._stream_epoch

    @property
    def highest_acknowledged_sequence(self) -> int:
        return self._highest_acknowledged


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


def _request_fingerprint(method: str, params: dict[str, Any]) -> str:
    return json.dumps({"method": method, "params": params}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
