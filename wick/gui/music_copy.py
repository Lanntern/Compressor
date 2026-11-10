"""Watch Folders → "Music for your phone": a smaller copy of the music library, kept in sync."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout

from .. import mirror
from ..formats import human
from ..log import log
from .style import Space, Type
from .widgets import Card, ElidedLabel, FlowLayout, button, hbox, text

AUTO_EVERY_MS = 60 * 60 * 1000


class _Sync(QThread):
    progress = Signal(str, int, int)
    done = Signal(object, str)

    def __init__(self, library: Path, dest: Path, fmt: str, quality: str, parent=None):
        super().__init__(parent)
        self.args = (library, dest, fmt, quality)
        self.cancel = threading.Event()

    def run(self) -> None:
        from .. import power

        try:
            with power.awake():
                res = mirror.sync(*self.args, progress=self.progress.emit, cancel=self.cancel)
            self.done.emit(res, "")
        except Exception as e:  # noqa: BLE001
            self.done.emit(None, str(e))


class MusicCopyCard(Card):
    synced = Signal(str)   # a line for the activity log

    def __init__(self, qs, parent=None):
        super().__init__(parent)
        self.qs = qs
        self.worker: _Sync | None = None
        self.lay.addLayout(hbox(text("Music for your phone", Type.HEADLINE, bold=True), None))
        self.lay.addWidget(text("A smaller copy of your music library that stays in sync: lossless songs become "
                                "Opus, AAC or MP3 with their tags and album art; songs that are already MP3 or AAC "
                                "are copied as they are. Only what changed is done again, and your library is "
                                "never changed.", Type.FOOTNOTE, secondary=True, wrap=True))
        self.library_label = ElidedLabel("", Type.BODY, secondary=True)   # paths: shortened, full in the tooltip
        self.dest_label = ElidedLabel("", Type.BODY, secondary=True)
        pick_lib = button("Choose…", "", "folder", "Choose your music library")
        pick_lib.setAccessibleName("Choose music library")
        pick_lib.clicked.connect(lambda: self._pick("library", "Your music library"))
        pick_dest = button("Choose…", "", "folder", "Choose where the copy goes (a phone, SD card or a folder)")
        pick_dest.setAccessibleName("Choose where the copy goes")
        pick_dest.clicked.connect(lambda: self._pick("dest", "Put the copy in"))
        grid = QGridLayout()          # labels in one column, paths beside them, buttons aligned on the right
        grid.setHorizontalSpacing(Space.M)
        grid.setVerticalSpacing(Space.S)
        for r, (label, path, pick) in enumerate((("Library", self.library_label, pick_lib),
                                                 ("Copy to", self.dest_label, pick_dest))):
            grid.addWidget(text(label, Type.BODY, bold=True), r, 0)
            grid.addWidget(path, r, 1)
            grid.addWidget(pick, r, 2)
        grid.setColumnStretch(1, 1)
        self.lay.addLayout(grid)
        self.fmt = QComboBox()
        for label, value in (("Opus (smallest)", "opus"), ("AAC (Apple devices)", "aac"),
                             ("MP3 (plays everywhere)", "mp3")):
            self.fmt.addItem(label, value)
        self.fmt.setAccessibleName("Format of the copy")
        self.quality = QComboBox()
        for label, value in (("High", "high"), ("Balanced", "balanced"), ("Smallest", "small")):
            self.quality.addItem(label, value)
        self.quality.setAccessibleName("Quality of the copy")
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(qs.value("mirror/format", "opus", str))))
        self.quality.setCurrentIndex(max(0, self.quality.findData(qs.value("mirror/quality", "balanced", str))))
        self.fmt.currentIndexChanged.connect(lambda: qs.setValue("mirror/format", self.fmt.currentData()))
        self.quality.currentIndexChanged.connect(lambda: qs.setValue("mirror/quality", self.quality.currentData()))
        self.auto = QCheckBox("Keep in sync automatically")
        self.auto.setToolTip("Syncs a few minutes after Wick starts and then every hour while it runs.")
        self.auto.setChecked(qs.value("mirror/auto", False, bool))
        self.auto.toggled.connect(lambda on: qs.setValue("mirror/auto", on))
        self.sync_btn = button("Sync Now", "primary", "refresh", "Bring the copy up to date")
        self.sync_btn.clicked.connect(self.toggle)
        options = FlowLayout()        # wraps instead of squeezing with large text or a narrow window
        options.add_group(text("Format", Type.BODY), self.fmt)
        options.add_group(text("Quality", Type.BODY), self.quality)
        options.add_group(self.auto)
        row = QHBoxLayout()
        row.setSpacing(Space.L)
        row.addLayout(options, 1)
        row.addWidget(self.sync_btn, 0, Qt.AlignBottom)
        self.lay.addLayout(row)
        self.status = text("", Type.FOOTNOTE, secondary=True, wrap=True)
        self.lay.addWidget(self.status)
        self._show()
        self.timer = QTimer(self, interval=AUTO_EVERY_MS)
        self.timer.timeout.connect(self._auto)
        self.timer.start()
        QTimer.singleShot(5 * 60 * 1000, self._auto)

    def _say(self, line: str) -> None:
        self.status.setText(line)
        self.status.setVisible(bool(line))     # (no empty line at the bottom of the card)

    def _folder(self, key: str) -> Path | None:
        v = self.qs.value(f"mirror/{key}", "", str)
        return Path(v) if v else None

    def _pick(self, key: str, title: str) -> None:
        start = str(self._folder(key) or Path.home())
        d = QFileDialog.getExistingDirectory(self, title, start)
        if d:
            self.qs.setValue(f"mirror/{key}", d)
            self._show()

    def _show(self) -> None:
        lib, dest = self._folder("library"), self._folder("dest")
        self.library_label.setText(str(lib) if lib else "Not chosen")
        self.dest_label.setText(str(dest) if dest else "Not chosen")
        self.sync_btn.setEnabled(bool(lib and dest) or self.busy())
        last = self.qs.value("mirror/last", "", str)
        if not self.busy():
            self._say(last)

    def busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _auto(self) -> None:
        if self.auto.isChecked() and not self.busy() and self._folder("library") and self._folder("dest"):
            if self._folder("library").is_dir() and self._folder("dest").is_dir():   # the phone may be unplugged
                self.start()

    def toggle(self) -> None:
        if self.busy():
            self.worker.cancel.set()
            self._say("Stopping…")
        else:
            self.start()

    def start(self) -> None:
        lib, dest = self._folder("library"), self._folder("dest")
        if not (lib and dest):
            return
        self.worker = _Sync(lib, dest, self.fmt.currentData(), self.quality.currentData(), self)
        self.worker.progress.connect(lambda msg, d, t: self._say(f"{d:,} of {t:,} · {msg[:80]}"))
        self.worker.done.connect(self._done)
        self.worker.finished.connect(self._show)
        self.sync_btn.setText("Stop")
        self._say("Looking at your library…")
        self.worker.start()

    def _done(self, res, error: str) -> None:
        self.sync_btn.setText("Sync Now")
        if res is None:
            self._say(f"Couldn't sync: {error}")
            log.warning("Music copy failed: %s", error)
            return
        songs = res.encoded + res.copied + res.unchanged
        line = (f"Synced {time.strftime('%d %b, %H:%M')}: {songs:,} files, library {human(res.library_bytes)} → "
                f"copy {human(res.copy_bytes)}")
        changes = [f"{res.encoded:,} encoded" if res.encoded else "", f"{res.copied:,} copied" if res.copied else "",
                   f"{res.removed:,} removed" if res.removed else "",
                   f"{len(res.failed):,} couldn't be converted" if res.failed else ""]
        changes = [c for c in changes if c]
        if changes:
            line += " (" + ", ".join(changes) + ")"
        self.qs.setValue("mirror/last", line)
        self._say(line)
        if res.encoded or res.copied or res.removed or res.failed:
            self.synced.emit("Music copy: " + ", ".join(changes))

    def shutdown(self) -> None:
        if self.busy():
            self.worker.cancel.set()
            self.worker.wait(30000)


__all__ = ["MusicCopyCard"]
