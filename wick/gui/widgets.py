"""Reusable UI components. Sizes, spacing and colors come only from style.py scales."""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QDoubleValidator, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLayout, QProgressBar, QPushButton,
    QScrollArea,
    QSizePolicy, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QToolButton, QVBoxLayout, QWidget, QWidgetItem,
)

from .style import Radius, Size, Space, Type, icon, icon_pixmap, keyboard_focus, text_scale, tokens, ui_font


# ---------------------------------------------------------------- text

def _might_be_rich(value: str) -> bool:
    from PySide6 import QtGui

    return bool(QtGui.Qt.mightBeRichText(value))


class WrapLabel(QLabel):
    """Wrapped text that keeps the height its lines need: a layout short of room shrinks something else
    (a list) instead of cutting sentences off mid-line."""

    def __init__(self, content: str = "", parent=None):
        super().__init__(content, parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)   # extra height (if any) goes below, never above
        policy = self.sizePolicy()
        policy.setVerticalPolicy(QSizePolicy.Fixed)     # exactly its lines' height: spare room goes elsewhere
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def _lines_height(self, width: int) -> int:
        """The height the text needs at ``width``. (QLabel's own heightForWidth never goes below the minimum
        height, which _keep_lines sets: measured with it, a label laid out narrow once would stay tall.)
        Remembered per width, text and font: a layout asks the same question many times while it settles."""
        m = self.contentsMargins()
        key = (width, self.text(), self.font().key(), m.left(), m.right(), m.top(), m.bottom(), self.margin())
        cache = self.__dict__.setdefault("_hfw", {})
        hit = cache.get(key)
        if hit is not None:
            return hit
        rect = self.fontMetrics().boundingRect(0, 0, max(1, width - m.left() - m.right()), 100_000,
                                               int(self.alignment()) | Qt.TextWordWrap, self._plain())
        height = rect.height() + m.top() + m.bottom() + 2 * self.margin()
        if len(cache) > 64:
            cache.clear()
        cache[key] = height
        return height

    def _plain(self) -> str:
        if self.textFormat() == Qt.PlainText or not _might_be_rich(self.text()):
            return self.text()
        from PySide6.QtGui import QTextDocument

        doc = QTextDocument()
        doc.setHtml(self.text())
        return doc.toPlainText()

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        if self.textFormat() == Qt.RichText or _might_be_rich(self.text()):
            return super().heightForWidth(width)          # (styled text: Qt measures it; it isn't ratcheted
        return self._lines_height(width)                  #  here as the minimum is reset before measuring)

    def sizeHint(self) -> QSize:  # noqa: N802
        hint = super().sizeHint()
        if self.width() > 0:                  # its height at the width it really has, not a guessed one
            return QSize(hint.width(), self.heightForWidth(self.width()))
        return hint

    def _keep_lines(self) -> None:
        if self.width() > 0:
            old = self.minimumHeight()
            self.setMinimumHeight(0)              # measure the text itself, not the last minimum
            need = self.heightForWidth(self.width())
            if need > 0 and need != old:
                self.setMinimumHeight(need)
                self.updateGeometry()
            else:
                self.setMinimumHeight(old)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._keep_lines()

    def setText(self, value: str) -> None:  # noqa: N802
        super().setText(value)
        self._keep_lines()


def text(content: str = "", role: float = Type.BODY, bold: bool = False, secondary: bool = False,
         wrap: bool = False, name: str | None = None) -> QLabel:
    lab = WrapLabel(content) if wrap else QLabel(content)
    lab.setFont(ui_font(role, QFont.DemiBold if bold else QFont.Normal))
    if secondary:
        lab.setObjectName("secondary")
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    lab.setTextInteractionFlags(Qt.TextSelectableByMouse if wrap else Qt.NoTextInteraction)
    return lab


class ElidedLabel(QLabel):
    """One line of text that may be long (a file name, a path): shortened to fit, never widening its window;
    the full text is in the tooltip when it doesn't fit."""

    def __init__(self, content: str = "", role: float = Type.BODY, bold: bool = False, secondary: bool = False,
                 mode=Qt.ElideMiddle, parent=None):
        super().__init__(parent)
        self.setFont(ui_font(role, QFont.DemiBold if bold else QFont.Normal))
        if secondary:
            self.setObjectName("secondary")
        self._full, self._mode, self._tip = "", mode, ""
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(content)

    def setText(self, value: str) -> None:  # noqa: N802
        self._full = value or ""
        self.setAccessibleName(self._full)
        self._fit()

    def text(self) -> str:
        return self._full

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.fontMetrics().horizontalAdvance("…") * 4, super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 2, super().sizeHint().height())

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit()

    def setToolTip(self, tip: str) -> None:  # noqa: N802
        """A tooltip of its own (e.g. every folder, one per line); else the full text when it's shortened."""
        self._tip = tip or ""
        self._fit()

    def _fit(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, self._mode, max(0, self.width()))
        super().setText(shown)
        super().setToolTip(self._tip or (self._full if shown != self._full else ""))


