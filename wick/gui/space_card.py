"""Home's "Storage overview": the drive as one bar, split into what's on it, with a way to act on each."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QToolTip, QWidget

from .. import space
from ..formats import human
from .style import Space, Type, tokens
from .widgets import Card, FlowLayout, button, text

# The reference categorical palette's first six slots, in fixed order (validated for colour-vision
# differences against the card colour in each mode). "Other" is neutral; free space is the empty track.
_COLORS = {
    False: ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"),
    True: ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"),
}
_ACTIONS = {space.PHOTOS: "Compress", space.VIDEOS: "Compress", space.MUSIC: "Compress",
            space.DOCUMENTS: "Compress", space.CACHES: "Clean Up", space.BIN: "Open"}


def color_of(key: str) -> QColor:
    tk = tokens()
    if key == "other":
        return QColor(tk.text3)
    return QColor(_COLORS[tk.dark][space.ORDER.index(key)])


class StackedBar(QWidget):
    """One horizontal bar: coloured segments in a neutral track (free space), 2 px gaps between them."""

    HEIGHT = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parts: list[tuple[str, str, int]] = []      # (key, label, bytes)
        self.total = 0
        self.setMinimumHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setAccessibleName("Space on the drive")

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(Space.XXL * 8, self.HEIGHT)

    def set_parts(self, parts, total: int) -> None:
        self.parts, self.total = [p for p in parts if p[2] > 0], total
        self.setAccessibleDescription(", ".join(f"{label} {human(size)}" for _k, label, size in self.parts))
        self.update()

    def _rects(self) -> list[tuple[str, str, int, QRectF]]:
        if not self.total:
            return []
        out, x, w = [], 0.0, float(self.width())
        for key, label, size in self.parts:
            width = max(2.0, w * size / self.total)
            out.append((key, label, size, QRectF(x, 0, width, self.height())))
            x += width
        return out

    def paintEvent(self, _e) -> None:
        tk = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.height() / 2
        track = QPainterPath()
        track.addRoundedRect(QRectF(self.rect()), r, r)
        p.fillPath(track, QColor(tk.neutral_soft))
        p.setClipPath(track)                               # the bar's ends stay round
        for key, _label, _size, rect in self._rects():
            p.fillRect(rect.adjusted(0, 0, -2, 0) if rect.width() > 4 else rect, color_of(key))   # 2 px gap

    def mouseMoveEvent(self, e) -> None:
        x = e.position().x()
        for _key, label, size, rect in self._rects():
            if rect.left() <= x < rect.right():
                pct = 100 * size / self.total if self.total else 0
                QToolTip.showText(e.globalPosition().toPoint(), f"{label}: {human(size)} ({pct:.0f}% of the drive)",
                                  self)
                return
        QToolTip.hideText()


def _swatch(color: QColor) -> QLabel:
    size = Space.M
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), 3, 3)
    p.fillPath(path, color)
    p.end()
    lab = QLabel()
    lab.setPixmap(pm)
    lab.setFixedSize(size, size)
    return lab


class SpaceCard(Card):
    """Emits ``act(key)`` for a category's action (Compress, Clean Up, Open)."""

    act = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, padding=Space.L, spacing=Space.S)
        self.setAccessibleName("Storage overview")
        head = text("Storage overview", Type.HEADLINE, bold=True)
        self.summary = text("", Type.FOOTNOTE, secondary=True, wrap=True)
        self.bar = StackedBar()
        self.legend = FlowLayout(spacing=Space.L, line_spacing=Space.XS)
        self.lay.addWidget(head)
        self.lay.addWidget(self.summary)
        self.lay.addSpacing(Space.XS)
        self.lay.addWidget(self.bar)
        self.lay.addLayout(self.legend)
        self.note = text("Photos, videos, music and documents count what's in the folders Home looks through.",
                         Type.FOOTNOTE, secondary=True, wrap=True)
        self.lay.addWidget(self.note)
        self._legend_widgets: list[QWidget] = []
        self.breakdown: space.Breakdown | None = None

    def changeEvent(self, e) -> None:  # noqa: N802
        from PySide6.QtCore import QEvent

        super().changeEvent(e)
        if e.type() in (QEvent.StyleChange, QEvent.PaletteChange) and not getattr(self, "_restyling", False):
            self._restyling = True          # the theme changed: colours of the swatches too
            try:
                if self.breakdown is not None:
                    self.show_breakdown(self.breakdown)
                elif getattr(self, "_drive", None):
                    self.show_drive(*self._drive)
            finally:
                self._restyling = False

    @staticmethod
    def _name(drive: str) -> str:
        return f"{drive} drive" if len(drive) == 2 and drive[1] == ":" else "Main drive" if drive in ("/", "") \
            else drive

    def show_drive(self, drive: str, total: int, free: int) -> None:
        """Before an analysis: only used and free."""
        self.breakdown = None
        self._drive = (drive, total, free)
        self.summary.setText(f"{self._name(drive)} · {human(total - free)} used of {human(total)} · "
                             f"{human(free)} free")
        self.bar.set_parts([("other", "Used", total - free)], total)
        self._legend([])
        self.note.setText("Scan to see what the used space is.")

    def show_breakdown(self, b: space.Breakdown) -> None:
        self.breakdown = b
        self.summary.setText(f"{self._name(b.drive)} · {human(b.used)} used of {human(b.total)} · "
                             f"{human(b.free)} free")
        parts = [(k, space.LABELS[k], b.sizes.get(k, 0)) for k in space.ORDER]
        parts.append(("other", "Everything else", b.other))
        self.bar.set_parts(parts, b.total)
        self._legend([p for p in parts if p[2] > 0])
        self.note.setText("Photos, videos, music and documents count what's in the folders Home looks through.")

    def _legend(self, parts) -> None:
        for w in self._legend_widgets:
            self.legend.removeWidget(w)
            w.deleteLater()
        self._legend_widgets = []
        for key, label, size in parts:
            widgets: list[QWidget] = [_swatch(color_of(key)), text(label, Type.FOOTNOTE),
                       text(human(size), Type.FOOTNOTE, secondary=True)]
            if key in _ACTIONS:
                link = button(_ACTIONS[key], "link", None, self._tip(key))
                link.setAccessibleName(f"{_ACTIONS[key]}: {label}")
                link.clicked.connect(lambda _=False, k=key: self.act.emit(k))
                widgets.append(link)
            self.legend.add_group(*widgets)
            self._legend_widgets += widgets

    @staticmethod
    def _tip(key: str) -> str:
        return {space.CACHES: "Review app caches on Clean Up (nothing is removed until you confirm)",
                space.BIN: "Open it to see what's there (emptying it frees this space)"}.get(
            key, "Review these files on Compress")


__all__ = ["SpaceCard", "StackedBar", "color_of"]
