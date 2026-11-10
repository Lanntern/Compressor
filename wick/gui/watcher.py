"""Watch folders and compress new files automatically."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..formats import TEMP_PREFIX, classify

POLL_SECONDS = 3.0
STABLE_POLLS = 2   # a file must look the same this many polls in a row (still being copied otherwise)


class FolderWatcher(QObject):
    """Polls folders in a background thread; emits files that appeared and stopped changing.

    Polling (instead of OS notifications) works the same on every drive type,
    including network shares and USB sticks.
    """

    files_ready = Signal(list)   # list[str]
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.folders: list[Path] = []
        self.recursive = True
        self._seen: dict[str, tuple[int, int]] = {}
        self._pending: dict[str, tuple[int, int, int]] = {}
        self._ignored: dict[str, tuple[int, int]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- control
    def start(self, folders: list[Path], recursive: bool) -> None:
        self.stop()
        self.folders, self.recursive = list(folders), recursive
        self._stop.clear()
        self._seen = self._snapshot()  # only files that appear from now on are processed
        self._pending.clear()
        self._thread = threading.Thread(target=self._loop, name="wick-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def ignore(self, path: Path) -> None:
        """Don't treat this file (e.g. our own output) as new while it stays the same."""
        try:
            st = path.stat()
        except OSError:
            return
        with self._lock:
            self._ignored[os.path.normcase(str(path))] = (st.st_size, st.st_mtime_ns)

    # ---- internals
    def _iter_files(self):
        for root in self.folders:
            stack = [root]
            while stack:
                d = stack.pop()
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            if e.name.startswith(".") or e.name.startswith(TEMP_PREFIX):
                                continue
                            try:
                                if e.is_dir(follow_symlinks=False):
                                    if self.recursive:
                                        stack.append(Path(e.path))
                                elif e.is_file(follow_symlinks=False) and classify(Path(e.name)):
                                    st = e.stat(follow_symlinks=False)
                                    yield os.path.normcase(e.path), e.path, (st.st_size, st.st_mtime_ns)
                            except OSError:
                                continue
                except OSError:
                    continue

    def _snapshot(self) -> dict[str, tuple[int, int]]:
        return {key: sig for key, _, sig in self._iter_files()}

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                ready = self.poll_once()
            except Exception as e:  # noqa: BLE001
                self.error.emit(str(e))
                continue
            if ready:
                self.files_ready.emit(ready)

    def poll_once(self) -> list[str]:
        ready: list[str] = []
        current: dict[str, tuple[int, int]] = {}
        for key, path, sig in self._iter_files():
            current[key] = sig
            with self._lock:
                if self._ignored.get(key) == sig:
                    continue
            if self._seen.get(key) == sig:
                continue
            prev = self._pending.get(key)
            if prev and (prev[0], prev[1]) == sig:
                count = prev[2] + 1
                if count >= STABLE_POLLS and _readable(path):
                    ready.append(path)
                    self._seen[key] = sig
                    self._pending.pop(key, None)
                else:
                    self._pending[key] = (sig[0], sig[1], count)
            else:
                self._pending[key] = (sig[0], sig[1], 0)
        # forget deleted files
        for key in list(self._seen):
            if key not in current:
                self._seen.pop(key, None)
        for key in list(self._pending):
            if key not in current:
                self._pending.pop(key, None)
        return ready


def _readable(path: str) -> bool:
    """False while another program still has the file open for writing (Windows)."""
    try:
        if os.name == "nt" and os.access(path, os.W_OK):
            # Opening for append fails with a sharing violation while the writer holds it.
            with open(path, "ab"):
                pass
        else:
            with open(path, "rb"):
                pass
        return True
    except OSError:
        return False


def wait_until(predicate, timeout: float) -> bool:  # small helper for tests
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False
