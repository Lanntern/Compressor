"""A short, click-through progressive blur at the top of an edge-aligned scroll area."""

from PySide6.QtCore import QEvent, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import QFrame, QScrollArea, QWidget

from . import style
from .style import Space


class _TopTransition(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.levels = []
        self.dpr = 1.0
        self.strength = 1.0
        self.hide()

    def paintEvent(self, event):  # noqa: N802
        if not self.levels or style.high_contrast_active():
            return
        painter = QPainter(self)
        try:
            painter.setOpacity(self.strength)
            # Blend mip levels row by row: strongest blur at the clipped edge, sharp at the bottom.
            for y in range(self.height()):
                amount = (1 - y / max(1, self.height() - 1)) * (len(self.levels) - 1)
                lower = int(amount)
                upper = min(lower + 1, len(self.levels) - 1)
                target = QRectF(0, y, self.width(), 1)
                source = QRectF(0, y * self.dpr, self.levels[0].width(), self.dpr)
                painter.setOpacity(self.strength)
                painter.drawImage(target, self.levels[lower], source)
                if upper != lower:
                    painter.setOpacity(self.strength * (amount - lower))
                    painter.drawImage(target, self.levels[upper], source)
            painter.setOpacity(self.strength)
            fade = QLinearGradient(0, 0, 0, self.height())
            background = QColor(style.tokens().window)
            fade.setColorAt(0, background)
            fade.setColorAt(0.15, background)
            background.setAlpha(175)
            fade.setColorAt(0.55, background)
            background.setAlpha(0)
            fade.setColorAt(1, background)
            painter.fillRect(self.rect(), QBrush(fade))
        finally:
            painter.end()


class EdgeScrollArea(QScrollArea):
    def __init__(self, content):
        super().__init__()
        self.setObjectName("inspectorScroll")
        self.setWidget(content)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.verticalScrollBar().setObjectName("edgeScrollBar")
        self.transition = _TopTransition(self.viewport())
        self._refresh_timer = QTimer(self, singleShot=True, interval=16)
        self._refresh_timer.timeout.connect(self.refresh_transition)
        self.verticalScrollBar().valueChanged.connect(self.schedule_transition)
        self.horizontalScrollBar().valueChanged.connect(self.schedule_transition)
        self.verticalScrollBar().rangeChanged.connect(self.schedule_transition)
        self.viewport().installEventFilter(self)
        for widget in [content, *content.findChildren(QWidget)]:
            widget.installEventFilter(self)

    def _enabled(self):
        return (self.isVisible() and self.verticalScrollBar().value() > self.verticalScrollBar().minimum()
                and not style.high_contrast_active())

    def schedule_transition(self, *_args):
        if not self._enabled():
            self._refresh_timer.stop()
            self.transition.hide()
            self.transition.levels = []
            return
        self.transition.setGeometry(0, 0, self.viewport().width(), min(Space.XXL, self.viewport().height()))
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def refresh_transition(self):
        if not self._enabled():
            self.schedule_transition()
            return
        edge = self.transition
        edge.setGeometry(0, 0, self.viewport().width(), min(Space.XXL, self.viewport().height()))
        if edge.width() < 1 or edge.height() < 1:
            return
        # Capture only the content strip, never the overlay or the whole settings page. No paint-event recursion.
        pixmap = self.widget().grab(QRect(self.horizontalScrollBar().value(), self.verticalScrollBar().value(),
                                         edge.width(), edge.height()))
        image = pixmap.toImage()
        if image.isNull():
            edge.hide()
            return
        edge.dpr = pixmap.devicePixelRatio()
        image.setDevicePixelRatio(1.0)   # source rectangles below are physical pixels, targets are logical pixels
        edge.levels = [image]
        for radius in (2, 4, 8):
            factor = max(1, round(radius * edge.dpr))
            small = image.scaled(max(1, image.width() // factor), max(1, image.height() // factor),
                                 Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            edge.levels.append(small.scaled(image.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
        edge.strength = min(1.0, self.verticalScrollBar().value() / max(1, edge.height()))
        edge.show()
        edge.raise_()
        edge.update()

    def eventFilter(self, obj, event):  # noqa: N802
        if hasattr(self, "_refresh_timer") and event.type() in (
                QEvent.Resize, QEvent.LayoutRequest, QEvent.StyleChange, QEvent.PaletteChange,
                QEvent.MouseButtonRelease, QEvent.KeyRelease, QEvent.FocusIn, QEvent.FocusOut):
            self.schedule_transition()
        return super().eventFilter(obj, event)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.schedule_transition()

    def hideEvent(self, event):  # noqa: N802
        self._refresh_timer.stop()
        self.transition.hide()
        self.transition.levels = []
        super().hideEvent(event)
