from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.storage.jobs import JobStore
from open_somnia.tools.background import BackgroundManager
from open_somnia.tools.process import CommandResult, decode_output, drop_windows_extended_prefix, run_command
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

    def fail_running_jobs(self, result: str) -> int:
        changed = 0
        for job in self.jobs.values():
            if job.get("status") == "running":
                job["status"] = "error"
                job["result"] = result
                changed += 1
        return changed

    def list_all(self):
        return self.jobs

    def notify(self, payload: dict) -> None:
        self.notifications.append(payload)


class ProcessOutputTests(unittest.TestCase):
    def _stable_test_dir(self, name: str) -> Path:
        root = Path.cwd() / ".tmp-tests" / f"{name}-{time.time_ns()}"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def test_decode_output_prefers_utf8_for_chinese_bytes(self) -> None:
        text = "submit git chinese infor"
        self.assertEqual(decode_output(text.encode("utf-8")), text)

    def test_run_command_uses_binary_mode_and_decodes_output(self) -> None:
        result = run_command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write('submit git chinese infor'.encode('utf-8'))",
            ],
            shell=False,
            cwd=Path.cwd(),
            timeout=10,
        )

        self.assertEqual(result.stdout, "submit git chinese infor")

    def test_run_command_timeout_kills_the_whole_tree_without_hanging(self) -> None:
        # The direct child spawns a grandchild that inherits the output pipes
        # and outlives every timeout; a naive kill+communicate() would block
        # on the pipes forever. The tree kill plus bounded drain must return.
        command = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
            "stdout=sys.stdout,stderr=sys.stderr);"
            "time.sleep(30)"
        )
        started_at = time.monotonic()

        with self.assertRaises(subprocess.TimeoutExpired):
            run_command(
                [sys.executable, "-c", command],
                shell=False,
                cwd=Path.cwd(),
                timeout=0.75,
            )

        self.assertLess(time.monotonic() - started_at, 15.0)

    def test_run_command_interrupt_kills_the_whole_tree_without_hanging(self) -> None:
        interrupt_requested = threading.Event()
        command = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
            "stdout=sys.stdout,stderr=sys.stderr);"
            "time.sleep(30)"
        )

        def request_interrupt() -> None:
            time.sleep(0.4)
            interrupt_requested.set()

        interrupter = threading.Thread(target=request_interrupt, daemon=True)
        interrupter.start()
        started_at = time.monotonic()

        with self.assertRaises(TurnInterrupted):
            run_command(
                [sys.executable, "-c", command],
                shell=False,
                cwd=Path.cwd(),
                timeout=60,
                stop_checker=interrupt_requested.is_set,
            )

        self.assertLess(time.monotonic() - started_at, 15.0)

    def test_run_command_raises_turn_interrupted_when_stop_requested(self) -> None:
        interrupt_requested = threading.Event()

        def request_interrupt() -> None:
            time.sleep(0.15)
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

    def test_run_shell_allows_ruff_format_subcommand(self) -> None:
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=Path.cwd(),
                    runtime=SimpleNamespace(command_timeout_seconds=15, max_tool_output_chars=500),
                )
            )
        )
        command = (
            "cd D:\\Project\\Git\\LibiCrab; uv run ruff format "
            "backend/libicrab/services/model_multimodal_registry.py 2>&1"
        )

        with patch("open_somnia.tools.shell._is_windows", return_value=True), patch(
            "open_somnia.tools.shell.run_command"
        ) as mock_run:
            mock_run.return_value = CommandResult(args=[], returncode=0, stdout="1 file left unchanged", stderr="")

            result = run_shell(ctx, {"command": command})

        self.assertEqual(result, "1 file left unchanged")
        self.assertEqual(mock_run.call_args.args[0], ["powershell", "-NoLogo", "-NoProfile", "-Command", command])

    def test_run_shell_still_blocks_format_command(self) -> None:
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=Path.cwd(),
                    runtime=SimpleNamespace(command_timeout_seconds=15, max_tool_output_chars=500),
                )
            )
        )

        with patch("open_somnia.tools.shell.run_command") as mock_run:
            result = run_shell(ctx, {"command": "format C:"})

        self.assertEqual(result, "Error: Dangerous command blocked")
        mock_run.assert_not_called()

    def test_background_manager_records_unicode_result(self) -> None:
        store = _FakeJobStore()
        root = self._stable_test_dir("process-output-bg")
        manager = BackgroundManager(store, root, default_timeout=30, max_output_chars=500)
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

    def test_background_manager_marks_zombie_running_jobs_as_error(self) -> None:
        root = self._stable_test_dir("process-output-zombie")
        store = JobStore(root)
        store.create("zombie", {"id": "zombie", "command": "sleep 999", "status": "running", "result": None})
        store.create("done", {"id": "done", "command": "ls", "status": "completed", "result": "ok"})

        BackgroundManager(store, root, default_timeout=30, max_output_chars=500)

        zombie = store.get("zombie")
        self.assertEqual(zombie["status"], "error")
        self.assertIn("interrupted", zombie["result"])
        self.assertEqual(store.get("done")["status"], "completed")

    def test_background_run_blocks_dangerous_command(self) -> None:
        store = _FakeJobStore()
        root = self._stable_test_dir("process-output-bg-danger")
        manager = BackgroundManager(store, root, default_timeout=30, max_output_chars=500)

        with patch("open_somnia.tools.background.threading.Thread") as mock_thread:
            result = manager.run("format C:")

        self.assertEqual(result, "Error: Dangerous command blocked")
        mock_thread.assert_not_called()
        self.assertEqual(store.jobs, {})

    def test_background_execute_translates_unix_commands_on_windows(self) -> None:
        store = _FakeJobStore()
        root = self._stable_test_dir("process-output-bg-win")
        manager = BackgroundManager(store, root, default_timeout=30, max_output_chars=500)
        store.create("job1", {"id": "job1", "command": "ls", "status": "running", "result": None})

        with (
            patch("open_somnia.tools.shell._is_windows", return_value=True),
            patch("open_somnia.tools.background.run_command") as mock_run,
        ):
            mock_run.return_value = CommandResult(args=["powershell"], returncode=0, stdout="ok", stderr="")
            manager._execute("job1", "ls", 30)

        self.assertEqual(
            mock_run.call_args.args[0],
            ["powershell", "-NoLogo", "-NoProfile", "-Command", "Get-ChildItem -Force"],
        )
        self.assertFalse(mock_run.call_args.kwargs["shell"])
        self.assertEqual(store.jobs["job1"]["status"], "completed")

    def test_background_execute_returns_guidance_for_untranslatable_unix_command(self) -> None:
        store = _FakeJobStore()
        root = self._stable_test_dir("process-output-bg-guidance")
        manager = BackgroundManager(store, root, default_timeout=30, max_output_chars=500)
        store.create("job1", {"id": "job1", "command": "grep foo bar", "status": "running", "result": None})

        with (
            patch("open_somnia.tools.shell._is_windows", return_value=True),
            patch("open_somnia.tools.background.run_command") as mock_run,
        ):
            manager._execute("job1", "grep foo bar", 30)

        mock_run.assert_not_called()
        self.assertEqual(store.jobs["job1"]["status"], "error")
        self.assertIn("Select-String", store.jobs["job1"]["result"])

    def test_drop_windows_extended_prefix_strips_drive_prefix(self) -> None:
        self.assertEqual(
            drop_windows_extended_prefix(Path("\\\\?\\D:\\Project\\Git\\somnia")),
            Path("D:\\Project\\Git\\somnia"),
        )

    def test_drop_windows_extended_prefix_strips_unc_variant(self) -> None:
        self.assertEqual(
            drop_windows_extended_prefix(Path("\\\\?\\UNC\\server\\share\\work")),
            Path("\\\\server\\share\\work"),
        )

    def test_drop_windows_extended_prefix_leaves_plain_paths_untouched(self) -> None:
        plain = Path("D:\\Project\\Git\\somnia")
        self.assertEqual(drop_windows_extended_prefix(plain), plain)

    @unittest.skipUnless(os.name == "nt", "cmd.exe cwd handling is Windows-only")
    def test_run_command_strips_extended_prefix_cwd_for_cmd(self) -> None:
        # Regression: the desktop side hands over workspace paths with the
        # \\?\ prefix (Tauri canonicalize). cmd.exe rejects such a cwd as an
        # unsupported UNC path, which silently broke shell=True background
        # jobs; run_command must normalize the cwd before spawning.
        root = Path.cwd().resolve()
        prefixed = Path("\\\\?\\" + str(root))

        result = run_command("cd", shell=True, cwd=prefixed, timeout=10)

        self.assertNotIn("UNC", result.combined_output())
        self.assertIn(str(root), result.stdout)


if __name__ == "__main__":
    unittest.main()
