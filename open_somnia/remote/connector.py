from __future__ import annotations

import base64
import json
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
import time
from typing import Any, Callable
from urllib.parse import quote, urlparse
import urllib.request
import uuid

from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

from open_somnia.remote.auth import encode_bytes
from open_somnia.remote.identity import DeviceIdentity


JsonRequest = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]
PROTOCOL_VERSION = 1
RECONNECT_INITIAL_DELAY_SECONDS = 1.0
RECONNECT_MAX_DELAY_SECONDS = 30.0
# A connection surviving at least this long counts as stable; the backoff resets after it drops.
RECONNECT_STABLE_SECONDS = 10.0
# Project sidecar event streams retry on their own: a sidecar that starts late
# or restarts mid-session must join/recover without taking the Relay link down.
SIDECAR_PUMP_INITIAL_DELAY_SECONDS = 1.0
SIDECAR_PUMP_MAX_DELAY_SECONDS = 5.0
# Relay close codes that will never succeed on retry (auth rejected / device revoked).
PERMANENT_CLOSE_CODES = frozenset({4401, 4403})


class DeviceAuthRejected(RuntimeError):
    """The Relay refused this Device's authentication proof."""


class ConnectorReplaced(RuntimeError):
    """Another Connector took over this Device identity on the Relay."""


def _is_permanent_failure(exc: Exception) -> bool:
    if isinstance(exc, (DeviceAuthRejected, ConnectorReplaced)):
        return True
    if isinstance(exc, ConnectionClosed):
        close = exc.rcvd
        if close is None:
            return False
        if close.code in PERMANENT_CLOSE_CODES:
            return True
        # "Connector replaced" (1012) can surface as a ConnectionClosed exception
        # instead of the clean close-code path in run(); being replaced by another
        # Connector with the same identity must never be retried.
        return close.code == 1012 and str(close.reason or "").startswith("Connector replaced")
    return False