def button(label: str, kind: str = "", icon_name: str | None = None, tip: str = "",
           shortcut_hint: str = "") -> QPushButton:
    b = QPushButton(label)
    if kind:
        b.setObjectName(kind)
    if icon_name:
        color = tokens().on_accent if kind == "primary" else None
        b.setIcon(icon(icon_name, color, Size.ICON_S))
        b.setIconSize(QSize(Size.ICON_S, Size.ICON_S))
        b.setProperty("iconName", icon_name)
    full_tip = tip + (f"  ({shortcut_hint})" if shortcut_hint else "")
    if full_tip:
        b.setToolTip(full_tip.strip())
    b.setAccessibleName(label.replace("…", "").replace("&", "") or tip)
    if tip:
        b.setAccessibleDescription(tip)
    b.setCursor(Qt.PointingHandCursor)
    return b


def set_compact(b: QPushButton, compact: bool) -> None:
    """Icon-only button in tight layouts (the label stays available as tooltip/accessible name)."""
    full = b.property("fullText")
    if full is None:
        b.setProperty("fullText", b.text())
        full = b.text()
    b.setText("" if compact and b.property("iconName") else full)


def refresh_icons(root: QWidget) -> None:
    """Re-render icons after a theme change."""
    for b in root.findChildren(QAbstractButton):
        name = b.property("iconName")
        if name:
            color = tokens().on_accent if b.objectName() == "primary" else None
            b.setIcon(icon(name, color, Size.ICON_S))


