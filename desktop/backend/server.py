from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import select
import socket
from threading import Lock, Thread
import time
import tomllib
from typing import Any
from urllib.parse import parse_qs, urlparse
import uuid

from desktop.backend.instance_lock import SidecarInstanceLock
from desktop.backend.remote_device import RemoteDeviceManager, RemoteNotPairedError, workspace_project_id
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
    serialize_session_summary,
    serialize_tool_log_detail,
    serialize_tool_log_index_entry,
    serialize_turn_result,
    websocket_accept_value,
)
from open_somnia import __version__
from open_somnia.app_service import AppService
from open_somnia.pid_liveness import pid_is_alive
from open_somnia.config.models import AppSettings
from open_somnia.config.backup import write_config_text, remove_config_file
from open_somnia.config.provider_presets import list_provider_presets, serialize_provider_preset
from open_somnia.config.settings import (
    APP_DIRNAME,
    _load_mcp_servers,
    _merge_config,
    _read_toml,
    global_config_path,
    persist_mcp_tool_enabled,
    workspace_config_path,
)
from open_somnia.mcp.registry import MCPRegistry
from open_somnia.path_completion import (
    MAX_PATH_COMPLETION_CANDIDATES,
    PATH_COMPLETION_CACHE_SECONDS,
    PathCandidate,
    match_path_completion_candidates,
    scan_path_completion_candidates,
    sort_path_completion_candidates,
)
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.execution_mode import execution_mode_spec, normalize_execution_mode
from open_somnia.runtime.messages import guess_image_media_type, parse_image_data_url
from open_somnia.runtime.project_init import build_project_init_prompt
from open_somnia.skills.loader import SkillLoader

CLIPBOARD_TEMP_DIRNAME = "temp"
IMAGE_MEDIA_TYPE_SUFFIXES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

logger = logging.getLogger(__name__)
CONFIG_SECTION_KEYS = {"provider", "runtime", "mcp", "hooks", "system_prompt"}
CONFIG_SCOPES = {"user", "project"}


class SidecarAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.message = str(message)


def _safe_image_stem(name: str) -> str:
    raw_name = os.path.splitext(os.path.basename(str(name or "").strip()))[0]
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in raw_name)
    normalized = cleaned.strip("-_")
    return normalized or "clipboard-image"


def _config_path_for_scope(workspace_root: Path, scope: str) -> Path:
    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope == "user":
        return global_config_path()
    if normalized_scope == "project":
        return workspace_config_path(workspace_root)
    raise ValueError("scope must be 'user' or 'project'.")


def _skills_dir_for_scope(workspace_root: Path, scope: str) -> Path:
    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope == "user":
        return Path.home() / APP_DIRNAME / "skills"
    if normalized_scope == "project":
        return workspace_root / APP_DIRNAME / "skills"
    raise ValueError("scope must be 'user' or 'project'.")


def _section_name(line: str) -> str | None:
    stripped = line.strip()
    if stripped.startswith("[[") and stripped.endswith("]]"):
        return stripped[2:-2].strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1].strip()
    return None


def _line_matches_config_section(line: str, section_key: str, *, in_builtin_hook_block: bool = False) -> bool:
    if section_key == "hooks" and in_builtin_hook_block:
        return True
    name = _section_name(line)
    if name is None:
        return False
    if section_key == "provider":
        return (
            name == "providers"
            or name.startswith("providers.")
            or name == "model_traits"
            or name.startswith("model_traits.")
            or name == "routing"
        )
    if section_key == "mcp":
        return name == "mcp_servers" or name.startswith("mcp_servers.")
    if section_key == "runtime":
        return name == "runtime"
    if section_key == "hooks":
        return name == "hooks" or name.startswith("hooks.")
    if section_key == "system_prompt":
        return name == "agent"
    return False


def _extract_config_section(text: str, section_key: str) -> str:
    lines = text.splitlines()
    selected: list[str] = []
    current_matches = False
    in_builtin_hook_block = False
    for line in lines:
        marker = line.strip()
        if marker == "# BEGIN SOMNIA BUILTIN HOOKS":
            in_builtin_hook_block = True
        section_name = _section_name(line)
        if section_name is not None:
            current_matches = _line_matches_config_section(
                line,
                section_key,
                in_builtin_hook_block=in_builtin_hook_block,
            )
        if current_matches or (section_key == "hooks" and in_builtin_hook_block):
            selected.append(line)
        if marker == "# END SOMNIA BUILTIN HOOKS":
            in_builtin_hook_block = False
            current_matches = False
    return "\n".join(selected).strip()


def _remove_config_section(text: str, section_key: str) -> str:
    lines = text.splitlines()
    kept: list[str] = []
    current_matches = False
    in_builtin_hook_block = False
    for line in lines:
        marker = line.strip()
        if marker == "# BEGIN SOMNIA BUILTIN HOOKS":
            in_builtin_hook_block = True
        section_name = _section_name(line)
        if section_name is not None:
            current_matches = _line_matches_config_section(
                line,
                section_key,
                in_builtin_hook_block=in_builtin_hook_block,
            )
        should_remove = current_matches or (section_key == "hooks" and in_builtin_hook_block)
        if not should_remove:
            kept.append(line)
        if marker == "# END SOMNIA BUILTIN HOOKS":
            in_builtin_hook_block = False
            current_matches = False
    return _normalize_config_text("\n".join(kept))


def _normalize_config_text(text: str) -> str:
    normalized: list[str] = []
    previous_blank = False
    for line in text.splitlines():
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        normalized.append(line.rstrip())
        previous_blank = is_blank
    while normalized and not normalized[0].strip():
        normalized.pop(0)
    while normalized and not normalized[-1].strip():
        normalized.pop()
    return "\n".join(normalized).strip()


def _replace_config_section(text: str, section_key: str, content: str) -> str:
    base = _remove_config_section(text, section_key)
    next_content = _normalize_config_text(str(content or ""))
    parts = [part for part in (base, next_content) if part]
    return ("\n\n".join(parts).strip() + "\n") if parts else ""


