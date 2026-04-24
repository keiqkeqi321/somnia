from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
from queue import Empty, Queue
from threading import Event, Lock, Thread

from open_somnia.app_service import AppService
from open_somnia.app_service.events import (
    ASSISTANT_DELTA,
    AUTHORIZATION_REQUESTED,
    MODE_SWITCH_REQUESTED,
    SESSION_UPDATED,
    TODO_UPDATED,
    TOOL_FINISHED,
)
from open_somnia.cli.commands import ConsoleStreamer, _assistant_prefix, _prefix_first_line, print_user_message
from open_somnia.cli.prompting import (
    COMMAND_SPECS,
    PROMPT_BORDER,
    PROMPT_TEXT,
    choose_authorization_interactively,
    choose_item_interactively,
    choose_mode_switch_interactively,
    create_prompt_session,
    fallback_prompt_message,
    prompt_text_interactively,
    styled_prompt_message,
)
from open_somnia.cli.provider_management import collect_provider_profile_interactively, choose_provider_target_interactively
from open_somnia.config.settings import persist_provider_profile
from open_somnia.config.settings import BUILTIN_NOTIFY_MANAGER
from open_somnia.hooks.models import normalize_hook_event
from open_somnia.reasoning import REASONING_LEVEL_VALUES, normalize_reasoning_level
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.compact import ContextWindowUsage
from open_somnia.runtime.execution_mode import (
    DEFAULT_EXECUTION_MODE,
    execution_mode_spec,
    execution_mode_status_text,
    next_execution_mode,
    normalize_execution_mode,
)
from open_somnia.runtime.messages import (
    encode_embedded_user_message,
    guess_image_media_type,
    make_user_multimodal_message,
    render_markdown_text,
    render_message_content,
    render_text_content,
)
from open_somnia.tools.filesystem import safe_path
from open_somnia.tools.todo import TODO_CLOSED_STATUSES, TODO_STATUS_MARKERS, TODO_VISIBLE_STATUSES

try:
    from prompt_toolkit.patch_stdout import patch_stdout
except Exception:  # pragma: no cover - prompt_toolkit may be unavailable in fallback mode
    patch_stdout = None


READ_ONLY_COMMAND_PREFIXES = (
    "/scan",
    "/symbols",
    "/janitor",
    "/providers",
    "/skills",
    "/tasks",
    "/team",
    "/teamlog",
    "/inbox",
    "/mcp",
    "/toollog",
    "/bg",
    "/hooks",
    "/help",
)
HOOK_EVENT_ORDER = (
    "SessionStart",
    "PreToolUse",
    "PostToolUse",
    "AssistantResponse",
    "UserChoiceRequested",
)
AUTHORIZATION_PROMPT_SENTINEL = "__open_somnia_authorization__"
CLIPBOARD_TEMP_DIRNAME = "temp"
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _parse_skill_command(query: str) -> tuple[str, str] | None:
    stripped = query.strip()
    if not stripped.startswith("/+"):
        return None
    payload = stripped[2:].strip()
    if not payload:
        return None
    parts = payload.split(maxsplit=1)
    skill_name = parts[0].strip()
    if not skill_name:
        return None
    remainder = parts[1].strip() if len(parts) > 1 else ""
    return skill_name, remainder


def _expand_skill_command(runtime, query: str) -> str:
    parsed = _parse_skill_command(query)
    if parsed is None:
        return query
    skill_name, remainder = parsed
    skill_payload = runtime.skill_loader.load(skill_name)
    if skill_payload.startswith("Error:"):
        return skill_payload
    instruction = f"The user explicitly requested skill '{skill_name}'. Follow it for this task."
    if remainder:
        return f"{skill_payload}\n\n{instruction}\n\n{remainder}"
    return f"{skill_payload}\n\n{instruction}"


def _parse_image_command(command: str) -> tuple[str, str] | None:
    payload = command[len("/image") :].strip()
    if not payload:
        return None
    if payload.startswith('"'):
        closing_quote = payload.find('"', 1)
        if closing_quote < 0:
            return None
        image_path = payload[1:closing_quote].strip()
        prompt = payload[closing_quote + 1 :].strip()
    else:
        parts = payload.split(maxsplit=1)
        image_path = parts[0].strip()
        prompt = parts[1].strip() if len(parts) > 1 else ""
    if not image_path:
        return None
    return image_path, prompt


def _build_image_query(runtime, command: str) -> str:
    parsed = _parse_image_command(command)
    if parsed is None:
        raise ValueError('Usage: /image <path> [prompt]. Use double quotes if the path contains spaces.')
    requested_path, prompt = parsed
    workspace_root = Path(runtime.settings.workspace_root)
    image_path = safe_path(workspace_root, requested_path)
    if not image_path.exists() or not image_path.is_file():
        raise ValueError(f"Image file not found: {requested_path}")
    media_type = guess_image_media_type(image_path)
    if media_type is None:
        raise ValueError(
            "Unsupported image format. Supported formats: png, jpg/jpeg, webp, gif."
        )
    message = make_user_multimodal_message(
        prompt or "Look at this image.",
        [
            {
                "type": "input_image",
                "path": requested_path.replace("\\", "/"),
                "absolute_path": str(image_path),
                "media_type": media_type,
            }
        ],
    )
    return encode_embedded_user_message(message)


def _format_image_command(relative_path: str) -> str:
    normalized = str(relative_path or "").replace("\\", "/")
    if any(char.isspace() for char in normalized):
        return f'/image "{normalized}" '
    return f"/image {normalized} "


def _powershell_single_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _clipboard_temp_dir(runtime) -> Path:
    return Path(runtime.settings.storage.data_dir) / CLIPBOARD_TEMP_DIRNAME


def _clipboard_temp_stem() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    token = f"{time.time_ns() % 1_000_000_000:09d}"
    return f"clipboard-{stamp}-{token}"


def _clipboard_image_command(runtime) -> str | None:
    saved_path = _save_clipboard_image(runtime)
    if saved_path is None:
        return None
    workspace_root = Path(runtime.settings.workspace_root).resolve()
    relative_path = saved_path.resolve().relative_to(workspace_root).as_posix()
    return _format_image_command(relative_path)


def _save_clipboard_image(runtime) -> Path | None:
    temp_dir = _clipboard_temp_dir(runtime)
    temp_dir.mkdir(parents=True, exist_ok=True)
    stem = _clipboard_temp_stem()
    if sys.platform.startswith("win"):
        return _save_windows_clipboard_image(temp_dir, stem)
    if sys.platform == "darwin":
        return _save_macos_clipboard_image(temp_dir, stem)
    return None


def _build_clipboard_image_query(runtime, prompt: str = "") -> str:
    saved_path = _save_clipboard_image(runtime)
    if saved_path is None:
        raise ValueError("No image found in the clipboard.")
    workspace_root = Path(runtime.settings.workspace_root).resolve()
    relative_path = saved_path.resolve().relative_to(workspace_root).as_posix()
    command = _format_image_command(relative_path)
    if prompt.strip():
        command += prompt.strip()
    return _build_image_query(runtime, command)


def _save_windows_clipboard_image(temp_dir: Path, stem: str) -> Path | None:
    copied_path = _copy_windows_clipboard_image_file(temp_dir, stem)
    if copied_path is not None:
        return copied_path
    dib_bytes = _read_windows_clipboard_dib_bytes()
    if not dib_bytes:
        return None
    bmp_path = temp_dir / f"{stem}.bmp"
    png_path = temp_dir / f"{stem}.png"
    try:
        bmp_path.write_bytes(_dib_to_bmp_bytes(dib_bytes))
        return _convert_bmp_file_to_png(bmp_path, png_path)
    finally:
        try:
            bmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@contextmanager
def _open_windows_clipboard(*, attempts: int = 8, delay_seconds: float = 0.05):
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    opened = False
    for attempt in range(max(1, int(attempts))):
        if user32.OpenClipboard(None):
            opened = True
            break
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    try:
        yield user32 if opened else None
    finally:
        if opened:
            try:
                user32.CloseClipboard()
            except Exception:
                pass


def _copy_windows_clipboard_image_file(temp_dir: Path, stem: str) -> Path | None:
    for source_path in _read_windows_clipboard_file_drop_paths():
        if not source_path.exists() or not source_path.is_file():
            continue
        suffix = source_path.suffix.lower()
        if suffix not in SUPPORTED_IMAGE_SUFFIXES:
            continue
        destination = temp_dir / f"{stem}{suffix}"
        shutil.copy2(source_path, destination)
        return destination
    return None


def _read_windows_clipboard_file_drop_paths() -> list[Path]:
    import ctypes
    from ctypes import wintypes

    clipboard_paths: list[Path] = []
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    with _open_windows_clipboard() as user32:
        if user32 is None:
            return clipboard_paths
        hdrop = user32.GetClipboardData(15)  # CF_HDROP
        if not hdrop:
            return clipboard_paths
        item_count = int(shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0))
        for index in range(item_count):
            path_length = int(shell32.DragQueryFileW(hdrop, index, None, 0))
            if path_length <= 0:
                continue
            buffer = ctypes.create_unicode_buffer(path_length + 1)
            shell32.DragQueryFileW(hdrop, index, buffer, path_length + 1)
            raw_path = buffer.value.strip()
            if raw_path:
                clipboard_paths.append(Path(raw_path))
    return clipboard_paths


