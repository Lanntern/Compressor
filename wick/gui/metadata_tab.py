"""Metadata page: see what files reveal, and remove it without re-encoding."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QCheckBox, QComboBox

from .. import metaclean
from ..engine import process_metadata
from ..plan import Plan
from ..scanner import FileEntry
from ..settings import Settings
from .filejob_page import FileJobPage, option_row
from .metadata_scan import MetadataScan
from .settings_panel import BIN_NAME
from .style import tokens
from .widgets import Card

SHOW_CHOICES = [("All files", "all"), ("With location", "gps"), ("With personal info", "personal"),
                ("With any metadata", "any")]
POLICIES = [("Location", "strip_gps"), ("Location and personal info", "strip_private"),
            ("Everything except the date taken", "strip_all_keep_date"), ("Everything", "strip_all")]
OUTPUTS = [(f"Replace originals ({BIN_NAME}, can be undone)", "replace"), ("Save with a new name", "suffix")]


class MetadataTab(FileJobPage):
    COLUMNS = ["Name", "Location", "Camera", "Date taken", "Other", "Status"]
    STATUS_COL = 5
    action_label = "Clean"
    tool = "metadata"
    status_words = ("Removing metadata from", "cleaned")
    confirm_title = "Remove metadata from {n} file{s}?"

    def __init__(self, window):
        self.qs = window.qsettings
        self.summaries: dict[int, metaclean.MetaSummary] = {}
        self._loading = False
        self._count = self._eligible = 0
        self._count_dirty = True
        self._located = self._cameras = self._unreadable = self._shown_count = 0
        super().__init__(window, "Metadata", "",
                         "Drop photos or videos to check their metadata",
                         "Location, camera, owner and dates are shown before anything is removed. The image and "
                         "video data are never re-encoded.")
        self.tree.header().set_check(0, self._header_state, self.tree.header()._check[2])
        self._drain_timer = QTimer(self, interval=16)
        self._drain_timer.timeout.connect(self._drain)
        self.confirm_info = ("Only metadata is removed; the image and video data stay byte-for-byte identical "
                             "(this is verified). You can undo this later.")

    def build_options(self, card: Card) -> None:
        qs = self.qs
        self.policy = QComboBox()
        for label, value in POLICIES:
            self.policy.addItem(label, value)
        self.policy.setCurrentIndex(max(0, self.policy.findData(qs.value("meta/policy", "strip_gps", str))))
        self.policy.setToolTip("Orientation and color profile are always kept so images look the same.")
        self.output = QComboBox()
        for label, value in OUTPUTS:
            self.output.addItem(label, value)
        self.output.setCurrentIndex(max(0, self.output.findData(qs.value("meta/output", "replace", str))))
        self.dates = QCheckBox("Keep original file dates")
        self.dates.setChecked(qs.value("meta/dates", True, bool))
        self.show_filter = QComboBox()
        for label, value in SHOW_CHOICES:
            self.show_filter.addItem(label, value)
        self.show_filter.setCurrentIndex(max(0, self.show_filter.findData(qs.value("meta/show", "all", str))))
        self.show_filter.setToolTip("Look through a folder and list only the files that reveal something. "
                                    "Hidden files are never changed.")
        self.show_filter.currentIndexChanged.connect(self._filter_changed)
        card.lay.addLayout(option_row(("Show", self.show_filter), ("Remove", self.policy), ("Save", self.output),
                                      ("", self.dates)))
        self.policy.currentIndexChanged.connect(self._changed)
        self.output.currentIndexChanged.connect(self._changed)
        self.dates.toggled.connect(self._changed)

    def _filter_changed(self, *_) -> None:
        self.qs.setValue("meta/show", self.show_filter.currentData())
        self.apply_filter()

    def _shown(self, e) -> bool:
        mode = self.show_filter.currentData()
        if mode == "all":
            return True
        s = self.summaries.get(id(e))
        if s is None:
            return False   # appears once its metadata has been read
        if mode == "gps":
            return bool(s.gps)
        if mode == "personal":
            return bool(s.gps or s.people or s.camera or s.software)
        return s.anything

    def apply_filter(self) -> None:
        self._shown_count = 0
        for e in self.entries:
            self._shown_count += self._shown(e)
            it = self.rows.get(id(e))
            if it is not None:
                it.setHidden(not self._shown(e))
        self._count_dirty = True
        self.tree.header().viewport().update()
        self._update()

    def refresh_rows(self) -> None:
        self._count_dirty = True
        super().refresh_rows()
        self.apply_filter()

    def _changed(self, *_) -> None:
        self.qs.setValue("meta/policy", self.policy.currentData())
        self.qs.setValue("meta/output", self.output.currentData())
        self.qs.setValue("meta/dates", self.dates.isChecked())

    def settings(self) -> Settings:
        return Settings(min_image_kb=0, min_video_mb=0, output_mode=self.output.currentData(),
                        output_suffix="_clean", preserve_dates=self.dates.isChecked(), metadata="keep")

    def accepts(self, path: Path) -> bool:
        from ..formats import AUDIO

        from ..formats import DOCUMENT

        kind = metaclean.kind_of(path)   # about photos and videos; music tags aren't private data
        return kind not in (AUDIO, DOCUMENT)

    def _why_not(self, e: FileEntry) -> str:
        summary = self.summaries.get(id(e))
        if summary is not None and summary.error:
            return f"Could not read metadata: {summary.error}"
        if summary is not None and not summary.anything:
            return "No metadata to remove"
        if e.image is not None and e.image.motion:
            return "Motion Photo: editing its metadata would detach the video inside"
        return "Can't edit without re-encoding"

    def plan(self, e: FileEntry) -> Plan | None:
        summary = self.summaries.get(id(e))
        if summary is not None and (summary.error or not summary.anything):
            return None
        if metaclean.kind_of(e.path) not in metaclean.EDITABLE or (e.image is not None and e.image.motion):
            return None
        return Plan(True, "metadata", e.path.suffix.lower(), route="metadata")

    def row_values(self, e: FileEntry, p: Plan | None) -> list[str]:
        s = self.summaries.get(id(e))
        if s is None:
            return [e.path.name, "…", "…", "…", "…", "Reading…" if p else self._why_not(e)]
        return [e.path.name, s.gps or "–", s.camera or "–", s.taken or "–",
                ", ".join(s.people + ([s.software] if s.software else [])) or "–",
                "Ready" if p else self._why_not(e)]

    def selection_count(self) -> int:
        if self._count_dirty:
            self._count = len(self.selected())
            self._eligible = sum(bool(row.flags() & Qt.ItemIsUserCheckable) and not row.isHidden()
                                 for row in self.rows.values())
            self._count_dirty = False
        return self._count

    def _header_state(self):
        count = self.selection_count()
        return Qt.Unchecked if not count else Qt.Checked if count == self._eligible else Qt.PartiallyChecked

    def _item_changed(self, *_):
        self._count_dirty = True
        self._update()

    def busy(self) -> bool:
        return self._loading or super().busy()

    def clear(self) -> None:
        if self.busy():
            return
        self.summaries.clear()
        self.rows.clear()
        self._count = self._eligible = 0
        self._count_dirty = False
        self._located = self._cameras = self._unreadable = self._shown_count = 0
        super().clear()

    def scan(self) -> None:
        if self.busy():
            self._rescan = True
            return
        if not self.paths:
            return
        paths = list(self.paths)
        self.clear()
        self.paths = paths
        self._next_progress = 0.0
        settings = self.settings()
        settings.scan_jxl = True
        self.scan_thread = MetadataScan(self.paths, settings, self)
        self._loading = True
        self._sorting = self.tree.isSortingEnabled()
        self.tree.setSortingEnabled(False)
        self.tree.header().setSectionsClickable(False)
        self._busy(True, "Looking for photos and videos…")
        self.placeholder.show_loading("Looking for photos and videos…")
        self.scan_thread.start()
        self._drain_timer.start()

    def _append_rows(self, entries, unticked=()) -> None:
        super()._append_rows(entries, unticked)
        for entry in entries:
            row = self.rows[id(entry)]
            row.setHidden(not self._shown(entry))
            summary = self.summaries.get(id(entry))
            if summary and summary.gps:
                row.setForeground(1, QColor(tokens().warning))
            if summary and summary.error:
                row.setToolTip(self.STATUS_COL, summary.error)

    def _drain(self) -> None:
        import queue
        import time

        worker = cast(MetadataScan, self.scan_thread)
        self.selection_count()  # incorporate any user checkbox changes before appending
        try:
            batch = worker.batches.get_nowait()
        except queue.Empty:
            batch = []
        if batch:
            entries = [entry for entry, _ in batch]
            self.tree.blockSignals(True)
            self.tree.setUpdatesEnabled(False)
            try:
                for entry, summary in batch:
                    self.summaries[id(entry)] = summary
                    self._located += bool(summary.gps)
                    self._cameras += bool(summary.camera)
                    self._unreadable += bool(summary.error)
                    self._shown_count += self._shown(entry)
                self.entries.extend(entries)
                self._append_rows(entries)
                added = sum(self.plan(entry) is not None and self._shown(entry) for entry in entries)
                self._count += added
                self._eligible += added
            finally:
                self.tree.setUpdatesEnabled(True)
                self.tree.blockSignals(False)
            self.placeholder.clear()
            self._update()
        if time.monotonic() >= self._next_progress:
            self._next_progress = time.monotonic() + 0.2
            self.job_status.busy(worker.stage)
            self.job_status.detail.setText(f"{worker.checked:,} files checked · {len(self.entries):,} listed")
            self.job_status.detail.setVisible(True)
            self.job_status.detail.setToolTip(worker.stage)
            if worker.cancel.is_set():
                self.job_status.stopping()
        if worker.isRunning() or not worker.batches.empty():
            return
        self._drain_timer.stop()
        self._loading = False
        self.tree.setSortingEnabled(self._sorting)
        self.tree.header().setSectionsClickable(True)
        self.tree.header().viewport().update()
        self.tree._fitter.fit_soon()
        self._finished()
        if worker.error:
            self.job_status.failed(worker.error)
        else:
            stopped = worker.cancel.is_set()
            self.job_status.busy("Metadata scan stopped" if stopped else "Metadata scan complete", 1, 1)
            details = [f"{len(self.entries):,} files checked"]
            if stopped:
                details.append("Partial results are kept; add the folder again to retry")
            if self._unreadable:
                details.append(f"{self._unreadable:,} unreadable files (see row status and log)")
            result = worker.result
            if result is not None:
                if result.unreadable_dirs:
                    details.append(f"{len(result.unreadable_dirs):,} folders could not be read")
                if result.cloud_only_files:
                    details.append(f"{result.cloud_only_files:,} cloud-only files skipped")
            self.job_status.detail.setText(" · ".join(details))
            self.job_status.detail.setVisible(True)
            self.job_status._set_buttons(close=True, log=bool(self._unreadable))
            self.job_status._taskbar("none")

    def stop(self) -> None:
        super().stop()
        if self._loading:
            self.job_status.stopping()

    def update_summary(self) -> None:
        located = self._located
        cams = self._cameras
        n = self.selection_count()
        parts = [f"{len(self.entries)} file{'s' if len(self.entries) != 1 else ''}"]
        if self.summaries:
            parts.append(f"{located} with location" if located else "none with location")
            parts.append(f"{cams} with camera details")
        mode = self.show_filter.currentData() if hasattr(self, "show_filter") else "all"
        if mode != "all" and self.entries:
            shown = self._shown_count
            parts.append(f"showing {shown}")
            reading = self._loading
            if shown:
                self.placeholder.clear()
            elif reading:
                self.placeholder.show_loading("Reading metadata… files that reveal something appear here")
            else:
                label = self.show_filter.currentText().lower().replace("with ", "")
                self.placeholder.show_message(f"None of these files have {label}.")
        elif self.entries and not self.busy():
            self.placeholder.clear()
        parts.append(f"<b>{n} selected</b>")
        self.summary.setText(" · ".join(parts))

    def processor(self):
        policy = self.policy.currentData()
        return lambda *a: process_metadata(*a, policy=policy)
