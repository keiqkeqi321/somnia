"""Cross-platform process liveness checks."""

from __future__ import annotations

import os


def pid_is_alive(pid: int) -> bool:
    """Return True when a process with ``pid`` currently exists.

    Windows uses ``OpenProcess``; POSIX uses ``kill(pid, 0)``. A permission
    error means the process exists but belongs to another user, which still
    counts as alive.
    """
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