def _raw_config_has_mcp_server(raw: dict[str, Any], server_name: str) -> bool:
    servers = raw.get("mcp_servers", {})
    if isinstance(servers, dict):
        return server_name in servers
    if isinstance(servers, list):
        return any(isinstance(item, dict) and str(item.get("name", "")).strip() == server_name for item in servers)
    return False


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _compact_subagent_text(text: Any, *, limit: int) -> str:
    compact = " ".join(str(text or "").split())
    if not compact:
        return ""
    return compact if len(compact) <= limit else f"{compact[:limit]}…"


def _render_subagent_log(entries: list[dict[str, Any]]) -> str:
    lines = ["[subagent log]"]
    for entry in entries:
        event_type = str(entry.get("type", "event"))
        if event_type == "started":
            lines.append(f"- started ({entry.get('agent_type', 'subagent')})")
            lines.append(f"  prompt: {_compact_subagent_text(entry.get('prompt'), limit=120)}")
        elif event_type == "assistant_message":
            lines.append(f"- assistant: {_compact_subagent_text(entry.get('content'), limit=200)}")
        elif event_type == "tool_call":
            lines.append(
                f"- tool {entry.get('tool_name', 'unknown')}: "
                f"{_compact_subagent_text(entry.get('tool_input', {}), limit=120)}"
            )
            lines.append(f"  result: {_compact_subagent_text(entry.get('output_preview', '(no output)'), limit=200)}")
        elif event_type == "summary":
            lines.append(f"- summary: {_compact_subagent_text(entry.get('content'), limit=200)}")
        elif event_type == "error":
            lines.append(f"- error: {_compact_subagent_text(entry.get('error', 'unknown error'), limit=200)}")
        else:
            lines.append(f"- {event_type}")
    return "\n".join(lines)


def _toml_unquote(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1]
    return stripped


def _upsert_mcp_enabled_in_table(lines: list[str], server_name: str, enabled: bool) -> bool:
    header = f"[mcp_servers.{server_name}]"
    start: int | None = None
    end = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == header:
            start = index
            continue
        if start is not None and stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    if start is None:
        return False
    assignment = f"enabled = {_toml_bool(enabled)}"
    for index in range(start + 1, end):
        if lines[index].strip().startswith("enabled ="):
            lines[index] = assignment
            return True
    lines.insert(end, assignment)
    return True


def _upsert_mcp_enabled_in_array_table(lines: list[str], server_name: str, enabled: bool) -> bool:
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[[mcp_servers]]":
            if start is not None:
                blocks.append((start, index))
            start = index
            continue
        if start is not None and stripped.startswith("[") and stripped.endswith("]") and stripped != "[[mcp_servers]]":
            blocks.append((start, index))
            start = None
    if start is not None:
        blocks.append((start, len(lines)))
    for start, end in blocks:
        has_name = False
        for index in range(start + 1, end):
            stripped = lines[index].strip()
            if not stripped.startswith("name"):
                continue
            key, _, value = stripped.partition("=")
            if key.strip() == "name" and _toml_unquote(value) == server_name:
                has_name = True
                break
        if not has_name:
            continue
        assignment = f"enabled = {_toml_bool(enabled)}"
        for index in range(start + 1, end):
            if lines[index].strip().startswith("enabled ="):
                lines[index] = assignment
                return True
        lines.insert(end, assignment)
        return True
    return False


def _persist_mcp_server_enabled(workspace_root: Path, server_name: str, enabled: bool) -> Path:
    global_path = global_config_path()
    project_path = workspace_config_path(workspace_root)
    project_raw = _read_toml(project_path)
    global_raw = _read_toml(global_path)
    if _raw_config_has_mcp_server(project_raw, server_name):
        config_path = project_path
    elif _raw_config_has_mcp_server(global_raw, server_name):
        config_path = global_path
    else:
        config_path = project_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = config_path.read_text(encoding="utf-8").splitlines() if config_path.exists() else []
    changed = _upsert_mcp_enabled_in_table(lines, server_name, enabled)
    if not changed:
        changed = _upsert_mcp_enabled_in_array_table(lines, server_name, enabled)
    if not changed:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"[mcp_servers.{server_name}]", f"enabled = {_toml_bool(enabled)}"])
    write_config_text(config_path, "\n".join(lines) + "\n")
    return config_path


def _list_skills_for_scope(workspace_root: Path, scope: str) -> list[dict[str, str]]:
    skills_dir = _skills_dir_for_scope(workspace_root, scope)
    loader = SkillLoader(skills_dir)
    return [entry for entry in loader.list_entries() if entry.get("scope") in {scope, "global", "workspace"}]


def _parse_init_command(command: str) -> tuple[bool, str] | None:
    stripped = str(command or "").strip()
    if stripped != "/init" and not stripped.startswith("/init "):
        return None

    payload = stripped[len("/init") :].strip()
    force = False
    while payload:
        if payload == "--force" or payload.startswith("--force "):
            force = True
            payload = payload[len("--force") :].strip()
            continue
        if payload == "-f" or payload.startswith("-f "):
            force = True
            payload = payload[len("-f") :].strip()
            continue
        if payload.startswith("--"):
            option = payload.split(maxsplit=1)[0]
            raise SidecarAPIError(
                HTTPStatus.BAD_REQUEST,
                f"Unknown /init option: {option}. Usage: /init [--force] [extra instructions]",
            )
        break
    return force, payload


@dataclass(slots=True)
class _WebSocketClient:
    id: str
    queue: Queue


# Set by the desktop app when spawning a managed sidecar; enables the parent watchdog.
PARENT_WATCHDOG_ENV = "SOMNIA_SIDECAR_PARENT_PID"
PARENT_WATCHDOG_INTERVAL_SECONDS = 5.0


class _SidecarHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, request_handler_class, *, sidecar: "SidecarServer") -> None:
        self.sidecar = sidecar
        super().__init__(server_address, request_handler_class)


