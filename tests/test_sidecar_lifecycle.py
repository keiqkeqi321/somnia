from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from desktop.backend.instance_lock import SidecarInstanceLock, SidecarInstanceLockError
from desktop.backend.server import PARENT_WATCHDOG_ENV, SidecarServer
from open_somnia.config.models import (
    AgentSettings,
    AppSettings,
    ModelTraits,
    ProviderProfileSettings,
    ProviderSettings,
    RuntimeSettings,
    StorageSettings,
)


def _stable_test_dir(name: str) -> Path:
    root = Path.cwd() / ".tmp-tests" / f"{name}-{time.time_ns()}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_settings(root: Path) -> AppSettings:
    data_dir = root / ".open_somnia"
    data_dir.mkdir(parents=True, exist_ok=True)
    return AppSettings(
        workspace_root=root,
        agent=AgentSettings(name="Somnia"),
        provider=ProviderSettings(
            name="openai",
            provider_type="openai",
            model="fake-model",
            api_key="fake",
            base_url="http://localhost",
        ),
        runtime=RuntimeSettings(),
        storage=StorageSettings(
            data_dir=data_dir,
            transcripts_dir=data_dir / "transcripts",
            sessions_dir=data_dir / "sessions",
            tasks_dir=data_dir / "tasks",
            inbox_dir=data_dir / "inbox",
            team_dir=data_dir / "team",
            jobs_dir=data_dir / "jobs",
            requests_dir=data_dir / "requests",
            logs_dir=data_dir / "logs",
            state_dir=data_dir / "state",
        ),
        provider_profiles={
            "openai": ProviderProfileSettings(
                name="openai",
                provider_type="openai",
                models=["fake-model"],
                model_traits={"fake-model": ModelTraits(context_window_tokens=64_000)},
                default_model="fake-model",
                api_key="fake",
                base_url="http://localhost",
            ),
        },
    )


def _dead_pid() -> int:
    from open_somnia.pid_liveness import pid_is_alive

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = process.pid
    process.wait()
    if os.name == "nt":
        # Popen keeps the process handle open; while any handle exists the
        # kernel keeps the process object and OpenProcess still succeeds.
        # Handle.Close() also marks it closed so GC does not double-close.
        process._handle.Close()
    # Kernel teardown of the process object is asynchronous; allow a brief
    # window for it to become invisible to OpenProcess/kill(pid, 0).
    deadline = time.monotonic() + 2.0
    while pid_is_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    return pid


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class SidecarInstanceLockTests(unittest.TestCase):
    def test_live_holder_rejects_second_instance(self) -> None:
        root = _stable_test_dir("lock-live")
        first = SidecarInstanceLock(root)
        first.acquire()
        self.addCleanup(first.release)
        second = SidecarInstanceLock(root)
        with self.assertRaises(SidecarInstanceLockError):
            second.acquire()

    def test_stale_lock_is_reclaimed(self) -> None:
        root = _stable_test_dir("lock-stale")
        lock_path = root / ".open_somnia" / "sidecar.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({"pid": _dead_pid(), "token": "stale"}), encoding="utf-8")
        lock = SidecarInstanceLock(root)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertTrue(lock_path.is_file())

    def test_release_removes_lock_file_and_allows_successor(self) -> None:
        root = _stable_test_dir("lock-release")
        first = SidecarInstanceLock(root)
        first.acquire()
        first.release()
        self.assertFalse(first.path.exists())
        second = SidecarInstanceLock(root)
        second.acquire()
        self.addCleanup(second.release)

    def test_release_does_not_remove_foreign_lock(self) -> None:
        root = _stable_test_dir("lock-foreign")
        first = SidecarInstanceLock(root)
        first.acquire()
        self.addCleanup(first.release)
        successor = SidecarInstanceLock(root)
        # Simulate the successor winning the race before the old holder releases.
        first.path.unlink()
        successor.acquire()
        self.addCleanup(successor.release)
        first.release()
        self.assertTrue(successor.path.is_file())


class ParentWatchdogTests(unittest.TestCase):
    def _make_server(self, name: str) -> SidecarServer:
        settings = _make_settings(_stable_test_dir(name))
        server = SidecarServer.from_settings(settings, port=0)
        self.addCleanup(server.close)
        return server

    def test_watchdog_disabled_without_env(self) -> None:
        server = self._make_server("watchdog-disabled")
        server.start_background()
        self.assertTrue(server.wait_until_ready())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PARENT_WATCHDOG_ENV, None)
            self.assertIsNone(server._start_parent_watchdog())

    def test_dead_parent_closes_server(self) -> None:
        server = self._make_server("watchdog-dead-parent")
        server.start_background()
        self.assertTrue(server.wait_until_ready())
        with patch.dict(os.environ, {PARENT_WATCHDOG_ENV: str(_dead_pid())}, clear=False):
            thread = server._start_parent_watchdog(interval_seconds=0.01)
        self.assertIsNotNone(thread)
        self.assertTrue(_wait_until(lambda: server.is_closed))
        thread.join(timeout=2.0)

    def test_alive_parent_keeps_server_running(self) -> None:
        server = self._make_server("watchdog-alive-parent")
        server.start_background()
        self.assertTrue(server.wait_until_ready())
        with patch.dict(os.environ, {PARENT_WATCHDOG_ENV: str(os.getpid())}, clear=False):
            thread = server._start_parent_watchdog(interval_seconds=0.01)
        self.assertIsNotNone(thread)
        self.assertFalse(_wait_until(lambda: server.is_closed, timeout=0.3))
        server.close()
        thread.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
