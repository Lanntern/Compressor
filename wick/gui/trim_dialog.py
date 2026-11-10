"""Convert › Trim…: choose where a video or song starts and ends; a trimmed copy is saved next to it."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QLabel, QLineEdit, QSlider, QVBoxLayout,
)

from .. import tools
from .style import Space, Type
from .widgets import text

STEP = 10            # slider steps per second


def clock(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:04.1f}" if h else f"{m}:{s:04.1f}"


def parse_clock(value: str) -> float | None:
    """"1:23.4", "0:01:23" or "83.4" as seconds; None if it isn't a time."""
    value = value.strip().replace(",", ".")
    if not re.fullmatch(r"(\d+:){0,2}\d+(\.\d*)?", value):
        return None
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


class _Frame(QThread):
    ready = Signal(int, QImage)

    def __init__(self, path: Path, at: float, ticket: int, parent=None):
        super().__init__(parent)
        self.path, self.at, self.ticket = path, at, ticket

    def run(self) -> None:
        ffmpeg = tools.find_tool("ffmpeg")
        if not ffmpeg:
            return
        try:
            p = tools.run([ffmpeg, "-nostdin", "-v", "error", "-ss", f"{self.at:.2f}", "-i", str(self.path),
                           "-frames:v", "1", "-vf", "scale=480:-2", "-f", "image2pipe", "-c:v", "png", "-"],
                          timeout=30, check=False, text=False)
        except Exception:  # noqa: BLE001 - no picture: the times still work
            return
        img = QImage.fromData(p.stdout)
        if not img.isNull():
            self.ready.emit(self.ticket, img)


class TrimDialog(QDialog):
    def __init__(self, path: Path, duration: float, is_video: bool, parent=None):
        super().__init__(parent)
        self.path, self.duration = path, max(0.1, duration)
        self.setWindowTitle(f"Trim {path.name}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Space.L, Space.L, Space.L, Space.L)
        lay.setSpacing(Space.M)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignCenter)
        self.picture.setMinimumSize(480, 270)
        self.picture.setAccessibleName("The frame at the chosen time")
        if is_video:
            lay.addWidget(self.picture)
        grid = QGridLayout()
        grid.setHorizontalSpacing(Space.M)
        self.sliders, self.fields = [], []
        for row, (label, value) in enumerate((("Start", 0.0), ("End", self.duration))):
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, round(self.duration * STEP))
            slider.setValue(round(value * STEP))
            slider.setAccessibleName(f"{label} time")
            field = QLineEdit(clock(value))
            field.setMaximumWidth(Space.XXL * 3)
            field.setAccessibleName(f"{label} time (minutes:seconds)")
            slider.valueChanged.connect(lambda v, i=row: self._slid(i, v))
            field.editingFinished.connect(lambda i=row: self._typed(i))
            grid.addWidget(text(label, Type.BODY), row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(field, row, 2)
            self.sliders.append(slider)
            self.fields.append(field)
        lay.addLayout(grid)
        self.summary = text("", Type.FOOTNOTE, secondary=True, wrap=True)
        lay.addWidget(self.summary)
        self.exact = QCheckBox("Cut exactly (re-encodes the video)")
        self.exact.setToolTip("Without it nothing is re-encoded and no quality is lost, but the copy starts at the "
                              "video's nearest keyframe before Start: usually up to a few seconds earlier.")
        if is_video:
            lay.addWidget(self.exact)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save = buttons.button(QDialogButtonBox.Save)
        self.save.setText("Save Trimmed Copy")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.is_video, self._ticket, self._shown, self._workers = is_video, 0, 0, []
        self._timer = QTimer(self, singleShot=True, interval=150)
        self._timer.timeout.connect(self._grab)
        self._editing = 0
        self._update()
        if is_video:
            self._grab()

    # ---- the two times
    def times(self) -> tuple[float, float]:
        end = self.sliders[1]
        return self.sliders[0].value() / STEP, self.duration if end.value() == end.maximum() else end.value() / STEP

    def _slid(self, i: int, value: int) -> None:
        other = self.sliders[1 - i]
        if i == 0 and value >= other.value():
            other.setValue(min(other.maximum(), value + 1))
        elif i == 1 and value <= other.value():
            other.setValue(max(0, value - 1))
        self.fields[i].setText(clock(value / STEP))
        self._editing = i
        self._update()
        if self.is_video:
            self._timer.start()

    def _typed(self, i: int) -> None:
        t = parse_clock(self.fields[i].text())
        if t is None:
            self.fields[i].setText(clock(self.sliders[i].value() / STEP))
            return
        self.sliders[i].setValue(round(min(self.duration, t) * STEP))

    def _update(self) -> None:
        start, end = self.times()
        cut = self.sliders[0].value() > 0 or self.sliders[1].value() < self.sliders[1].maximum()
        self.save.setEnabled(end > start and cut)
        self.summary.setText(f"Keeps {clock(end - start)} of {clock(self.duration)}. The copy is saved next to "
                             "the original, which isn't changed.")

    # ---- the picture at the time being changed
    def _grab(self) -> None:
        self._ticket += 1
        w = _Frame(self.path, self.times()[self._editing], self._ticket, self)
        w.ready.connect(self._show)
        w.finished.connect(lambda w=w: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _show(self, ticket: int, img: QImage) -> None:
        if ticket >= self._shown:
            self._shown = ticket
            self.picture.setPixmap(QPixmap.fromImage(img))

    def done(self, result: int) -> None:
        for w in list(self._workers):
            w.wait(2000)
        super().done(result)


__all__ = ["TrimDialog", "clock", "parse_clock"]
