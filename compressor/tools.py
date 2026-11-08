"""Locating and running the external command-line encoders.

Search order for each tool:
1. ``$COMPRESSOR_TOOLS_DIR``
2. ``<app dir>/tools`` (next to the .exe when packaged, repo root in dev)
3. the bundled PyInstaller data dir
4. ``PATH``
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from functools import lru_cache
from pathlib import Path

TOOL_NAMES = ("ffmpeg", "ffprobe", "cjxl", "djxl", "cjpegli", "gifsicle", "ssimulacra2")
# Optional: cjpegli (better lossy JPEG; else Pillow + mozjpeg), ssimulacra2 (Smart quality; else SSIM).
OPTIONAL_TOOLS = ("cjpegli", "ssimulacra2")

# Set by the engine from Settings.low_priority.
LOW_PRIORITY = False
_BELOW_NORMAL = 0x00004000  # Windows BELOW_NORMAL_PRIORITY_CLASS

# Hide the console window that would otherwise flash for every subprocess
# when running as a GUI app on Windows.
_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _popen_kwargs() -> dict:
    flags = _CREATIONFLAGS
    kw: dict = {}
    if LOW_PRIORITY:
        if os.name == "nt":
            flags |= _BELOW_NORMAL
        else:
            kw["preexec_fn"] = _nice
    kw["creationflags"] = flags
    return kw


def _nice() -> None:  # pragma: no cover - runs in the child
    try:
        os.nice(10)
    except OSError:
        pass


@lru_cache(maxsize=1)
def total_memory_gb() -> float:
    """Installed memory in GB (0 if unknown)."""
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MemoryStatus()
            st.dwLength = ctypes.sizeof(st)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))  # type: ignore[attr-defined]
            return st.ullTotalPhys / 1024 ** 3
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (OSError, ValueError, AttributeError):
        return 0.0


def lower_thread_priority() -> None:
    """Make the calling thread (in-process encoders) run at below-normal priority."""
    try:
        if os.name == "nt":
            import ctypes

            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k32.SetThreadPriority(k32.GetCurrentThread(), -1)  # THREAD_PRIORITY_BELOW_NORMAL
        elif hasattr(os, "setpriority"):
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except Exception:  # noqa: BLE001
        pass


class ToolMissing(RuntimeError):
    def __init__(self, name: str):
        super().__init__(
            f"'{name}' was not found. Put it in the 'tools' folder next to "
            f"Compressor or on your PATH (see README)."
        )
        self.name = name


class ToolError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int, stderr: str):
        tool = Path(str(args[0])).stem
        detail = (stderr or "").strip().splitlines()
        tail = " | ".join(detail[-3:]) if detail else "no error output"
        super().__init__(f"{tool} failed (exit {returncode}): {tail}")
        self.returncode = returncode
        self.stderr = stderr


class Cancelled(Exception):
    """Raised when the user cancels while a tool is running."""


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _candidate_dirs() -> Iterator[Path]:
    env = os.environ.get("COMPRESSOR_TOOLS_DIR")
    if env:
        yield Path(env)
    yield app_dir() / "tools"
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        yield Path(meipass) / "tools"
    if os.name == "nt":                    # tools downloaded when first needed (addons.py)
        from .journal import data_dir

        yield data_dir() / "tools"


@lru_cache(maxsize=None)
def find_tool(name: str) -> str | None:
    exe = name + (".exe" if os.name == "nt" else "")
    for d in _candidate_dirs():
        p = d / exe
        if p.is_file():
            return str(p)
    return shutil.which(name)


def require(name: str) -> str:
    path = find_tool(name)
    if not path:
        raise ToolMissing(name)
    return path


def tool_status() -> dict[str, str | None]:
    return {name: find_tool(name) for name in TOOL_NAMES}


def run(
    args: Sequence[str | os.PathLike],
    *,
    cwd: str | os.PathLike | None = None,
    timeout: float | None = None,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [str(a) for a in args],
        cwd=cwd,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        **_popen_kwargs(),
    )
    if check and proc.returncode != 0:
        stderr = proc.stderr if text else proc.stderr.decode("utf-8", "replace")
        raise ToolError([str(a) for a in args], proc.returncode, stderr)
    return proc


def run_ffmpeg_progress(
    args: Sequence[str | os.PathLike],
    duration: float | None,
    on_progress: Callable[[float], None] | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """Run ffmpeg with ``-progress pipe:1`` already in ``args``.

    Calls ``on_progress(fraction)`` and kills ffmpeg if ``cancel`` is set.
    """
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(
            [str(a) for a in args],
            stdout=subprocess.PIPE,
            stderr=errf,
            stdin=subprocess.DEVNULL,
            **_popen_kwargs(),
        )
        cancelled = False
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    proc.kill()
                    break
                line = raw.decode("ascii", "replace").strip()
                if on_progress and duration and line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    on_progress(max(0.0, min(1.0, us / 1e6 / duration)))
        except BaseException:
            proc.kill()     # nobody reads its output any more: waiting below would hang once the pipe is full
            raise
        finally:
            proc.wait()
        if cancelled:
            raise Cancelled()
        if proc.returncode != 0:
            errf.seek(0)
            raise ToolError(
                [str(a) for a in args], proc.returncode, errf.read().decode("utf-8", "replace")
            )
