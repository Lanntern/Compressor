"""A shared pool of helper processes for GIL-holding encoders (see _workers.py).

Started on first use, sized to the CPU, at low priority like the other encoders. If helper processes
can't be started (locked-down PC, antivirus) the work simply runs in the calling thread instead.
"""

from __future__ import annotations

import atexit
import os
import threading

_lock = threading.Lock()
_pool = None
_broken = False


def _get():
    global _pool
    with _lock:
        if _pool is None and not _broken:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor

            from . import _workers, tools

            _pool = ProcessPoolExecutor(max_workers=max(1, min(os.cpu_count() or 2, 16)),
                                        mp_context=multiprocessing.get_context("spawn"),
                                        initializer=_workers.set_low_priority, initargs=(tools.LOW_PRIORITY,))
            atexit.register(shutdown)
        return _pool


def call(fn, *args):
    """Run ``fn(*args)`` in a helper process (fn must live in _workers); in this thread if that fails."""
    global _broken
    pool = None if os.environ.get("WICK_NO_PROCESSES") else _get()
    if pool is not None:
        try:
            return pool.submit(fn, *args).result()
        except Exception as e:  # noqa: BLE001
            from concurrent.futures.process import BrokenProcessPool

            if not isinstance(e, (BrokenProcessPool, OSError, PermissionError, EOFError)):
                raise  # the encoder itself failed: report it like any other failure
            from .log import log

            log.warning("Helper processes unavailable (%s); working in-process", e)
            _broken = True
            shutdown()
    return fn(*args)


def shutdown() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
            _pool = None
