"""Before/after preview of one image with the current settings."""

from __future__ import annotations

import copy
import html
import re
import threading
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QHBoxLayout, QSizePolicy, QSlider, QSpinBox, QTabBar, QVBoxLayout, QWidget,
)

from ..engine import preview_file
from ..fileops import remove_quietly
from ..formats import KIND_LABEL, VIDEO, human
from ..images import NotWorthIt, decode_for_display, measure_quality
from ..plan import plan_entry
from ..scanner import FileEntry
from ..settings import Settings
from ..video import VideoPreview
from .. import steps, tools
from ..log import log
from .settings_panel import BIN_NAME
from .style import Radius, Space, Type, tokens, ui_font
from .widgets import ElidedLabel, button, text


_background_previews: set = set()


def _release_preview(worker) -> None:
    if worker not in _background_previews:
        return
    worker.discard_video()
    _background_previews.discard(worker)
    worker.deleteLater()


def quit_after_previews(app) -> None:
    """Keep the event loop alive during final decoder cleanup, without blocking a closing window."""
    if not _background_previews:
        app.quit()
        return
    app.setQuitOnLastWindowClosed(False)

    def check() -> None:
        if _background_previews:
            QTimer.singleShot(50, check)
        else:
            app.quit()

    check()


class PreviewThread(QThread):
    ready = Signal(bytes, bytes, int, str)   # original png, converted png, (estimated) new size, description
    video_ready = Signal(object, str)        # VideoPreview, description
    failed = Signal(str)
    step = Signal(str)

    def __init__(self, entry: FileEntry, settings: Settings, parent=None, start: float | None = None):
        super().__init__(parent)
        self.entry, self.settings, self.sample_start = entry, settings, start
        self.cancel = threading.Event()
        self._video_result: VideoPreview | None = None

    def run(self) -> None:
        with tools.cancelling(self.cancel), steps.reporting(self.step.emit):
            self._run()

    def _run(self) -> None:
        try:
            plan = plan_entry(self.entry, self.settings)
            if not plan.convert:
                self.failed.emit(plan.reason)
                return
            if self.entry.kind == VIDEO:
                self._video(plan)
            else:
                self._image(plan)
        except tools.Cancelled:
            return
        except NotWorthIt as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("Preview failed for %s", self.entry.path)
            self.failed.emit(str(e))

    def _image(self, plan) -> None:
        out: Path | None = None
        try:
            out = preview_file(self.entry, plan, self.settings)
            size = out.stat().st_size
            desc = plan.reason
            if plan.outcome:  # Smart quality: its choice and score
                desc += f" · {plan.outcome}"
            elif not plan.lossless:
                try:
                    e = self.entry
                    desc += f" · {measure_quality(e.path, e.kind, e.image, plan, self.settings, out)}"
                except Exception:  # noqa: BLE001 - the score is a bonus
                    log.warning("Preview quality measurement failed for %s", self.entry.path, exc_info=True)
            if self.cancel.is_set():
                return
            self.ready.emit(decode_for_display(self.entry.path), decode_for_display(out), size, desc + " · verified")
        finally:
            remove_quietly(out)

    def _video(self, plan) -> None:
        from ..video import preview_video

        assert self.entry.video is not None
        r = preview_video(self.entry.path, self.entry.video, self.settings, self.cancel, start=self.sample_start)
        self._video_result = r
        desc = plan.reason + (f" · {r.note}" if r.note else "")
        desc += f" · estimated from a {r.length:.0f} s sample at {_clock(r.start)}"
        self.video_ready.emit(r, desc)

    def discard_video(self) -> None:
        import shutil

        if self._video_result is not None and self._video_result.folder is not None:
            shutil.rmtree(self._video_result.folder, ignore_errors=True)
        self._video_result = None


def _clock(seconds: float) -> str:
    m, sec = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _display_size(entry: FileEntry) -> tuple[int, int] | None:
    """The picture's size as it's shown (EXIF orientation applied), from what the scan read: no decoding."""
    if entry.kind == VIDEO:
        v = entry.video
        return (v.width, v.height) if v is not None and v.width and v.height else None
    info = entry.image
    if info is None or not info.width or not info.height:
        return None
    w, h = info.width, info.height
    try:
        from PIL import Image

        with Image.open(entry.path) as im:              # (header only)
            if int(im.getexif().get(0x0112, 1) or 1) in (5, 6, 7, 8):
                w, h = h, w
    except Exception:  # noqa: BLE001 - no EXIF, or a format Pillow doesn't read: as stored
        pass
    return w, h


