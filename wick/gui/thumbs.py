"""Hover previews: thumbnails made in the background, shown in a floating card."""

from __future__ import annotations

import io
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .. import tools
from ..formats import AUDIO, VIDEO, human
from .style import Radius, Space, Type, safe_filter, tokens, ui_font

THUMB_PX = 320          # longest edge of a hover thumbnail (logical pixels)
HOVER_DELAY_MS = 350    # how long the pointer must rest on a row
CACHE_SIZE = 96


def make_thumbnail(path: Path, kind: str, max_px: int) -> bytes | None:
    """PNG bytes of a small preview, or None if it can't be decoded. On Windows, Explorer's own cached thumbnail
    first (instant); then our decoders; then Windows making one, for types they can't read."""
    from ..cleanup.common import cloud_only
    from . import winthumbs

    cached = winthumbs.thumbnail(path, max_px, cache_only=True)
    if cached is not None:
        return _png(cached)
    try:
        if cloud_only(path.stat()):       # only in the cloud (OneDrive, iCloud): decoding it would download it
            return None
    except OSError:
        return None
    own = _own_thumbnail(path, kind, max_px)
    if own is None and winthumbs.IS_WINDOWS:
        made = winthumbs.thumbnail(path, max_px, cache_only=False)
        return _png(made) if made is not None else None
    return own