def _read_windows_clipboard_dib_bytes() -> bytes | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    with _open_windows_clipboard() as user32:
        if user32 is None:
            return None
        for clipboard_format in (17, 8):  # CF_DIBV5, CF_DIB
            handle = user32.GetClipboardData(clipboard_format)
            if not handle:
                continue
            size = int(kernel32.GlobalSize(handle) or 0)
            if size <= 0:
                continue
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                continue
            try:
                payload = ctypes.string_at(pointer, size)
            finally:
                try:
                    kernel32.GlobalUnlock(handle)
                except Exception:
                    pass
            if payload:
                return payload
    return None


def _dib_to_bmp_bytes(dib_bytes: bytes) -> bytes:
    import struct

    if len(dib_bytes) < 16:
        raise ValueError("Clipboard image is missing a valid DIB header.")
    header_size = struct.unpack_from("<I", dib_bytes, 0)[0]
    if header_size < 12 or len(dib_bytes) < header_size:
        raise ValueError("Clipboard image uses an unsupported DIB header.")
    if header_size == 12:
        bits_per_pixel = struct.unpack_from("<H", dib_bytes, 10)[0]
        compression = 0
        colors_used = 0
    else:
        if len(dib_bytes) < 36:
            raise ValueError("Clipboard image header is truncated.")
        bits_per_pixel = struct.unpack_from("<H", dib_bytes, 14)[0]
        compression = struct.unpack_from("<I", dib_bytes, 16)[0]
        colors_used = struct.unpack_from("<I", dib_bytes, 32)[0]
    color_table_size = 0
    if header_size == 12 and bits_per_pixel <= 8:
        color_table_size = (colors_used or (1 << bits_per_pixel)) * 3
    elif bits_per_pixel <= 8:
        color_table_size = (colors_used or (1 << bits_per_pixel)) * 4
    elif compression == 3 and header_size == 40:
        color_table_size = 12
    elif compression == 6 and header_size == 40:
        color_table_size = 16
    pixel_offset = 14 + header_size + color_table_size
    file_size = 14 + len(dib_bytes)
    header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, pixel_offset)
    return header + dib_bytes


def _convert_bmp_file_to_png(source_path: Path, destination: Path) -> Path | None:
    script = """
Add-Type -AssemblyName System.Drawing
$sourcePath = __SOURCE_PATH__
$destinationPath = __DESTINATION_PATH__
$bitmap = $null
try {
    $bitmap = New-Object System.Drawing.Bitmap($sourcePath)
    $bitmap.Save($destinationPath, [System.Drawing.Imaging.ImageFormat]::Png)
    Write-Output $destinationPath
    exit 0
} catch {
    exit 4
} finally {
    if ($bitmap -ne $null) {
        $bitmap.Dispose()
    }
}
""".strip()
    script = script.replace("__SOURCE_PATH__", _powershell_single_quote(str(source_path)))
    script = script.replace("__DESTINATION_PATH__", _powershell_single_quote(str(destination)))
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    return _clipboard_command_result_path(completed, no_image_codes={4})