class FlowLayout(QLayout):
    """Lays groups of widgets left to right and wraps to a new line when the width runs out.

    A group (e.g. a label and its control) always stays on one line; hidden widgets take no space.
    """

    def __init__(self, parent=None, spacing: int = Space.L, line_spacing: int = Space.S, inner: int = Space.S):
        super().__init__(parent)
        self._items: list = []
        self._groups: list[list] = []
        self._spacing, self._line_spacing, self._inner = spacing, line_spacing, inner
        # Sizes measured during one call from Qt (sizeHint, heightForWidth...), so each widget is asked once
        # rather than once per group and line. Not kept between calls: overriding invalidate() to clear it
        # would make Qt call into Python while tearing the window down, which crashes at exit.
        self._cache: dict | None = None
        self.setContentsMargins(0, 0, 0, 0)

    def _measuring(self, fn, *args):
        if self._cache is not None:          # (already inside a call: share its measurements)
            return fn(*args)
        self._cache = {}
        try:
            return fn(*args)
        finally:
            self._cache = None

    # QLayout interface
    def addItem(self, item) -> None:
        self._items.append(item)
        self._groups.append([item])

    def add_group(self, *widgets) -> None:
        group = []
        for w in widgets:
            self.addChildWidget(w)
            item = QWidgetItem(w)
            self._items.append(item)
            group.append(item)
        self._groups.append(group)
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        if not 0 <= index < len(self._items):
            return None
        item = self._items.pop(index)
        for g in self._groups:
            if item in g:
                g.remove(item)
        self._groups = [g for g in self._groups if g]
        return item

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._measuring(self._layout, QRect(0, 0, width, 0), False)

    def setGeometry(self, rect) -> None:
        super().setGeometry(rect)
        self._measuring(self._layout, rect, True)

    def sizeHint(self) -> QSize:
        return self._measuring(self._size, False)

    def minimumSize(self) -> QSize:
        return self._measuring(self._size, True)

    def _size(self, minimum: bool) -> QSize:
        widths = [self._group_width(g) for g in self._visible_groups()]
        w = max(widths, default=0) if minimum else sum(widths) + self._spacing * max(0, len(widths) - 1)
        return QSize(w, self._layout(QRect(0, 0, w, 0), apply=False) if w else 0)

    # internals
    def _visible_groups(self) -> list[list]:
        cache = self._cache if self._cache is not None else {}
        if "groups" not in cache:
            cache["groups"] = [vis for g in self._groups if (vis := [i for i in g if not i.isEmpty()])]
        return cache["groups"]

    def _hint(self, item) -> QSize:
        if self._cache is None:
            return item.sizeHint()
        hints = self._cache.setdefault("hints", {})
        if id(item) not in hints:
            hints[id(item)] = item.sizeHint()
        return hints[id(item)]

    def _group_width(self, group) -> int:
        return sum(self._hint(i).width() for i in group) + self._inner * (len(group) - 1)

    def _layout(self, rect, apply: bool) -> int:
        lines: list[list] = [[]]
        x = 0
        for g in self._visible_groups():
            gw = self._group_width(g)
            if lines[-1] and x + self._spacing + gw > rect.width():
                lines.append([])
                x = 0
            x += (self._spacing if lines[-1] else 0) + gw
            lines[-1].append(g)
        y = rect.y()
        for line in lines:
            if not line:
                continue
            h = max(self._hint(i).height() for g in line for i in g)
            if apply:
                x = rect.x()
                for g in line:
                    for i in g:
                        sh = self._hint(i)
                        i.setGeometry(QRect(x, y + (h - sh.height()) // 2, sh.width(), sh.height()))
                        x += sh.width() + self._inner
                    x += self._spacing - self._inner
            y += h + self._line_spacing
        return max(0, y - rect.y() - self._line_spacing)


def hbox(*items, spacing: int = Space.S, margins: tuple = (0, 0, 0, 0)) -> QHBoxLayout:
    h = QHBoxLayout()
    h.setSpacing(spacing)
    h.setContentsMargins(*margins)
    for it in items:
        if it is None:
            h.addStretch(1)
        elif isinstance(it, QWidget):
            h.addWidget(it)
        else:
            h.addLayout(it)
    return h


# ---------------------------------------------------------------- page structure

CARD_INSET = Space.XS   # 4 px: clears a 12 px rounded corner (12 * 0.29 = 3.5)


class _StatusLine(QLabel):
    """The line under a page title: only there when it has something to say (e.g. "media · 4 files")."""

    def setText(self, value: str) -> None:  # noqa: N802
        super().setText(value)
        self.setVisible(bool(value))


def page_top() -> int:
    """Space above a page's title: below the app's own title bar when it has one."""
    from .winframe import custom_frame

    return Size.TITLE_BAR + Space.S if custom_frame() else Space.XL


class Page(QWidget):
    """A page with a large title, an optional status line, a right-aligned action area and a body."""

    def __init__(self, title: str, subtitle: str = "", parent=None, scrolls: bool = False, edge_to_edge: bool = False):
        """``scrolls``: the body is a scroll area that runs to the window's bottom edge (content scrolls out
        of view there instead of being cut off in mid-air above it). ``edge_to_edge`` keeps the header inset
        while the body reaches the page edges; put the content padding inside that body."""
        super().__init__(parent)
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        side = 0 if edge_to_edge else Space.XXL
        outer.setContentsMargins(side, page_top(), side, 0 if scrolls else Space.XL)
        outer.setSpacing(Space.L)
        head = QHBoxLayout()
        head.setSpacing(Space.S)
        if edge_to_edge:
            head.setContentsMargins(Space.XXL, 0, Space.XXL, 0)
        self.title = text(title, Type.TITLE1, bold=True, name="pageTitle")
        self.title.setAccessibleName(title)
        head.addWidget(self.title, 1, Qt.AlignVCenter)
        self.action_bar = QHBoxLayout()
        self.action_bar.setSpacing(Space.S)
        head.addLayout(self.action_bar)
        outer.addLayout(head)
        self.subtitle = _StatusLine()
        self.subtitle.setObjectName("pageSubtitle")
        self.subtitle.setFont(ui_font(Type.BODY))
        self.subtitle.setTextFormat(Qt.RichText)
        self.subtitle.setWordWrap(True)
        if edge_to_edge:
            self.subtitle.setContentsMargins(Space.XXL, 0, Space.XXL, 0)
        self.subtitle.setText(subtitle)
        outer.addWidget(self.subtitle)
        self.body = QVBoxLayout()
        self.body.setSpacing(Space.L)
        if scrolls:                    # the page brings its own scroll area
            outer.addLayout(self.body, 1)
        else:
            self.body.setContentsMargins(0, 0, 0, 0)
            outer.addWidget(_BodyScroll(self.body), 1)


def available_width(w: QWidget) -> int:
    """The width ``w``'s page really has. Inside a page body (which scrolls rather than squeezing) a widget
    is never narrower than its own minimum, so its width can't tell whether its contents still fit: decide
    from the page's visible width instead."""
    p = w.parentWidget()
    while p is not None and not isinstance(p, _BodyScroll):
        p = p.parentWidget()
    if p is None:
        return w.width()
    m = p.widget().layout().contentsMargins()
    return p.viewport().width() - m.left() - m.right()


class _BodyScroll(QScrollArea):
    """A page's body: fills the page, and scrolls only when its content can't fit (large text, a short
    window) instead of squeezing it until text is cut off or widgets overlap. Lists still stretch."""

    def __init__(self, body: QLayout):
        super().__init__()
        holder = QWidget()
        holder.setObjectName("inspectorBody")      # transparent, like the other scrolling panes
        holder.setLayout(body)
        self.setObjectName("inspectorScroll")
        self.setWidget(holder)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        # The content adapts to the width it's given (rows wrap, toolbars compact); sideways scrolling is only
        # a last resort, never clipping.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # A little room between the content and the scroll bar, only while it shows.
        self.verticalScrollBar().rangeChanged.connect(
            lambda _lo, hi: body.setContentsMargins(0, 0, Space.M if hi > 0 else 0, 0))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        # Never forces the page wider or taller than the window: the content adapts to the real width (code
        # deciding what fits must see that width, see available_width), and it scrolls when it's too tall.
        return QSize(Space.XXL * 4, Space.XXL * 3)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.widget().sizeHint()


class Card(QFrame):
    def __init__(self, parent=None, padding: int = Space.L, spacing: int = Space.M, inset: bool = False):
        """``inset``: for edge-to-edge lists. A small inset keeps square rows and headers inside the rounded
        corners (a child's corner must sit at least radius * (1 - 1/sqrt 2) in from both edges)."""
        super().__init__(parent)
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        m = max(padding, CARD_INSET) if inset else padding
        self.lay.setContentsMargins(m, m, m, m)
        self.lay.setSpacing(spacing)


class Section(QWidget):
    """Collapsible inspector section (keyboard accessible: Tab to the header, Space toggles)."""

    def __init__(self, title: str, content: QWidget, expanded: bool = True, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(Space.XS)
        self.toggle = QToolButton()
        self.toggle.setObjectName("sectionToggle")
        self.toggle.setText(title)
        self.toggle.setFont(ui_font(Type.HEADLINE, QFont.DemiBold))
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setIconSize(QSize(Size.ICON_S - 4, Size.ICON_S - 4))
        self.toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.toggle.setAccessibleName(f"{title} section")
        self.toggle.toggled.connect(self._set)
        self.content = content
        lay.addWidget(self.toggle)
        lay.addWidget(content)
        self._set(expanded)

    def _set(self, on: bool) -> None:
        self.content.setVisible(on)
        self.toggle.setIcon(icon("chevron_down" if on else "chevron", tokens().text2))
        self.toggle.setIconSize(QSize(Size.ICON_S - 4, Size.ICON_S - 4))
        self.toggle.setAccessibleDescription("expanded" if on else "collapsed")

    def refresh(self) -> None:
        self._set(self.toggle.isChecked())


class StatTile(QFrame):
    def __init__(self, label: str, value: str = "—", accent: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Space.L, Space.M, Space.L, Space.M)
        lay.setSpacing(Space.XXS)
        self.label = text(label, Type.FOOTNOTE, bold=True, name="statLabel")
        self.value = text(value, Type.TITLE2, bold=True, name="statValueAccent" if accent else "statValue")
        self.detail = text("", Type.FOOTNOTE, secondary=True)
        for w in (self.label, self.value, self.detail):
            lay.addWidget(w)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAccessibleName(label)

    def set(self, value: str, detail: str = "") -> None:
        self.value.setText(value)
        self.detail.setText(detail or " ")  # keep the line so all tiles stay the same height
        self.setAccessibleDescription(f"{value}. {detail}".strip())


# ---------------------------------------------------------------- drop zone

class DropZone(QFrame):
    """The empty state: a large dashed target with the two ways to add files."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.hover = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Space.XXL, Space.XXL, Space.XXL, Space.XXL)
        lay.setSpacing(Space.M)
        lay.addStretch(1)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.icon)
        self.title = text("Drop photos, videos, music or folders here", Type.TITLE2, bold=True, wrap=True)
        self.title.setAlignment(Qt.AlignCenter)
        self.sub = text("JPEG, PNG, GIF, WebP, HEIC, AVIF, TIFF, BMP and common video and audio formats.",
                        Type.BODY, secondary=True, wrap=True)
        self.sub.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        self.add_folder = button("Choose Folder…", "primary", "folder", "Add a folder", "Ctrl+Shift+O")
        self.add_files = button("Choose Files…", "", "files", "Add individual files", "Ctrl+O")
        lay.addLayout(hbox(None, self.add_files, self.add_folder, None, spacing=Space.M))
        lay.addStretch(1)
        self.setAccessibleName("Drop area")
        self.setAccessibleDescription("Drop files or folders here, or use the Choose buttons")
        self.refresh()

    def refresh(self) -> None:
        self.icon.setPixmap(icon_pixmap("drop", tokens().accent_text, Size.ICON_L, stroke=1.4))

    def set_hover(self, on: bool) -> None:
        self.hover = on
        self.update()

    def paintEvent(self, e) -> None:
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        p.setBrush(QColor(t.accent_soft if self.hover else t.card))
        pen = QPen(QColor(t.accent if self.hover else t.control_border), 1.5, Qt.DashLine)
        pen.setDashPattern([4, 4])
        p.setPen(pen)
        p.drawRoundedRect(r, Radius.LARGE, Radius.LARGE)


# ---------------------------------------------------------------- info button

class InfoPopover(QWidget):
    """A small card that points at what it explains (Fluent's info popover). Opened by a click or the keyboard
    it stays until you click elsewhere or press Esc; opened by hovering (``sticky=False``) it takes no focus and
    its owner closes it when the pointer leaves. Text wraps at a comfortable reading width."""

    BEAK = 7        # the arrow's height
    SHADOW = 10     # room around the card for its shadow

    def __init__(self, body: str, title: str = "", parent=None, sticky: bool = True):
        kind = Qt.Popup if sticky else Qt.ToolTip
        super().__init__(parent, kind | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.sticky = sticky
        if not sticky:
            self.setAttribute(Qt.WA_ShowWithoutActivating)   # hovering mustn't take the keyboard focus
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoMouseReplay)      # clicking the "?" again closes it, rather than reopening it
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAccessibleName(title or "More information")
        self.below = True                           # the card is below its anchor (arrow at the top)
        self._beak_x = 0.0
        lay = QVBoxLayout(self)
        lay.setSpacing(Space.XS)
        if title:
            lay.addWidget(text(title, Type.BODY, bold=True))
        self.body = text(body, Type.FOOTNOTE, wrap=True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.body)
        self._width = int(Space.XXL * 10 * text_scale())          # ~ 50-60 characters a line

    def _margins(self) -> None:
        pad, sh = Space.M, self.SHADOW
        top = sh + pad + (self.BEAK if self.below else 0)
        bottom = sh + pad + (0 if self.below else self.BEAK)
        self.layout().setContentsMargins(sh + pad, top, sh + pad, bottom)

    def show_at(self, anchor: QWidget) -> None:
        """Open next to ``anchor``: below it if there's room on screen, else above; kept on screen sideways."""
        from PySide6.QtCore import QPoint

        screen = (anchor.screen() or QApplication.primaryScreen()).availableGeometry()
        a_top = anchor.mapToGlobal(QPoint(0, 0))
        centre_x = a_top.x() + anchor.width() / 2
        for below in (True, False):
            self.below = below
            self._margins()
            self.setFixedWidth(self._width)
            self.layout().activate()
            h = self.layout().totalHeightForWidth(self._width)
            y = a_top.y() + anchor.height() + Space.XXS - self.SHADOW if below \
                else a_top.y() - h - Space.XXS + self.SHADOW
            if (below and y + h <= screen.bottom()) or (not below and y >= screen.top()):
                break
        self.setFixedHeight(h)
        x = int(centre_x - self._width / 2)
        x = max(screen.left() - self.SHADOW, min(x, screen.right() - self._width + self.SHADOW))
        self._beak_x = centre_x - x
        self.move(x, int(y))
        self.show()
        if self.sticky:
            self.setFocus(Qt.PopupFocusReason)

    def _shape(self) -> QPainterPath:
        sh, b = self.SHADOW, self.BEAK
        card = QRectF(sh, sh + (b if self.below else 0), self.width() - 2 * sh, self.height() - 2 * sh - b)
        path = QPainterPath()
        path.addRoundedRect(card, Radius.CONTROL, Radius.CONTROL)
        bx = max(card.left() + Radius.CONTROL + b, min(self._beak_x, card.right() - Radius.CONTROL - b))
        beak = QPainterPath()
        if self.below:
            beak.moveTo(bx - b, card.top() + 1)
            beak.lineTo(bx, card.top() - b)
            beak.lineTo(bx + b, card.top() + 1)
        else:
            beak.moveTo(bx - b, card.bottom() - 1)
            beak.lineTo(bx, card.bottom() + b)
            beak.lineTo(bx + b, card.bottom() - 1)
        beak.closeSubpath()
        return path.united(beak)

    def paintEvent(self, _e) -> None:  # noqa: N802
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        shape = self._shape()
        for i in range(self.SHADOW, 0, -2):      # a soft shadow: widening, fading outlines
            c = QColor(0, 0, 0, int((0.18 if t.dark else 0.06) * 255 * (1 - i / (self.SHADOW + 1)) / 2))
            p.setPen(QPen(c, i * 2))
            p.drawPath(shape.translated(0, 2))
        p.setPen(QPen(QColor(t.border), 1))
        p.setBrush(QColor(t.card))
        p.drawPath(shape)


class HelpTip(QAbstractButton):
    """A small "?" beside a setting. Hover over it for the explanation in a popover; click it (or press Enter or
    Space) to keep the popover open."""

    HOVER_DELAY_MS = 250      # long enough that passing over the "?" doesn't flash a card

    SIZE = 18

    def __init__(self, help_text: str, title: str = "", parent=None):
        super().__init__(parent)
        self.help_text, self.title = help_text, title
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName(f"More information: {title}" if title else "More information")
        self.setAccessibleDescription(help_text)
        self.clicked.connect(lambda: self.open(sticky=True))   # (clicked passes a bool)
        self._popover: InfoPopover | None = None
        from PySide6.QtCore import QTimer

        self._hover_timer = QTimer(self, singleShot=True, interval=self.HOVER_DELAY_MS)
        self._hover_timer.timeout.connect(lambda: self.open(sticky=False))

    def open(self, sticky: bool = True) -> None:
        from PySide6.QtWidgets import QToolTip

        self._hover_timer.stop()
        if self.is_open():
            if not sticky or self._popover.sticky:
                return
            self._popover.close()           # a click on a hovered "?" keeps the card: reopen it as sticky
        QToolTip.hideText()
        pop = InfoPopover(self.help_text, self.title, self.window(), sticky=sticky)
        pop.destroyed.connect(lambda *_: self.update())
        self._popover = pop
        pop.show_at(self)
        self.update()

    def enterEvent(self, e) -> None:  # noqa: N802
        super().enterEvent(e)
        if not self.is_open():
            self._hover_timer.start()

    def leaveEvent(self, e) -> None:  # noqa: N802
        super().leaveEvent(e)
        self._hover_timer.stop()
        if self.is_open() and not self._popover.sticky:
            self._popover.close()

    def is_open(self) -> bool:
        try:
            return self._popover is not None and self._popover.isVisible()
        except RuntimeError:          # (deleted on close)
            return False

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        super().keyPressEvent(e)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.SIZE, self.SIZE)

    def paintEvent(self, _e) -> None:  # noqa: N802
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        active = self.underMouse() or keyboard_focus(self) or self.is_open()
        color = QColor(t.accent if active else t.text2)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.is_open():
            fill = QColor(t.accent)
            fill.setAlphaF(0.12)
            p.setBrush(fill)
        else:
            p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(t.accent) if active else QColor(t.separator), 2 if keyboard_focus(self) else 1))
        p.drawEllipse(r)
        p.setPen(color)
        p.setFont(ui_font(Type.FOOTNOTE, QFont.DemiBold))
        p.drawText(self.rect(), Qt.AlignCenter, "?")


# ---------------------------------------------------------------- toggle switch

class ToggleSwitch(QAbstractButton):
    """An on/off switch. A real checkable button: Space toggles, screen readers see its state."""

    def __init__(self, accessible_name: str, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName(accessible_name)
        self.setToolTip(accessible_name)

    def sizeHint(self) -> QSize:
        return QSize(Space.XL * 2 - Space.S, Space.XL + 2)

    def paintEvent(self, e) -> None:
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        th = Space.XL - 2
        track = QRectF(2, (h - th) / 2, min(w - 4, th * 1.75), th)
        on = self.isChecked()
        color = QColor(t.accent if on else t.control_border)
        if not self.isEnabled():
            color = QColor(t.neutral_soft)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(track, th / 2, th / 2)
        knob = th - 4
        x = track.right() - knob - 2 if on else track.left() + 2
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QRectF(x, track.top() + 2, knob, knob))
        if keyboard_focus(self):
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(t.accent), 2))
            p.drawRoundedRect(track.adjusted(-2, -2, 2, 2), th / 2 + 2, th / 2 + 2)


