"""Log file and diagnostics.

The log lives in ``<data dir>/logs`` (rotated, a few MB at most) and records runs,
failures and crashes. It never leaves the computer; "Copy diagnostics" puts a
summary on the clipboard for the user to share if they want to.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger("compressor")
_installed = False
_handler: logging.Handler | None = None


def set_enabled(on: bool) -> None:
    """Settings › "Keep a history and log": off, nothing is written to the log file (it names files)."""
    global _handler
    if on and _handler is None:
        try:
            _handler = RotatingFileHandler(log_file(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        except OSError:
            return
        _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s: %(message)s"))
        log.addHandler(_handler)
        log.setLevel(logging.INFO)
    elif not on and _handler is not None:
        log.removeHandler(_handler)
        _handler.close()
        _handler = None


def delete_logs() -> int:
    """Remove the log files (closing the open one first: Windows won't delete a file in use). Returns how many."""
    was_on = _handler is not None
    set_enabled(False)
    removed = 0
    for p in log_dir().glob("compressor.log*"):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    if was_on:
        set_enabled(True)
    return removed


def log_dir() -> Path:
    from .journal import data_dir

    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_file() -> Path:
    return log_dir() / "compressor.log"


def _journal_enabled() -> bool:
    """The saved "Keep a history and log" choice (read before the window exists)."""
    try:
        from PySide6.QtCore import QSettings

        from . import APP_NAME

        path = os.environ.get("COMPRESSOR_SETTINGS_FILE")     # (the same store the app uses)
        qs = QSettings(path, QSettings.IniFormat) if path else QSettings(APP_NAME, APP_NAME)
        return bool(qs.value("history_enabled", True, bool))
    except Exception:  # noqa: BLE001
        return True


def setup() -> None:
    """Start logging to the file and record uncaught exceptions (idempotent)."""
    global _installed
    if _installed:
        return
    _installed = True
    from . import journal

    journal.ENABLED = _journal_enabled()
    set_enabled(journal.ENABLED)
    log.setLevel(logging.INFO)
    log.info("Compressor %s started (%s, Python %s)", _version(), platform.platform(), platform.python_version())

    previous = sys.excepthook

    def excepthook(tp, value, tb):
        log.critical("Uncaught exception", exc_info=(tp, value, tb))
        previous(tp, value, tb)

    sys.excepthook = excepthook

    previous_thread = threading.excepthook

    def thread_hook(args):
        log.critical("Uncaught exception in %s", getattr(args.thread, "name", "thread"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        previous_thread(args)

    threading.excepthook = thread_hook


def _version() -> str:
    from . import __version__

    return __version__


def tail(lines: int = 60) -> list[str]:
    try:
        return log_file().read_text("utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def diagnostics(include_log: bool = True) -> str:
    """A plain-text summary for bug reports: versions, tools, GPU encoders, recent log lines.

    The recent log lines can contain names of files that failed; the user reviews it before sharing.
    """
    from . import index, tools
    from .journal import data_dir

    out = [f"Compressor {_version()}",
           f"OS: {platform.platform()} ({platform.machine()})",
           f"Python: {platform.python_version()}{' (packaged)' if getattr(sys, 'frozen', False) else ''}"]
    try:
        import PySide6
        from PySide6.QtCore import qVersion

        out.append(f"Qt: {qVersion()} (PySide6 {PySide6.__version__})")
    except Exception:  # noqa: BLE001
        pass
    try:
        from PIL import __version__ as pil

        out.append(f"Pillow: {pil}")
    except Exception:  # noqa: BLE001
        pass
    out.append(f"Data folder: {data_dir()}")
    out.append(f"CPU threads: {os.cpu_count()}")
    out.append("")
    out.append("Tools:")
    for name, path in tools.tool_status().items():
        out.append(f"  {name:12} {path or 'missing'}")
    try:
        from .video import available_encoders

        enc = ", ".join(k for k, ok in available_encoders().items() if ok) or "none"
        out.append(f"Video encoders: {enc}")
    except Exception as e:  # noqa: BLE001
        out.append(f"Video encoders: unknown ({e})")
    try:
        from .quality import has_vmaf, image_metric

        out.append(f"Smart quality metrics: {image_metric()} (images), {'VMAF' if has_vmaf() else 'SSIM'} (videos)")
    except Exception:  # noqa: BLE001
        pass
    try:
        n, size = index.stats()
        out.append(f"File index: {n} files, {size // 1024} KB")
    except Exception:  # noqa: BLE001
        pass
    if include_log:
        out += ["", "Recent log:"] + [f"  {line}" for line in tail()]
    return "\n".join(out)


__all__ = ["log", "setup", "log_dir", "log_file", "diagnostics", "tail"]