def _save_macos_clipboard_image(temp_dir: Path, stem: str) -> Path | None:
    copied_path = _copy_macos_clipboard_image_file(temp_dir, stem)
    if copied_path is not None:
        return copied_path

    destination = temp_dir / f"{stem}.png"
    png_script = [
        f'set outPath to POSIX file "{destination.as_posix()}"',
        "try",
        "set imageData to the clipboard as «class PNGf»",
        "set fileHandle to open for access outPath with write permission",
        "set eof fileHandle to 0",
        "write imageData to fileHandle",
        "close access fileHandle",
        "return POSIX path of outPath",
        "on error errMsg number errNum",
        "try",
        "close access outPath",
        "end try",
        "error errMsg number errNum",
        "end try",
    ]
    completed = _run_osascript(png_script)
    saved_path = _clipboard_command_result_path(completed, no_image_codes=set())
    if saved_path is not None:
        return saved_path

    tiff_path = temp_dir / f"{stem}.tiff"
    tiff_script = [
        f'set outPath to POSIX file "{tiff_path.as_posix()}"',
        "try",
        "set imageData to the clipboard as TIFF picture",
        "set fileHandle to open for access outPath with write permission",
        "set eof fileHandle to 0",
        "write imageData to fileHandle",
        "close access fileHandle",
        "return POSIX path of outPath",
        "on error errMsg number errNum",
        "try",
        "close access outPath",
        "end try",
        "error errMsg number errNum",
        "end try",
    ]
    completed = _run_osascript(tiff_script)
    exported_tiff = _clipboard_command_result_path(completed, no_image_codes=set())
    if exported_tiff is None:
        return None
    converted = subprocess.run(
        ["sips", "-s", "format", "png", str(exported_tiff), "--out", str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        exported_tiff.unlink(missing_ok=True)
    except Exception:
        pass
    if converted.returncode != 0 or not destination.exists():
        details = (converted.stderr or converted.stdout).strip()
        raise RuntimeError(details or "Failed to convert clipboard TIFF image to PNG.")
    return destination


def _copy_macos_clipboard_image_file(temp_dir: Path, stem: str) -> Path | None:
    file_list_script = [
        "try",
        "set aliasItems to the clipboard as alias list",
        "set AppleScript's text item delimiters to linefeed",
        "set posixItems to {}",
        "repeat with currentAlias in aliasItems",
        "copy POSIX path of currentAlias to end of posixItems",
        "end repeat",
        "set joinedText to posixItems as string",
        "set AppleScript's text item delimiters to \"\"",
        "return joinedText",
        "on error",
        "set AppleScript's text item delimiters to \"\"",
        "return \"\"",
        "end try",
    ]
    completed = _run_osascript(file_list_script)
    if completed.returncode != 0:
        return None
    for raw_path in (completed.stdout or "").splitlines():
        source_path = Path(raw_path.strip())
        if not source_path.exists() or not source_path.is_file():
            continue
        suffix = source_path.suffix.lower()
        if suffix not in SUPPORTED_IMAGE_SUFFIXES:
            continue
        destination = temp_dir / f"{stem}{suffix}"
        shutil.copy2(source_path, destination)
        return destination
    return None


def _run_osascript(lines: list[str]) -> subprocess.CompletedProcess[str]:
    args = ["osascript"]
    for line in lines:
        args.extend(["-e", line])
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )


def _clipboard_command_result_path(
    completed: subprocess.CompletedProcess[str],
    *,
    no_image_codes: set[int],
) -> Path | None:
    if completed.returncode == 0:
        output = (completed.stdout or "").strip().splitlines()
        if not output:
            return None
        saved_path = Path(output[-1].strip())
        if saved_path.exists():
            return saved_path
        return None
    if completed.returncode in no_image_codes:
        return None
    details = (completed.stderr or completed.stdout).strip()
    if "clipboard" in details.lower() or "PNGf" in details or "TIFF" in details:
        return None
    return None


def _assistant_tool_calls(content: object) -> list[dict[str, object]]:
    if not isinstance(content, list):
        return []
    calls: list[dict[str, object]] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_call":
            calls.append(item)
    return calls


def _tool_result_map(content: object) -> dict[str, object]:
    if not isinstance(content, list):
        return {}
    results: dict[str, object] = {}
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_result":
            results[str(item.get("tool_call_id", ""))] = item
    return results


def _print_resumed_tool_call(runtime, tool_name: str, payload: dict[str, object], result_payload: object) -> None:
    if isinstance(result_payload, dict):
        output = result_payload.get("raw_output", result_payload.get("content", "(no output)"))
        log_id = str(result_payload.get("log_id", "")).strip() or None
    else:
        output = result_payload
        log_id = None
    print()
    for line in runtime.render_tool_event_lines(tool_name, payload, output, log_id=log_id):
        print(line)
    print()


def _print_resumed_history(session, runtime=None) -> None:
    printed_any = False
    header_printed = False
    index = 0
    messages = list(getattr(session, "messages", []) or [])
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        content = message.get("content")
        if role == "user":
            if isinstance(content, str):
                if content.startswith("<background-results>") or content.startswith("<inbox>"):
                    index += 1
                    continue
                if not header_printed:
                    print("[resumed history]")
                    header_printed = True
                print_user_message(content)
                printed_any = True
            index += 1
            continue
        if role == "assistant":
            text = render_message_content(content, ansi=sys.stdout.isatty()).strip()
            if text:
                if not header_printed:
                    print("[resumed history]")
                    header_printed = True
                print()
                print(_prefix_first_line(text, _assistant_prefix(ansi=sys.stdout.isatty())))
                print()
                printed_any = True
            tool_calls = _assistant_tool_calls(content)
            tool_results = {}
            if index + 1 < len(messages):
                next_message = messages[index + 1]
                if next_message.get("role") == "user":
                    tool_results = _tool_result_map(next_message.get("content"))
                    if tool_results:
                        index += 1
            for tool_call in tool_calls:
                if not header_printed:
                    print("[resumed history]")
                    header_printed = True
                if runtime is None:
                    index += 1
                    continue
                _print_resumed_tool_call(
                    runtime,
                    str(tool_call.get("name", "")),
                    dict(tool_call.get("input", {}) or {}),
                    tool_results.get(str(tool_call.get("id", "")), "(no output)"),
                )
                printed_any = True
        index += 1
    if not printed_any:
        print("[resumed session has no visible chat history]")


@dataclass(slots=True)
class AuthorizationRequest:
    tool_name: str
    reason: str
    argument_summary: str
    execution_mode: str
    completed: Event
    response: dict[str, str] | None = None
    request_id: str | None = None


@dataclass(slots=True)
class ModeSwitchRequest:
    target_mode: str
    current_mode: str
    reason: str
    completed: Event
    response: dict[str, str] | None = None
    request_id: str | None = None


@dataclass(slots=True)
class QueueTask:
    id: int
    kind: str
    payload: str
    echo_on_start: bool
    preview: str


class TurnQueueRunner:
    THINKING_PHRASES = (
        "AI is cooking",
        "Processing vibes",
        "Doing robot thoughts",
        "Consulting the void",
        "Loading genius",
    )
    DONE_TEXT = "done"
    OPEN_TODOS_TEXT = "waiting_on_open_todos"
    STOPPED_WITH_OPEN_TODOS_TEXT = "stopped_with_open_todos"
    STOPPED_AFTER_MAX_ROUNDS_TEXT = "stopped_after_max_rounds"
    QUEUED_MESSAGES_NOTICE = "Queued: after turn; Esc sends next after tool"
    QUEUED_MESSAGES_ARMED_NOTICE = "Queued: next one sends after current tool"
    THINKING_FRAME_SECONDS = 0.25
    CONTEXT_HEALTHY_STYLE = "fg:#22c55e"
    CONTEXT_WARNING_STYLE = "fg:#84cc16"
    CONTEXT_REDUCING_STYLE = "fg:#f59e0b"
    CONTEXT_CRITICAL_STYLE = "fg:#ef4444"

    def __init__(self, runtime, session, *, stable_prompt: bool = False, service: AppService | None = None) -> None:
        self.runtime = runtime
        self.session = session
        self.service = service
        self.stable_prompt = stable_prompt
        self._execution_mode = normalize_execution_mode(getattr(runtime, "execution_mode", DEFAULT_EXECUTION_MODE))
        setattr(self.runtime, "execution_mode", self._execution_mode)
        self._queue: Queue[QueueTask | None] = Queue()
        self._lock = Lock()
        self._worker = Thread(target=self._worker_loop, name="open-somnia-chat-worker", daemon=True)
        self._active = False
        self._queued = 0
        self._status = ""
        self._status_changed_at = time.monotonic()
        self._ui_invalidator = None
        self._prompt_interrupter = None
        self._thinking_phrase = self.THINKING_PHRASES[0]
        self._next_query_id = 1
        self._queued_previews: list[tuple[int, str]] = []
        self._ready_loop_injections: list[str] = []
        self._ready_loop_injection_previews: list[str] = []
        self._loop_injection_requests = 0
        self._interrupt_requested = False
        self._authorization_requests: list[AuthorizationRequest] = []
        self._mode_switch_requests: list[ModeSwitchRequest] = []
        self._active_turn_handle = None

    def start(self) -> None:
        self._worker.start()

    def stats(self) -> tuple[bool, int]:
        with self._lock:
            return self._active, self._queued

    def set_ui_invalidator(self, invalidator) -> None:
        self._ui_invalidator = invalidator

    def set_prompt_interrupter(self, interrupter) -> None:
        self._prompt_interrupter = interrupter

    def enqueue(self, query: str) -> tuple[bool, int]:
        return self._enqueue_task("turn", query)

    def enqueue_compact(self) -> tuple[bool, int]:
        return self._enqueue_task("compact", "/compact")

    def _enqueue_task(self, kind: str, payload: str) -> tuple[bool, int]:
        with self._lock:
            was_active = self._active
            queued_before = self._queued
            query_id = self._next_query_id
            self._next_query_id += 1
            self._queued += 1
            show_queue_preview = was_active or queued_before > 0
            preview = self._summarize_preview(kind, payload)
            if show_queue_preview:
                self._queued_previews.append((query_id, preview))
        self._queue.put(
            QueueTask(
                id=query_id,
                kind=kind,
                payload=payload,
                echo_on_start=show_queue_preview and kind == "turn",
                preview=preview,
            )
        )
        self._invalidate_ui()
        return was_active, queued_before

    def has_inflight_work(self) -> bool:
        active, queued = self.stats()
        return active or queued > 0

    def _notify_request_available(self) -> None:
        self._invalidate_ui()
        if self._prompt_interrupter is not None:
            try:
                self._prompt_interrupter()
            except Exception:
                pass

    def _enqueue_authorization_request(
        self,
        *,
        tool_name: str,
        reason: str,
        argument_summary: str = "",
        execution_mode: str = DEFAULT_EXECUTION_MODE,
        request_id: str | None = None,
    ) -> AuthorizationRequest:
        request = AuthorizationRequest(
            tool_name=tool_name,
            reason=reason,
            argument_summary=argument_summary,
            execution_mode=execution_mode,
            completed=Event(),
            request_id=request_id,
        )
        with self._lock:
            self._authorization_requests.append(request)
        self._notify_request_available()
        return request

    def _enqueue_mode_switch_request(
        self,
        *,
        target_mode: str,
        reason: str = "",
        current_mode: str = DEFAULT_EXECUTION_MODE,
        request_id: str | None = None,
    ) -> ModeSwitchRequest:
        request = ModeSwitchRequest(
            target_mode=target_mode,
            current_mode=current_mode,
            reason=reason,
            completed=Event(),
            request_id=request_id,
        )
        with self._lock:
            self._mode_switch_requests.append(request)
        self._notify_request_available()
        return request

    def request_authorization(
        self,
        *,
        tool_name: str,
        reason: str,
        argument_summary: str = "",
        execution_mode: str = DEFAULT_EXECUTION_MODE,
    ) -> dict[str, str]:
        request = self._enqueue_authorization_request(
            tool_name=tool_name,
            reason=reason,
            argument_summary=argument_summary,
            execution_mode=execution_mode,
        )
        if not request.completed.wait(timeout=300):
            return {"status": "denied", "scope": "deny", "reason": "Authorization request timed out."}
        return request.response or {"status": "denied", "scope": "deny", "reason": "Authorization denied."}

    def drain_authorization_requests(self) -> list[AuthorizationRequest]:
        with self._lock:
            pending = list(self._authorization_requests)
            self._authorization_requests = []
        return pending

    def request_mode_switch(self, *, target_mode: str, reason: str = "", current_mode: str = DEFAULT_EXECUTION_MODE) -> dict[str, str]:
        request = self._enqueue_mode_switch_request(
            target_mode=target_mode,
            reason=reason,
            current_mode=current_mode,
        )
        if not request.completed.wait(timeout=300):
            return {
                "approved": False,
                "active_mode": self._execution_mode,
                "reason": "Mode switch request timed out.",
            }
        return request.response or {"approved": False, "active_mode": self._execution_mode, "reason": "Mode switch denied."}

    def drain_mode_switch_requests(self) -> list[ModeSwitchRequest]:
        with self._lock:
            pending = list(self._mode_switch_requests)
            self._mode_switch_requests = []
        return pending

    def close(self, *, drain: bool) -> int:
        dropped = 0
        if not drain:
            dropped = self._clear_pending()
        self._queue.put(None)
        self._worker.join()
        return dropped

    def request_interrupt(self) -> bool:
        with self._lock:
            if not self._active or self._interrupt_requested:
                return False
            self._interrupt_requested = True
            active_turn_handle = self._active_turn_handle
        if self.service is not None and active_turn_handle is not None:
            if not self.service.interrupt_turn(active_turn_handle.turn_id):
                with self._lock:
                    self._interrupt_requested = False
                return False
        elif self.service is None:
            interrupter = getattr(self.runtime, "interrupt_active_teammates", None)
            if callable(interrupter):
                try:
                    interrupter(reason="lead_interrupt")
                except Exception:
                    pass
        self._set_status("interrupting")
        return True

    def request_loop_injection(self) -> bool:
        with self._lock:
            if not self._active:
                return False
            existing_requests = self._loop_injection_requests
            ready_count = len(self._ready_loop_injections)
        if self._pending_turn_count() <= existing_requests:
            return ready_count > 0 or existing_requests > 0
        with self._lock:
            if not self._active:
                return False
            if self._pending_turn_count() <= self._loop_injection_requests:
                return bool(self._ready_loop_injections) or self._loop_injection_requests > 0
            self._loop_injection_requests += 1
        self._invalidate_ui()
        return True

    def prepare_next_loop_injection(self) -> bool:
        with self._lock:
            if self._loop_injection_requests <= 0:
                return False
        task = self._pop_next_queued_turn_task()
        if task is None:
            return False
        with self._lock:
            if self._loop_injection_requests > 0:
                self._loop_injection_requests -= 1
            self._queued = max(0, self._queued - 1)
            self._queued_previews = [
                (preview_id, preview)
                for preview_id, preview in self._queued_previews
                if preview_id != task.id
            ]
            self._ready_loop_injections.append(task.payload)
            self._ready_loop_injection_previews.append(task.preview)
        self._invalidate_ui()
        return True

    def take_next_loop_injection(self) -> str | None:
        with self._lock:
            if not self._ready_loop_injections:
                return None
            payload = self._ready_loop_injections.pop(0)
            if self._ready_loop_injection_previews:
                self._ready_loop_injection_previews.pop(0)
        if payload:
            print_user_message(payload)
        self._invalidate_ui()
        return payload

    def should_interrupt(self) -> bool:
        with self._lock:
            return self._interrupt_requested

    def _clear_pending(self) -> int:
        dropped = 0
        dropped_ids: set[int] = set()
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                break
            if item is None:
                self._queue.put(None)
                break
            dropped_ids.add(item.id)
            dropped += 1
            self._queue.task_done()
        with self._lock:
            ready_dropped = len(self._ready_loop_injections)
            if dropped:
                self._queued = max(0, self._queued - dropped)
                self._queued_previews = [
                    (preview_id, preview)
                    for preview_id, preview in self._queued_previews
                    if preview_id not in dropped_ids
                ]
            if self._ready_loop_injections:
                self._ready_loop_injections = []
                self._ready_loop_injection_previews = []
            self._loop_injection_requests = 0
        if dropped or ready_dropped:
            dropped += ready_dropped
            self._invalidate_ui()
        return dropped

    def _pending_turn_count(self) -> int:
        with self._queue.mutex:
            return sum(
                1
                for item in list(self._queue.queue)
                if isinstance(item, QueueTask) and item.kind == "turn"
            )

    def _pop_next_queued_turn_task(self) -> QueueTask | None:
        with self._queue.mutex:
            queue_items = self._queue.queue
            for item in list(queue_items):
                if not isinstance(item, QueueTask) or item.kind != "turn":
                    continue
                queue_items.remove(item)
                if self._queue.unfinished_tasks > 0:
                    self._queue.unfinished_tasks -= 1
                    if self._queue.unfinished_tasks == 0:
                        self._queue.all_tasks_done.notify_all()
                self._queue.not_full.notify()
                return item
        return None

    def _run_runtime_task(self, task: QueueTask):
        if task.echo_on_start:
            print_user_message(task.payload)
        streamer = ConsoleStreamer(
            start_on_new_line=True,
            line_buffered=self.stable_prompt,
            on_first_output=None,
        )
        run_turn = getattr(self.runtime, "run_turn")
        turn_kwargs = {
            "text_callback": streamer,
            "should_interrupt": self.should_interrupt,
        }
        try:
            run_turn_parameters = inspect.signature(run_turn).parameters
        except (TypeError, ValueError):
            run_turn_parameters = {}
        accepts_var_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in run_turn_parameters.values()
        )
        if "take_next_loop_user_message" in run_turn_parameters or accepts_var_kwargs:
            turn_kwargs["take_next_loop_user_message"] = self.take_next_loop_injection
        if "prepare_next_loop_user_message" in run_turn_parameters or accepts_var_kwargs:
            turn_kwargs["prepare_next_loop_user_message"] = self.prepare_next_loop_injection
        response = run_turn(self.session, task.payload, **turn_kwargs)
        response_status = str(getattr(response, "status", "")).strip()
        if streamer.has_output:
            streamer.finish()
            print()
            if response_status in {"stopped_with_open_todos", "stopped_after_max_rounds"} and response:
                print(
                    _prefix_first_line(
                        render_markdown_text(response, ansi=sys.stdout.isatty()),
                        _assistant_prefix(ansi=sys.stdout.isatty()),
                    )
                )
                print()
        elif response:
            print()
            print(
                _prefix_first_line(
                    render_markdown_text(response, ansi=sys.stdout.isatty()),
                    _assistant_prefix(ansi=sys.stdout.isatty()),
                )
            )
            print()
        self.runtime.print_last_turn_file_summary(self.session)
        return response

    def _run_service_task(self, task: QueueTask):
        if self.service is None:
            raise RuntimeError("App service is not available.")
        if task.echo_on_start:
            print_user_message(task.payload)
        streamer = ConsoleStreamer(
            start_on_new_line=True,
            line_buffered=self.stable_prompt,
            on_first_output=None,
        )
        run_turn = getattr(self.service, "run_turn")
        turn_kwargs = {}
        try:
            run_turn_parameters = inspect.signature(run_turn).parameters
        except (TypeError, ValueError):
            run_turn_parameters = {}
        accepts_var_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in run_turn_parameters.values()
        )
        if "take_next_loop_user_message" in run_turn_parameters or accepts_var_kwargs:
            turn_kwargs["take_next_loop_user_message"] = self.take_next_loop_injection
        if "prepare_next_loop_user_message" in run_turn_parameters or accepts_var_kwargs:
            turn_kwargs["prepare_next_loop_user_message"] = self.prepare_next_loop_injection
        handle = run_turn(self.session, task.payload, **turn_kwargs)
        with self._lock:
            self._active_turn_handle = handle
        if self.should_interrupt():
            try:
                self.service.interrupt_turn(handle.turn_id)
            except Exception:
                pass

        while True:
            batch = handle.drain_events(block=not handle.is_done(), timeout=0.05)
            if batch:
                for event in batch:
                    self._process_service_event(event, streamer)
                continue
            if handle.is_done():
                trailing = handle.drain_events()
                if trailing:
                    for event in trailing:
                        self._process_service_event(event, streamer)
                    continue
                break

        response = handle.result
        response_status = str(getattr(response, "status", "")).strip()
        if response_status == "interrupted" or bool(getattr(response, "interrupted", False)):
            print()
            print("[interrupted]")
            print()
            return response
        if response_status == "failed":
            message = str(getattr(response, "error", "")).strip() or "unknown error"
            print(f"[turn failed] {message}")
            print()
            return response
        if streamer.has_output:
            streamer.finish()
            print()
            if response_status in {"stopped_with_open_todos", "stopped_after_max_rounds"} and response:
                print(
                    _prefix_first_line(
                        render_markdown_text(response.text, ansi=sys.stdout.isatty()),
                        _assistant_prefix(ansi=sys.stdout.isatty()),
                    )
                )
                print()
        elif response and getattr(response, "text", ""):
            print()
            print(
                _prefix_first_line(
                    render_markdown_text(response.text, ansi=sys.stdout.isatty()),
                    _assistant_prefix(ansi=sys.stdout.isatty()),
                )
            )
            print()
        self.runtime.print_last_turn_file_summary(self.session)
        return response

    def _process_service_event(self, event, streamer: ConsoleStreamer) -> None:
        event_type = str(getattr(event, "type", "")).strip()
        payload = getattr(event, "payload", {}) or {}
        if event_type == ASSISTANT_DELTA:
            streamer(str(payload.get("delta", "")))
            return
        if event_type == TOOL_FINISHED:
            self._print_service_tool_event(payload)
            return
        if event_type == AUTHORIZATION_REQUESTED:
            self._enqueue_authorization_request(
                tool_name=str(payload.get("tool_name", "")).strip(),
                reason=str(payload.get("reason", "")).strip(),
                argument_summary=str(payload.get("argument_summary", "")).strip(),
                execution_mode=normalize_execution_mode(payload.get("execution_mode", DEFAULT_EXECUTION_MODE)),
                request_id=str(payload.get("request_id", "")).strip() or None,
            )
            return
        if event_type == MODE_SWITCH_REQUESTED:
            self._enqueue_mode_switch_request(
                target_mode=normalize_execution_mode(payload.get("target_mode", DEFAULT_EXECUTION_MODE)),
                reason=str(payload.get("reason", "")).strip(),
                current_mode=normalize_execution_mode(payload.get("current_mode", DEFAULT_EXECUTION_MODE)),
                request_id=str(payload.get("request_id", "")).strip() or None,
            )
            return
        if event_type in {TODO_UPDATED, SESSION_UPDATED}:
            self._invalidate_ui()

    def _print_service_tool_event(self, payload: dict[str, object]) -> None:
        tool_name = str(payload.get("tool_name", "")).strip()
        actor = str(payload.get("actor", "")).strip() or "lead"
        if tool_name == "TodoWrite" or actor != "lead" or not sys.stdout.isatty():
            return
        rendered_lines = payload.get("rendered_lines")
        if not isinstance(rendered_lines, list) or not rendered_lines:
            return
        print()
        for line in rendered_lines:
            print(str(line))
        print()

    def _worker_loop(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                self._queue.task_done()
                return
            with self._lock:
                self._queued = max(0, self._queued - 1)
                self._active = True
                self._thinking_phrase = random.choice(self.THINKING_PHRASES)
                self._interrupt_requested = False
                self._queued_previews = [
                    (preview_id, preview)
                    for preview_id, preview in self._queued_previews
                    if preview_id != task.id
                ]
                self._active_turn_handle = None
            self._set_status("compacting" if task.kind == "compact" else "thinking")
            response = None
            try:
                if task.kind == "compact":
                    self.runtime.compact_session(self.session)
                    print("[manual compact complete]")
                    print()
                else:
                    if self.service is not None:
                        response = self._run_service_task(task)
                    else:
                        response = self._run_runtime_task(task)
            except TurnInterrupted:
                print()
                print("[interrupted]")
                print()
            except Exception as exc:
                print(f"[turn failed] {exc}")
                print()
            finally:
                with self._lock:
                    self._active = False
                    self._interrupt_requested = False
                    self._loop_injection_requests = 0
                    self._ready_loop_injections = []
                    self._ready_loop_injection_previews = []
                    self._active_turn_handle = None
                self._set_status(self._status_for_response(response))
                self._queue.task_done()

    def _set_status(self, status: str) -> None:
        with self._lock:
            self._status = status
            self._status_changed_at = time.monotonic()
        self._invalidate_ui()

    def prompt_message(self):
        prompt_line = list(styled_prompt_message())
        mode_line = self._execution_mode_fragments()
        status_line = self._status_line()
        context_line = self.current_context_label()
        governance_line = self.current_context_governance_label()
        todo_lines = self._todo_lines()
        team_lines = self._team_lines()
        queue_notice = self._queue_notice()
        queue_lines = self._queue_preview_lines()
        fragments = []
        panel_prefix = ("fg:#64748b", "│ ")
        if self.stable_prompt and status_line:
            style = "fg:#9fb8ab" if status_line == self.DONE_TEXT else "fg:#eab308"
            fragments.extend([panel_prefix, (style, status_line), ("", "\n")])
        if self.stable_prompt:
            for style, line in todo_lines:
                fragments.extend([panel_prefix, (style, line), ("", "\n")])
            for style, line in team_lines:
                fragments.extend([panel_prefix, (style, line), ("", "\n")])
            if context_line:
                fragments.extend([panel_prefix, (self.current_context_style(), context_line), ("", "\n")])
            if governance_line:
                fragments.extend([panel_prefix, ("fg:#67e8f9", governance_line), ("", "\n")])
            if queue_notice:
                fragments.extend([panel_prefix, ("fg:#94a3b8", queue_notice), ("", "\n")])
            if queue_lines:
                for index, queue_line in enumerate(queue_lines, start=1):
                    fragments.extend([panel_prefix, ("fg:#cbd5e1", f"{index}. {queue_line}"), ("", "\n")])
        fragments.append(panel_prefix)
        fragments.extend([*mode_line, ("", "\n")])
        fragments.extend([("fg:#64748b", PROMPT_BORDER), ("", "\n")])
        fragments.extend(prompt_line)
        return fragments

    def current_model_label(self) -> str:
        settings = getattr(self.runtime, "settings", None)
        provider = getattr(settings, "provider", None)
        if provider is None:
            return "model: unknown"
        provider_name = getattr(provider, "name", "unknown")
        model_name = getattr(provider, "model", "unknown")
        reasoning_level = normalize_reasoning_level(getattr(provider, "reasoning_level", None)) or "auto"
        model_name = f"{model_name}|{reasoning_level}"
        return f"model: {provider_name} / {model_name}"

    def _format_token_count(self, token_count: int) -> str:
        if token_count >= 1_000_000:
            return f"{token_count / 1_000_000:.2f}M"
        if token_count >= 1_000:
            return f"{token_count / 1_000:.1f}k"
        return str(token_count)

    def current_context_usage(self) -> ContextWindowUsage | None:
        usage_getter = getattr(self.runtime, "recent_context_window_usage", None)
        if not callable(usage_getter):
            usage_getter = getattr(self.runtime, "context_window_usage", None)
        if not callable(usage_getter):
            return None
        try:
            return usage_getter(self.session)
        except Exception:
            return None

    def current_context_label(self) -> str:
        usage = self.current_context_usage()
        if usage is None:
            return ""
        if usage.max_tokens:
            percent = usage.usage_percent or 0.0
            return (
                f"ctx: {percent:.1f}% "
                f"({self._format_token_count(usage.used_tokens)} / {self._format_token_count(usage.max_tokens)} tokens)"
            )
        return f"ctx: {self._format_token_count(usage.used_tokens)} tokens"

    def current_context_style(self) -> str:
        usage = self.current_context_usage()
        percent = usage.usage_percent if usage is not None else None
        if percent is None:
            return "fg:#7dd3fc"
        if percent <= 30.0:
            return self.CONTEXT_HEALTHY_STYLE
        if percent <= 60.0:
            return self.CONTEXT_WARNING_STYLE
        if percent <= 80.0:
            return self.CONTEXT_REDUCING_STYLE
        return self.CONTEXT_CRITICAL_STYLE

    def current_token_sum_label(self) -> str:
        usage = getattr(self.session, "token_usage", None)
        if not isinstance(usage, dict):
            return ""
        total_tokens = int(usage.get("total_tokens") or 0)
        if total_tokens <= 0:
            return ""
        return f"sum: {self._format_token_count(total_tokens)}"

    def current_context_governance_label(self) -> str:
        label_getter = getattr(self.runtime, "recent_context_governance_label", None)
        if not callable(label_getter):
            return ""
        try:
            return str(label_getter(self.session) or "").strip()
        except Exception:
            return ""

    def current_status_label(self) -> str:
        context_label = self.current_context_label()
        token_sum_label = self.current_token_sum_label()
        governance_label = self.current_context_governance_label()
        parts = [self.current_model_label()]
        if context_label:
            parts.append(context_label)
        if governance_label:
            parts.append(governance_label)
        if token_sum_label:
            parts.append(token_sum_label)
        return " | ".join(parts)

    def bottom_toolbar(self):
        context_label = self.current_context_label()
        token_sum_label = self.current_token_sum_label()
        governance_label = self.current_context_governance_label()
        fragments = [("fg:#94a3b8", self.current_model_label())]
        if context_label:
            fragments.extend([("fg:#64748b", " | "), (self.current_context_style(), context_label)])
        if governance_label:
            fragments.extend([("fg:#64748b", " | "), ("fg:#67e8f9", governance_label)])
        if token_sum_label:
            fragments.extend([("fg:#64748b", " | "), ("fg:#7dd3fc", token_sum_label)])
        return fragments

    def current_execution_mode(self):
        return execution_mode_spec(self._execution_mode)

    def execution_mode_label(self) -> str:
        return execution_mode_status_text(self._execution_mode)

    def execution_mode_ansi_label(self) -> str:
        spec = self.current_execution_mode()
        return f"{spec.ansi_color}{self.execution_mode_label()}\x1b[0m"

    def cycle_execution_mode(self):
        self._execution_mode = next_execution_mode(self._execution_mode)
        setattr(self.runtime, "execution_mode", self._execution_mode)
        self._invalidate_ui()
        return self.current_execution_mode()

    def set_execution_mode(self, mode: str):
        self._execution_mode = normalize_execution_mode(mode)
        setattr(self.runtime, "execution_mode", self._execution_mode)
        self._invalidate_ui()
        return self.current_execution_mode()

    def _status_line(self) -> str:
        with self._lock:
            status = self._status
            changed_at = self._status_changed_at
            thinking_phrase = self._thinking_phrase
        if status == "thinking":
            dots = int((time.monotonic() - changed_at) / self.THINKING_FRAME_SECONDS) % 4
            return thinking_phrase + ("." * dots)
        if status == "compacting":
            dots = int((time.monotonic() - changed_at) / self.THINKING_FRAME_SECONDS) % 4
            return "compacting context" + ("." * dots)
        if status == "interrupting":
            return "interrupting"
        if status == "done":
            return self.DONE_TEXT
        if status == "waiting_on_open_todos":
            return self.OPEN_TODOS_TEXT
        if status == "stopped_with_open_todos":
            return self.STOPPED_WITH_OPEN_TODOS_TEXT
        if status == "stopped_after_max_rounds":
            return self.STOPPED_AFTER_MAX_ROUNDS_TEXT
        return ""

    def _session_has_open_todos(self) -> bool:
        todo_items = list(getattr(self.session, "todo_items", []) or [])
        return any(str(item.get("status", "pending")).lower() not in TODO_CLOSED_STATUSES for item in todo_items)

    def _status_for_response(self, response) -> str:
        status = str(getattr(response, "status", "")).strip()
        if status == "stopped_with_open_todos":
            return "stopped_with_open_todos"
        if status == "stopped_after_max_rounds":
            return "stopped_after_max_rounds"
        if self._session_has_open_todos():
            return "waiting_on_open_todos"
        return "done"

    def _queue_preview_lines(self) -> list[str]:
        with self._lock:
            ready = [f"[next] {preview}" for preview in self._ready_loop_injection_previews]
            queued = [preview for _, preview in self._queued_previews]
        return [*ready, *queued]

    def _queue_notice(self) -> str:
        with self._lock:
            if self._ready_loop_injections or self._loop_injection_requests > 0:
                return self.QUEUED_MESSAGES_ARMED_NOTICE
            if self._queued_previews:
                return self.QUEUED_MESSAGES_NOTICE
        return ""

    def _execution_mode_fragments(self):
        spec = self.current_execution_mode()
        return [
            (spec.color, spec.title),
            ("fg:#64748b", "  (Shift+Tab to cycle)"),
        ]

    def _todo_lines(self) -> list[tuple[str, str]]:
        todo_items = [
            item
            for item in list(getattr(self.session, "todo_items", []) or [])
            if str(item.get("status", "pending")).lower() in TODO_VISIBLE_STATUSES
        ]
        if not todo_items:
            return []
        if not any(str(item.get("status", "pending")).lower() not in TODO_CLOSED_STATUSES for item in todo_items):
            return []

        completed = sum(1 for item in todo_items if item.get("status") == "completed")
        lines: list[tuple[str, str]] = [("fg:#5eead4", f"todo ({completed}/{len(todo_items)} completed)")]
        styles = {
            "pending": "fg:#cbd5e1",
            "in_progress": "fg:#fbbf24",
            "completed": "fg:#64748b",
            "cancelled": "fg:#64748b",
        }
        for item in todo_items:
            status = str(item.get("status", "pending")).lower()
            marker = TODO_STATUS_MARKERS.get(status, "•")
            style = styles.get(status, "fg:#cbd5e1")
            text = str(item.get("content", "")).strip()
            if not text:
                continue
            if status == "in_progress":
                active_form = str(item.get("activeForm", "")).strip()
                suffix = f" <- {active_form}" if active_form else ""
            else:
                suffix = ""
            lines.append((style, f"{marker} {text}{suffix}"))
        return lines

    def _team_lines(self) -> list[tuple[str, str]]:
        manager = getattr(self.runtime, "team_manager", None)
        summaries = getattr(manager, "active_member_summaries", None)
        formatter = getattr(manager, "_format_member_summary", None)
        if not callable(summaries) or not callable(formatter):
            return []
        members = summaries()
        if not members:
            return []
        lines: list[tuple[str, str]] = [("fg:#c4b5fd", f"team ({len(members)} active)")]
        for member in members:
            status = str(member.get("status", "")).strip()
            if status == "working":
                style = "fg:#fbbf24"
            elif status == "idle":
                style = "fg:#93c5fd"
            else:
                style = "fg:#cbd5e1"
            lines.append((style, formatter(member)))
        return lines

    def _summarize_preview(self, kind: str, payload: str) -> str:
        if kind == "compact":
            return "/compact"
        single_line = " ".join(payload.split())
        if len(single_line) <= 48:
            return single_line
        return single_line[:45] + "..."

    def _invalidate_ui(self) -> None:
        if self._ui_invalidator is not None:
            try:
                self._ui_invalidator()
            except Exception:
                pass


def _is_read_only_command(command: str) -> bool:
    return any(command == prefix or command.startswith(f"{prefix} ") for prefix in READ_ONLY_COMMAND_PREFIXES)


def _ensure_accept_edits_for_command(runner: TurnQueueRunner, command_name: str, reason: str) -> bool:
    current_mode = runner.current_execution_mode()
    if current_mode.key in {"accept_edits", "yolo"}:
        return True
    selection = choose_mode_switch_interactively(
        execution_mode_spec("accept_edits").title,
        current_mode.title,
        reason,
    )
    if selection == "switch":
        runner.set_execution_mode("accept_edits")
        return True
    print(f"[blocked in {current_mode.title}: {command_name} requires {execution_mode_spec('accept_edits').title}]")
    return False


def _is_exit_command(command: str) -> bool:
    stripped = command.strip()
    return stripped in {"q", "exit", "/exit"}


def _handle_scan_command(runtime, session, command: str) -> None:
    args = command.split()[1:]
    if args and args[0] == "--refresh":
        args = args[1:]
    target_path = " ".join(args).strip() or "."
    output = runtime.invoke_tool(
        session,
        "project_scan",
        {
            "path": target_path,
            "depth": 2,
            "limit": 8,
        },
    )
    print(output)


def _handle_symbols_command(runtime, session, command: str) -> None:
    query = command.split(maxsplit=1)[1].strip() if " " in command else ""
    if not query:
        query = (
            prompt_text_interactively(
                "Find Symbols",
                "Enter one or more symbol substrings. Use `|` to search alternatives in one pass, up to 10 terms.",
            )
            or ""
        ).strip()
    if not query:
        print("[symbol search cancelled]")
        return
    output = runtime.invoke_tool(
        session,
        "find_symbol",
        {
            "query": query,
            "path": ".",
            "limit": 50,
        },
    )
    matches = runtime.parse_symbol_output(output)
    if not matches:
        print(output)
        return
    items = [
        (
            str(index),
            f"{match['name']} | {match['kind']} | {match['path']}:{match['line']}",
        )
        for index, match in enumerate(matches, start=1)
    ]
    selection = choose_item_interactively(
        "Symbols",
        f"Found {len(matches)} match(es) for '{query}'. Choose one to preview the source location.",
        items,
    )
    if not selection:
        print(output)
        return
    match = matches[int(selection) - 1]
    print(runtime.render_symbol_preview(match["path"], int(match["line"])))

def _handle_model_command(runtime) -> None:
    profiles = runtime.configured_provider_profiles()
    if not profiles:
        print("[no configured providers]")
        return
    provider_items = [
        (
            name,
            f"{name} | default={profile.default_model} | models={len(profile.models)}",
        )
        for name, profile in sorted(profiles.items())
    ]
    selected_provider = choose_item_interactively("Choose Provider", "Select the provider to use for subsequent turns.", provider_items)
    if not selected_provider:
        print("[model selection cancelled]")
        return
    profile = profiles[selected_provider]
    model_items = [
        (
            model,
            f"{model}{' (default)' if model == profile.default_model else ''}",
        )
        for model in profile.models
    ]
    selected_model = choose_item_interactively(
        "Choose Model",
        f"Select a configured model under provider '{selected_provider}'.",
        model_items,
    )
    if not selected_model:
        print("[model selection cancelled]")
        return
    print(runtime.switch_provider_model(selected_provider, selected_model))


def _handle_reasoning_command(runtime, command: str) -> None:
    parts = command.strip().split(maxsplit=1)
    if len(parts) > 1:
        raw_level = parts[1].strip()
        if raw_level.lower() in {"auto", "none"}:
            print(runtime.set_reasoning_level(None))
            return
        selected_level = normalize_reasoning_level(raw_level)
        if selected_level is None:
            print("[usage: /reasoning <auto|low|medium|high|deep>]")
            return
        print(runtime.set_reasoning_level(selected_level))
        return

    descriptions = {
        "auto": "auto | provider default reasoning behavior",
        "low": "low | fastest, lightest reasoning",
        "medium": "medium | balanced default reasoning",
        "high": "high | slower, more deliberate reasoning",
        "deep": "deep | heaviest reasoning budget",
    }
    current_level = normalize_reasoning_level(getattr(runtime.settings.provider, "reasoning_level", None))
    current_option = current_level or "auto"
    ordered_levels = ["auto", *REASONING_LEVEL_VALUES]
    if current_option in ordered_levels:
        ordered_levels.remove(current_option)
        ordered_levels.insert(0, current_option)
    items = [
        (
            level,
            f"{descriptions[level]}{' (current)' if level == current_option else ''}",
        )
        for level in ordered_levels
    ]
    selected_level = choose_item_interactively(
        "Choose Reasoning",
        "Select the reasoning level to use for subsequent turns, or choose auto to restore the unset state.",
        items,
    )
    if not selected_level:
        print("[reasoning selection cancelled]")
        return
    print(runtime.set_reasoning_level(None if selected_level == "auto" else selected_level))


def _handle_providers_command(runtime) -> None:
    profiles = runtime.configured_provider_profiles()
    selected = choose_provider_target_interactively(profiles)
    if not selected:
        return
    previous_provider_name = None if selected == "__add__" else selected
    submission = collect_provider_profile_interactively(
        profiles,
        previous_provider_name=previous_provider_name,
    )
    if submission is None:
        return

    config_path = persist_provider_profile(
        submission.provider_name,
        submission.provider_type,
        submission.models,
        api_key=submission.api_key,
        base_url=submission.base_url,
        previous_provider_name=submission.previous_provider_name,
    )
    current_provider_name = runtime.settings.provider.name
    current_model = runtime.settings.provider.model
    if submission.previous_provider_name == current_provider_name:
        current_provider_name = submission.provider_name
        if current_model not in submission.models:
            current_model = submission.models[0]
    runtime.reload_provider_configuration(provider_name=current_provider_name, model=current_model)

    if submission.previous_provider_name and submission.previous_provider_name != submission.provider_name:
        print(f"Renamed provider '{submission.previous_provider_name}' to '{submission.provider_name}' in {config_path}.")
    elif submission.previous_provider_name:
        print(f"Updated provider '{submission.provider_name}' in {config_path}.")
    else:
        print(f"Added provider '{submission.provider_name}' in {config_path}.")


def _handle_mcp_command(runtime) -> None:
    registry = getattr(runtime, "mcp_registry", None)
    if registry is None or not callable(getattr(registry, "server_summaries", None)):
        print(runtime.mcp_status())
        return
    summaries = registry.server_summaries()
    if not summaries:
        print("No MCP servers configured.")
        return

    while True:
        items = []
        for summary in summaries:
            status = summary["status"]
            suffix = f"tools={summary['tool_count']}" if status == "connected" else (summary["error"] or status)
            items.append(
                (
                    summary["name"],
                    f"{summary['name']} | {status} | {summary['transport']} | {suffix}",
                )
            )
        selected_server = choose_item_interactively(
            "MCP Servers",
            "Choose an MCP server to inspect its registered tools.",
            items,
        )
        if not selected_server:
            return

        server_summary = next((item for item in summaries if item["name"] == selected_server), None)
        if server_summary is None:
            return
        while True:
            tool_summaries = registry.tool_summaries(selected_server)
            subtitle_lines = [
                f"Server: {selected_server}",
                f"Status: {server_summary['status']}",
                f"Transport: {server_summary['transport']}",
                f"Target: {server_summary['target']}",
            ]
            if server_summary["error"]:
                subtitle_lines.append(f"Error: {server_summary['error']}")
            subtitle_lines.append("Choose a tool to inspect, or go back.")
            tool_items = [("__back__", "Back to MCP servers")]
            tool_items.extend(
                (
                    tool["name"],
                    f"{tool['name']} | {tool['description'] or '(no description)'}",
                )
                for tool in tool_summaries
            )
            selected_tool = choose_item_interactively(
                "MCP Tools",
                "\n".join(subtitle_lines),
                tool_items,
            )
            if not selected_tool or selected_tool == "__back__":
                break

            tool_summary = next((item for item in tool_summaries if item["name"] == selected_tool), None)
            if tool_summary is None:
                continue
            choose_item_interactively(
                "MCP Tool Details",
                (
                    f"Server: {selected_server}\n"
                    f"Tool: {tool_summary['name']}\n"
                    f"Description: {tool_summary['description'] or '(no description)'}\n"
                    f"Input schema:\n{json.dumps(tool_summary['input_schema'], ensure_ascii=False, indent=2)}"
                ),
                [("__back__", "Back to tools list")],
            )


def _hook_identity_label(hook) -> str:
    command = str(getattr(hook, "command", "")).strip() or "(no command)"
    args = [str(arg) for arg in getattr(hook, "args", []) or []]
    joined = " ".join([command, *args]).strip()
    return joined[:80] + "..." if len(joined) > 83 else joined


def _handle_hooks_command(runtime) -> None:
    getter = getattr(runtime, "configured_hooks", None)
    hooks = list(getter() if callable(getter) else getattr(getattr(runtime, "settings", None), "hooks", []) or [])
    hooks_by_event: dict[str, list[object]] = {}
    for hook in hooks:
        event = normalize_hook_event(str(getattr(hook, "event", "")).strip())
        hooks_by_event.setdefault(event, []).append(hook)

    while True:
        event_items = []
        for event in HOOK_EVENT_ORDER:
            event_hooks = hooks_by_event.get(event, [])
            enabled_count = sum(1 for hook in event_hooks if bool(getattr(hook, "enabled", True)))
            event_items.append((event, f"{event} | hooks={len(event_hooks)} | enabled={enabled_count}"))
        selected_event = choose_item_interactively(
            "Hooks",
            "Choose an event to inspect its configured hooks.",
            event_items,
        )
        if not selected_event:
            return

        while True:
            event_hooks = hooks_by_event.get(selected_event, [])
            hook_items = [("__back__", "Back to events")]
            hook_items.extend(
                (
                    str(index),
                    (
                        f"{'on' if bool(getattr(hook, 'enabled', True)) else 'off'}"
                        f" | {'builtin' if getattr(hook, 'managed_by', None) == BUILTIN_NOTIFY_MANAGER else 'custom'}"
                        f" | {_hook_identity_label(hook)}"
                    ),
                )
                for index, hook in enumerate(event_hooks, start=1)
            )
            selected_hook = choose_item_interactively(
                "Event Hooks",
                f"Event: {selected_event}\nChoose a hook to inspect or toggle.",
                hook_items,
            )
            if not selected_hook or selected_hook == "__back__":
                break

            hook = event_hooks[int(selected_hook) - 1]
            enabled = bool(getattr(hook, "enabled", True))
            builtin = getattr(hook, "managed_by", None) == BUILTIN_NOTIFY_MANAGER
            scope = str(getattr(hook, "config_scope", "")).strip() or "unknown"
            detail_selection = choose_item_interactively(
                "Hook Details",
                (
                    f"Event: {selected_event}\n"
                    f"Enabled: {'yes' if enabled else 'no'}\n"
                    f"Builtin: {'yes' if builtin else 'no'}\n"
                    f"Scope: {scope}\n"
                    f"Command: {_hook_identity_label(hook)}"
                ),
                [
                    ("toggle", "Disable hook" if enabled else "Enable hook"),
                    ("back", "Back to event hooks"),
                ],
            )
            if detail_selection != "toggle":
                continue
            setter = getattr(runtime, "set_hook_enabled", None)
            if not callable(setter):
                print("Hook toggling is unavailable in this session.")
                return
            print(setter(hook, not enabled))
            getter = getattr(runtime, "configured_hooks", None)
            hooks = list(getter() if callable(getter) else getattr(getattr(runtime, "settings", None), "hooks", []) or [])
            hooks_by_event = {}
            for refreshed_hook in hooks:
                event = normalize_hook_event(str(getattr(refreshed_hook, "event", "")).strip())
                hooks_by_event.setdefault(event, []).append(refreshed_hook)
            continue


def _handle_undo_command(runtime, session) -> None:
    undo_stack = list(getattr(session, "undo_stack", []) or [])
    if not undo_stack:
        print("Nothing to undo.")
        return
    selection = choose_item_interactively(
        "Confirm Undo",
        "Undo the most recent file change set?",
        [
            ("cancel", "Cancel (default)"),
            ("confirm", "Confirm undo"),
        ],
    )
    if selection != "confirm":
        return
    print(runtime.undo_last_turn(session))


def _handle_checkpoint_command(runtime, session, command: str) -> None:
    args = command.split()
    tag = " ".join(args[1:]).strip() if len(args) > 1 else ""
    result = runtime.checkpoint_session(session, tag)
    tag_display = result["tag"]
    msg_count = result["message_count"]
    file_count = result["file_count"]
    print(f"[checkpoint] tag={tag_display} messages={msg_count} tracked_files={file_count}")


def _handle_rollback_command(runtime, session, command: str) -> None:
    args = command.split()
    explicit_tag = " ".join(args[1:]).strip() if len(args) > 1 else ""

    checkpoints = runtime.list_checkpoints(session)
    if not checkpoints:
        print("No checkpoints available. Use /checkpoint <tag> to create one.")
        return

    if explicit_tag:
        matching = [cp for cp in checkpoints if cp["tag"] == explicit_tag]
        if not matching:
            print(f"[rollback] Checkpoint '{explicit_tag}' not found.")
            available_labels = []
            for cp in checkpoints:
                preview = cp.get("last_user_message", "").replace("\n", " ").strip()
                if preview and len(preview) > 40:
                    preview = preview[:37] + "..."
                if preview:
                    available_labels.append(f"  {cp['tag']} ({preview})")
                else:
                    available_labels.append(f"  {cp['tag']}")
            print(f"Available:\n" + "\n".join(available_labels))
            return
        selected_tag = explicit_tag
    else:
        from datetime import datetime as _dt
        items = []
        for cp in checkpoints:
            ts = cp.get("timestamp", 0)
            time_label = "unknown time"
            if ts:
                try:
                    time_label = _dt.fromtimestamp(ts).astimezone().strftime("%H:%M:%S")
                except (OSError, OverflowError, ValueError):
                    pass
            preview = cp.get("last_user_message", "")
            if preview:
                # Truncate long messages for display
                preview = preview.replace("\n", " ").strip()
                if len(preview) > 60:
                    preview = preview[:57] + "..."
                preview_label = f" | {preview}"
            else:
                preview_label = ""
            label = f"{cp['tag']} | {time_label} | {cp['message_count']} msgs | {cp.get('file_count', 0)} files{preview_label}"
            items.append((cp["tag"], label))
        items.append(("cancel", "Cancel (default)"))
        selected_tag = choose_item_interactively(
            "Rollback to Checkpoint",
            "Choose a checkpoint to roll back to. This will revert messages and file changes.",
            items,
        )
        if not selected_tag or selected_tag == "cancel":
            print("[rollback cancelled]")
            return

    # Pre-check for external modifications
    ext_mods = runtime.detect_external_modifications(session, selected_tag)
    skip_ext = False
    if ext_mods:
        print(f"\n[warning] {len(ext_mods)} file(s) were modified externally since the checkpoint:")
        for em in ext_mods:
            print(f"  - {em['path']} ({em['reason']}: {em['detail']})")
        print()
        choice = choose_item_interactively(
            "External Modifications Detected",
            "These files were changed outside the agent. How do you want to proceed?",
            [
                ("skip", "Skip these files (revert only agent-verified files + messages)"),
                ("overwrite", "Overwrite them (revert everything to checkpoint state)"),
                ("cancel", "Cancel rollback (default)"),
            ],
        )
        if not choice or choice == "cancel":
            print("[rollback cancelled]")
            return
        skip_ext = choice == "skip"
        if skip_ext:
            print(f"[rollback] Will skip {len(ext_mods)} externally modified file(s)")

    result = runtime.rollback_session(session, selected_tag, skip_externally_modified=skip_ext)
    if result.get("status") == "error":
        print(f"[rollback failed] {result.get('message', 'unknown error')}")
        return
    print(
        f"[rollback complete] tag={result['tag']} "
        f"messages_restored={result['messages_restored']} "
        f"files_reverted={result['files_reverted']} "
        f"files_skipped={result.get('files_skipped', 0)} "
        f"undo_entries_removed={result['undo_entries_removed']}"
    )
    if result.get("orphaned_checkpoints_deleted", 0):
        print(f"[rollback] {result['orphaned_checkpoints_deleted']} later checkpoint(s) deleted")


def _handle_skills_command(runtime) -> str | None:
    entries = list(runtime.skill_loader.list_entries())
    if not entries:
        print("No skills.")
        return None
    items = [
        (
            str(entry["name"]),
            f"{entry['name']} [{entry['scope']}] - {entry['description']}",
        )
        for entry in entries
    ]
    selected = choose_item_interactively(
        "Choose Skill",
        "Select a skill to apply to the next prompt.",
        items,
    )
    if not selected:
        return None
    return f"/+{selected} "


def _resolve_authorization_requests(runner: TurnQueueRunner) -> bool:
    pending = runner.drain_authorization_requests()
    if not pending:
        return False
    for request in pending:
        selection = choose_authorization_interactively(
            request.tool_name,
            request.reason,
            argument_summary=request.argument_summary,
            mode_label=execution_mode_spec(request.execution_mode).title,
        )
        if selection == "workspace":
            request.response = {"status": "approved", "scope": "workspace", "reason": "Allowed in this workspace."}
        elif selection == "once":
            request.response = {"status": "approved", "scope": "once", "reason": "Allowed once."}
        else:
            request.response = {"status": "denied", "scope": "deny", "reason": "Not allowed."}
        if request.request_id and runner.service is not None:
            runner.service.resolve_authorization(
                request.request_id,
                scope=request.response["scope"],
                approved=request.response["status"] == "approved",
                reason=request.response["reason"],
            )
        request.completed.set()
    return True


def _resolve_mode_switch_requests(runner: TurnQueueRunner) -> bool:
    pending = runner.drain_mode_switch_requests()
    if not pending:
        return False
    for request in pending:
        selection = choose_mode_switch_interactively(
            execution_mode_spec(request.target_mode).title,
            execution_mode_spec(request.current_mode).title,
            request.reason,
        )
        if selection == "switch":
            active_mode = runner.set_execution_mode(request.target_mode).key
            request.response = {
                "approved": True,
                "active_mode": active_mode,
                "reason": f"Switched to {execution_mode_spec(active_mode).title}.",
            }
        else:
            request.response = {
                "approved": False,
                "active_mode": runner.current_execution_mode().key,
                "reason": "Stayed in the current mode.",
            }
        if request.request_id and runner.service is not None:
            runner.service.resolve_mode_switch(
                request.request_id,
                approved=bool(request.response["approved"]),
                active_mode=str(request.response["active_mode"]),
                reason=str(request.response["reason"]),
            )
        request.completed.set()
    return True


def run_repl(runtime, session, resumed: bool = False, service: AppService | None = None) -> int:
    runner = TurnQueueRunner(runtime, session, stable_prompt=False, service=service)
    if service is None:
        runtime.authorization_request_handler = runner.request_authorization
        runtime.mode_switch_request_handler = runner.request_mode_switch
    prompt_session = None
    pending_query_prefix = ""
    try:
        prompt_session = create_prompt_session(
            runtime.settings.workspace_root,
            on_interrupt=runner.request_interrupt,
            on_busy_escape=runner.request_loop_injection,
            is_busy=runner.has_inflight_work,
            on_cycle_mode=runner.cycle_execution_mode,
            skill_names_getter=lambda: runtime.skill_loader.names(),
            clipboard_image_command_getter=lambda: _clipboard_image_command(runtime),
        )
    except Exception:
        prompt_session = None
    runner.stable_prompt = prompt_session is not None and patch_stdout is not None
    if prompt_session is not None:
        runner.set_ui_invalidator(lambda: prompt_session.app.invalidate() if prompt_session.app else None)
        runner.set_prompt_interrupter(
            lambda: prompt_session.app.exit(result=AUTHORIZATION_PROMPT_SENTINEL) if prompt_session.app else None
        )
    runner.start()
    print(f"[session {session.id}]")
    if sys.platform.startswith("win") and os.environ.get("TERM_PROGRAM") == "vscode":
        print("[paste hint] VS Code terminal may intercept Ctrl+V. Use Alt+V, Shift+Insert, or /paste-image.")
    if resumed:
        _print_resumed_history(session, runtime)
    prompt_context = patch_stdout(raw=True) if prompt_session is not None and patch_stdout is not None else nullcontext()
    try:
        with prompt_context:
            while True:
                if _resolve_mode_switch_requests(runner):
                    continue
                if _resolve_authorization_requests(runner):
                    continue
                try:
                    if prompt_session is not None:
                        prompt_kwargs = {
                            "refresh_interval": 0.1,
                            "bottom_toolbar": runner.bottom_toolbar,
                        }
                        if pending_query_prefix:
                            prompt_kwargs["default"] = pending_query_prefix
                        query = prompt_session.prompt(
                            runner.prompt_message,
                            **prompt_kwargs,
                        )
                    else:
                        prefix = pending_query_prefix
                        if sys.stdout.isatty():
                            query = input(
                                f"{runner.execution_mode_ansi_label()}\n"
                                f"{runner.current_status_label()}\n"
                                f"{fallback_prompt_message()}{prefix}"
                            )
                        else:
                            query = input(
                                f"{runner.execution_mode_label()}\n"
                                f"{runner.current_status_label()}\n"
                                f"{PROMPT_TEXT}{prefix}"
                            )
                        if prefix:
                            query = prefix + query
                    pending_query_prefix = ""
                except (EOFError, KeyboardInterrupt):
                    print()
                    active, queued = runner.stats()
                    if active:
                        if runner.request_interrupt():
                            print("[interrupt requested]")
                        continue
                    if queued:
                        print(f"[waiting for {queued} queued item(s) before exit]")
                        runner.close(drain=True)
                        break
                    runner.close(drain=True)
                    break
                if query == AUTHORIZATION_PROMPT_SENTINEL:
                    _resolve_mode_switch_requests(runner)
                    _resolve_authorization_requests(runner)
                    continue
                stripped = query.strip()
                if not stripped:
                    continue
                if _is_exit_command(stripped):
                    active, queued = runner.stats()
                    if queued:
                        dropped = runner.close(drain=False)
                        if active:
                            print(f"[exiting after current response; dropped {dropped} queued prompt(s)]")
                        elif dropped:
                            print(f"[dropped {dropped} queued prompt(s)]")
                    else:
                        if active:
                            print("[waiting for current response before exit]")
                        runner.close(drain=True)
                    break
                if stripped == "/compact":
                    was_active, queued_before = runner.enqueue_compact()
                    if (was_active or queued_before) and not runner.stable_prompt:
                        ahead = queued_before + (1 if was_active else 0)
                        print(f"[queued compact; {ahead} item(s) ahead]")
                    continue
                if stripped == "/janitor":
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /janitor]")
                        continue
                    print("[janitor started]")
                    print(runtime.run_semantic_janitor(session))
                    print("[janitor complete]")
                    continue
                if stripped == "/scan" or stripped.startswith("/scan "):
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /scan]")
                        continue
                    _handle_scan_command(runtime, session, stripped)
                    continue
                if stripped == "/symbols" or stripped.startswith("/symbols "):
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /symbols]")
                        continue
                    _handle_symbols_command(runtime, session, stripped)
                    continue
                if stripped == "/image" or stripped.startswith("/image "):
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /image]")
                        continue
                    try:
                        encoded_query = _build_image_query(runtime, stripped)
                    except ValueError as exc:
                        print(str(exc))
                        continue
                    was_active, queued_before = runner.enqueue(encoded_query)
                    if runner.stable_prompt and not was_active and queued_before == 0:
                        print_user_message(query)
                    if not runner.stable_prompt and not was_active and queued_before == 0:
                        print_user_message(query)
                    continue
                if stripped == "/paste-image" or stripped.startswith("/paste-image "):
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /paste-image]")
                        continue
                    clipboard_prompt = stripped[len("/paste-image") :].strip()
                    try:
                        encoded_query = _build_clipboard_image_query(runtime, clipboard_prompt)
                    except ValueError as exc:
                        print(str(exc))
                        continue
                    was_active, queued_before = runner.enqueue(encoded_query)
                    if runner.stable_prompt and not was_active and queued_before == 0:
                        print_user_message(query)
                    if not runner.stable_prompt and not was_active and queued_before == 0:
                        print_user_message(query)
                    continue
                if stripped == "/skills":
                    skill_prefix = _handle_skills_command(runtime)
                    if skill_prefix is not None:
                        pending_query_prefix = skill_prefix
                        continue
                if stripped == "/undo":
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /undo]")
                        continue
                    _handle_undo_command(runtime, session)
                    continue
                if stripped == "/checkpoint" or stripped.startswith("/checkpoint "):
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /checkpoint]")
                        continue
                    if not _ensure_accept_edits_for_command(
                        runner,
                        "/checkpoint",
                        "Saving a checkpoint updates the persisted session state.",
                    ):
                        continue
                    _handle_checkpoint_command(runtime, session, stripped)
                    continue
                if stripped == "/rollback" or stripped.startswith("/rollback "):
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /rollback]")
                        continue
                    if not _ensure_accept_edits_for_command(
                        runner,
                        "/rollback",
                        "Rollback reverts workspace files and restores session state.",
                    ):
                        continue
                    _handle_rollback_command(runtime, session, stripped)
                    continue
                if stripped == "/model":
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /model]")
                        continue
                    _handle_model_command(runtime)
                    continue
                if stripped == "/reasoning" or stripped.startswith("/reasoning "):
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /reasoning]")
                        continue
                    _handle_reasoning_command(runtime, stripped)
                    continue
                if stripped == "/providers":
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /providers]")
                        continue
                    _handle_providers_command(runtime)
                    continue
                if stripped == "/hooks":
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /hooks]")
                        continue
                    _handle_hooks_command(runtime)
                    continue
                if stripped == "/tasks":
                    tasks = runtime.task_store.list_all()
                    if not tasks:
                        print("No tasks.")
                    else:
                        for task in tasks:
                            print(json.dumps(task, ensure_ascii=False, indent=2))
                    continue
                if stripped == "/team":
                    print(runtime.team_manager.list_all())
                    continue
                if stripped == "/teamlog":
                    active = runtime.team_manager.active_member_summaries()
                    if not active:
                        print("No active teammates. Use /team to inspect the full roster.")
                    else:
                        print("Use /teamlog <name>. Active teammates: " + ", ".join(member["name"] for member in active))
                    continue
                if stripped.startswith("/teamlog "):
                    name = stripped.split(maxsplit=1)[1].strip()
                    print(runtime.render_team_log(name))
                    continue
                if stripped == "/inbox":
                    print(json.dumps(runtime.bus.read_inbox("lead"), indent=2, ensure_ascii=False))
                    continue
                if stripped == "/mcp":
                    if runner.has_inflight_work():
                        print("[busy; wait for queued responses before /mcp]")
                        continue
                    _handle_mcp_command(runtime)
                    continue
                if stripped == "/toollog":
                    print(runtime.recent_tool_logs())
                    continue
                if stripped.startswith("/toollog "):
                    log_id = stripped.split(maxsplit=1)[1].strip()
                    print(runtime.render_tool_log(log_id))
                    continue
                if stripped == "/bg":
                    print(runtime.background_manager.check())
                    continue
                if stripped == "/help":
                    print("\n".join(f"{command} - {description}" for command, description in COMMAND_SPECS))
                    continue
                skill_command = _parse_skill_command(query)
                expanded_query = _expand_skill_command(runtime, query)
                if expanded_query.startswith("Error: Unknown skill '"):
                    print(expanded_query)
                    continue
                if stripped.startswith("/") and skill_command is None and not _is_read_only_command(stripped):
                    print(f"[unknown command] {stripped}")
                    continue
                was_active, queued_before = runner.enqueue(expanded_query)
                if runner.stable_prompt and not was_active and queued_before == 0:
                    print_user_message(query)
                if not runner.stable_prompt and not was_active and queued_before == 0:
                    print_user_message(query)
                if (was_active or queued_before) and not runner.stable_prompt:
                    ahead = queued_before + (1 if was_active else 0)
                    print(f"[queued; {ahead} item(s) ahead]")
    finally:
        if service is None:
            runtime.authorization_request_handler = None
            runtime.mode_switch_request_handler = None
    return 0
