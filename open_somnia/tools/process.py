from __future__ import annotations

import locale
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from open_somnia.runtime.interrupts import TurnInterrupted

# Upper bound for collecting output after a kill; a descendant that escaped
# the kill tree can hold the pipes open, so this must never wait forever.
_KILL_DRAIN_TIMEOUT_SECONDS = 5.0
# Grace window for terminate() before escalating to a forced tree kill.
_TERMINATE_GRACE_SECONDS = 1.0


@dataclass(slots=True)
class CommandResult:
    args: Any
    returncode: int
    stdout: str
    stderr: str

    def combined_output(self) -> str:
        return f"{self.stdout}{self.stderr}"


def drop_windows_extended_prefix(path: Path) -> Path:
    """Strip the Windows extended-length prefix (``\\?\\``) from a path.

    Workspace paths coming from the desktop side (Tauri/Rust canonicalize)
    carry this prefix. PowerShell tolerates it as a working directory, but
    cmd.exe — the interpreter behind ``shell=True`` — rejects it as an
    unsupported UNC path and the command fails before it even runs.
    """
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + text[len("\\\\?\\UNC\\"):])
    if text.startswith("\\\\?\\"):
        return Path(text[len("\\\\?\\"):])
    return path


def _candidate_encodings() -> list[str]:
    encodings = ["utf-8", "utf-8-sig", locale.getpreferredencoding(False)]
    if os.name == "nt":
        encodings.extend(["mbcs", "gb18030"])

    seen: set[str] = set()
    ordered: list[str] = []
    for encoding in encodings:
        normalized = (encoding or "").strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            ordered.append(encoding)
    return ordered


def decode_output(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if not data:
        return ""

    for encoding in _candidate_encodings():
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _kill_process_tree(process: subprocess.Popen) -> None:
    """Kill the child and every descendant, not just the direct child.

    Killing only the shell leaves grandchildren (e.g. a GUI app launched in
    the foreground) alive; on Windows they also keep the output pipes open,
    which deadlocks any subsequent communicate() waiting for EOF. Must be
    called while the child pid is still valid (i.e. before wait() reaps it),
    otherwise taskkill can no longer walk the tree.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
        )
        return
    try:
        import signal

        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()


def _communicate_after_kill(process: subprocess.Popen) -> tuple[bytes, bytes]:
    """Drain output after a kill without ever blocking forever.

    A descendant that escaped the kill tree can hold the pipe write ends
    open; waiting for EOF would hang the whole tool call (and the turn with
    it). After the drain timeout just reap the child and abandon the pipes.
    Do NOT force-close them: on Windows communicate() may have a pending
    overlapped read, and closing the handle mid-read crashes the process.
    """
    try:
        stdout, stderr = process.communicate(timeout=_KILL_DRAIN_TIMEOUT_SECONDS)
        return stdout or b"", stderr or b""
    except subprocess.TimeoutExpired:
        process.wait()
        return b"", b""


def run_command(
    command: str | Sequence[str],
    *,
    shell: bool,
    cwd: Path,
    timeout: int,
    env: Mapping[str, str] | None = None,
    stop_checker: Callable[[], bool] | None = None,
) -> CommandResult:
    popen_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        # Own process group so the kill tree reaches shell grandchildren.
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(
        command,
        shell=shell,
        cwd=drop_windows_extended_prefix(cwd),
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **popen_kwargs,
    )
    deadline = time.monotonic() + timeout
    poll_interval_seconds = 0.1

    while True:
        if stop_checker is not None and stop_checker():
            # Kill the whole tree first, while the child pid is still valid;
            # after wait() reaps it, taskkill can no longer find descendants.
            _kill_process_tree(process)
            try:
                process.wait(timeout=_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
            _communicate_after_kill(process)
            raise TurnInterrupted("Interrupted by user.")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process_tree(process)
            stdout, stderr = _communicate_after_kill(process)
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)

        try:
            stdout, stderr = process.communicate(timeout=min(poll_interval_seconds, remaining))
        except subprocess.TimeoutExpired:
            continue
        return CommandResult(
            args=command,
            returncode=int(process.returncode or 0),
            stdout=decode_output(stdout),
            stderr=decode_output(stderr),
        )
