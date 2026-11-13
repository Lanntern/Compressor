"""Background threads for scanning, compressing, undoing and probing encoders."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..engine import JobEvents, run_job, undo_record
from ..journal import FileRecord
from ..plan import Plan
from ..scanner import FileEntry, scan
from ..settings import Settings


class ScanThread(QThread):
    progress = Signal(str, int, int)
    result = Signal(object)  # ScanResult
    error = Signal(str)

    def __init__(self, paths: list[Path], settings: Settings, parent=None):
        super().__init__(parent)
        self.paths, self.settings = paths, settings
        self.cancel = threading.Event()

    def run(self) -> None:
        from .. import power

        try:
            with power.awake():
                self.result.emit(scan(self.paths, self.settings, self.progress.emit, self.cancel, picked=True))
        except Exception as e:  # noqa: BLE001
            from ..log import log

            log.exception("File scan failed")
            self.error.emit(str(e))


class _Bridge(JobEvents):
    def __init__(self, thread: "JobThread"):
        self.t = thread

    def file_started(self, entry):
        self.t.file_started.emit(entry)

    def file_progress(self, entry, fraction):
        self.t.file_progress.emit(entry, fraction)

    def file_finished(self, result):
        self.t.file_finished.emit(result)

    def file_step(self, entry, message):
        self.t.file_step.emit(entry, message)


class JobThread(QThread):
    file_started = Signal(object)          # FileEntry
    file_progress = Signal(object, float)  # FileEntry, 0..1
    file_finished = Signal(object)         # FileResult
    file_step = Signal(object, str)        # FileEntry, what it's doing ("Encoding JPEG XL")
    done = Signal(object, object)          # list[FileResult], run_id | None
    error = Signal(str)

    def __init__(self, items: list[tuple[FileEntry, Plan]], settings: Settings, root: Path, parent=None,
                 processor=None, tool: str = ""):
        super().__init__(parent)
        self.items, self.settings, self.root = items, settings, root
        self.processor, self.tool = processor, tool
        self.cancel = threading.Event()
        self.pause = threading.Event()      # set: no new file starts (Pause, or on battery)

    def run(self) -> None:
        try:
            results, run_id = run_job(self.items, self.settings, self.root, _Bridge(self), self.cancel,
                                      processor=self.processor, tool=self.tool, pause=self.pause)
            self.done.emit(results, run_id)
        except Exception as e:  # noqa: BLE001
            from ..log import log

            log.exception("%s job failed", self.tool or "Compression")
            self.error.emit(str(e))


class UndoThread(QThread):
    item_done = Signal(object, bool, str)  # FileRecord, ok, message
    done = Signal(int, int)

    def __init__(self, run_id: str, records: list[FileRecord], parent=None):
        super().__init__(parent)
        self.run_id, self.records = run_id, records

    def run(self) -> None:
        ok = failed = 0
        for rec in self.records:
            try:
                success, msg = undo_record(self.run_id, rec)
            except Exception as e:  # noqa: BLE001 - one file's problem mustn't stop the rest (or hang the page)
                from ..log import log

                log.exception("Undo failed: %s", rec.original)
                success, msg = False, str(e)
            ok += success
            failed += not success
            self.item_done.emit(rec, success, msg)
        self.done.emit(ok, failed)


class EncoderProbeThread(QThread):
    result = Signal(dict)

    def run(self) -> None:
        from ..video import available_encoders

        try:
            self.result.emit(available_encoders())
        except Exception:  # noqa: BLE001
            from ..log import log

            log.exception("Encoder detection failed")
            self.result.emit({})
