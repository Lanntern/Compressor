"""Metadata discovery with bounded delivery: a fast disk cannot flood Qt's event queue."""

from __future__ import annotations

import queue
import threading
import time

from PySide6.QtCore import QThread

from .. import metaclean
from ..formats import AUDIO, DOCUMENT, JPEG, ImageInfo, has_embedded_video
from ..scanner import scan


class MetadataScan(QThread):
    BATCH_SIZE = 32

    def __init__(self, paths, settings, parent=None):
        super().__init__(parent)
        self.paths, self.settings = list(paths), settings
        self.cancel = threading.Event()
        self.batches: queue.Queue = queue.Queue(maxsize=4)
        self.stage = "Looking for photos and videos…"
        self.checked = 0
        self.error = ""
        self.result = None
        self._pending: list = []
        self._last_send = time.monotonic()

    def _flush(self):
        if not self._pending:
            return
        while not self.cancel.is_set():
            try:
                self.batches.put(self._pending, timeout=0.05)
                self._pending = []
                self._last_send = time.monotonic()
                return
            except queue.Full:
                continue

    def _found(self, entry):
        if self.cancel.is_set() or entry.kind in (AUDIO, DOCUMENT):
            return
        self.stage = f"Reading metadata: {entry.path}"
        # Preserve the Motion Photo safeguard without counting GIF frames, decoding
        # pictures or running video cadence analysis for a metadata-only operation.
        if entry.kind == JPEG and has_embedded_video(entry.path):
            entry.image = ImageInfo(0, 0, motion=True)
        summary = metaclean.summarize(entry.path)
        self.checked += 1
        self._pending.append((entry, summary))
        if len(self._pending) >= self.BATCH_SIZE or time.monotonic() - self._last_send >= 0.1:
            self._flush()

    def run(self):
        from ..log import log

        try:
            self.result = scan(self.paths, self.settings, self._progress, self.cancel,
                               probe=False, picked=True, found=self._found)
        except Exception as exc:  # noqa: BLE001 - preserve partial results and report the failure
            log.exception("Metadata scan failed")
            self.error = str(exc)
        finally:
            self._flush()

    def _progress(self, message, _done, _total):
        self.stage = message