@dataclass
class _ProjectStream:
    sidecar: LocalSidecarBridge
    stream_epoch: str = ""
    next_sequence: int = 0
    event_ring: deque[dict[str, Any]] = field(default_factory=deque)
    highest_acknowledged: int = 0
    deduplicated: OrderedDict[str, tuple[str, dict[str, Any]]] = field(default_factory=OrderedDict)


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
        if method == "session.list":
            payload = self._request("GET", "/sessions", None)
            sessions = payload.get("sessions")
            if not isinstance(sessions, list):
                raise RuntimeError("Sidecar response did not include sessions.")
            return {"sessions": sessions}
        if method == "session.delete":
            session_id = _required_text(params, "session_id")
            return self._request("DELETE", f"/sessions/{quote(session_id, safe='')}", None)
        if method == "session.compact":
            session_id = _required_text(params, "session_id")
            return self._request("POST", f"/sessions/{quote(session_id, safe='')}/compact", {})
        if method == "session.janitor":
            session_id = _required_text(params, "session_id")
            return self._request("POST", f"/sessions/{quote(session_id, safe='')}/janitor", {})
        if method == "session.set_model":
            session_id = _required_text(params, "session_id")
            body: dict[str, Any] = {}
            if "provider" in params:
                body["provider_name"] = params.get("provider")
            if "model" in params:
                body["model"] = params.get("model")
            return self._request("POST", f"/sessions/{quote(session_id, safe='')}/model", body)
        if method == "turn.start":
            session_id = _required_text(params, "session_id")
            if "user_input" not in params:
                raise ValueError("user_input is required.")
            return self._request(
                "POST",
                "/turns",
                {"session_id": session_id, "user_input": params["user_input"]},
            )
        if method == "turn.interrupt":
            turn_id = _required_text(params, "turn_id")
            return self._request("POST", f"/turns/{quote(turn_id, safe='')}/interrupt", {})
        if method == "turn.inject":
            turn_id = _required_text(params, "turn_id")
            injection_id = _required_text(params, "injection_id")
            if "user_input" not in params:
                raise ValueError("user_input is required.")
            return self._request(
                "POST",
                f"/turns/{quote(turn_id, safe='')}/loop-injections",
                {"injection_id": injection_id, "user_input": params["user_input"]},
            )
        if method == "stream.snapshot":
            sessions = self._request("GET", "/sessions", None)
            runtime = self._request("GET", "/runtime/status", None)
            return {
                "sessions": sessions.get("sessions", []),
                "runtime": runtime,
            }
        if method == "tool_log.list":
            limit = params.get("limit", 24)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 100:
                raise ValueError("limit must be an integer between 1 and 100.")
            return self._request("GET", f"/tool-logs?limit={limit}", None)
        if method == "tool_log.get":
            log_id = _required_text(params, "log_id")
            payload = self._request("GET", f"/tool-logs/{quote(log_id, safe='')}", None)
            return _required_mapping(payload, "tool_log")
        if method == "thinking_log.get":
            path = _required_text(params, "path")
            payload = self._request("GET", f"/thinking-log?path={quote(path, safe='')}", None)
            return _required_mapping(payload, "thinking_log")
        if method == "team.members":
            session_id = str(params.get("session_id", "")).strip()
            path = "/team/active" if not session_id else f"/team/active?session_id={quote(session_id, safe='')}"
            return self._request("GET", path, None)
        if method == "team.log":
            name = _required_text(params, "name")
            session_id = str(params.get("session_id", "")).strip()
            path = f"/team/log?name={quote(name, safe='')}"
            if session_id:
                path += f"&session_id={quote(session_id, safe='')}"
            payload = self._request("GET", path, None)
            return _required_mapping(payload, "team_log")
        if method == "task.list":
            session_id = str(params.get("session_id", "")).strip()
            path = "/tasks" if not session_id else f"/tasks?session_id={quote(session_id, safe='')}"
            return self._request("GET", path, None)
        if method == "provider.list":
            return self._request("GET", "/providers", None)
        if method == "runtime.status":
            return self._request("GET", "/runtime/status", None)
        if method == "model.list":
            provider = str(params.get("provider", "")).strip()
            path = "/models" if not provider else f"/models?provider={quote(provider, safe='')}"
            return self._request("GET", path, None)
        if method == "provider.presets":
            return self._request("GET", "/provider-presets", None)
        if method == "provider.switch":
            provider = _required_text(params, "provider")
            model = _required_text(params, "model")
            return self._request("POST", "/providers/switch", {"provider_name": provider, "model": model})
        if method == "provider.debug_model":
            provider = _required_text(params, "provider")
            model = _required_text(params, "model")
            return self._request("POST", "/providers/debug-model", {"provider_name": provider, "model": model})
        if method == "settings.config.get":
            return self._request("GET", "/settings/config", None)
        if method == "settings.config.save":
            scope = _required_text(params, "scope")
            section = _required_text(params, "section")
            content = str(params.get("content", ""))
            return self._request("POST", "/settings/config", {"scope": scope, "section": section, "content": content})
        if method == "mcp.list":
            return self._request("GET", "/mcp/servers", None)
        if method == "mcp.debug":
            name = _required_text(params, "name")
            return self._request("POST", f"/mcp/servers/{quote(name, safe='')}/debug", {})
        if method == "mcp.set_enabled":
            name = _required_text(params, "name")
            enabled = params.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be a boolean.")
            return self._request("POST", f"/mcp/servers/{quote(name, safe='')}/enabled", {"enabled": enabled})
        if method == "vision.set":
            provider = str(params.get("provider", "")).strip()
            model = str(params.get("model", "")).strip()
            scope = str(params.get("scope", "project")).strip().lower()
            if scope not in {"user", "project"}:
                raise ValueError("scope must be 'user' or 'project'.")
            return self._request("POST", "/vision-model", {"scope": scope, "vision_provider": provider, "vision_model": model})
        if method == "reasoning.set":
            level = str(params.get("level", "")).strip()
            if not level:
                raise ValueError("level is required.")
            return self._request("POST", "/reasoning", {"reasoning_level": level})
        if method == "interaction.list":
            return self._request("GET", "/interactions", None)
        if method == "interaction.resolve_authorization":
            interaction_id = _required_text(params, "interaction_id")
            scope = _required_text(params, "scope")
            approved = params.get("approved", True)
            if not isinstance(approved, bool):
                raise ValueError("approved must be a boolean.")
            reason = str(params.get("reason", "")).strip()
            return self._request(
                "POST",
                f"/interactions/{quote(interaction_id, safe='')}/authorization",
                {"scope": scope, "approved": approved, "reason": reason},
            )
        if method == "interaction.resolve_mode_switch":
            interaction_id = _required_text(params, "interaction_id")
            approved = params.get("approved", False)
            if not isinstance(approved, bool):
                raise ValueError("approved must be a boolean.")
            active_mode = params.get("active_mode")
            reason = str(params.get("reason", "")).strip()
            return self._request(
                "POST",
                f"/interactions/{quote(interaction_id, safe='')}/mode-switch",
                {"approved": approved, "active_mode": active_mode, "reason": reason},
            )
        if method == "execution.mode":
            mode = _required_text(params, "mode").lower()
            if mode not in {"shortcuts", "plan", "accept_edits", "yolo"}:
                raise ValueError("Unsupported remote execution mode.")
            return self._request("POST", "/execution-mode", {"mode": mode})
        if method == "workspace.paths":
            query = str(params.get("query", ""))
            limit = params.get("limit", 30)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 100:
                raise ValueError("limit must be an integer between 1 and 100.")
            return self._request("GET", f"/workspace/paths?q={quote(query, safe='')}&limit={limit}", None)
        if method == "workspace.image.stage":
            name = str(params.get("name", "")).strip()
            media_type = str(params.get("media_type", "")).strip()
            data_url = str(params.get("data_url", "")).strip()
            if not data_url:
                raise ValueError("data_url is required.")
            return self._request(
                "POST",
                "/workspace/images",
                {"name": name, "media_type": media_type, "data_url": data_url},
            )
        if method == "workspace.image":
            path = _required_text(params, "path")
            request = urllib.request.Request(
                f"{self.base_url}/workspace/images?path={quote(path, safe='')}",
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=30.0) as response:
                data = response.read(10 * 1024 * 1024 + 1)
                media_type = str(response.headers.get_content_type())
            if len(data) > 10 * 1024 * 1024:
                raise ValueError("Workspace image exceeds the 10 MB remote limit.")
            if not media_type.startswith("image/"):
                raise RuntimeError("Sidecar workspace image response was not an image.")
            encoded = base64.b64encode(data).decode("ascii")
            return {"data_url": f"data:{media_type};base64,{encoded}"}
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


