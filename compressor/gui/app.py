"""GUI entry point (single instance; accepts paths from Explorer's menu)."""

from __future__ import annotations

from typing import cast

import os
import sys
import tempfile
import time


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    background, preset, convert_to, paths = parse_args(argv)

    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication

    from .. import APP_NAME
    from . import integration

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    # One window per user: later launches hand their paths over and exit.
    lock = QLockFile(os.path.join(tempfile.gettempdir(), f"{APP_NAME.lower()}-{_user()}.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        for _ in range(30):
            if integration.send_to_running_instance(paths, show=not background, preset=preset, convert=convert_to):
                return 0
            time.sleep(0.1)
        if not lock.tryLock(1000):
            return 1  # another instance holds the lock but doesn't answer

    from ..log import setup as setup_log
    from .main_window import MainWindow, app_icon, open_qsettings
    from .theme import apply_theme

    setup_log()
    _report_unexpected_errors()
    app.setWindowIcon(app_icon())
    apply_theme(cast(str, open_qsettings().value("theme", "system", str)))
    server = integration.InstanceServer()
    win = MainWindow()
    server.paths_received.connect(win.receive_paths)
    if not background:
        win.show()
    probe = os.environ.get("COMPRESSOR_STARTUP_PROBE")
    if probe:  # build checks: write a file once the window is up, then quit (used to time the start-up)
        from PySide6.QtCore import QTimer

        def done():
            with open(probe, "w", encoding="utf-8") as f:
                f.write("shown")
            app.quit()
        QTimer.singleShot(0, done)
    if paths:
        win.receive_paths(paths, not background, preset, convert_to)
    code = app.exec()
    lock.unlock()
    return code


def _report_unexpected_errors() -> None:
    """A mistake in a button's handler mustn't look like a button that does nothing: say so (once at a time),
    with the error, and where the log is. (The log already records it.)"""
    import sys
    import threading

    from PySide6.QtCore import QTimer

    previous = sys.excepthook
    showing = False

    def hook(tp, value, tb):
        previous(tp, value, tb)
        if threading.current_thread() is not threading.main_thread() or showing or issubclass(tp, KeyboardInterrupt):
            return

        def show():
            nonlocal showing
            from ..log import log_file
            from .dialogs import warn

            showing = True
            try:
                warn(None, "Something went wrong", f"{tp.__name__}: {value}\n\nThe details are in the log ({log_file()}); "
                     "Settings › Copy diagnostics includes them for a bug report.")
            finally:
                showing = False
        QTimer.singleShot(0, show)

    sys.excepthook = hook


def parse_args(argv: list[str]) -> tuple[bool, str, str, list[str]]:
    """(--background, --preset NAME, --convert FORMAT, absolute paths) from the GUI command line.

    --convert (Explorer's "Convert with Compressor" menu): a Convert format id such as jpeg or mp4_h264, or
    "choose" to open Convert with the files and let you pick."""
    background, preset, convert_to, paths = False, "", "", []
    it = iter(argv)
    for a in it:
        if a == "--background":
            background = True
        elif a in ("--preset", "--convert"):
            value = next(it, "")
            if a == "--preset":
                preset = value
            else:
                convert_to = value
        elif a.startswith(("--preset=", "--convert=")):
            key, value = a.split("=", 1)
            if key == "--preset":
                preset = value
            else:
                convert_to = value
        elif not a.startswith("--"):
            paths.append(os.path.abspath(a))
    return background, preset, convert_to, paths


def _user() -> str:
    try:
        import getpass

        return "".join(c for c in getpass.getuser() if c.isalnum()) or "user"
    except Exception:  # noqa: BLE001
        return "user"


if __name__ == "__main__":
    sys.exit(main())