class CompareView(QWidget):
    """Two ways to compare: a split line (original left of it, result right) or side by side. In both,
    the wheel zooms and dragging pans; side by side, both panes move together so details stay aligned."""

    GAP = Space.S

    split_changed = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.before: QPixmap | None = None
        self.after: QPixmap | None = None
        self.split = 0.5
        self.side_by_side = False   # two panes instead of the split line
        self.zoom = 1.0
        self.offset = QPointF(0, 0)
        self._drag: QPointF | None = None
        self._fit = True
        self.message = "Making the preview…"
        self.expected: tuple[int, int] | None = None   # the picture's size, known before it's decoded
        self.setMinimumSize(Space.XXL * 12, Space.XXL * 8)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("Comparison")
        self.setAccessibleDescription("Left: original. Right: compressed. Arrow keys move the line, "
                                      "plus and minus zoom, 0 fits, 1 shows actual size.")

    def set_images(self, before: QPixmap, after: QPixmap) -> None:
        self.before, self.after = before, after
        self.fit()

    def fit(self) -> None:
        self._fit = True
        self.update()

    def set_side_by_side(self, on: bool) -> None:
        if on != self.side_by_side:
            self.side_by_side = on
            self._fit = True          # the pane width changed: start from a fitted view
            self.unsetCursor()
            self.update()

    def _pane_width(self) -> float:
        return (self.width() - self.GAP) / 2 if self.side_by_side else float(self.width())

    def _local(self, pos: QPointF) -> QPointF:
        """``pos`` in the coordinates of the pane under it (side by side: either half)."""
        if self.side_by_side and pos.x() > self._pane_width() + self.GAP / 2:
            return QPointF(pos.x() - self._pane_width() - self.GAP, pos.y())
        return pos

    def actual_size(self) -> None:
        if not self.before:
            return
        self._fit = False
        self.zoom = 1.0 / max(1.0, self.devicePixelRatioF())
        self.offset = QPointF((self._pane_width() - self.before.width() * self.zoom) / 2,
                              (self.height() - self.before.height() * self.zoom) / 2)
        self.update()

    def _apply_fit(self) -> None:
        if not self.before:
            return
        w, h, pw = self.before.width(), self.before.height(), self._pane_width()
        self.zoom = min(pw / w, self.height() / h, 1.0 / max(1.0, self.devicePixelRatioF()) * 4)
        self.offset = QPointF((pw - w * self.zoom) / 2, (self.height() - h * self.zoom) / 2)

    def paintEvent(self, _event) -> None:
        tk = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bg = QPainterPath()
        bg.addRoundedRect(QRectF(self.rect()), Radius.CARD, Radius.CARD)
        p.fillPath(bg, QColor(tk.canvas))
        p.setClipPath(bg)
        if not self.before or not self.after:
            area = QRectF(self.rect())
            if self.expected and not self.side_by_side:
                # Where the picture will be, at its size: it then appears in place instead of moving things.
                w, h = self.expected
                zoom = min(area.width() / w, area.height() / h, 4.0 / max(1.0, self.devicePixelRatioF()))
                area = QRectF((area.width() - w * zoom) / 2, (area.height() - h * zoom) / 2, w * zoom, h * zoom)
                p.fillRect(area, QColor(tk.card))
                p.setPen(QPen(QColor(tk.separator), 1))
                p.drawRect(area.adjusted(0.5, 0.5, -0.5, -0.5))
            p.setPen(QColor(tk.text2))
            p.setFont(ui_font(Type.BODY))
            p.drawText(area, Qt.AlignCenter | Qt.TextWordWrap, self.message)
            return
        if self._fit:
            self._apply_fit()
        if self.side_by_side:
            self._paint_side_by_side(p)
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform, self.zoom < 1)
        w, h = self.before.width() * self.zoom, self.before.height() * self.zoom
        target = QRectF(self.offset.x(), self.offset.y(), w, h)
        split_x = self.width() * self.split
        p.save()
        p.setClipRect(QRectF(0, 0, split_x, self.height()))
        p.drawPixmap(target, self.before, QRectF(self.before.rect()))
        p.restore()
        p.save()
        p.setClipRect(QRectF(split_x, 0, self.width() - split_x, self.height()))
        # A resized result is stretched to the original's size so details line up.
        p.drawPixmap(target, self.after, QRectF(self.after.rect()))
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 220), 2))
        p.drawLine(QPointF(split_x, 0), QPointF(split_x, self.height()))
        p.setPen(QPen(QColor(0, 0, 0, 160), 1))
        p.drawLine(QPointF(split_x + 1.5, 0), QPointF(split_x + 1.5, self.height()))
        p.setFont(ui_font(Type.FOOTNOTE, QFont.DemiBold))
        fm = p.fontMetrics()
        for label, left in (("Original", True), ("Compressed", False)):
            w_txt = fm.horizontalAdvance(label) + Space.L
            h_txt = fm.height() + Space.S
            rect = QRectF(Space.M if left else self.width() - Space.M - w_txt, Space.M, w_txt, h_txt)
            pill = QPainterPath()
            pill.addRoundedRect(rect, h_txt / 2, h_txt / 2)
            p.fillPath(pill, QColor(0, 0, 0, 150))
            p.setPen(QColor("#FFFFFF"))
            p.drawText(rect, Qt.AlignCenter, label)
        # handle on the split line
        knob = QRectF(split_x - Space.M, self.height() / 2 - Space.M, Space.XL, Space.XL)
        p.setPen(QPen(QColor(0, 0, 0, 80), 1))
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(knob)
        p.setPen(QPen(QColor("#3A3A3C"), 1.6, Qt.SolidLine, Qt.RoundCap))
        cx, cy = knob.center().x(), knob.center().y()
        for d in (-1, 1):
            p.drawLine(QPointF(cx + d * 2, cy - 4), QPointF(cx + d * 6, cy))
            p.drawLine(QPointF(cx + d * 6, cy), QPointF(cx + d * 2, cy + 4))

    def _paint_side_by_side(self, p: QPainter) -> None:
        """Original left, compressed right: same zoom and position in both, so details line up."""
        tk = tokens()
        pw = self._pane_width()
        w, h = self.before.width() * self.zoom, self.before.height() * self.zoom
        p.setRenderHint(QPainter.SmoothPixmapTransform, self.zoom < 1)
        p.fillRect(QRectF(pw, 0, self.GAP, self.height()), QColor(tk.window))
        for i, (pm, label) in enumerate(((self.before, "Original"), (self.after, "Compressed"))):
            if pm is None:
                continue
            left = i * (pw + self.GAP)
            p.save()
            p.setClipRect(QRectF(left, 0, pw, self.height()), Qt.IntersectClip)
            # A resized result is stretched to the original's size so details line up.
            p.drawPixmap(QRectF(left + self.offset.x(), self.offset.y(), w, h), pm, QRectF(pm.rect()))
            p.restore()
            self._pill(p, label, QPointF(left + Space.M, Space.M))

    def _pill(self, p: QPainter, label: str, at: QPointF) -> None:
        p.setFont(ui_font(Type.FOOTNOTE, QFont.DemiBold))
        fm = p.fontMetrics()
        rect = QRectF(at.x(), at.y(), fm.horizontalAdvance(label) + Space.L, fm.height() + Space.S)
        pill = QPainterPath()
        pill.addRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        p.fillPath(pill, QColor(0, 0, 0, 150))
        p.setPen(QColor("#FFFFFF"))
        p.drawText(rect, Qt.AlignCenter, label)

    def keyPressEvent(self, e) -> None:
        k = e.key()
        if k in (Qt.Key_Left, Qt.Key_Right) and not self.side_by_side:
            step = 0.1 if e.modifiers() & Qt.ShiftModifier else 0.02
            self.split = max(0.0, min(1.0, self.split + (step if k == Qt.Key_Right else -step)))
            self.split_changed.emit(self.split)
            self.update()
        elif k in (Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Minus):
            self._zoom_at(QPointF(self.width() / 2, self.height() / 2), 1.25 if k != Qt.Key_Minus else 0.8)
        elif k == Qt.Key_0:
            self.fit()
        elif k == Qt.Key_1:
            self.actual_size()
        else:
            super().keyPressEvent(e)

    def _zoom_at(self, pos: QPointF, factor: float) -> None:
        if not self.before:
            return
        if self._fit:
            self._apply_fit()
            self._fit = False
        new_zoom = max(0.02, min(32.0, self.zoom * factor))
        self.offset = pos - (pos - self.offset) * (new_zoom / self.zoom)
        self.zoom = new_zoom
        self.update()

    def wheelEvent(self, e) -> None:
        self._zoom_at(self._local(e.position()), 1.25 if e.angleDelta().y() > 0 else 0.8)

    def mousePressEvent(self, e) -> None:
        if not self.side_by_side and e.button() == Qt.LeftButton and \
                abs(e.position().x() - self.width() * self.split) < 8:
            self._drag = None
            self._splitting = True
        else:
            self._splitting = False
            self._drag = e.position()

    def mouseMoveEvent(self, e) -> None:
        near = not self.side_by_side and abs(e.position().x() - self.width() * self.split) < 8
        self.setCursor(Qt.SplitHCursor if near or getattr(self, "_splitting", False) else Qt.OpenHandCursor)
        if getattr(self, "_splitting", False) and e.buttons() & Qt.LeftButton:
            self.split = max(0.0, min(1.0, e.position().x() / max(1, self.width())))
            self.split_changed.emit(self.split)
            self.update()
        elif self._drag is not None and e.buttons() & Qt.LeftButton:
            if self._fit:
                self._apply_fit()
                self._fit = False
            self.offset += e.position() - self._drag
            self._drag = e.position()
            self.update()

    def mouseReleaseEvent(self, _e) -> None:
        self._drag = None
        self._splitting = False


