from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
from threading import Event, Lock, Thread
import time
from typing import Any
from urllib.parse import quote, urlparse
import urllib.error
import urllib.request
import webbrowser

from open_somnia.remote.connector import ConnectorReplaced, LocalSidecarBridge, RemoteConnector
from open_somnia.remote.identity import DeviceIdentity, default_identity_path, pair_device
from open_somnia.remote.identity import _relay_http_url as validate_relay_http_url
from open_somnia.remote.runtime_manager import RuntimeOwnershipError, _OwnerLease

CONNECTOR_JOIN_TIMEOUT_SECONDS = 5.0
PAIR_POLL_INTERVAL_SECONDS = 1.5
PAIR_POLL_JOIN_TIMEOUT_SECONDS = 5.0
REMOTE_SETTINGS_DIRNAME = "remote"
REMOTE_SETTINGS_FILENAME = "settings.json"


class RemoteNotPairedError(RuntimeError):
    """Raised when a Connector launch requires a paired Device identity."""


def workspace_project_id(workspace_root: Path) -> str:
    """Return the stable Connector project id for one Desktop workspace."""
    normalized = str(Path(workspace_root).resolve()).replace("\\", "/").rstrip("/").lower()
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"desktop-{digest}"


def _relay_websocket_url(relay_http_url: str) -> str:
    parsed = urlparse(str(relay_http_url))
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme)
    if scheme is None:
        raise ValueError("Paired Relay URL must use http or https.")
    return parsed._replace(scheme=scheme).geturl()


def _default_device_name() -> str:
    """Device name suggested to the approving browser; the hostname identifies the machine."""
    return socket.gethostname().strip() or "Somnia Desktop"


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        message = str(payload.get("error", "")).strip()
        if message:
            return message
    except Exception:
        pass
    return f"Relay request failed ({exc.code} {exc.reason})."


