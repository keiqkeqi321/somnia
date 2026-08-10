"""Shared test-suite safeguards.

The somnia parallel-dispatch pools (``somnia-tool_*`` / ``somnia-subagent_*``
in ``open_somnia/runtime/parallel_dispatch.py``) are process-global and never
shut down. Their workers are non-daemon threads, so the interpreter's exit
join (``concurrent.futures``' ``_python_exit`` and ``threading._shutdown``)
hangs the whole suite whenever a test leaves a pool task blocked.

Two mitigations, both test-only:

1. At interpreter exit, shut both pools down without waiting. Idle workers
   pick up the sentinel and exit immediately, so the exit join stays fast in
   the common case.
2. A faulthandler watchdog dumps every thread's stack if the process is still
   alive 150s in — this is how a stuck pool task is supposed to be identified
   instead of guessed at.
"""

from __future__ import annotations

import atexit
import faulthandler


def _shutdown_somnia_pools() -> None:
    try:
        from open_somnia.runtime import parallel_dispatch
    except Exception:
        return
    for shared in (parallel_dispatch._POOL, parallel_dispatch._SUBAGENT_POOL):
        pool = getattr(shared, "_pool", None)
        if pool is None:
            continue
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


atexit.register(_shutdown_somnia_pools)
faulthandler.dump_traceback_later(150, repeat=True)
