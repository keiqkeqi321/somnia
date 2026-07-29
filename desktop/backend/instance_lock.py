"""Per-workspace single-instance lock for sidecar processes.

A sidecar writes an O_EXCL lock file at ``<workspace>/.open_somnia/sidecar.lock``
on startup. A second sidecar for the same workspace is rejected while the lock
holder is alive; a stale lock (holder process is gone, e.g. after a force-kill)
is reclaimed automatically. This is the last line of defense against duplicate
sidecars regardless of who spawned them (Tauri, dev scripts, supervisors).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

from open_somnia.pid_liveness import pid_is_alive


class SidecarInstanceLockError(RuntimeError):
    """Another live sidecar already serves this workspace."""


class SidecarInstanceLock:
    def __init__(self, workspace_root: Path) -> None:
        self.path = Path(workspace_root) / ".open_somnia" / "sidecar.lock"
        self.token = uuid.uuid4().hex
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if self._holder_is_alive():
                    raise SidecarInstanceLockError(
                        f"Another sidecar (pid {self._holder_pid()}) already serves workspace '{self.path.parent.parent}'."
                    )
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "token": self.token, "started_at": time.time()}, handle)
            self._acquired = True
            return
        raise SidecarInstanceLockError(f"Unable to acquire the sidecar instance lock at '{self.path}'.")

    def _holder_pid(self) -> int:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return int(payload.get("pid", 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0

    def _holder_is_alive(self) -> bool:
        return pid_is_alive(self._holder_pid())

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