# ---------------------------------------------------------------- status pill

STATUS_ROLE = Qt.UserRole + 10      # "done" | "failed" | "skipped" | "running" | "ready" | "excluded"
PROGRESS_ROLE = Qt.UserRole + 11    # 0..1 while running


class StatusDelegate(QStyledItemDelegate):
    """Draws the Status column as a colored pill with a symbol (color is never the only cue)."""

    _SYMBOL = {"done": "check", "failed": "xmark", "skipped": "minus", "running": "bolt",
               "ready": None, "excluded": "minus", "high": None, "medium": None, "low": None}

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index) -> None:
        kind = index.data(STATUS_ROLE)
        if not kind:
            return super().paint(p, option, index)
        t = tokens()
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else None
        txt = opt.text
        opt.text = ""
        if style:
            style.drawControl(QStyle.CE_ItemViewItem, opt, p, opt.widget)  # selection background
        fg, bg = {
            "done": (t.success, t.success_soft), "failed": (t.danger, t.danger_soft),
            "running": (t.accent_text, t.accent_soft), "skipped": (t.text2, t.neutral_soft),
            "excluded": (t.text2, t.neutral_soft), "ready": (t.text2, t.neutral_soft),
            "high": (t.success, t.success_soft), "medium": (t.accent_text, t.accent_soft),
            "low": (t.text2, t.neutral_soft),
        }.get(kind, (t.text2, t.neutral_soft))     # an unknown kind must not break painting the list
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        font = ui_font(Type.FOOTNOTE, QFont.DemiBold if kind in ("done", "failed", "running", "high")
                       else QFont.Normal)
        p.setFont(font)
        fm = p.fontMetrics()
        rect = option.rect.adjusted(Space.S, Space.XS + 2, -Space.S, -(Space.XS + 2))
        symbol = self._SYMBOL[kind]
        icon_w = Size.ICON_S - 4 if symbol else -Space.XS
        max_text = rect.width() - icon_w - Space.S * 2 - Space.XS
        shown = fm.elidedText(txt, Qt.ElideRight, max(0, max_text))
        pill_w = min(rect.width(), fm.horizontalAdvance(shown) + icon_w + Space.S * 2 + Space.XS)
        pill = QRectF(rect.left(), rect.top(), pill_w, rect.height())
        path = QPainterPath()
        path.addRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        p.fillPath(path, QColor(bg))
        frac = index.data(PROGRESS_ROLE)
        if kind == "running" and frac:
            p.save()
            p.setClipPath(path)
            p.fillRect(QRectF(pill.left(), pill.top(), pill.width() * float(frac), pill.height()),
                       QColor(t.accent if not t.dark else t.accent_soft).lighter(170 if not t.dark else 130))
            p.restore()
        if symbol:
            pm = icon_pixmap(symbol, fg, icon_w, stroke=2.4)
            p.drawPixmap(int(pill.left() + Space.S), int(pill.center().y() - icon_w / 2), pm)
        p.setPen(QColor(fg))
        p.drawText(QRectF(pill.left() + Space.S + icon_w + Space.XS, pill.top(), max_text + 2, pill.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, shown)
        p.restore()

    def sizeHint(self, option, index) -> QSize:
        s = super().sizeHint(option, index)
        return QSize(s.width() + Space.XXL, Size.ROW)


# ---------------------------------------------------------------- sidebar


# ---------------------------------------------------------------- progress

class ProgressBar(QProgressBar):
    """A thin bar drawn here rather than by the style: the fill is clipped to the rounded track, so its ends stay
    round at any value (a styled chunk turns square when it's shorter than its corner radius). With no known
    total it shows a segment gliding along, timed by the clock so it never stutters."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtCore import QElapsedTimer, QTimer

        self.setTextVisible(False)
        self.setFixedHeight(Space.XS + 2)
        self._clock = QElapsedTimer()
        self._clock.start()
        self._paused_at: int | None = None
        self._paused_ms = 0
        self._tick = QTimer(self, interval=16, timerType=Qt.PreciseTimer)
        self._tick.timeout.connect(self.update)

    def _busy(self) -> bool:
        return self.maximum() == self.minimum() == 0

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self._sync_timer()

    def hideEvent(self, e) -> None:  # noqa: N802
        super().hideEvent(e)
        self._tick.stop()

    def setRange(self, lo: int, hi: int) -> None:  # noqa: N802
        super().setRange(lo, hi)
        self._sync_timer()

    def setMaximum(self, hi: int) -> None:  # noqa: N802
        super().setMaximum(hi)
        self._sync_timer()

    def _sync_timer(self) -> None:
        if self._busy() and self.isVisible() and self._paused_at is None:
            self._tick.start()
        else:
            self._tick.stop()

    def set_paused(self, paused: bool) -> None:
        if paused and self._paused_at is None:
            self._paused_at = self._clock.elapsed()
        elif not paused and self._paused_at is not None:
            self._paused_ms += self._clock.elapsed() - self._paused_at
            self._paused_at = None
        self._sync_timer()
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        radius = r.height() / 2
        track = QPainterPath()
        track.addRoundedRect(r, radius, radius)
        p.fillPath(track, QColor(t.neutral_soft))
        p.setClipPath(track)
        if self._busy():
            span = r.width() * 0.3
            elapsed = self._clock.elapsed() if self._paused_at is None else self._paused_at
            phase = ((elapsed - self._paused_ms) % 1400) / 1400      # one pass every 1.4 s
            x = -span + (r.width() + span) * phase
            fill = QRectF(x, 0, span, r.height())
        else:
            total = self.maximum() - self.minimum()
            frac = (self.value() - self.minimum()) / total if total > 0 else 0.0
            w = r.width() * max(0.0, min(1.0, frac))
            fill = QRectF(0, 0, max(w, r.height()) if w > 0 else 0, r.height())   # (at least a round dot)
        if fill.width() > 0:
            pill = QPainterPath()
            pill.addRoundedRect(fill, radius, radius)
            p.fillPath(pill, QColor(t.accent))


class SizeInput(QWidget):
    """A file size: a number you type or pick (common values in its list) and a unit (KB, MB, GB). ``changed``
    gives the size in bytes."""

    changed = Signal(int)
    UNITS = (("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3))

    def __init__(self, size: int = 0, presets: tuple[float, ...] = (0, 1, 10, 50, 100, 250, 500), parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(Space.S)
        self.number = QComboBox()
        self.number.setEditable(True)
        self.number.setInsertPolicy(QComboBox.NoInsert)
        for v in presets:
            self.number.addItem(f"{v:g}")
        validator = QDoubleValidator(0, 1_000_000, 2, self.number)
        validator.setNotation(QDoubleValidator.StandardNotation)
        self.number.lineEdit().setValidator(validator)
        self.number.setMinimumContentsLength(5)
        self.unit = QComboBox()
        for name, _factor in self.UNITS:
            self.unit.addItem(name)
        lay.addWidget(self.number)
        lay.addWidget(self.unit)
        self.setFocusProxy(self.number)
        self.setAccessibleName("Size")
        self.set_bytes(size)
        self.number.editTextChanged.connect(lambda _t: self.changed.emit(self.bytes()))
        self.unit.currentIndexChanged.connect(lambda _i: self.changed.emit(self.bytes()))

    def bytes(self) -> int:
        try:
            value = float(self.number.currentText().replace(",", "") or 0)
        except ValueError:
            value = 0.0
        return max(0, int(value * self.UNITS[self.unit.currentIndex()][1]))

    def set_bytes(self, size: int) -> None:
        """Shown in the largest unit it's a whole number of (1.5 MB stays 1536 KB; 0 keeps the unit shown)."""
        index = self.unit.currentIndex()
        if size > 0:
            index = 0
            for i, (_name, factor) in enumerate(self.UNITS):
                if size >= factor and size % factor == 0:
                    index = i
        self.unit.blockSignals(True)
        self.unit.setCurrentIndex(index)
        self.unit.blockSignals(False)
        self.number.setEditText(f"{size / self.UNITS[index][1]:g}")

    def setAccessibleName(self, name: str) -> None:  # noqa: N802 (Qt's name)
        super().setAccessibleName(name)
        self.number.setAccessibleName(name)
        self.number.lineEdit().setAccessibleName(name)
        self.unit.setAccessibleName(f"{name}: unit")

    def setToolTip(self, tip: str) -> None:  # noqa: N802
        super().setToolTip(tip)
        self.number.setToolTip(tip)
        self.unit.setToolTip(tip)