class RemoteDeviceManager:
    """Owns the Desktop "controlled device" state for one sidecar.

    The Connector runs on a daemon thread inside the sidecar process and
    bridges the sidecar's own loopback URL; it never spawns a child process.
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        data_dir: Path,
        sidecar_base_url: str,
        identity_path: Path | None = None,
        owner_lease_path: Path | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._settings_path = Path(data_dir) / REMOTE_SETTINGS_DIRNAME / REMOTE_SETTINGS_FILENAME
        self._identity_path = Path(identity_path) if identity_path is not None else default_identity_path()
        self._sidecar_base_url = str(sidecar_base_url).strip().rstrip("/")
        # Machine-wide host lease: exactly one sidecar per device runs the
        # Connector, so concurrent autostarts no longer 1012-ping-pong.
        self._owner_lease_path = (
            Path(owner_lease_path) if owner_lease_path is not None else self._identity_path.with_name("connector.owner")
        )
        self._owner_lease: _OwnerLease | None = None
        self._lock = Lock()
        self._stop_event: Event | None = None
        self._thread: Thread | None = None
        self._connector: RemoteConnector | None = None
        self._pair_stop_event: Event | None = None
        self._pair_thread: Thread | None = None
        self._last_error = ""
        self._exposed_projects: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # Persistence (relay_url, device_name, enabled)
    # ------------------------------------------------------------------
    def _load_settings(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_settings(self, updates: dict[str, Any]) -> None:
        settings = self._load_settings()
        settings.update(updates)
        settings.pop("password", None)
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._settings_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._settings_path)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def _load_identity(self) -> DeviceIdentity | None:
        if not self._identity_path.exists():
            return None
        try:
            return DeviceIdentity.load(self._identity_path)
        except ValueError:
            return None

    def status(self) -> dict[str, Any]:
        settings = self._load_settings()
        identity = self._load_identity()
        paired = bool(identity is not None and identity.is_paired)
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            pair_pending = self._pair_thread is not None and self._pair_thread.is_alive()
            last_error = self._last_error
            exposed_projects = list(self._exposed_projects)
        if not exposed_projects:
            exposed_projects = self._persisted_projects(settings)
        return {
            "paired": paired,
            "device_id": identity.device_id if paired and identity is not None else "",
            "device_name": (identity.device_name if paired and identity is not None else "")
            or str(settings.get("device_name", "")),
            "relay_url": (identity.relay_url if paired and identity is not None else "")
            or str(settings.get("relay_url", "")),
            "username": str(settings.get("username", "")),
            "enabled": bool(settings.get("enabled", False)),
            "connector_running": running,
            "connector_hosted_here": running,
            "pair_pending": pair_pending,
            "last_error": last_error,
            "projects": exposed_projects,
        }

    @staticmethod
    def _persisted_projects(settings: dict[str, Any]) -> list[dict[str, str]]:
        persisted = settings.get("projects")
        if not isinstance(persisted, list):
            return []
        projects = []
        for entry in persisted:
            if not isinstance(entry, dict):
                continue
            project_id = str(entry.get("project_id", "")).strip()
            if not project_id:
                continue
            projects.append({"project_id": project_id, "name": str(entry.get("name", "")).strip() or project_id})
        return projects

    # ------------------------------------------------------------------
    # Pairing (device flow: pair session → browser approval → poll → claim)
    # ------------------------------------------------------------------
    def pair_begin(self, *, relay_url: str) -> dict[str, Any]:
        base_url = validate_relay_http_url(relay_url)
        with self._lock:
            if self._pair_thread is not None and self._pair_thread.is_alive():
                pending = True
            else:
                pending = False
        if pending:
            # A pairing flow is already running; starting another is a no-op.
            return self.status()

        session = self._post_json(
            f"{base_url}/api/pair-sessions",
            {"device_name": _default_device_name()},
            action="Relay pair session creation failed",
        )
        session_id = str(session.get("session_id", "")).strip()
        secret = str(session.get("secret", "")).strip()
        try:
            expires_at = float(session.get("expires_at", 0) or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
        if not session_id or not secret or expires_at <= 0:
            raise RuntimeError("Relay pair session creation failed: the Relay response was incomplete.")

        web_origin = base_url
        try:
            info = self._get_json(f"{base_url}/api/info", action="Relay info lookup failed")
            candidate = str(info.get("web_origin", "") or "").strip().rstrip("/")
            if candidate:
                web_origin = candidate
        except RuntimeError:
            # Relays without /api/info predate split hosting; fall back to same-origin.
            pass
        confirm_url = f"{web_origin}/?remote=1#/pair?session={quote(session_id)}&secret={quote(secret)}"
        try:
            webbrowser.open(confirm_url)
        except Exception as exc:  # the poll can still succeed if the user opens the URL manually
            self._record_last_error(f"Unable to open the pairing confirmation page: {exc}")

        stop_event = Event()
        thread = Thread(
            target=self._run_pair_poll,
            args=(stop_event, base_url, session_id, secret, expires_at),
            name="somnia-desktop-remote-pair",
            daemon=True,
        )
        with self._lock:
            existing = self._pair_thread
            if existing is None or not existing.is_alive():
                self._pair_stop_event = stop_event
                self._pair_thread = thread
                start = True
            else:
                start = False
        if start:
            thread.start()
        return self.status()

    def pair_cancel(self) -> dict[str, Any]:
        self._stop_pair_poll()
        return self.status()

    def _stop_pair_poll(self) -> None:
        with self._lock:
            stop_event = self._pair_stop_event
            thread = self._pair_thread
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=PAIR_POLL_JOIN_TIMEOUT_SECONDS)
        with self._lock:
            if self._pair_thread is thread:
                self._pair_thread = None
                self._pair_stop_event = None

    def _run_pair_poll(
        self,
        stop_event: Event,
        base_url: str,
        session_id: str,
        secret: str,
        expires_at: float,
    ) -> None:
        try:
            last_poll_error = ""
            status_url = f"{base_url}/api/pair-sessions/{quote(session_id)}?secret={quote(secret)}"
            while not stop_event.is_set():
                if time.time() >= expires_at:
                    self._record_last_error(
                        last_poll_error or "Pairing session expired before it was approved."
                    )
                    return
                try:
                    result = self._get_json(status_url, action="Relay pair session poll failed")
                except RuntimeError as exc:
                    # Transient Relay/network failures keep polling until the session expires.
                    last_poll_error = str(exc)
                    stop_event.wait(PAIR_POLL_INTERVAL_SECONDS)
                    continue
                session_status = str(result.get("status", ""))
                if session_status == "approved":
                    if stop_event.is_set():
                        return
                    code = str(result.get("code", "")).strip()
                    if not code:
                        raise RuntimeError("Relay approved the pair session without a code.")
                    self._complete_pairing(base_url, code)
                    return
                if session_status == "expired":
                    self._record_last_error("Pairing session expired before it was approved.")
                    return
                stop_event.wait(PAIR_POLL_INTERVAL_SECONDS)
        except BaseException as exc:  # noqa: BLE001 - the sidecar must survive anything
            self._record_last_error(f"Device pairing failed: {exc}")
        finally:
            with self._lock:
                if self._pair_stop_event is stop_event:
                    self._pair_thread = None
                    self._pair_stop_event = None

    def _complete_pairing(self, base_url: str, code: str) -> None:
        identity = DeviceIdentity.load_or_create(self._identity_path)
        result = pair_device(identity, relay_url=base_url, code=code)
        self._save_settings(
            {
                "relay_url": base_url,
                "device_name": result.device_name,
                "enabled": True,
            }
        )
        self._record_last_error("")
        try:
            self.enable()
        except Exception as exc:  # pairing succeeded; only the auto-enable failed
            self._record_last_error(f"Device paired but the Remote Connector failed to start: {exc}")

    def _record_last_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any], *, action: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return RemoteDeviceManager._open_json(request, action=action)

    @staticmethod
    def _get_json(url: str, *, action: str) -> dict[str, Any]:
        return RemoteDeviceManager._open_json(urllib.request.Request(url, method="GET"), action=action)

    @staticmethod
    def _open_json(request: urllib.request.Request, *, action: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=15.0) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{action}: {_http_error_message(exc)}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{action}: {exc.reason}") from exc
        if not isinstance(body, dict):
            raise RuntimeError(f"{action}: the Relay response was not a JSON object.")
        return body

    # ------------------------------------------------------------------
    # Connector lifecycle (in-process daemon thread)
    # ------------------------------------------------------------------
    def enable(self, projects: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        identity = self._load_identity()
        if identity is None or not identity.is_paired:
            raise RemoteNotPairedError("Pair this device before enabling remote control.")
        own_project_id = workspace_project_id(self._workspace_root)
        if projects is None or projects == []:
            entries = [self._own_project_entry(own_project_id)]
        else:
            entries = self._normalize_projects(projects, own_project_id)
        primary = next(entry for entry in entries if entry["project_id"] == own_project_id)
        extras = [entry for entry in entries if entry is not primary]
        with self._lock:
            running_connector = self._connector if (self._thread is not None and self._thread.is_alive()) else None
        if running_connector is not None:
            # Live reconfiguration: added/removed/late Projects join the running
            # Connector; the Relay connection and its clients stay up.
            bridges = {entry["project_id"]: LocalSidecarBridge(entry["base_url"]) for entry in entries}
            names = {entry["project_id"]: entry["name"] for entry in entries}
            running_connector.update_projects(bridges, names)
            with self._lock:
                self._exposed_projects = [
                    {"project_id": entry["project_id"], "name": entry["name"]} for entry in entries
                ]
            self._save_settings({"enabled": True, "projects": entries})
            return self.status()
        lease = self._take_host_lease(steal=True)
        if lease is None:
            raise RuntimeError("Another process on this device already hosts the Remote Connector.")
        connector = RemoteConnector(
            _relay_websocket_url(identity.relay_url),
            identity=identity,
            project_id=own_project_id,
            sidecar=LocalSidecarBridge(primary["base_url"]),
            sidecars={entry["project_id"]: LocalSidecarBridge(entry["base_url"]) for entry in extras} or None,
            project_names={entry["project_id"]: entry["name"] for entry in entries},
        )
        stop_event = Event()
        thread = Thread(
            target=self._run_connector,
            args=(connector, stop_event),
            name="somnia-desktop-remote-connector",
            daemon=True,
        )
        with self._lock:
            self._stop_event = stop_event
            self._thread = thread
            self._connector = connector
            self._last_error = ""
            self._exposed_projects = [
                {"project_id": entry["project_id"], "name": entry["name"]} for entry in entries
            ]
            thread.start()
        self._save_settings({"enabled": True, "projects": entries})
        return self.status()

    def _take_host_lease(self, *, steal: bool) -> _OwnerLease | None:
        lease = _OwnerLease(self._owner_lease_path, "remote-connector")
        if steal:
            # Explicit enable wins over any previous host; the Relay replaces the
            # old Connector (1012) once this one connects.
            lease.path.unlink(missing_ok=True)
        try:
            lease.acquire()
        except RuntimeOwnershipError:
            return None
        self._owner_lease = lease
        return lease

    def _own_project_entry(self, own_project_id: str) -> dict[str, str]:
        return {
            "project_id": own_project_id,
            "name": self._workspace_root.name or own_project_id,
            "base_url": self._sidecar_base_url,
        }

    def _normalize_projects(
        self, projects: list[dict[str, Any]], own_project_id: str
    ) -> list[dict[str, str]]:
        if not isinstance(projects, list):
            raise ValueError("Remote projects must be a list.")
        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in projects:
            if not isinstance(raw, dict):
                raise ValueError("Remote project entries must be objects.")
            project_id = str(raw.get("project_id", "")).strip()
            if not project_id:
                raise ValueError("Remote project entries require a project_id.")
            if project_id in seen:
                continue
            seen.add(project_id)
            base_url = str(raw.get("base_url", "")).strip()
            if not base_url:
                raise ValueError(f"Remote project '{project_id}' requires a base_url.")
            if project_id == own_project_id:
                # A persisted base_url for this sidecar's own project goes stale
                # on every restart (ephemeral ports), which wedges the Connector
                # against a dead port. Always bridge the live sidecar instead.
                base_url = self._sidecar_base_url
            entries.append(
                {
                    "project_id": project_id,
                    "name": str(raw.get("name", "")).strip() or project_id,
                    "base_url": base_url,
                }
            )
        if own_project_id not in seen:
            # The caller did not list this sidecar's own project; bridge it directly
            # so the Connector always exposes the workspace it runs on.
            entries.insert(0, self._own_project_entry(own_project_id))
        return entries

    def disable(self) -> dict[str, Any]:
        self._stop_connector()
        self._save_settings({"enabled": False})
        return self.status()

    def unpair(self) -> dict[str, Any]:
        self._stop_pair_poll()
        self._stop_connector()
        self._save_settings({"enabled": False})
        try:
            self._identity_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"Unable to delete the Device identity: {exc}") from exc
        return self.status()

    def autostart_if_enabled(self) -> None:
        """Start the Connector on sidecar start when it was left enabled.

        Late-starting Project sidecars no longer get pruned: the Connector's
        per-Project event pumps retry until each sidecar answers, and the
        Desktop pushes fresh loopback URLs via enable() once its projects load.
        """
        settings = self._load_settings()
        if not settings.get("enabled"):
            return
        persisted = settings.get("projects")
        projects = persisted if isinstance(persisted, list) and persisted else None
        with self._lock:
            already_running = self._thread is not None and self._thread.is_alive()
        if already_running:
            return
        identity = self._load_identity()
        if identity is None or not identity.is_paired:
            return
        own_project_id = workspace_project_id(self._workspace_root)
        if projects is None:
            entries = [self._own_project_entry(own_project_id)]
        else:
            try:
                entries = self._normalize_projects(projects, own_project_id)
            except ValueError as exc:
                self._record_last_error(f"Remote Connector autostart failed: {exc}")
                return
        lease = self._take_host_lease(steal=False)
        if lease is None:
            # Another sidecar on this device already hosts the Connector; it will
            # pick this Project up via the Desktop's live project updates.
            return
        primary = next(entry for entry in entries if entry["project_id"] == own_project_id)
        extras = [entry for entry in entries if entry is not primary]
        try:
            connector = RemoteConnector(
                _relay_websocket_url(identity.relay_url),
                identity=identity,
                project_id=own_project_id,
                sidecar=LocalSidecarBridge(primary["base_url"]),
                sidecars={entry["project_id"]: LocalSidecarBridge(entry["base_url"]) for entry in extras} or None,
                project_names={entry["project_id"]: entry["name"] for entry in entries},
            )
        except Exception as exc:  # never let autostart take the sidecar down
            lease.release()
            with self._lock:
                if self._owner_lease is lease:
                    self._owner_lease = None
                self._last_error = f"Remote Connector autostart failed: {exc}"
            return
        stop_event = Event()
        thread = Thread(
            target=self._run_connector,
            args=(connector, stop_event),
            name="somnia-desktop-remote-connector",
            daemon=True,
        )
        with self._lock:
            self._stop_event = stop_event
            self._thread = thread
            self._connector = connector
            self._exposed_projects = [
                {"project_id": entry["project_id"], "name": entry["name"]} for entry in entries
            ]
            thread.start()

    def shutdown(self) -> None:
        """Stop the Connector and pair-poll threads with the sidecar (keeps enabled state)."""
        self._stop_pair_poll()
        self._stop_connector()

    def _stop_connector(self) -> None:
        with self._lock:
            stop_event = self._stop_event
            thread = self._thread
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=CONNECTOR_JOIN_TIMEOUT_SECONDS)
        with self._lock:
            if self._thread is thread:
                self._thread = None
                self._stop_event = None
                self._connector = None
            lease = self._owner_lease
            self._owner_lease = None
        if lease is not None:
            lease.release()

    def _run_connector(self, connector: RemoteConnector, stop_event: Event) -> None:
        def _on_connect() -> None:
            with self._lock:
                self._last_error = ""

        def _on_retry(reason: str, delay: float) -> None:
            with self._lock:
                if not stop_event.is_set():
                    self._last_error = f"Remote Connector disconnected ({reason}); reconnecting in {delay:.0f}s."

        try:
            connector.run_forever(stop_event, on_retry=_on_retry, on_connect=_on_connect)
        except ConnectorReplaced:
            # Informational, not an error: another sidecar on this device took
            # over hosting; its live project updates keep this Project exposed.
            with self._lock:
                self._last_error = ""
        except Exception as exc:
            with self._lock:
                if not stop_event.is_set():
                    self._last_error = f"Remote Connector stopped unexpectedly: {exc}"
                else:
                    self._last_error = self._last_error or ""
        except BaseException as exc:  # noqa: BLE001 - the sidecar must survive anything
            with self._lock:
                self._last_error = f"Remote Connector crashed: {exc}"
