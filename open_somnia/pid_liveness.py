"""Cross-platform process liveness checks."""

from __future__ import annotations

import os


def pid_is_alive(pid: int) -> bool:
    """Return True when a process with ``pid`` is currently running.

    Windows uses ``OpenProcess`` + ``GetExitCodeProcess``: a successful open
    is not proof of life, because the kernel keeps the process object alive
    while any handle references it (e.g. a parent that never closed its child
    handle) — only ``STILL_ACTIVE`` distinguishes a running process from one
    the kernel merely has not forgotten. POSIX uses ``kill(pid, 0)``. A
    permission error means the process exists but belongs to another user,
    which still counts as alive.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                # The process object exists but its state is unreadable; the
                # conservative answer stays "alive" (see the docstring).
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