class PreviewDialog(QDialog):
    """Shows before/after for one image or a video sample; lets you try other qualities."""

    quality_chosen = Signal(object)              # Settings with the chosen quality: use it for every file
    compress_requested = Signal(object)          # Settings: compress this file now
    save_as_requested = Signal(object, object)   # Settings, Path: save the result there (original kept)

    def __init__(self, entry: FileEntry, settings: Settings, parent=None):
        super().__init__(parent)
        self.entry, self.settings = entry, copy.deepcopy(settings)
        self.worker: PreviewThread | None = None
        self.is_video = entry.kind == VIDEO
        self.video = None            # the current VideoPreview
        self.sample_start: float | None = None
        self._frames: dict[int, tuple[QPixmap, QPixmap]] = {}
        self.setWindowTitle(f"Preview — {entry.path.name}")
        self.resize(1120, 780)
        self.setMinimumWidth(640)     # (the height follows the controls: videos have an extra row)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(Space.L, Space.L, Space.L, Space.L)
        lay.setSpacing(Space.M)
        head = QHBoxLayout()
        head.setSpacing(Space.S)
        title_box = QVBoxLayout()
        title_box.setSpacing(Space.XXS)
        title_box.addWidget(ElidedLabel(entry.path.name, Type.TITLE3, bold=True))   # long names never widen it
        # Two lines that never change height (so nothing below moves when the result arrives): the outcome, and
        # the details (shortened to fit, in full in the tooltip).
        self.info = text("Making the preview…", Type.BODY, secondary=True)
        self.info.setTextFormat(Qt.RichText)
        self.info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.info.setAccessibleName("Result")
        title_box.addWidget(self.info)
        self.details = ElidedLabel(self._source_line(entry), Type.FOOTNOTE, secondary=True, mode=Qt.ElideRight)
        self.details.setAccessibleName("Details")
        title_box.addWidget(self.details)
        head.addLayout(title_box, 1)
        fit = button("Fit", "", None, "Fit to window", "0")
        fit.clicked.connect(lambda: self.view.fit())
        one = button("Actual Size", "", None, "Show actual pixels", "1")
        one.clicked.connect(lambda: self.view.actual_size())
        self.mode = QTabBar()
        self.mode.setObjectName("segmented")
        self.mode.setExpanding(False)
        self.mode.setDrawBase(False)
        self.mode.setUsesScrollButtons(False)        # always both segments in full, never arrows
        self.mode.setElideMode(Qt.ElideNone)
        self.mode.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.mode.setAccessibleName("Comparison style")
        self.mode.addTab("Split")
        self.mode.addTab("Side by Side")
        self.mode.setTabToolTip(0, "One picture, original left of the line, result right of it  (S)")
        self.mode.setTabToolTip(1, "The original and the result next to each other  (S)")
        self.mode.currentChanged.connect(lambda i: self._side_by_side(i == 1))
        head.addWidget(self.mode)
        head.addSpacing(Space.S)
        head.addWidget(fit)
        head.addWidget(one)
        lay.addLayout(head)

        self.view = CompareView()
        self.view.expected = _display_size(entry)
        lay.addWidget(self.view, 1)
        if self.is_video:
            lay.addLayout(self._video_controls(entry))

        bottom = QHBoxLayout()
        bottom.setSpacing(Space.S)
        self.split_label = text("Line", Type.BODY, secondary=True)
        bottom.addWidget(self.split_label)
        self.split = QSlider(Qt.Horizontal)
        self.split.setRange(0, 100)
        self.split.setValue(50)
        self.split.setAccessibleName("Comparison line position")
        self.split.valueChanged.connect(lambda v: (setattr(self.view, "split", v / 100), self.view.update()))
        self.view.split_changed.connect(lambda f: (self.split.blockSignals(True), self.split.setValue(round(f * 100)),
                                                    self.split.blockSignals(False)))
        bottom.addWidget(self.split, 1)
        bottom.addStretch(0)        # takes the slider's room while it's hidden (side by side)
        bottom.addSpacing(Space.L)
        lay.addLayout(bottom)       # (first: a widget shown before it has a parent flashes up as a window)
        self._build_quality(bottom, settings)
        actions = QHBoxLayout()            # a row of its own: everything fits a narrow window
        actions.setSpacing(Space.S)
        close = button("Close", "", None, "", "Esc")
        close.clicked.connect(self.reject)
        self.use = button("Use for All Files", "", None, "Use this quality for every file in the list (nothing is "
                                                       "compressed yet)")
        self.use.clicked.connect(self._use)
        self.save_as = button("Save a Copy…", "", None, "Save the result as a new file where you choose; the "
                                                      "original stays as it is", "Ctrl+S")
        self.save_as.clicked.connect(self._save_as)
        self.compress = button({"replace": "Compress and Replace", "folder": "Compress to Folder"}.get(
            self.settings.output_mode, "Compress Beside Original"), "primary", None, self._compress_tip(), "Ctrl+Enter")
        self.compress.clicked.connect(self._compress)
        actions.addWidget(self.use)
        actions.addStretch(1)
        actions.addWidget(close)
        actions.addWidget(self.save_as)
        actions.addWidget(self.compress)
        for b in (close, self.use, self.save_as, self.compress, fit, one):
            b.setAutoDefault(False)          # Enter makes the preview again; it never compresses by accident
        self.rerun.setDefault(True)
        lay.addLayout(actions)
        self.use.setVisible(self.quality.isEnabled())
        self.view.setFocus()
        self.run()

    # ---- video: frames through the sample, and where the sample comes from
    def _video_controls(self, entry) -> QHBoxLayout:
        from PySide6.QtCore import QTimer

        row = QHBoxLayout()
        row.setSpacing(Space.S)
        self.play = button("Play", "", None, "Step through the sample's frames", "Space")
        self.play.setCheckable(True)
        self.play.toggled.connect(self._play_toggled)
        self.frame = QSlider(Qt.Horizontal)
        self.frame.setAccessibleName("Frame")
        self.frame.setEnabled(False)
        self.frame.valueChanged.connect(self._show_frame)
        self.frame_label = text("", Type.FOOTNOTE, secondary=True)
        self.frame_label.setMinimumWidth(Space.XXL * 4)
        self.timer = QTimer(self, interval=250)
        self.timer.timeout.connect(lambda: self.frame.setValue((self.frame.value() + 1) % max(1, self.frame.maximum()
                                                                                           + 1)))
        duration = max(1, int(entry.video.duration)) if entry.video else 1
        self.position = QSlider(Qt.Horizontal)
        self.position.setRange(0, duration)
        self.position.setValue(duration // 2)
        self.position.setAccessibleName("Sample position in the video")
        self.position.setToolTip("Where in the video the sample comes from. Let go to encode a new sample.")
        self.position_label = text(_clock(duration / 2), Type.FOOTNOTE, secondary=True)
        self.position.valueChanged.connect(lambda v: self.position_label.setText(_clock(v)))
        self.position.sliderReleased.connect(self._new_sample)
        row.addWidget(self.play)
        row.addWidget(self.frame, 2)
        row.addWidget(self.frame_label)
        row.addSpacing(Space.L)
        row.addWidget(text("Sample at", Type.BODY, secondary=True))
        row.addWidget(self.position, 1)
        row.addWidget(self.position_label)
        return row

    def _new_sample(self) -> None:
        length = 6.0
        self.sample_start = max(0.0, self.position.value() - length / 2)
        self.run()

    def _play_toggled(self, on: bool) -> None:
        self.play.setText("Pause" if on else "Play")
        self.timer.start() if on else self.timer.stop()

    def _show_frame(self, i: int) -> None:
        r = self.video
        if r is None or not r.frames or not 0 <= i < len(r.frames):
            return
        if i not in self._frames:
            a, b = r.frames[i]
            self._frames[i] = (QPixmap(str(a)), QPixmap(str(b)))
        before, after = self._frames[i]
        keep_view = self.view.before is not None and not self.view._fit
        self.view.before, self.view.after = before, after
        if not keep_view:
            self.view.fit()
        self.view.update()
        t = r.times[i] if i < len(r.times) else r.start
        self.frame_label.setText(f"Frame {i + 1} of {len(r.frames)} · {_clock(t)}.{int(t * 10) % 10}")

    def _video_ready(self, r, desc: str) -> None:
        if getattr(self, "_closing", False):
            import shutil

            if r.folder is not None:
                shutil.rmtree(r.folder, ignore_errors=True)
            return
        self._drop_frames()
        self.video = r
        if self.worker is not None and self.worker._video_result is r:
            self.worker._video_result = None    # the dialog now owns the frame files
        if r.frames:
            self.frame.setRange(0, len(r.frames) - 1)
            self.frame.setEnabled(True)
            self.frame.setValue(len(r.frames) // 2)
            self._show_frame(self.frame.value())
            after = self._frames[self.frame.value()][1]
            size = (after.width(), after.height())
        else:  # frames unavailable: the single middle frame
            self.view.set_images(QPixmap.fromImage(QImage.fromData(r.before)),
                                 QPixmap.fromImage(QImage.fromData(r.after)))
            size = (self.view.after.width(), self.view.after.height())
        self._describe(r.est_size, size, desc)

    def _drop_frames(self) -> None:
        import shutil

        if self.is_video:
            self.timer.stop()
            self.play.setChecked(False)
        self._frames.clear()
        if self.video is not None and self.video.folder is not None:
            shutil.rmtree(self.video.folder, ignore_errors=True)
        self.video = None

    def _side_by_side(self, on: bool) -> None:
        self.view.set_side_by_side(on)
        self.split_label.setVisible(not on)
        self.split.setVisible(not on)
        self.view.setAccessibleDescription(
            "Left: original. Right: compressed. Plus and minus zoom, 0 fits, 1 shows actual size."
            if on else "Left of the line: original. Right: compressed. Arrow keys move the line, plus and minus "
                       "zoom, 0 fits, 1 shows actual size.")

    def keyPressEvent(self, e) -> None:
        ctrl = e.modifiers() & Qt.ControlModifier
        if ctrl and e.key() == Qt.Key_S and self.save_as.isEnabled():
            self._save_as()
        elif ctrl and e.key() in (Qt.Key_Return, Qt.Key_Enter) and self.compress.isEnabled():
            self._compress()
        elif e.key() == Qt.Key_S and not e.modifiers():
            self.mode.setCurrentIndex(1 - self.mode.currentIndex())
        elif e.key() == Qt.Key_Space and self.is_video and self.frame.isEnabled():
            self.play.toggle()
        elif e.key() in (Qt.Key_Comma, Qt.Key_Period) and self.is_video and self.frame.isEnabled():
            self.frame.setValue(self.frame.value() + (1 if e.key() == Qt.Key_Period else -1))
        else:
            super().keyPressEvent(e)

    # ---- quality: the same choices as the options, plus a value of your own
    IMAGE_CHOICES = (("Visually identical", "identical"), ("High", "high"), ("Good", "good"), ("Custom", "custom"))
    VIDEO_CHOICES = (("Visually identical", "identical"), ("High", "high"), ("Balanced", "balanced"),
                     ("Smallest", "small"), ("Custom", "custom"))

    def _build_quality(self, bottom: QHBoxLayout, settings: Settings) -> None:
        from .settings_panel import VIDEO_CRF_TIP

        self.quality = QComboBox()
        self.quality.setAccessibleName("Quality")
        self.value = QSpinBox()
        self.value.setAccessibleName("Quality value")
        if self.is_video:
            for label, key in self.VIDEO_CHOICES:
                self.quality.addItem(label, key)
            measured = settings.video_quality == "identical" or (
                settings.smart_quality and settings.smart_target == "identical" and settings.video_quality != "custom")
            start = "identical" if measured else settings.video_quality
            self.value.setRange(1, 63)
            self.value.setValue(settings.video_crf)
            self.value.setToolTip(VIDEO_CRF_TIP)
            self.quality.setToolTip("Visually identical: measures short test encodes against the original (VMAF) "
                                    "and uses the smallest setting that still looks the same.\nHigh, Balanced, "
                                    "Smallest: fixed settings.\nCustom: the encoder's quality value, set by you.")
        else:
            for label, key in self.IMAGE_CHOICES:
                self.quality.addItem(label, key)
            start = settings.smart_target if settings.smart_quality else "custom"
            self.value.setRange(1, 100)
            self.value.setValue(settings.image_quality)
            self.value.setToolTip("Quality 1–100: higher means better quality and larger files. 85–90 is close to "
                                  "the original for most photos.")
            self.quality.setToolTip("Visually identical, High, Good: Smart quality measures the result "
                                    "(SSIMULACRA2) and picks the smallest quality that meets this target.\n"
                                    "Custom: choose the quality value yourself.")
        self.quality.setCurrentIndex(max(0, self.quality.findData(start)))
        self.quality.activated.connect(self._quality_picked)
        self.value.editingFinished.connect(self._value_changed)
        self.rerun = button("Try", "", "refresh", "Make the preview again with this quality", "Enter")
        self.rerun.clicked.connect(self.run)
        self.lossless_note = text("Lossless: the same pixels, nothing to choose", Type.BODY, secondary=True)
        self.quality_label = text("Quality", Type.BODY, secondary=True)
        for w in (self.quality_label, self.quality, self.value, self.rerun, self.lossless_note):
            bottom.addWidget(w)
        self._smart_value: int | None = None     # what the measured choice settled on (to start Custom from)
        lossless = (not self.is_video) and plan_entry(self.entry, settings).lossless
        for w in (self.quality_label, self.quality, self.value, self.rerun):
            w.setEnabled(not lossless)
            w.setVisible(not lossless)
        self.lossless_note.setVisible(lossless)
        self._show_value()
        self._apply_quality(self.settings)

    def _show_value(self) -> None:
        self.value.setVisible(self.quality.isEnabled() and self.quality.currentData() == "custom")

    def _quality_picked(self, _i: int) -> None:
        if self.quality.currentData() == "custom" and self._smart_value is not None:
            self.value.setValue(self._smart_value)      # start from what the measurement chose
        self._show_value()
        self.run()

    def _value_changed(self) -> None:
        if self.quality.currentData() == "custom" and self.value.value() != self._applied_value:
            self.run()

    def _apply_quality(self, s: Settings) -> Settings:
        key, value = self.quality.currentData(), self.value.value()
        self._applied_value = value
        if self.is_video:
            s.video_quality = key
            s.video_crf = value
            s.smart_quality = False        # the choice here is explicit ("Visually identical" measures)
        elif key == "custom":
            s.smart_quality, s.image_quality = False, value
        else:
            s.smart_quality, s.smart_target = True, key
        return s

    def _use(self) -> None:
        self.quality_chosen.emit(self._chosen_settings())
        self.accept()

    def _compress_tip(self) -> str:
        where = {"replace": f"the original goes to the {BIN_NAME} once the result is checked",
                 "folder": f"the result is saved in {self.settings.output_dir}"}.get(
            self.settings.output_mode, "the result is saved next to the original")
        return f"Compress this file now with these settings: {where}"

    def _chosen_settings(self) -> Settings:
        return self._apply_quality(copy.deepcopy(self.settings))

    def _compress(self) -> None:
        self.compress_requested.emit(self._chosen_settings())
        self.accept()

    def _save_as(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        from ..engine import _unique
        from ..steps import format_name

        s = self._chosen_settings()
        ext = plan_entry(self.entry, s).target_ext or self.entry.path.suffix
        suggested = _unique(self.entry.path.with_suffix(ext))
        name, _ = QFileDialog.getSaveFileName(self, "Save the result as", str(suggested),
                                              f"{format_name(ext)} (*{ext})")
        if not name:
            return
        dest = Path(name)
        if dest.suffix.lower() != ext.lower():
            dest = dest.with_name(dest.name + ext)
        self.save_as_requested.emit(s, dest)
        self.accept()

    def run(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        if self.worker is not None:
            self.worker.deleteLater()
        self._closing = False
        self._apply_quality(self.settings)
        self.view.before = self.view.after = None
        self.view.message = "Encoding a sample…" if self.is_video else "Converting…"
        self.view.update()
        self.info.setText("Encoding and measuring a sample…" if self.is_video else "Converting and verifying…")
        self.rerun.setEnabled(False)
        self._result_ready(False)
        if self.is_video:
            self._drop_frames()
            self.frame.setEnabled(False)
        self.worker = PreviewThread(self.entry, self.settings, self, self.sample_start)
        self.worker.ready.connect(self._ready)
        self.worker.video_ready.connect(self._video_ready)
        self.worker.failed.connect(self._failed)
        self.worker.step.connect(self._preview_step)
        self.worker.finished.connect(self._preview_finished)
        self.worker.start()

    def _preview_finished(self) -> None:
        if not getattr(self, "_closing", False):
            self.rerun.setEnabled(True)

    def _preview_step(self, message: str) -> None:
        if not getattr(self, "_closing", False):
            self.info.setText(html.escape(message) + "…")

    def _ready(self, before: bytes, after: bytes, size: int, desc: str) -> None:
        if getattr(self, "_closing", False):
            return
        self.view.set_images(QPixmap.fromImage(QImage.fromData(before)), QPixmap.fromImage(QImage.fromData(after)))
        a = QImage.fromData(after)
        self._describe(size, (a.width(), a.height()), desc)

    def _result_ready(self, ok: bool) -> None:
        self.save_as.setEnabled(ok)
        self.compress.setEnabled(ok)

    def _kept(self, size: int) -> str:
        """Why compressing would leave the original as it is with this result ("" if it would be kept).
        (Images only: a video's size here is estimated from a sample.)"""
        orig, s = self.entry.size, self.settings
        if self.is_video or not orig:
            return ""
        pct = 100 * (orig - size) / orig
        if size >= orig:
            return "The result is larger than the original, so compressing would leave the file as it is."
        if s.require_savings and (pct < s.min_savings_pct or orig - size < s.min_savings_kb * 1024):
            return (f"The result saves only {pct:.1f}%, less than the minimum set in Settings, so compressing "
                    "would leave the file as it is.")
        return ""

    def _describe(self, size: int, dims: tuple[int, int], desc: str) -> None:
        self._result_ready(True)
        why = self._kept(size)
        self.compress.setEnabled(not why)
        self.compress.setToolTip(f"{why} Try other settings, or use Save As… to keep this result as a new file."
                                 if why else self._compress_tip())
        m = re.search(r"\b(?:CRF|CQ) (\d+)" if self.is_video else r"\bquality (\d+)", desc)
        if m and self.quality.currentData() != "custom":
            self._smart_value = int(m.group(1))
        orig = self.entry.size
        pct = 100 * (orig - size) / orig if orig else 0
        tk = tokens()
        verdict = (f"<span style='color:{tk.success}'><b>{pct:.0f}% smaller</b></span>" if pct >= 0
                   else f"<span style='color:{tk.danger}'><b>{-pct:.0f}% larger</b></span>")
        approx = "~" if self.is_video else ""
        self.info.setText(f"{human(orig)} → <b>{approx}{human(size)}</b> · {verdict}")
        self.details.setText(f"{dims[0]}×{dims[1]} · {desc}")
        self.view.setAccessibleDescription(f"{human(orig)} to {human(size)}, {pct:.0f} percent smaller. {desc}.")

    @staticmethod
    def _source_line(entry: FileEntry) -> str:
        size = _display_size(entry)
        dims = f"{size[0]}×{size[1]} · " if size else ""
        return f"{dims}{KIND_LABEL.get(entry.kind, entry.kind)} · {human(entry.size)}"

    def _failed(self, msg: str) -> None:
        if getattr(self, "_closing", False):
            return
        self.view.message = f"Preview not possible: {msg}"
        self.view.update()
        self.info.setText(f"Preview not possible: {html.escape(msg[:240])}. Details are in the log.")
        self.info.setToolTip(msg)

    def closeEvent(self, e) -> None:
        self._cancel_preview()
        super().closeEvent(e)

    def reject(self) -> None:
        self._cancel_preview()
        super().reject()

    def _cancel_preview(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._closing = True
        self._drop_frames()
        worker = self.worker
        if worker is None:
            return
        worker.cancel.set()
        for signal, slot in ((worker.ready, self._ready), (worker.video_ready, self._video_ready),
                             (worker.failed, self._failed), (worker.step, self._preview_step),
                             (worker.finished, self._preview_finished)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                log.debug("Preview signal was already disconnected")
        if worker.isRunning():
            # The dialog can close immediately. The application owns the live thread until it finishes;
            # a decoder that cannot be interrupted must never be waited on by the UI thread.
            worker.setParent(QApplication.instance())
            _background_previews.add(worker)
            worker.finished.connect(lambda: _release_preview(worker), Qt.QueuedConnection)
            if not worker.isRunning():    # finished between the check and connecting cleanup
                _release_preview(worker)
        else:
            worker.discard_video()
        self.worker = None

    def done(self, result: int) -> None:
        self._cancel_preview()
        super().done(result)
