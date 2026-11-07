"""PyInstaller entry point."""

import os
import sys

from compressor.cli import main

def _run() -> int:
    try:
        return main() or 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    except BaseException:  # noqa: BLE001 - log it; never fall into a hanging teardown
        import traceback

        try:
            from compressor.journal import data_dir

            (data_dir() / "last-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()   # helper processes (see compressor/procpool.py) start here and stop
    # No arguments (or file paths) open the window; "scan"/"run"/... use the command line.
    code = _run()
    # Exit immediately: native libraries' worker threads can deadlock while they are
    # unloaded on Windows. All files and the history journal
    # are already flushed and fsynced at this point.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    if os.name == "nt":
        # Even os._exit runs DLL detach routines on Windows; one of the native
        # codec libraries deadlocks there. TerminateProcess skips them.
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32")
        # Declare the types: without them the pseudo-handle (-1) is truncated to
        # 32 bits on x64 and TerminateProcess silently fails.
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.TerminateProcess(k32.GetCurrentProcess(), int(code or 0))
    os._exit(code or 0)
