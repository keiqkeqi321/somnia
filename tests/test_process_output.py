from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.tools.background import BackgroundManager
from open_somnia.tools.process import CommandResult, decode_output, run_command
from open_somnia.tools.shell import run_shell


class _FakeJobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.notifications: list[dict] = []

    def create(self, job_id: str, payload: dict) -> None:
        self.jobs[job_id] = dict(payload)

    def update(self, job_id: str, **changes):
        self.jobs[job_id].update(changes)
        return self.jobs[job_id]

    def get(self, job_id: str):
        return self.jobs.get(job_id)

    def list_all(self):
        return self.jobs

    def notify(self, payload: dict) -> None:
        self.notifications.append(payload)


class ProcessOutputTests(unittest.TestCase):
    def test_decode_output_prefers_utf8_for_chinese_bytes(self) -> None:
        text = "submit git chinese infor"
        self.assertEqual(decode_output(text.encode("utf-8")), text)

    def test_run_command_uses_binary_mode_and_decodes_output(self) -> None:
        with patch("open_somnia.tools.process.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args="git status",
                returncode=0,
                stdout="submit git chinese infor".encode("utf-8"),
                stderr=b"",
            )

            result = run_command("git status", shell=True, cwd=Path.cwd(), timeout=10)

        self.assertEqual(result.stdout, "submit git chinese infor")
        self.assertFalse(mock_run.call_args.kwargs["text"])

    def test_run_command_raises_turn_interrupted_when_stop_requested(self) -> None:
        interrupt_requested = threading.Event()

        def request_interrupt() -> None:
            time.sleep(0.2)
            interrupt_requested.set()

        interrupter = threading.Thread(target=request_interrupt, daemon=True)
        interrupter.start()
        started_at = time.monotonic()

        with self.assertRaises(TurnInterrupted):
            run_command(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                shell=False,
                cwd=Path.cwd(),
                timeout=10,
                stop_checker=interrupt_requested.is_set,
            )

        self.assertLess(time.monotonic() - started_at, 2.0)

    def test_run_shell_returns_unicode_output(self) -> None:
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=Path.cwd(),
                    runtime=SimpleNamespace(command_timeout_seconds=15, max_tool_output_chars=500),
                )
            )
        )

        with patch("open_somnia.tools.shell._is_windows", return_value=False), patch(
            "open_somnia.tools.shell.run_command"
        ) as mock_run:
            mock_run.return_value = CommandResult(
                args="git status",
                returncode=0,
                stdout="submit git chinese infor\n",
                stderr="",
            )

            result = run_shell(ctx, {"command": "git status"})

        self.assertEqual(result, "submit git chinese infor")

    def test_run_shell_translates_common_windows_ls_command(self) -> None:
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=Path.cwd(),
                    runtime=SimpleNamespace(command_timeout_seconds=15, max_tool_output_chars=500),
                )
            )
        )

        with patch("open_somnia.tools.shell._is_windows", return_value=True), patch(
            "open_somnia.tools.shell.run_command"
        ) as mock_run:
            mock_run.return_value = CommandResult(args=[], returncode=0, stdout="ok", stderr="")

            result = run_shell(ctx, {"command": "ls -la"})

        self.assertEqual(result, "ok")
        self.assertEqual(
            mock_run.call_args.args[0],
            ["powershell", "-NoLogo", "-NoProfile", "-Command", "Get-ChildItem -Force"],
        )
        self.assertFalse(mock_run.call_args.kwargs["shell"])

    def test_run_shell_translates_common_windows_find_command(self) -> None:
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=Path.cwd(),
                    runtime=SimpleNamespace(command_timeout_seconds=15, max_tool_output_chars=500),
                )
            )
        )

        with patch("open_somnia.tools.shell._is_windows", return_value=True), patch(
            "open_somnia.tools.shell.run_command"
        ) as mock_run:
            mock_run.return_value = CommandResult(args=[], returncode=0, stdout="ok", stderr="")

            run_shell(ctx, {"command": 'find . -name "*.py" -type f 2>/dev/null | head -20'})

        self.assertEqual(
            mock_run.call_args.args[0],
            [
                "powershell",
                "-NoLogo",
                "-NoProfile",
                "-Command",
                "Get-ChildItem -Recurse -Filter *.py -File | Select-Object -First 20",
            ],
        )

    def test_run_shell_returns_windows_guidance_for_untranslated_unix_command(self) -> None:
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=Path.cwd(),
                    runtime=SimpleNamespace(command_timeout_seconds=15, max_tool_output_chars=500),
                )
            )
        )

        with patch("open_somnia.tools.shell._is_windows", return_value=True), patch(
            "open_somnia.tools.shell.run_command"
        ) as mock_run:
            result = run_shell(ctx, {"command": "grep foo README.md"})

        self.assertIn("Select-String", result)
        mock_run.assert_not_called()

    def test_background_manager_records_unicode_result(self) -> None:
        store = _FakeJobStore()
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = BackgroundManager(store, Path(tmpdir), default_timeout=30, max_output_chars=500)
            store.create("job1", {"id": "job1", "command": "git status", "status": "running", "result": None})

            with patch("open_somnia.tools.background.run_command") as mock_run:
                mock_run.return_value = CommandResult(
                    args="git status",
                    returncode=0,
                    stdout="submit git chinese infor",
                    stderr="",
                )

                manager._execute("job1", "git status", 30)

        self.assertEqual(store.jobs["job1"]["status"], "completed")
        self.assertEqual(store.jobs["job1"]["result"], "submit git chinese infor")


if __name__ == "__main__":
    unittest.main()
