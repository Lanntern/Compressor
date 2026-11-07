"""Metadata page: see what files reveal, and remove it without re-encoding."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QCheckBox, QComboBox

from .. import metaclean
from ..engine import process_metadata
from ..plan import Plan
from ..scanner import FileEntry
from ..settings import Settings
from .filejob_page import ROLE_ROW, FileJobPage, option_row
from .settings_panel import BIN_NAME
from .style import tokens
from .widgets import Card

SHOW_CHOICES = [("All files", "all"), ("With location", "gps"), ("With personal info", "personal"),
                ("With any metadata", "any")]
POLICIES = [("Location", "strip_gps"), ("Location and personal info", "strip_private"),
            ("Everything except the date taken", "strip_all_keep_date"), ("Everything", "strip_all")]
OUTPUTS = [(f"Replace originals ({BIN_NAME}, can be undone)", "replace"), ("Save with a new name", "suffix")]


class _Summaries(QThread):
    one = Signal(object, object)   # entry, MetaSummary

    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self.entries = entries

    def run(self) -> None:
        for e in self.entries:
            try:
                self.one.emit(e, metaclean.summarise(e.path))
            except Exception:  # noqa: BLE001 - an unreadable file just shows no summary
                continue


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
        self.reader: _Summaries | None = None
        super().__init__(window, "Metadata", "",
                         "Drop photos or videos to check their metadata",
                         "Location, camera, owner and dates are shown before anything is removed. The image and "
                         "video data are never re-encoded.")
        self.confirm_info = ("Only metadata is removed; the image and video data stay byte-for-byte identical "
                             "(this is verified). You can undo this later.")

    def build_options(self, card: Card) -> None:
        qs = self.qs
        self.policy = QComboBox()
        for label, value in POLICIES:
            self.policy.addItem(label, value)
        self.policy.setCurrentIndex(max(0, self.policy.findData(qs.value("meta/policy", "strip_gps", str))))
        self.policy.setToolTip("Orientation and colour profile are always kept so images look the same.")
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
        for e in self.entries:
            it = self.rows.get(id(e))
            if it is not None:
                it.setHidden(not self._shown(e))
        self.tree.header().viewport().update()
        self._update()
        self.update_summary()

    def refresh_rows(self) -> None:
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

    @staticmethod
    def _why_not(e: FileEntry) -> str:
        if e.image is not None and e.image.motion:
            return "Motion Photo: editing its metadata would detach the video inside"
        return "Can't edit without re-encoding"

    def plan(self, e: FileEntry) -> Plan | None:
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

    def after_scan(self) -> None:
        self.summaries = {}
        self.reader = _Summaries(list(self.entries), self)
        self.reader.one.connect(self._got)
        self.reader.finished.connect(self.update_summary)
        self.reader.start()

    def _got(self, e, summary) -> None:
        self.summaries[id(e)] = summary
        it = self.rows.get(id(e))
        if it is None:
            return
        data = it.data(0, ROLE_ROW)
        vals = self.row_values(e, data[1] if data else None)
        self.tree.blockSignals(True)
        for c, v in enumerate(vals[:-1]):
            it.setText(c, v)
            it.setToolTip(c, v)
        if summary.gps:
            it.setForeground(1, QColor(tokens().warning))
        if data and data[1] is not None:
            if summary.anything:
                it.setText(self.STATUS_COL, "Ready")
            else:
                it.setCheckState(0, Qt.Unchecked)
                it.setText(self.STATUS_COL, "No metadata to remove")
        elif data:
            it.setText(self.STATUS_COL, "Can't edit without re-encoding")
        it.setHidden(not self._shown(e))
        self.tree.blockSignals(False)
        self._update()

    def update_summary(self) -> None:
        located = sum(1 for s in self.summaries.values() if s.gps)
        cams = sum(1 for s in self.summaries.values() if s.camera)
        n = len(self.selected())
        parts = [f"{len(self.entries)} file{'s' if len(self.entries) != 1 else ''}"]
        if self.summaries:
            parts.append(f"{located} with location" if located else "none with location")
            parts.append(f"{cams} with camera details")
        mode = self.show_filter.currentData() if hasattr(self, "show_filter") else "all"
        if mode != "all" and self.entries:
            shown = sum(1 for e in self.entries if self._shown(e))
            parts.append(f"showing {shown}")
            reading = getattr(self, "reader", None) is not None and self.reader.isRunning()
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