class SidecarServer:
    def __init__(self, settings: AppSettings, *, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.settings = settings
        self._instance_lock = SidecarInstanceLock(settings.workspace_root)
        self._instance_lock.acquire()
        try:
            self._initialize(host, port)
        except Exception:
            self._instance_lock.release()
            raise

    def _initialize(self, host: str, port: int) -> None:
        self.runtime = OpenAgentRuntime(self.settings)
        self.service = AppService(self.runtime)
        self._lock = Lock()
        self._clients: dict[str, _WebSocketClient] = {}
        self._active_turns: dict[str, Any] = {}
        self._turn_threads: dict[str, Thread] = {}
        self._path_completion_cache: list[PathCandidate] = []
        self._path_completion_scanned_at = 0.0
        self._top_level_path_completion_cache: list[PathCandidate] = []
        self._top_level_path_completion_scanned_at = 0.0
        self._closed = False
        self._server_thread: Thread | None = None
        self.httpd = _SidecarHTTPServer((host, port), _SidecarRequestHandler, sidecar=self)
        self.remote_device = RemoteDeviceManager(
            workspace_root=self.settings.workspace_root,
            data_dir=self.settings.storage.data_dir,
            sidecar_base_url=self.base_url,
        )

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
            "vision_provider": getattr(self.runtime.settings, "vision_provider", None),
            "vision_model": getattr(self.runtime.settings, "vision_model", None),
            "reasoning_level": self.runtime.settings.provider.reasoning_level,
            "execution_mode": execution_mode,
            "execution_mode_title": execution_mode_spec(execution_mode).title,
        }

    def list_workspace_paths(self, *, query: str = "", limit: int = 30) -> list[dict[str, str]]:
        normalized_query = str(query or "").strip()
        lowered = normalized_query.lower()
        max_results = max(1, min(100, int(limit)))
        if not lowered:
            candidates = self._top_level_workspace_path_candidates()
        elif "/" not in lowered and "\\" not in lowered and len(lowered) < 2 and not self._path_completion_cache:
            candidates = self._top_level_workspace_path_candidates()
        else:
            candidates = self._workspace_path_candidates()
        matches = (
            candidates[:max_results]
            if not lowered
            else match_path_completion_candidates(candidates, normalized_query, limit=max_results)
        )
        return [
            {"path": item.relative_path, "basename": item.basename, "kind": item.kind}
            for item in matches
        ]

    def _workspace_path_candidates(self) -> list[PathCandidate]:
        now = time.time()
        if self._path_completion_cache and now - self._path_completion_scanned_at < PATH_COMPLETION_CACHE_SECONDS:
            return self._path_completion_cache
        self._path_completion_cache = sort_path_completion_candidates(
            scan_path_completion_candidates(
                self.settings.workspace_root,
                max_candidates=MAX_PATH_COMPLETION_CANDIDATES,
            )
        )
        self._path_completion_scanned_at = now
        return self._path_completion_cache

    def _top_level_workspace_path_candidates(self) -> list[PathCandidate]:
        now = time.time()
        if (
            self._top_level_path_completion_cache
            and now - self._top_level_path_completion_scanned_at < PATH_COMPLETION_CACHE_SECONDS
        ):
            return self._top_level_path_completion_cache
        self._top_level_path_completion_cache = sorted(
            scan_path_completion_candidates(self.settings.workspace_root, max_depth=1, max_candidates=200),
            key=lambda item: (0 if item.kind == "dir" else 1, item.relative_path),
        )
        self._top_level_path_completion_scanned_at = now
        return self._top_level_path_completion_cache

    def save_inline_image(self, *, name: str, media_type: str, data_url: str) -> dict[str, str]:
        parsed = parse_image_data_url(data_url)
        if parsed is None:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "Inline image must be a supported image data URL.")
        parsed_media_type, encoded = parsed
        normalized_media_type = str(media_type or "").strip().lower() or parsed_media_type
        if normalized_media_type != parsed_media_type:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "Inline image media type does not match the data URL.")
        suffix = IMAGE_MEDIA_TYPE_SUFFIXES.get(parsed_media_type)
        if not suffix:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, f"Unsupported inline image media type: {parsed_media_type}")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "Inline image payload is not valid base64.") from exc

        temp_dir = self.settings.storage.data_dir / CLIPBOARD_TEMP_DIRNAME
        temp_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{_safe_image_stem(name)}-{uuid.uuid4().hex[:10]}{suffix}"
        image_path = temp_dir / filename
        image_path.write_bytes(image_bytes)
        relative_path = os.path.relpath(image_path, self.settings.workspace_root).replace(os.sep, "/")
        return {
            "path": relative_path,
            "absolute_path": str(image_path),
            "media_type": parsed_media_type,
        }

    def resolve_workspace_image(self, image_path: str) -> tuple[Path, str]:
        raw_path = str(image_path or "").strip()
        if not raw_path:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "path is required.")
        candidate = Path(raw_path)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.settings.workspace_root / candidate).resolve()
        workspace_root = self.settings.workspace_root.resolve()
        try:
            resolved.relative_to(workspace_root)
        except ValueError as exc:
            raise SidecarAPIError(HTTPStatus.FORBIDDEN, "Image path must stay inside the workspace.") from exc
        if not resolved.is_file():
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Image not found: {raw_path}")
        media_type = guess_image_media_type(resolved)
        if media_type is None:
            raise SidecarAPIError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, f"Unsupported image format: {raw_path}")
        return resolved, media_type

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.remote_device.autostart_if_enabled()
        self._start_parent_watchdog()
        self.httpd.serve_forever(poll_interval=poll_interval)

    def _start_parent_watchdog(
        self,
        *,
        interval_seconds: float = PARENT_WATCHDOG_INTERVAL_SECONDS,
        is_alive: Any = pid_is_alive,
    ) -> Thread | None:
        """Exit this sidecar when the spawning desktop app disappears.

        The app normally terminates its sidecars on exit, but a crash,
        force-kill, or `tauri dev` restart skips that path; without this
        watchdog the orphaned sidecar (and its remote connector) leaks.
        Disabled unless the spawner set SOMNIA_SIDECAR_PARENT_PID, so manual
        dev sidecars are unaffected. Known limitation: the OS may recycle the
        parent PID; the per-workspace instance lock still prevents duplicates.
        """
        raw_pid = os.environ.get(PARENT_WATCHDOG_ENV, "").strip()
        try:
            parent_pid = int(raw_pid)
        except ValueError:
            return None
        if parent_pid <= 0:
            return None

        def _watch() -> None:
            while not self.is_closed:
                if not is_alive(parent_pid):
                    self.close()
                    return
                time.sleep(interval_seconds)

        thread = Thread(target=_watch, name="somnia-sidecar-parent-watchdog", daemon=True)
        thread.start()
        return thread

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
        try:
            self.remote_device.shutdown()
        except Exception:
            pass
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
        self._instance_lock.release()

    def list_sessions(self) -> list[dict[str, Any]]:
        return [serialize_session_summary(summary) for summary in self.service.list_session_summaries()]

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

    def delete_session(self, session_id: str) -> dict[str, Any]:
        active_turn = next(
            (turn for turn in self._active_turns.values() if getattr(turn.session, "id", None) == session_id and not turn.is_done()),
            None,
        )
        if active_turn is not None:
            raise SidecarAPIError(HTTPStatus.CONFLICT, f"Session '{session_id}' has an active turn and cannot be deleted.")
        deleted = self.service.delete_session(session_id)
        if not deleted:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Session '{session_id}' was not found.")
        self.broadcast_event(make_sidecar_event("session_deleted", payload={"session_id": session_id}, session_id=session_id))
        return {"session_id": session_id, "deleted": True}

    def set_session_provider_model(
        self,
        session_id: str,
        provider_name: str | None,
        model: str | None,
    ) -> dict[str, Any]:
        try:
            session = self.service.set_session_provider_model(session_id, provider_name, model)
        except FileNotFoundError as exc:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Session '{session_id}' was not found.") from exc
        except ValueError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        # Normalize so clients get the workspace default explicitly, plus a
        # human-readable message describing what changed.
        effective_provider, effective_model = self.runtime.session_effective_provider(session)
        pinned = bool(getattr(session, "provider_override", None))
        message = (
            f"Session '{session.id}' pinned to provider '{effective_provider}' "
            f"with model '{effective_model}'."
            if pinned
            else f"Session '{session.id}' now follows the workspace default "
            f"(provider '{effective_provider}', model '{effective_model}')."
        )
        payload = {
            "message": message,
            "session": self._serialize_session(session),
            "provider": effective_provider,
            "model": effective_model,
            "pinned": pinned,
        }
        self.broadcast_event(
            make_sidecar_event(
                "session_model_updated",
                payload=payload,
                session_id=session.id,
            ),
        )
        return payload

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

    def list_provider_presets(self) -> list[dict[str, object]]:
        return [serialize_provider_preset(preset) for preset in list_provider_presets()]

    def debug_model_connection(self, provider_name: str, model: str) -> dict[str, Any]:
        try:
            result = self.service.debug_model_connection(provider_name, model)
        except ValueError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        return {"provider": provider_name, "model": model, **result}

    def config_payload(self) -> dict[str, Any]:
        scopes: list[dict[str, Any]] = []
        for scope in ("user", "project"):
            config_path = _config_path_for_scope(self.settings.workspace_root, scope)
            skills_dir = _skills_dir_for_scope(self.settings.workspace_root, scope)
            text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
            scopes.append(
                {
                    "scope": scope,
                    "label": "User" if scope == "user" else "Project",
                    "config_path": str(config_path),
                    "config_exists": config_path.exists(),
                    "skills_path": str(skills_dir),
                    "skills_exists": skills_dir.exists(),
                    "sections": {
                        "provider": _extract_config_section(text, "provider"),
                        "runtime": _extract_config_section(text, "runtime"),
                        "mcp": _extract_config_section(text, "mcp"),
                        "hooks": _extract_config_section(text, "hooks"),
                        "system_prompt": _extract_config_section(text, "system_prompt"),
                    },
                    "skills": _list_skills_for_scope(self.settings.workspace_root, scope),
                }
            )
        return {"scopes": scopes}

    def mcp_servers_payload(self) -> dict[str, Any]:
        registry = getattr(self.runtime, "mcp_registry", None)
        server_summaries = getattr(registry, "server_summaries", None)
        tool_summaries = getattr(registry, "tool_summaries", None)
        if registry is None or not callable(server_summaries):
            return {"servers": []}
        servers: list[dict[str, Any]] = []
        for summary in server_summaries():
            name = str(summary.get("name", ""))
            tools = tool_summaries(name) if callable(tool_summaries) else []
            servers.append({**summary, "tools": tools})
        return {"servers": servers}

    def debug_mcp_server(self, server_name: str) -> dict[str, Any]:
        normalized_name = str(server_name or "").strip()
        if not normalized_name:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "server name is required.")
        registry = getattr(self.runtime, "mcp_registry", None)
        refresh_server_tools = getattr(registry, "refresh_server_tools", None)
        if registry is None or not callable(refresh_server_tools):
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, "MCP registry is unavailable.")
        try:
            server = refresh_server_tools(normalized_name, registry=self.runtime.registry)
        except ValueError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        except Exception as exc:
            raise SidecarAPIError(HTTPStatus.BAD_GATEWAY, f"MCP tools/list failed for '{normalized_name}': {exc}") from exc
        self._mark_tool_registry_changed()
        return {"server": server, "tool_count": int(server.get("tool_count", 0))}

    def set_mcp_server_enabled(self, server_name: str, enabled: bool) -> dict[str, Any]:
        normalized_name = str(server_name or "").strip()
        if not normalized_name:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "server name is required.")
        try:
            config_path = _persist_mcp_server_enabled(self.settings.workspace_root, normalized_name, bool(enabled))
            self.reload_mcp_runtime()
        except Exception as exc:
            action = "enable" if enabled else "disable"
            raise SidecarAPIError(HTTPStatus.BAD_GATEWAY, f"MCP {action} failed for '{normalized_name}': {exc}") from exc
        server = next((item for item in self.mcp_servers_payload()["servers"] if item.get("name") == normalized_name), None)
        if server is None:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"MCP server '{normalized_name}' was not found after updating {config_path}.")
        return {
            "server": server,
            "enabled": bool(server.get("enabled")),
            "tool_count": int(server.get("tool_count", 0)),
            "config_path": str(config_path),
        }

    def set_mcp_tool_enabled(self, server_name: str, tool_name: str, enabled: bool) -> dict[str, Any]:
        normalized_server = str(server_name or "").strip()
        normalized_tool = str(tool_name or "").strip()
        if not normalized_server or not normalized_tool:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "server name and tool name are required.")
        try:
            config_path = persist_mcp_tool_enabled(self.settings.workspace_root, normalized_server, normalized_tool, bool(enabled))
            self.reload_mcp_runtime()
        except Exception as exc:
            action = "enable" if enabled else "disable"
            raise SidecarAPIError(
                HTTPStatus.BAD_GATEWAY,
                f"MCP {action} failed for tool '{normalized_tool}' on '{normalized_server}': {exc}",
            ) from exc
        server = next((item for item in self.mcp_servers_payload()["servers"] if item.get("name") == normalized_server), None)
        if server is None:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"MCP server '{normalized_server}' was not found after updating {config_path}.")
        return {
            "server": server,
            "tool": normalized_tool,
            "enabled": bool(enabled),
            "config_path": str(config_path),
        }

    def save_config_section(self, *, scope: str, section: str, content: str) -> dict[str, Any]:
        normalized_scope = str(scope or "").strip().lower()
        normalized_section = str(section or "").strip().lower()
        if normalized_scope not in CONFIG_SCOPES:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "scope must be 'user' or 'project'.")
        if normalized_section not in CONFIG_SECTION_KEYS:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "section must be provider, runtime, mcp, hooks, or system_prompt.")
        config_path = _config_path_for_scope(self.settings.workspace_root, normalized_scope)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        updated = _replace_config_section(original, normalized_section, str(content or ""))
        try:
            tomllib.loads(updated or "")
        except tomllib.TOMLDecodeError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, f"Config TOML is invalid: {exc}") from exc
        if updated:
            write_config_text(config_path, updated)
        else:
            remove_config_file(config_path)
        runtime_reloaded = False
        if normalized_section == "mcp":
            self.reload_mcp_runtime()
            runtime_reloaded = True
        elif normalized_section == "provider":
            try:
                self.runtime.reload_provider_configuration()
            except ValueError as exc:
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
            runtime_reloaded = True
        elif normalized_section == "runtime":
            self.runtime.reload_runtime_configuration()
            runtime_reloaded = True
        return {
            "scope": normalized_scope,
            "section": normalized_section,
            "config_path": str(config_path),
            "saved": True,
            "restart_required": not runtime_reloaded,
            "runtime_reloaded": runtime_reloaded,
        }

    def reload_mcp_runtime(self) -> None:
        self.runtime.reload_plugin_configuration(mcp_registry_factory=MCPRegistry)
        self.settings.mcp_servers = self.runtime.settings.mcp_servers
        self._mark_tool_registry_changed()

    def _mark_tool_registry_changed(self) -> None:
        invalidator = getattr(self.runtime, "invalidate_tool_schema_state", None)
        if callable(invalidator):
            invalidator()

    def switch_provider_model(self, provider_name: str, model: str) -> dict[str, Any]:
        try:
            message = self.service.switch_provider_model(provider_name, model)
        except ValueError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        payload = {
            "message": message,
            "provider": str(self.runtime.settings.provider.name),
            "model": str(self.runtime.settings.provider.model),
            "vision_provider": getattr(self.runtime.settings, "vision_provider", None),
            "vision_model": getattr(self.runtime.settings, "vision_model", None),
        }
        self.broadcast_event(make_sidecar_event("provider_switched", payload=payload))
        return payload

    def set_vision_model(self, vision_provider: str | None, vision_model: str | None, *, scope: str = "project") -> dict[str, Any]:
        try:
            message = self.service.set_vision_model(vision_provider, vision_model, scope=scope)
        except ValueError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        payload = {
            "message": message,
            "provider": str(self.runtime.settings.provider.name),
            "model": str(self.runtime.settings.provider.model),
            "vision_provider": getattr(self.runtime.settings, "vision_provider", None),
            "vision_model": getattr(self.runtime.settings, "vision_model", None),
        }
        self.broadcast_event(make_sidecar_event("vision_model_updated", payload=payload))
        return payload

    def set_reasoning_level(self, reasoning_level: str | None) -> dict[str, Any]:
        message = self.service.set_reasoning_level(reasoning_level)
        payload = {
            "message": message,
            "provider": str(self.runtime.settings.provider.name),
            "model": str(self.runtime.settings.provider.model),
            "vision_provider": getattr(self.runtime.settings, "vision_provider", None),
            "vision_model": getattr(self.runtime.settings, "vision_model", None),
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

    def remote_status(self) -> dict[str, Any]:
        return self.remote_device.status()

    def remote_pair_begin(self, *, relay_url: str) -> dict[str, Any]:
        try:
            return self.remote_device.pair_begin(relay_url=relay_url)
        except ValueError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        except RuntimeError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_GATEWAY, str(exc)) from exc

    def remote_pair_cancel(self) -> dict[str, Any]:
        return self.remote_device.pair_cancel()

    def remote_enable(self, projects: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        try:
            return self.remote_device.enable(projects)
        except RemoteNotPairedError as exc:
            raise SidecarAPIError(HTTPStatus.CONFLICT, str(exc)) from exc
        except ValueError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        except RuntimeError as exc:
            raise SidecarAPIError(HTTPStatus.BAD_GATEWAY, str(exc)) from exc

    def remote_project_id(self) -> dict[str, Any]:
        return {"project_id": workspace_project_id(self.settings.workspace_root)}

    def remote_disable(self) -> dict[str, Any]:
        return self.remote_device.disable()

    def remote_unpair(self) -> dict[str, Any]:
        try:
            return self.remote_device.unpair()
        except RuntimeError as exc:
            raise SidecarAPIError(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc)) from exc

    def start_turn(self, session_id: str, user_input: str | dict[str, Any]) -> dict[str, Any]:
        try:
            session = self.service.load_session(session_id)
        except FileNotFoundError as exc:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Session '{session_id}' was not found.") from exc
        try:
            handle = self.service.run_turn(session, self._prepare_turn_user_input(user_input))
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

    def _prepare_turn_user_input(self, user_input: str | dict[str, Any]) -> str | dict[str, Any]:
        if not isinstance(user_input, str):
            return user_input
        init_command = _parse_init_command(user_input)
        if init_command is None:
            return user_input
        force, extra_prompt = init_command
        target = self.settings.workspace_root / "AGENTS.md"
        if target.exists() and not force:
            raise SidecarAPIError(
                HTTPStatus.CONFLICT,
                "AGENTS.md already exists. Use /init --force to regenerate it.",
            )
        return build_project_init_prompt(
            self.settings.workspace_root,
            force=force,
            extra_prompt=extra_prompt,
        ).prompt

    def compact_session(self, session_id: str) -> dict[str, Any]:
        try:
            session = self.service.load_session(session_id)
        except FileNotFoundError as exc:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Session '{session_id}' was not found.") from exc
        payload = {"message": self.service.compact_session(session), "session": self._serialize_session(session)}
        self.broadcast_event(make_sidecar_event("session_updated", payload={"session": payload["session"]}, session_id=session.id))
        return payload

    def janitor_session(self, session_id: str) -> dict[str, Any]:
        try:
            session = self.service.load_session(session_id)
        except FileNotFoundError as exc:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Session '{session_id}' was not found.") from exc
        message = self.service.run_semantic_janitor(session)
        payload = {"message": message, "session": self._serialize_session(session)}
        self.broadcast_event(make_sidecar_event("session_updated", payload={"session": payload["session"]}, session_id=session.id))
        return payload

    def interrupt_turn(self, turn_id: str) -> dict[str, Any]:
        interrupted = self.service.interrupt_turn(turn_id)
        return {"turn_id": str(turn_id).strip(), "interrupted": bool(interrupted)}

    def queue_loop_injection(self, turn_id: str, user_input: str | dict[str, Any], injection_id: str | None = None) -> dict[str, Any]:
        queued = self.service.queue_loop_injection(turn_id, user_input, injection_id=injection_id)
        if not queued:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Active turn '{turn_id}' was not found.")
        return {"turn_id": str(turn_id).strip(), "injection_id": str(injection_id or "").strip(), "queued": True}

    def cancel_loop_injection(self, turn_id: str, injection_id: str) -> dict[str, Any]:
        cancelled = self.service.cancel_loop_injection(turn_id, injection_id)
        if not cancelled:
            raise SidecarAPIError(
                HTTPStatus.NOT_FOUND,
                f"Queued prompt '{injection_id}' on turn '{turn_id}' was not found or is already being processed.",
            )
        return {"turn_id": str(turn_id).strip(), "injection_id": str(injection_id).strip(), "cancelled": True}

    def pending_interactions(self) -> list[dict[str, Any]]:
        return [serialize_interaction(interaction) for interaction in self.service.pending_interactions()]

    def runtime_status(self) -> dict[str, Any]:
        payload = self.ready_payload()
        payload["pending_interaction_count"] = len(self.service.pending_interactions())
        payload["open_session_count"] = len(self.service.list_session_summaries())
        payload["active_turns"] = [
            {
                "turn_id": str(turn.turn_id),
                "session_id": str(turn.session.id),
            }
            for turn in self._active_turns.values()
            if not turn.is_done()
        ]
        return payload

    def list_tool_logs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        entries = self.runtime.tool_log_store.list_recent(limit=max(1, int(limit)))
        return [serialize_tool_log_index_entry(entry) for entry in entries]

    def active_team_members(self, session_id: str | None = None) -> list[dict[str, Any]]:
        manager = getattr(self.runtime, "team_manager", None)
        summaries = getattr(manager, "active_member_summaries", None)
        formatter = getattr(manager, "_format_member_summary", None)
        if not callable(summaries):
            return []
        try:
            members = list(summaries(session_id=session_id))
        except TypeError:
            members = list(summaries())
        payload: list[dict[str, Any]] = []
        for member in members:
            item = deepcopy(member) if isinstance(member, dict) else {}
            if callable(formatter):
                try:
                    item["summary"] = formatter(member)
                except Exception:
                    item["summary"] = ""
            payload.append(item)
        return payload

    def list_tasks(self, session_id: str | None = None) -> list[dict[str, Any]]:
        store = getattr(self.runtime, "task_store", None)
        lister = getattr(store, "list_all", None)
        if not callable(lister):
            return []
        return [deepcopy(task) for task in lister(session_id=session_id)]

    def get_tool_log(self, log_id: str) -> dict[str, Any]:
        entry = self.runtime.tool_log_store.get(log_id)
        if entry is None:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Tool log '{log_id}' was not found.")
        payload = serialize_tool_log_detail(entry)
        payload["rendered"] = self.runtime.render_tool_log(log_id)
        return payload

    def get_thinking_log(self, path: str) -> dict[str, Any]:
        raw_path = str(path or "").strip()
        if not raw_path:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "path is required.")
        transcript_root = Path(getattr(self.runtime.transcript_store, "root", ""))
        thinking_root = (transcript_root / "thinking").resolve()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (thinking_root / candidate.name).resolve()
        else:
            candidate = candidate.resolve()
        if thinking_root not in candidate.parents and candidate != thinking_root:
            raise SidecarAPIError(HTTPStatus.FORBIDDEN, "Thinking log path is outside this workspace.")
        if not candidate.exists() or not candidate.is_file():
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, "Thinking log was not found.")
        text_parts: list[str] = []
        for line in candidate.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            value = item.get("delta", item.get("thinking", item.get("data", "")))
            if value:
                text_parts.append(str(value))
        return {"thinking_log": {"path": str(candidate), "text": "".join(text_parts)}}

    def get_team_log(self, name: str, session_id: str | None = None) -> dict[str, Any]:
        member_name = str(name or "").strip()
        if not member_name:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "name is required.")
        manager = getattr(self.runtime, "team_manager", None)
        renderer = getattr(manager, "render_log", None)
        entries_reader = getattr(manager, "log_entries", None)
        entries = entries_reader(member_name, session_id=session_id) if callable(entries_reader) else []
        if callable(renderer):
            rendered = renderer(member_name, session_id=session_id)
        else:
            runtime_renderer = getattr(self.runtime, "render_team_log", None)
            if not callable(runtime_renderer):
                raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Teammate '{member_name}' was not found.")
            rendered = runtime_renderer(member_name)
        return {"team_log": {"name": member_name, "session_id": session_id, "rendered": rendered, "entries": entries}}

    def get_subagent_log(self, activity_id: str) -> dict[str, Any]:
        normalized_id = str(activity_id or "").strip()
        if not normalized_id:
            raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "activity_id is required.")
        store = getattr(self.runtime, "subagent_log_store", None)
        if store is None:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Subagent log '{normalized_id}' was not found.")
        entries = store.read(normalized_id)
        if not entries:
            raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Subagent log '{normalized_id}' was not found.")
        return {
            "subagent_log": {
                "activity_id": normalized_id,
                "rendered": _render_subagent_log(entries),
                "entries": entries,
            }
        }

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
                # Was a silent "continue": a dropped event (e.g. turn_result)
                # leaves remote clients stuck on a stale turn with no trace.
                logger.warning(
                    "Dropping event for WebSocket client %s (type=%s)",
                    client.id,
                    event.get("type"),
                    exc_info=True,
                )

    def _drain_turn_events(self, handle) -> None:
        latest_context_usage = None
        try:
            while True:
                batch = handle.drain_events(block=not handle.is_done(), timeout=0.05)
                if batch:
                    for event in batch:
                        payload = serialize_app_event(event)
                        if payload.get("type") == "context_usage_updated":
                            context_usage = (payload.get("payload") or {}).get("context_window_usage")
                            if isinstance(context_usage, dict):
                                latest_context_usage = deepcopy(context_usage)
                        self.broadcast_event(payload)
                    continue
                if handle.is_done():
                    trailing = handle.drain_events()
                    if trailing:
                        for event in trailing:
                            payload = serialize_app_event(event)
                            if payload.get("type") == "context_usage_updated":
                                context_usage = (payload.get("payload") or {}).get("context_window_usage")
                                if isinstance(context_usage, dict):
                                    latest_context_usage = deepcopy(context_usage)
                            self.broadcast_event(payload)
                        continue
                    break
            if handle.result is not None:
                payload = serialize_turn_result(handle.result)
                if payload.get("session") is not None:
                    payload["session"] = self._serialize_session(handle.result.session)
                    if latest_context_usage is not None:
                        payload["session"]["context_window_usage"] = deepcopy(latest_context_usage)
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
    protocol_version = "HTTP/1.1"

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
        if parsed.path == "/workspace/images":
            self._handle_workspace_image(parsed)
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

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload, status_code = self._route_delete(parsed)
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
        if path_parts == ["provider-presets"]:
            return {"presets": self.sidecar.list_provider_presets()}
        if path_parts == ["models"]:
            provider_name = (query.get("provider") or [None])[0]
            return {"models": self.sidecar.list_models(provider_name)}
        if path_parts == ["settings", "config"]:
            return self.sidecar.config_payload()
        if path_parts == ["mcp", "servers"]:
            return self.sidecar.mcp_servers_payload()
        if path_parts == ["workspace", "paths"]:
            raw_limit = (query.get("limit") or [30])[0]
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "limit must be an integer.")
            path_query = (query.get("q") or [""])[0]
            return {"paths": self.sidecar.list_workspace_paths(query=path_query, limit=limit)}
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
        if len(path_parts) == 2 and path_parts[0] == "subagent-logs":
            return self.sidecar.get_subagent_log(path_parts[1])
        if path_parts == ["thinking-log"]:
            return self.sidecar.get_thinking_log((query.get("path") or [""])[0])
        if path_parts == ["team", "log"]:
            return self.sidecar.get_team_log((query.get("name") or [""])[0], (query.get("session_id") or [None])[0])
        if path_parts == ["team", "active"]:
            session_id = (query.get("session_id") or [None])[0]
            return {"members": self.sidecar.active_team_members(session_id)}
        if path_parts == ["tasks"]:
            session_id = (query.get("session_id") or [None])[0]
            return {"tasks": self.sidecar.list_tasks(session_id)}
        if path_parts == ["remote", "status"]:
            return self.sidecar.remote_status()
        if path_parts == ["remote", "project-id"]:
            return self.sidecar.remote_project_id()
        raise SidecarAPIError(HTTPStatus.NOT_FOUND, f"Unknown route: {parsed.path}")

    def _handle_workspace_image(self, parsed) -> None:
        try:
            query = parse_qs(parsed.query)
            image_path = (query.get("path") or [""])[0]
            resolved, media_type = self.sidecar.resolve_workspace_image(image_path)
            data = resolved.read_bytes()
            self.send_response(HTTPStatus.OK)
            self._send_common_headers(content_type=media_type, content_length=len(data))
            self.end_headers()
            self.wfile.write(data)
        except SidecarAPIError as exc:
            self._send_json(exc.status_code, {"error": exc.message})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

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
        if len(path_parts) == 3 and path_parts[0] == "sessions" and path_parts[2] == "compact":
            return self.sidecar.compact_session(path_parts[1]), HTTPStatus.OK
        if len(path_parts) == 3 and path_parts[0] == "sessions" and path_parts[2] == "janitor":
            return self.sidecar.janitor_session(path_parts[1]), HTTPStatus.OK
        if len(path_parts) == 3 and path_parts[0] == "sessions" and path_parts[2] == "model":
            session_id = path_parts[1]
            raw_provider = body.get("provider_name")
            raw_model = body.get("model")
            provider_name = None if raw_provider in {None, "", "none", "auto", "default"} else str(raw_provider).strip()
            model = None if raw_model in {None, "", "none", "auto", "default"} else str(raw_model).strip()
            # Either both or neither: a pin with only one field is ambiguous.
            if (provider_name is None) != (model is None):
                raise SidecarAPIError(
                    HTTPStatus.BAD_REQUEST,
                    "provider_name and model must be set together, or both omitted to follow the default.",
                )
            return self.sidecar.set_session_provider_model(session_id, provider_name, model), HTTPStatus.OK
        if path_parts == ["workspace", "images"]:
            data_url = str(body.get("data_url", "")).strip()
            if not data_url:
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "data_url is required.")
            return (
                self.sidecar.save_inline_image(
                    name=str(body.get("name", "")).strip(),
                    media_type=str(body.get("media_type", "")).strip(),
                    data_url=data_url,
                ),
                HTTPStatus.CREATED,
            )
        if len(path_parts) == 3 and path_parts[0] == "turns" and path_parts[2] == "interrupt":
            return self.sidecar.interrupt_turn(path_parts[1]), HTTPStatus.OK
        if len(path_parts) == 3 and path_parts[0] == "turns" and path_parts[2] == "loop-injections":
            if "user_input" not in body:
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "user_input is required.")
            injection_id = body.get("injection_id")
            return (
                self.sidecar.queue_loop_injection(
                    path_parts[1],
                    body["user_input"],
                    str(injection_id).strip() if injection_id is not None else None,
                ),
                HTTPStatus.ACCEPTED,
            )
        if path_parts == ["providers", "switch"]:
            provider_name = str(body.get("provider_name", "")).strip()
            model = str(body.get("model", "")).strip()
            if not provider_name or not model:
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "provider_name and model are required.")
            return self.sidecar.switch_provider_model(provider_name, model), HTTPStatus.OK
        if path_parts == ["providers", "debug-model"]:
            provider_name = str(body.get("provider_name", "")).strip()
            model = str(body.get("model", "")).strip()
            if not provider_name or not model:
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "provider_name and model are required.")
            return self.sidecar.debug_model_connection(provider_name, model), HTTPStatus.OK
        if path_parts == ["vision-model"]:
            raw_scope = str(body.get("scope", "project")).strip().lower()
            raw_provider = body.get("vision_provider")
            raw_model = body.get("vision_model")
            vision_provider = None if raw_provider in {None, "", "none", "auto"} else str(raw_provider).strip()
            vision_model = None if raw_model in {None, "", "none", "auto"} else str(raw_model).strip()
            return self.sidecar.set_vision_model(vision_provider, vision_model, scope=raw_scope), HTTPStatus.OK
        if path_parts == ["reasoning"]:
            raw_level = body.get("reasoning_level")
            return self.sidecar.set_reasoning_level(None if raw_level in {"", "auto"} else raw_level), HTTPStatus.OK
        if path_parts == ["execution-mode"]:
            mode = str(body.get("mode", "")).strip()
            if not mode:
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "mode is required.")
            return self.sidecar.set_execution_mode(mode), HTTPStatus.OK
        if path_parts == ["remote", "pair-begin"]:
            return (
                self.sidecar.remote_pair_begin(relay_url=str(body.get("relay_url", "")).strip()),
                HTTPStatus.OK,
            )
        if path_parts == ["remote", "pair-cancel"]:
            return self.sidecar.remote_pair_cancel(), HTTPStatus.OK
        if path_parts == ["remote", "enable"]:
            projects = body.get("projects")
            return (
                self.sidecar.remote_enable(projects=projects if isinstance(projects, list) else None),
                HTTPStatus.OK,
            )
        if path_parts == ["remote", "disable"]:
            return self.sidecar.remote_disable(), HTTPStatus.OK
        if path_parts == ["remote", "unpair"]:
            return self.sidecar.remote_unpair(), HTTPStatus.OK
        if len(path_parts) == 4 and path_parts[0] == "mcp" and path_parts[1] == "servers" and path_parts[3] == "debug":
            return self.sidecar.debug_mcp_server(path_parts[2]), HTTPStatus.OK
        if len(path_parts) == 4 and path_parts[0] == "mcp" and path_parts[1] == "servers" and path_parts[3] == "enabled":
            if "enabled" not in body:
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "enabled is required.")
            return self.sidecar.set_mcp_server_enabled(path_parts[2], bool(body.get("enabled"))), HTTPStatus.OK
        if (
            len(path_parts) == 6
            and path_parts[0] == "mcp"
            and path_parts[1] == "servers"
            and path_parts[3] == "tools"
            and path_parts[5] == "enabled"
        ):
            if "enabled" not in body:
                raise SidecarAPIError(HTTPStatus.BAD_REQUEST, "enabled is required.")
            return (
                self.sidecar.set_mcp_tool_enabled(path_parts[2], path_parts[4], bool(body.get("enabled"))),
                HTTPStatus.OK,
            )
        if path_parts == ["settings", "config"]:
            return (
                self.sidecar.save_config_section(
                    scope=str(body.get("scope", "")).strip(),
                    section=str(body.get("section", "")).strip(),
                    content=str(body.get("content", "")),
                ),
                HTTPStatus.OK,
            )
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

    def _route_delete(self, parsed) -> tuple[dict[str, Any], int]:
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) == 4 and path_parts[0] == "turns" and path_parts[2] == "loop-injections":
            return self.sidecar.cancel_loop_injection(path_parts[1], path_parts[3]), HTTPStatus.OK
        if len(path_parts) == 2 and path_parts[0] == "sessions":
            return self.sidecar.delete_session(path_parts[1]), HTTPStatus.OK
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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

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
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            # The pump will reconnect, but events queued in the meantime are
            # lost; log it so silent event gaps stay diagnosable.
            logger.info("WebSocket client %s connection failed: %s", client.id, exc)
            return
        finally:
            self.sidecar.unregister_client(client.id)
