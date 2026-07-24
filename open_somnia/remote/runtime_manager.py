"""Local Project registration and single-owner Runtime management.

The registry and owner leases are deliberately device-local.  Project paths
are never returned by the Relay-facing protocol; a Connector turns a local
registration into a loopback bridge only after it has acquired its owner lease.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable
from urllib.parse import urlparse
import uuid

from open_somnia.remote.connector import LocalSidecarBridge
from open_somnia.storage.common import atomic_write_text, get_lock


PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RuntimeOwnershipError(RuntimeError):
    """Raised when another live process owns a registered Project Runtime."""


@dataclass(frozen=True, slots=True)
class ProjectRegistration:
    project_id: str
    name: str
    path: Path
    created_at: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "path": str(self.path),
            "created_at": self.created_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ProjectRegistration":
        return cls(
            project_id=_required_project_id(payload.get("project_id")),
            name=_required_name(payload.get("name")),
            path=Path(str(payload.get("path", ""))).expanduser().resolve(),
            created_at=float(payload.get("created_at", 0.0)),
        )


class ProjectRegistry:
    """Persistent, device-local registry of approved Project folders."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = get_lock(self.path)

    def list(self) -> list[ProjectRegistration]:
        with self._lock:
            payload = self._read_payload()
        return [ProjectRegistration.from_payload(item) for item in payload["projects"]]

    def get(self, project_id: str) -> ProjectRegistration:
        normalized = _required_project_id(project_id)
        for project in self.list():
            if project.project_id == normalized:
                return project
        raise KeyError(f"Registered Project not found: {normalized}")

    def register(self, project_id: str, path: Path, *, name: str | None = None) -> ProjectRegistration:
        normalized_id = _required_project_id(project_id)
        resolved_path = Path(path).expanduser().resolve()
        if not resolved_path.is_dir():
            raise ValueError(f"Project path must be an existing directory: {resolved_path}")
        normalized_name = _required_name(name or resolved_path.name)
        with self._lock:
            payload = self._read_payload()
            projects = [ProjectRegistration.from_payload(item) for item in payload["projects"]]
            existing = next((item for item in projects if item.project_id == normalized_id), None)
            duplicate_path = next((item for item in projects if item.path == resolved_path and item.project_id != normalized_id), None)
            if duplicate_path is not None:
                raise ValueError(f"Project path is already registered as '{duplicate_path.project_id}'.")
            registration = ProjectRegistration(
                project_id=normalized_id,
                name=normalized_name,
                path=resolved_path,
                created_at=existing.created_at if existing is not None else time.time(),
            )
            projects = [item for item in projects if item.project_id != normalized_id]
            projects.append(registration)
            self._write_payload(projects)
            return registration

    def unregister(self, project_id: str) -> bool:
        normalized_id = _required_project_id(project_id)
        with self._lock:
            payload = self._read_payload()
            projects = [ProjectRegistration.from_payload(item) for item in payload["projects"]]
            kept = [item for item in projects if item.project_id != normalized_id]
            if len(kept) == len(projects):
                return False
            self._write_payload(kept)
            return True

    def _read_payload(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "projects": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Project registry cannot be read: {self.path}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("projects"), list):
            raise RuntimeError(f"Project registry has an invalid shape: {self.path}")
        return payload

    def _write_payload(self, projects: list[ProjectRegistration]) -> None:
        atomic_write_text(
            self.path,
            json.dumps(
                {"version": 1, "projects": [project.to_payload() for project in projects]},
                indent=2,
                ensure_ascii=False,
            ),
        )


@dataclass(slots=True)
class _ManagedRuntime:
    project: ProjectRegistration
    runtime: Any
    lease: "_OwnerLease"


@dataclass(frozen=True, slots=True)
class RuntimeConnection:
    """A device-local endpoint advertised by a Connector-owned Runtime."""

    project_id: str
    workspace_root: Path
    base_url: str
    ws_url: str
    owner_pid: int
    owner_token: str
    started_at: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "workspace_root": str(self.workspace_root),
            "base_url": self.base_url,
            "ws_url": self.ws_url,
            "owner_pid": self.owner_pid,
            "owner_token": self.owner_token,
            "started_at": self.started_at,
        }