class _ProjectEventPump:
    """Streams one Project's sidecar events, retrying independently.

    A Project sidecar may start after the Connector (async Desktop loading) or
    restart mid-session; the pump reconnects with backoff so late Projects join
    without disturbing the Relay connection or the other Projects.
    """

    def __init__(self, connector: "RemoteConnector", project_id: str) -> None:
        self._connector = connector
        self.project_id = project_id
        self._stop = Event()
        self._outage_reported = False
        self.thread = Thread(
            target=self._run,
            name=f"somnia-connector-events-{connector.device_id}-{project_id}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self, *, join_timeout: float | None = None) -> None:
        self._stop.set()
        if join_timeout is not None and self.thread.is_alive():
            self.thread.join(timeout=join_timeout)

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def _run(self) -> None:
        delay = SIDECAR_PUMP_INITIAL_DELAY_SECONDS
        had_connection = False
        while not self._stop.is_set():
            sidecar = self._connector.project_sidecar(self.project_id)
            if sidecar is None:
                return
            try:
                with connect(sidecar.event_url, open_timeout=5, close_timeout=2) as events:
                    delay = SIDECAR_PUMP_INITIAL_DELAY_SECONDS
                    if had_connection:
                        # Reconnected after an outage: events the sidecar
                        # broadcast while this pump was away are lost without
                        # any sequence gap, so force every client to resync.
                        self._connector.resync_project_stream(self.project_id)
                    had_connection = True
                    self._outage_reported = False
                    for raw_event in events:
                        if self._stop.is_set():
                            break
                        event = json.loads(raw_event)
                        if isinstance(event, dict):
                            self._connector.forward_pumped_event(self.project_id, event)
            except Exception as exc:
                if self._stop.is_set():
                    break
                if not self._outage_reported:
                    self._outage_reported = True
                    self._connector.report_pump_error(self.project_id, exc)
            if self._stop.wait(delay):
                break
            delay = min(delay * 2.0, SIDECAR_PUMP_MAX_DELAY_SECONDS)


class RemoteConnector:
    """Maintains one outbound Relay connection for one or more tracer Projects."""

    def __init__(
        self,
        relay_url: str,
        *,
        identity: DeviceIdentity,
        project_id: str,
        sidecar: LocalSidecarBridge,
        sidecars: dict[str, LocalSidecarBridge] | None = None,
        project_names: dict[str, str] | None = None,
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
        bridges = {self.project_id: sidecar}
        if sidecars:
            bridges.update({_nonempty(key, "project_id"): value for key, value in sidecars.items()})
        self._projects = {
            key: _ProjectStream(bridge, event_ring=deque(maxlen=replay_limit)) for key, bridge in bridges.items()
        }
        supplied_names = project_names or {}
        self._project_names = {
            key: _nonempty(supplied_names.get(key, key), "project name") for key in self._projects
        }
        self._state_lock = Lock()
        # Guards the Project set itself (pumps, names, the live send callback);
        # _state_lock keeps guarding per-Project stream state.
        self._projects_lock = Lock()
        self._pumps: dict[str, _ProjectEventPump] = {}
        self._active_send: Callable[[dict[str, Any]], None] | None = None
        self._run_active = False

    def project_sidecar(self, project_id: str) -> LocalSidecarBridge | None:
        with self._projects_lock:
            project = self._projects.get(project_id)
            return project.sidecar if project is not None else None

    def forward_pumped_event(self, project_id: str, event: dict[str, Any]) -> None:
        with self._projects_lock:
            send = self._active_send
        if send is None:
            return
        envelope = self._record_event(project_id, event)
        if envelope is not None:
            send(envelope)

    def report_pump_error(self, project_id: str, exc: Exception) -> None:
        with self._projects_lock:
            send = self._active_send
        if send is None:
            return
        send(
            {
                "kind": "connector_error",
                "project_id": project_id,
                "error": f"Sidecar event stream failed: {exc}",
            }
        )

    def resync_project_stream(self, project_id: str) -> None:
        """Force clients onto a fresh stream epoch and push a snapshot.

        The sidecar event pump has no replay channel: events broadcast while
        it was reconnecting vanish without any sequence gap, so downstream
        recovery (resume/replay) can never notice the loss — a lost
        ``turn_result`` leaves clients stuck on a turn that actually finished.
        Resetting the epoch and pushing an authoritative snapshot moves every
        client back to the resync path (``restoreActiveTurnsFromStatus``).
        """
        with self._state_lock:
            state = self._projects.get(project_id)
            if state is None:
                return
            state.stream_epoch = uuid.uuid4().hex
            state.next_sequence = 0
            state.highest_acknowledged = 0
            state.event_ring.clear()
            epoch = state.stream_epoch
            sidecar = state.sidecar
        try:
            snapshot = sidecar.execute("stream.snapshot", {})
        except Exception:
            # The next stream_resume (or the epoch mismatch on the next live
            # event) still drives clients to a snapshot on their own.
            return
        with self._projects_lock:
            send = self._active_send
        if send is None:
            # Relay link is down; run() resets the epoch on reconnect anyway.
            return
        send(
            {
                "kind": "stream_snapshot",
                "protocol_version": PROTOCOL_VERSION,
                "device_id": self.device_id,
                "project_id": project_id,
                "stream_epoch": epoch,
                "sequence": 0,
                "snapshot": snapshot,
            }
        )

    def update_projects(
        self,
        bridges: dict[str, LocalSidecarBridge],
        names: dict[str, str] | None = None,
    ) -> None:
        """Live-reconfigure the served Project set without dropping the Relay link.

        Added Projects start their event pump immediately (when running);
        removed Projects' pumps stop; renamed/re-pointed Projects update in
        place. A fresh ``connector_presence`` is announced after any change.
        """
        normalized = {_nonempty(key, "project_id"): value for key, value in bridges.items()}
        if self.project_id not in normalized:
            raise ValueError("The Connector's primary Project must stay registered.")
        supplied_names = names or {}
        stopped_pumps: list[_ProjectEventPump] = []
        with self._projects_lock:
            changed = False
            for project_id in [pid for pid in self._projects if pid not in normalized]:
                pump = self._pumps.pop(project_id, None)
                if pump is not None:
                    pump.stop()
                    stopped_pumps.append(pump)
                self._projects.pop(project_id, None)
                self._project_names.pop(project_id, None)
                changed = True
            for project_id, bridge in normalized.items():
                name = _nonempty(supplied_names.get(project_id, project_id), "project name")
                existing = self._projects.get(project_id)
                if existing is None:
                    self._projects[project_id] = _ProjectStream(
                        bridge, event_ring=deque(maxlen=self.replay_limit)
                    )
                    self._project_names[project_id] = name
                    self._begin_stream(project_id)
                    changed = True
                    continue
                if getattr(existing.sidecar, "base_url", None) != getattr(bridge, "base_url", None):
                    existing.sidecar = bridge
                    pump = self._pumps.pop(project_id, None)
                    if pump is not None:
                        pump.stop()
                        stopped_pumps.append(pump)
                    changed = True
                if self._project_names.get(project_id) != name:
                    self._project_names[project_id] = name
                    changed = True
            if self._run_active:
                for project_id in normalized:
                    self._start_pump_locked(project_id)
            send = self._active_send if changed else None
            presence = self._presence_message_locked() if changed and send is not None else None
        for pump in stopped_pumps:
            pump.stop(join_timeout=2.0)
        if presence is not None and send is not None:
            send(presence)

    def _start_pump_locked(self, project_id: str) -> None:
        pump = self._pumps.get(project_id)
        if pump is not None and pump.is_alive():
            return
        pump = _ProjectEventPump(self, project_id)
        self._pumps[project_id] = pump
        pump.start()

    def run(self, stop_event: Event | None = None, *, on_connect: Callable[[], None] | None = None) -> int | None:
        """Serve one Relay connection until it drops or ``stop_event`` is set.

        Returns the Relay close code when the Relay closed the connection, or
        ``None`` when the stop event ended the run locally.
        """
        external_stop = stop_event or Event()
        with self._projects_lock:
            self._run_active = True
            project_ids = list(self._projects)
        for project_id in project_ids:
            self._begin_stream(project_id)
        with self._projects_lock:
            for project_id in project_ids:
                self._start_pump_locked(project_id)
        connector_url = f"{self.relay_url.rstrip('/')}/ws/connector/{quote(self.device_id, safe='')}"
        try:
            with connect(connector_url, open_timeout=10, close_timeout=2) as relay:
                self._authenticate_relay(relay)
                if on_connect is not None:
                    on_connect()
                send_lock = Lock()

                def send(message: dict[str, Any]) -> None:
                    with send_lock:
                        relay.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

                with self._projects_lock:
                    self._active_send = send
                try:
                    send(self.presence_message())
                    for raw_message in relay:
                        if external_stop.is_set():
                            break
                        self.handle_relay_message(raw_message, send)
                finally:
                    with self._projects_lock:
                        self._active_send = None
                if external_stop.is_set():
                    return None
                close_reason = str(relay.close_reason or "")
                if relay.close_code == 1012 and close_reason.startswith("Connector replaced"):
                    raise ConnectorReplaced("Another Connector took over this Device on the Relay.")
                return relay.close_code
        finally:
            with self._projects_lock:
                self._run_active = False
                self._active_send = None
                pumps = list(self._pumps.values())
                self._pumps = {}
            for pump in pumps:
                pump.stop(join_timeout=2.0)

    def run_forever(
        self,
        stop_event: Event | None = None,
        *,
        on_retry: Callable[[str, float], None] | None = None,
        on_connect: Callable[[], None] | None = None,
    ) -> None:
        """Keep this Connector connected until ``stop_event`` is set.

        Transient failures (network loss, Relay restarts during deploys) are
        retried with exponential backoff; permanent ones (Device revoked,
        authentication rejected, another Connector replacing this one) are
        raised to the caller. ``on_retry(reason, delay)`` fires before each
        backoff wait; ``on_connect()`` fires after every successful Relay
        authentication.
        """
        stop = stop_event or Event()
        delay = RECONNECT_INITIAL_DELAY_SECONDS
        while not stop.is_set():
            started = time.monotonic()
            try:
                close_code = self.run(stop, on_connect=on_connect)
            except Exception as exc:
                if stop.is_set():
                    return
                if _is_permanent_failure(exc):
                    raise
                reason = str(exc)
            else:
                if stop.is_set():
                    return
                reason = f"Relay closed the connection (code {close_code})."
            if time.monotonic() - started >= RECONNECT_STABLE_SECONDS:
                delay = RECONNECT_INITIAL_DELAY_SECONDS
            if on_retry is not None:
                on_retry(reason, delay)
            if stop.wait(delay):
                return
            delay = min(delay * 2.0, RECONNECT_MAX_DELAY_SECONDS)

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
            raise DeviceAuthRejected("Relay rejected Device authentication.")

    def presence_message(self) -> dict[str, Any]:
        """Return the Relay-safe metadata for Projects served by this Connector."""
        with self._projects_lock:
            return self._presence_message_locked()

    def _presence_message_locked(self) -> dict[str, Any]:
        projects = [
            {"project_id": project_id, "name": self._project_names[project_id]}
            for project_id in self._projects
        ]
        return {
            "kind": "connector_presence",
            "projects": projects,
        }

    def handle_relay_message(self, raw_message: str | bytes, send: Callable[[dict[str, Any]], None]) -> None:
        """Handle one Relay frame; this is the Connector's protocol seam."""
        request_id = ""
        project_id = self.project_id
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
            project_id = str(message.get("project_id", "")).strip()
            project = self._projects.get(project_id)
            if project is None:
                raise ValueError("Project is not registered by this Connector.")
            method = _required_text(message, "method")
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("params must be an object.")
            fingerprint = _request_fingerprint(method, params)
            cached = project.deduplicated.get(request_id)
            if cached is not None:
                previous_fingerprint, response = cached
                if previous_fingerprint != fingerprint:
                    raise ValueError("request_id was reused for a different command.")
                project.deduplicated.move_to_end(request_id)
                send(dict(response))
                return
            cache_response = True
            result = project.sidecar.execute(method, params)
            response = self._response(request_id, project_id=project_id, ok=True, result=result)
        except Exception as exc:
            response = self._response(request_id, project_id=project_id, ok=False, error=str(exc))
        if request_id and cache_response:
            self._remember_request(project_id, request_id, fingerprint, response)
        send(response)

    def _forward_sidecar_events(self, project_id: str, sidecar_events: Any, send: Callable[[dict[str, Any]], None], stop: Event) -> None:
        """Legacy single-shot forwarder kept for tests; the live path is _ProjectEventPump."""
        try:
            for raw_event in sidecar_events:
                if stop.is_set():
                    break
                event = json.loads(raw_event)
                if isinstance(event, dict):
                    send(self.publish_sidecar_event(event, project_id=project_id))
        except Exception as exc:
            if not stop.is_set():
                send(
                    {
                        "kind": "connector_error",
                        "project_id": project_id,
                        "error": f"Sidecar event stream failed: {exc}",
                    }
                )

    def publish_sidecar_event(self, event: dict[str, Any], *, project_id: str | None = None) -> dict[str, Any]:
        """Add stream identity and ordering to one local Runtime event."""
        return self._record_event(project_id or self.project_id, event)

    def _begin_stream(self, project_id: str) -> None:
        with self._state_lock:
            state = self._projects[project_id]
            state.stream_epoch = uuid.uuid4().hex
            state.next_sequence = 0
            state.highest_acknowledged = 0
            state.event_ring.clear()

    def _record_event(self, project_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
        with self._state_lock:
            state = self._projects.get(project_id)
            if state is None:
                # The Project was removed mid-stream; drop its late events.
                return None
            if not state.stream_epoch:
                state.stream_epoch = uuid.uuid4().hex
                state.next_sequence = 0
                state.highest_acknowledged = 0
                state.event_ring.clear()
            state.next_sequence += 1
            envelope = {
                "kind": "event",
                "protocol_version": PROTOCOL_VERSION,
                "device_id": self.device_id,
                "project_id": project_id,
                "stream_epoch": state.stream_epoch,
                "sequence": state.next_sequence,
                "event": event,
            }
            state.event_ring.append(envelope)
            return envelope

    def _handle_stream_ack(self, message: dict[str, Any]) -> None:
        project = self._projects.get(str(message.get("project_id", "")).strip())
        if project is None:
            return
        with self._state_lock:
            if str(message.get("stream_epoch", "")).strip() != project.stream_epoch:
                return
            try:
                sequence = int(message.get("sequence", -1))
            except (TypeError, ValueError):
                return
            if sequence > project.highest_acknowledged:
                project.highest_acknowledged = min(sequence, project.next_sequence)

    def _handle_stream_resume(self, message: dict[str, Any], send: Callable[[dict[str, Any]], None]) -> None:
        project_id = str(message.get("project_id", "")).strip()
        project = self._projects.get(project_id)
        if project is None:
            return
        try:
            after_sequence = int(message.get("after_sequence", 0))
        except (TypeError, ValueError):
            after_sequence = -1
        # JSON null (fresh client without an epoch) must stay empty, not "None".
        requested_epoch = str(message.get("stream_epoch") or "").strip()
        with self._state_lock:
            epoch = project.stream_epoch
            events = list(project.event_ring)
            latest_sequence = project.next_sequence
            oldest_sequence = events[0]["sequence"] if events else latest_sequence + 1
            replay_available = (
                requested_epoch == epoch
                and after_sequence >= oldest_sequence - 1
                and after_sequence <= latest_sequence
            )
            # A fresh client (no epoch yet, nothing applied) gets the whole current
            # epoch replayed while the ring still holds it, so in-flight turns
            # resume their streamed output instead of jumping to a bare snapshot.
            fresh_replay_available = (
                not requested_epoch
                and after_sequence <= 0
                and oldest_sequence == 1
                and latest_sequence >= 1
            )
        if replay_available or fresh_replay_available:
            replay_from = after_sequence if replay_available else 0
            send(
                {
                    "kind": "stream_replay",
                    "protocol_version": PROTOCOL_VERSION,
                    "device_id": self.device_id,
                    "project_id": project_id,
                    "stream_epoch": epoch,
                    "after_sequence": replay_from,
                    "events": [event for event in events if event["sequence"] > replay_from],
                }
            )
            return
        # The snapshot round-trip must not hold the state lock: recording live
        # events would stall behind it and freeze the whole event pipeline.
        try:
            snapshot = project.sidecar.execute("stream.snapshot", {})
        except Exception as exc:
            send(
                {
                    "kind": "snapshot_required",
                    "protocol_version": PROTOCOL_VERSION,
                    "device_id": self.device_id,
                    "project_id": project_id,
                    "stream_epoch": epoch,
                    "sequence": latest_sequence,
                    "reason": f"Snapshot resync failed: {exc}",
                }
            )
            return
        with self._state_lock:
            latest_sequence = project.next_sequence
        send(
            {
                "kind": "stream_snapshot",
                "protocol_version": PROTOCOL_VERSION,
                "device_id": self.device_id,
                "project_id": project_id,
                "stream_epoch": epoch,
                "sequence": latest_sequence,
                "snapshot": snapshot,
            }
        )

    def _response(self, request_id: str, *, project_id: str, ok: bool, result: Any = None, error: str = "") -> dict[str, Any]:
        response: dict[str, Any] = {
            "kind": "response",
            "protocol_version": PROTOCOL_VERSION,
            "device_id": self.device_id,
            "project_id": project_id,
            "request_id": request_id,
            "ok": ok,
        }
        if ok:
            response["result"] = result
        else:
            response["error"] = error
        return response

    def _remember_request(self, project_id: str, request_id: str, fingerprint: str, response: dict[str, Any]) -> None:
        deduplicated = self._projects[project_id].deduplicated
        deduplicated[request_id] = (fingerprint, dict(response))
        deduplicated.move_to_end(request_id)
        while len(deduplicated) > self.deduplication_limit:
            deduplicated.popitem(last=False)

    @property
    def stream_epoch(self) -> str:
        return self._projects[self.project_id].stream_epoch

    @property
    def highest_acknowledged_sequence(self) -> int:
        return self._projects[self.project_id].highest_acknowledged


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