def _own_thumbnail(path: Path, kind: str, max_px: int) -> bytes | None:
    try:
        if kind == AUDIO:  # album art, if the file has any
            ffmpeg = tools.find_tool("ffmpeg")
            if not ffmpeg:
                return None
            proc = tools.run([ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-an", "-frames:v", "1",
                              "-vf", f"scale={max_px}:{max_px}:force_original_aspect_ratio=decrease",
                              "-f", "image2pipe", "-c:v", "png", "-"], timeout=20, check=False, text=False)
            return proc.stdout or None
        if kind == VIDEO or path.suffix.lower() in (".mp4", ".mkv", ".webm"):
            ffmpeg = tools.find_tool("ffmpeg")
            if not ffmpeg:
                return None
            proc = tools.run([ffmpeg, "-v", "error", "-nostdin", "-ss", "1", "-i", str(path), "-frames:v", "1",
                              "-vf", f"scale={max_px}:{max_px}:force_original_aspect_ratio=decrease",
                              "-f", "image2pipe", "-c:v", "png", "-"], timeout=20, check=False, text=False)
            if proc.returncode != 0 or not proc.stdout:  # very short clip: take the first frame
                proc = tools.run([ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-frames:v", "1",
                                  "-vf", f"scale={max_px}:{max_px}:force_original_aspect_ratio=decrease",
                                  "-f", "image2pipe", "-c:v", "png", "-"], timeout=20, check=False, text=False)
            return proc.stdout or None
        if path.suffix.lower() == ".jxl":
            from ..images import decode_for_display

            data = decode_for_display(path)
            img = QImage.fromData(data)
            return _png(img.scaled(max_px, max_px, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        from PIL import Image, ImageOps

        from ..formats import HEIC, register_heif

        if kind == HEIC:   # only then: it loads a large library (and is preloaded by the scan)
            register_heif()
        with Image.open(path) as im:
            if im.format == "JPEG":
                im.draft("RGB", (max_px * 2, max_px * 2))  # fast DCT-domain downscale
            im.seek(0)
            pic = (ImageOps.exif_transpose(im) or im).convert("RGBA")
            pic.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            pic.save(buf, "PNG", compress_level=1)
            return buf.getvalue()
    except Exception:  # noqa: BLE001 - a preview is a nicety, never an error
        return None


def _png(img: QImage) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")       # type: ignore[call-overload]  # (the stubs say bytes; PySide6 6.12 only takes str)
    return bytes(ba.data())


class _Signals(QObject):
    done = Signal(str, bytes)


class _Job(QRunnable):
    def __init__(self, key: str, path: Path, kind: str, max_px: int, signals: _Signals):
        super().__init__()
        self.key, self.path, self.kind, self.max_px, self.signals = key, path, kind, max_px, signals

    def run(self) -> None:
        data = make_thumbnail(self.path, self.kind, self.max_px)
        self.signals.done.emit(self.key, data or b"")


class ThumbnailCache(QObject):
    ready = Signal(str)  # key

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cache: OrderedDict[str, QPixmap | None] = OrderedDict()
        self.pending: set[str] = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.signals = _Signals()
        self.signals.done.connect(self._done)

    @staticmethod
    def key(path: Path) -> str:
        try:
            return f"{path}|{path.stat().st_mtime_ns}"
        except OSError:
            return str(path)

    def get(self, path: Path, kind: str, dpr: float) -> QPixmap | None:
        k = self.key(path)
        if k in self.cache:
            self.cache.move_to_end(k)
            return self.cache[k]
        if k not in self.pending:
            self.pending.add(k)
            self.pool.start(_Job(k, path, kind, int(THUMB_PX * dpr), self.signals))
        return None

    def _done(self, key: str, data: bytes) -> None:
        self.pending.discard(key)
        pm = QPixmap.fromImage(QImage.fromData(data)) if data else None
        if pm is not None and pm.isNull():
            pm = None
        self.cache[key] = pm
        while len(self.cache) > CACHE_SIZE:
            self.cache.popitem(last=False)
        self.ready.emit(key)


class HoverCard(QWidget):
    """Floating preview card (doesn't take focus; hidden on any key, click or scroll)."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Space.M, Space.M, Space.M, Space.M)
        lay.setSpacing(Space.S)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setMinimumSize(QSize(THUMB_PX // 2, THUMB_PX // 3))
        self.title = QLabel()
        self.title.setFont(ui_font(Type.BODY, QFont.DemiBold))
        self.meta = QLabel()
        self.meta.setFont(ui_font(Type.FOOTNOTE))
        lay.addWidget(self.image)
        self.where = QLabel()
        self.where.setFont(ui_font(Type.FOOTNOTE))
        lay.addWidget(self.title)
        lay.addWidget(self.meta)
        lay.addWidget(self.where)

    def set_content(self, pixmap: QPixmap | None, title: str, meta: str, loading: bool = False,
                    where: str = "") -> None:
        t = tokens()
        self.title.setStyleSheet(f"color: {t.text}; background: transparent;")
        self.meta.setStyleSheet(f"color: {t.text2}; background: transparent;")
        self.where.setStyleSheet(f"color: {t.text3}; background: transparent;")
        self.where.setText(self.where.fontMetrics().elidedText(where, Qt.ElideMiddle, THUMB_PX))
        self.where.setVisible(bool(where))
        self.image.setStyleSheet(f"color: {t.text2}; background: transparent;")
        self.image.setVisible(pixmap is not None or loading)
        if pixmap is None:
            self.image.setPixmap(QPixmap())
            self.image.setText("Loading preview…" if loading else "")
        else:
            self.image.setText("")
            self.image.setPixmap(pixmap)
        fm = self.title.fontMetrics()
        self.title.setText(fm.elidedText(title, Qt.ElideMiddle, THUMB_PX))
        self.meta.setText(meta)
        self.adjustSize()

    def paintEvent(self, _e) -> None:
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), Radius.CARD, Radius.CARD)
        p.fillPath(path, QColor(t.card))
        p.setPen(QColor(t.border))
        p.drawPath(path)

    def show_near(self, global_pos: QPoint) -> None:
        screen = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        offset = Space.L
        x = global_pos.x() + offset
        y = global_pos.y() + offset
        if x + self.width() > area.right():
            x = global_pos.x() - offset - self.width()
        if y + self.height() > area.bottom():
            y = global_pos.y() - offset - self.height()
        self.move(max(area.left(), x), max(area.top(), y))
        self.show()


class HoverPreviewer(QObject):
    """Watches a table's viewport and shows a HoverCard for the row under the pointer."""

    def __init__(self, view, row_info, parent=None, column: int = 0):
        """row_info(index) -> (path, kind, title, meta) or None. The card appears over the name ``column``;
        other columns keep their own tooltips (so there's only ever one popup)."""
        super().__init__(parent or view)
        self.view, self.row_info, self.column = view, row_info, column
        self.enabled = True
        self.card = HoverCard()
        self.cache = ThumbnailCache(self)
        self.cache.ready.connect(self._ready)
        self.timer = QTimer(self, singleShot=True, interval=HOVER_DELAY_MS)
        self.timer.timeout.connect(self._show)
        self.row = -1
        self.index = None
        self.current: tuple | None = None
        view.setMouseTracking(True)
        view.viewport().installEventFilter(self)
        view.installEventFilter(self)
        view.verticalScrollBar().valueChanged.connect(self.hide)

    @safe_filter
    def eventFilter(self, obj, ev):  # noqa: N802
        et = ev.type()
        if et == QEvent.MouseMove and self.enabled:
            idx = self.view.indexAt(ev.position().toPoint())
            if idx.isValid() and idx.column() != self.column:
                idx = idx.siblingAtColumn(-1)   # not over the name: no preview card
            row = (idx.row(), idx.parent().row(), idx.parent().parent().row()) if idx.isValid() else -1
            if row != self.row:
                self.row = row
                from PySide6.QtCore import QPersistentModelIndex

                self.index = QPersistentModelIndex(idx.siblingAtColumn(0)) if idx.isValid() else None
                self.card.hide()
                if row != -1:
                    self.timer.start()
                else:
                    self.timer.stop()
            elif self.card.isVisible():
                self.card.show_near(QCursor.pos())
        elif et == QEvent.ToolTip and self.enabled and obj is self.view.viewport():
            idx = self.view.indexAt(ev.pos())
            if idx.isValid() and idx.column() == self.column:
                return True   # the preview card is this column's popup: no second, separate tooltip
        elif et in (QEvent.Leave, QEvent.MouseButtonPress, QEvent.Wheel, QEvent.KeyPress, QEvent.FocusOut,
                    QEvent.Hide):
            self.hide()
        return False

    def hide(self, *_):
        self.timer.stop()
        self.row = -1
        self.card.hide()

    def _show(self) -> None:
        if self.row == -1 or self.index is None or not self.index.isValid() or not self.view.underMouse():
            return
        info = self.row_info(self.view.model().index(self.index.row(), 0, self.index.parent()))
        if not info:
            return
        path, kind, title, meta = info
        self.current = (self.cache.key(path), title, meta, str(path.parent))
        pm = self.cache.get(path, kind, self.view.devicePixelRatioF())
        if pm is not None:
            pm.setDevicePixelRatio(self.view.devicePixelRatioF())
        loading = pm is None and self.cache.key(path) in self.cache.pending
        self.card.set_content(pm, title, meta if pm is not None or loading else meta + " · no preview", loading,
                              str(path.parent))
        self.card.show_near(QCursor.pos())

    def _ready(self, key: str) -> None:
        if self.current and self.current[0] == key and self.card.isVisible():
            pm = self.cache.cache.get(key)
            if pm is not None:
                pm.setDevicePixelRatio(self.view.devicePixelRatioF())
            self.card.set_content(pm, self.current[1], self.current[2] if pm else self.current[2] + " · no preview",
                                  where=self.current[3])
            self.card.show_near(QCursor.pos())


__all__ = ["HoverPreviewer", "make_thumbnail", "human"]
