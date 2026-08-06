from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from open_somnia.pid_liveness import pid_is_alive


class PidLivenessTests(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(pid_is_alive(os.getpid()))

    def test_exited_process_is_dead_with_handle_still_open(self):
        # Regression: on Windows, Popen keeps the child handle open, so the
        # kernel retains the process object and a bare OpenProcess succeeds
        # even though the process has exited. Liveness must consult the exit
        # code, or per-workspace locks are never reclaimed (real-world victim:
        # desktop/backend/instance_lock.py).
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        pid = process.pid
        process.wait()
        self.assertFalse(pid_is_alive(pid))

    def test_exited_process_is_dead(self):
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
        self.assertFalse(pid_is_alive(pid))

    def test_invalid_pid_is_dead(self):
        self.assertFalse(pid_is_alive(0))
        self.assertFalse(pid_is_alive(-1))


if __name__ == "__main__":
    unittest.main()