class ProjectRuntimeManager:
    """Own at most one live Runtime host for each registered Project."""

    def __init__(
        self,
        registry: ProjectRegistry,
        *,
        runtime_factory: Callable[[ProjectRegistration], Any] | None = None,
        owner_dir: Path | None = None,
    ) -> None:
        self.registry = registry
        self.runtime_factory = runtime_factory or _default_runtime_factory
        self.owner_dir = Path(owner_dir or registry.path.with_name("runtime-owners")).expanduser()
        self.connection_path = registry.path.with_name("runtime-connections.json")
        self._connection_lock = get_lock(self.connection_path)
        self._managed: dict[str, _ManagedRuntime] = {}

    def start(self, project_id: str) -> Any:
        project = self.registry.get(project_id)
        current = self._managed.get(project.project_id)
        if current is not None:
            if not _runtime_is_closed(current.runtime):
                return current.runtime
            self.stop(project.project_id)

        lease = _OwnerLease(self.owner_path(project.project_id), project.project_id)
        lease.acquire()
        try:
            runtime = self.runtime_factory(project)
            _start_runtime(runtime)
        except Exception:
            lease.release()
            raise
        try:
            self._publish_connection(project, runtime, lease)
        except Exception:
            _stop_runtime(runtime)
            lease.release()
            raise
        self._managed[project.project_id] = _ManagedRuntime(project, runtime, lease)
        return runtime

    def ensure_started(self, project_id: str) -> Any:
        return self.start(project_id)

    def stop(self, project_id: str) -> bool:
        normalized = _required_project_id(project_id)
        managed = self._managed.pop(normalized, None)
        if managed is None:
            return False
        try:
            _stop_runtime(managed.runtime)
        finally:
            self._remove_connection(normalized, managed.lease.token)
            managed.lease.release()
        return True

    def stop_all(self) -> None:
        for project_id in list(self._managed):
            self.stop(project_id)

    def is_started(self, project_id: str) -> bool:
        managed = self._managed.get(_required_project_id(project_id))
        return managed is not None and not _runtime_is_closed(managed.runtime)

    def bridge(self, project_id: str) -> LocalSidecarBridge:
        runtime = self.ensure_started(project_id)
        base_url = str(getattr(runtime, "base_url", "")).strip()
        if not base_url:
            raise RuntimeError(f"Managed Runtime for '{project_id}' does not expose a loopback base URL.")
        return LocalSidecarBridge(base_url)

    def bridges(self, project_ids: list[str] | None = None) -> dict[str, LocalSidecarBridge]:
        selected = project_ids if project_ids is not None else [project.project_id for project in self.registry.list()]
        return {project_id: self.bridge(project_id) for project_id in selected}

    def owner_path(self, project_id: str) -> Path:
        return self.owner_dir / f"{_required_project_id(project_id)}.owner"

    def _publish_connection(self, project: ProjectRegistration, runtime: Any, lease: "_OwnerLease") -> None:
        base_url = str(getattr(runtime, "base_url", "")).strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError(f"Managed Runtime for '{project.project_id}' does not expose a loopback base URL.")
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        connection = RuntimeConnection(
            project_id=project.project_id,
            workspace_root=project.path,
            base_url=base_url,
            ws_url=parsed._replace(scheme=ws_scheme, path="/ws", params="", query="", fragment="").geturl(),
            owner_pid=os.getpid(),
            owner_token=lease.token,
            started_at=time.time(),
        )
        with self._connection_lock:
            connections = self._read_connections()
            connections = [item for item in connections if item.get("project_id") != project.project_id]
            connections.append(connection.to_payload())
            self._write_connections(connections)

    def _remove_connection(self, project_id: str, owner_token: str) -> None:
        with self._connection_lock:
            connections = self._read_connections()
            kept = [
                item for item in connections
                if not (item.get("project_id") == project_id and item.get("owner_token") == owner_token)
            ]
            if len(kept) != len(connections):
                self._write_connections(kept)

    def _read_connections(self) -> list[dict[str, Any]]:
        if not self.connection_path.exists():
            return []
        try:
            payload = json.loads(self.connection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        connections = payload.get("connections") if isinstance(payload, dict) else None
        return [item for item in connections if isinstance(item, dict)] if isinstance(connections, list) else []

    def _write_connections(self, connections: list[dict[str, Any]]) -> None:
        atomic_write_text(
            self.connection_path,
            json.dumps({"version": 1, "connections": connections}, indent=2, ensure_ascii=False),
        )


class _OwnerLease:
    def __init__(self, path: Path, project_id: str) -> None:
        self.path = path
        self.project_id = project_id
        self.token = uuid.uuid4().hex
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if _owner_is_alive(self.path):
                    raise RuntimeOwnershipError(f"Project '{self.project_id}' is already owned by another process.")
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {"pid": os.getpid(), "project_id": self.project_id, "token": self.token, "started_at": time.time()},
                    handle,
                )
            self._acquired = True
            return
        raise RuntimeOwnershipError(f"Unable to acquire the owner lease for '{self.project_id}'.")

    def release(self) -> None:
        if not self._acquired or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if payload.get("token") == self.token:
            self.path.unlink(missing_ok=True)
        self._acquired = False


def _owner_is_alive(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload.get("pid", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        ctypes.windll.kernel32.CloseHandle(process)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _required_project_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not PROJECT_ID_PATTERN.fullmatch(normalized):
        raise ValueError("project_id must contain 1-128 letters, numbers, '.', '_' or '-'.")
    return normalized


def _required_name(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 120:
        raise ValueError("Project name must contain between 1 and 120 characters.")
    return normalized


def _runtime_is_closed(runtime: Any) -> bool:
    value = getattr(runtime, "is_closed", False)
    return bool(value() if callable(value) else value)


def _start_runtime(runtime: Any) -> None:
    starter = getattr(runtime, "start", None) or getattr(runtime, "start_background", None)
    if not callable(starter):
        raise TypeError("Runtime host must expose start() or start_background().")
    starter()
    waiter = getattr(runtime, "wait_until_ready", None)
    if callable(waiter) and not waiter():
        raise RuntimeError("Managed Runtime did not become ready.")


def _stop_runtime(runtime: Any) -> None:
    stopper = getattr(runtime, "stop", None) or getattr(runtime, "close", None)
    if callable(stopper):
        stopper()


def _default_runtime_factory(project: ProjectRegistration) -> Any:
    from desktop.backend.server import SidecarServer
    from open_somnia.config.settings import load_settings

    settings = load_settings(project.path, allow_missing_provider=True)
    return SidecarServer.from_settings(settings, host="127.0.0.1", port=0)


def default_registry_path() -> Path:
    return Path.home() / ".open_somnia" / "remote" / "projects.json"
