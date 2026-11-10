"""The header of every list: tokens-drawn, a select-all checkbox, minimum column widths and dragging that
keeps the last column in view."""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractScrollArea, QHeaderView, QWidget

from .style import Size, Space, Type, tokens, ui_font


class _HeaderCap(QWidget):
    """Sits above the list's scroll bar, level with the header, so the bar starts under the column headings and
    the header's look (and its bottom line) runs the full width."""

    def __init__(self, header: "Header"):
        super().__init__()
        self.header = header

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, self.header.height())

    def paintEvent(self, _e) -> None:  # noqa: N802
        t = tokens()
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(t.card))
        p.setPen(QPen(QColor(t.separator), 1))
        p.drawLine(self.rect().bottomLeft(), self.rect().bottomRight())


class Header(QHeaderView):
    """Flat header drawn from tokens: alignment follows the model, the sort arrow sits beside the text."""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setAccessibleName("Column headings")
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setMinimumHeight(Size.ROW)
        self._check = None   # (column, state() -> Qt.CheckState, toggle())
        self._hover_x: int | None = None   # pointer position while over the header (shows resize handles)
        self._hand = False
        self.setMouseTracking(True)
        self._mins: dict[int, int] = {}
        self._clamping = False
        self.keep_in_view = False     # the last column fills the rest; a drag never pushes it out of sight
        self._dragging = False
        self.sectionResized.connect(self._keep_min)
        self.user_hidden: set[int] = set()     # columns the person turned off (fitters never bring them back)
        self._menu_key: str | None = None
        self._optional: list[int] = []

    # ---- choosing columns
    def enable_column_menu(self, key: str, optional: list[int],
                           hidden_by_default: set[int] | frozenset[int] = frozenset()) -> None:
        """Right-click on the headings: tick the ``optional`` columns to show. Remembered under ``key``."""
        from .main_window import open_qsettings

        self._menu_key, self._optional = key, list(optional)
        saved = open_qsettings().value(f"columns/{key}/hidden", None)
        hidden = {int(c) for c in saved} if isinstance(saved, (list, tuple)) else \
            ({int(saved)} if isinstance(saved, (str, int)) and str(saved).lstrip("-").isdigit() else set(hidden_by_default))
        self.user_hidden = {c for c in hidden if c in self._optional}
        view = self.parentWidget()
        for c in self._optional:
            if hasattr(view, "setColumnHidden"):
                view.setColumnHidden(c, c in self.user_hidden)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._column_menu)

    def _column_menu(self, pos) -> None:
        from PySide6.QtWidgets import QMenu

        model = self.model()
        menu = QMenu(self)
        for c in self._optional:
            name = str(model.headerData(c, Qt.Horizontal, Qt.DisplayRole) or "") if model else str(c)
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(c not in self.user_hidden)
            act.toggled.connect(lambda on, col=c: self.set_column_shown(col, on))
        menu.exec(self.mapToGlobal(pos))

    column_choice_changed = None   # (set by the page: called after a column is shown or hidden)

    def set_column_shown(self, column: int, shown: bool) -> None:
        from .main_window import open_qsettings

        if shown:
            self.user_hidden.discard(column)
        else:
            self.user_hidden.add(column)
        view = self.parentWidget()
        if hasattr(view, "setColumnHidden"):
            view.setColumnHidden(column, not shown)
        if self._menu_key:
            open_qsettings().setValue(f"columns/{self._menu_key}/hidden", sorted(self.user_hidden) or [-1])
        self.forget_min_widths()
        if callable(self.column_choice_changed):
            self.column_choice_changed()
        fitter = getattr(view, "_fitter", None)
        if fitter is not None:
            fitter.fit_soon()

    def sizeHint(self) -> QSize:
        s = super().sizeHint()
        return QSize(s.width(), Size.ROW)

    # ---- per-column minimum widths
    SHORT_VALUE = 160    # values up to this wide (sizes, counts, types, dates) are never cut short
    MIN_TEXT = 96        # long text (names, paths, details) can be narrowed to this, then it's elided

    def min_width(self, index: int) -> int:
        """The narrowest ``index`` may get: its heading always fits; short values always show in full."""
        cached = self._mins.get(index)
        if cached is not None:
            return cached
        head = self.sectionSizeFromContents(index).width()
        if self._check is not None and self._check[0] == index:
            head += Size.INDICATOR + Space.L
        floor = max(self.minimumSectionSize(), head)
        view = self.parentWidget()
        content = view.sizeHintForColumn(index) + Space.S if hasattr(view, "sizeHintForColumn") else 0
        m = max(floor, content) if 0 < content <= self.SHORT_VALUE else max(floor, self.MIN_TEXT)
        self._mins[index] = m
        return m

    def forget_min_widths(self) -> None:
        self._mins.clear()

    def _shown(self) -> list[int]:
        return [self.logicalIndex(v) for v in range(self.count()) if not self.isSectionHidden(self.logicalIndex(v))]

    def _keep_min(self, index: int, _old: int, new: int) -> None:
        if self._clamping or self.sectionResizeMode(index) != QHeaderView.Interactive or self.isSectionHidden(index) \
                or new == 0:
            return              # (hiding a column sets its width to 0, before it counts as hidden: not a drag)
        shown = self._shown()
        last = shown[-1] if shown else -1
        if index == last and (self.stretchLastSection() or self.keep_in_view):
            return
        m = self.min_width(index)
        width = max(new, m)
        if self.keep_in_view and self._dragging:
            # Only as far as the last column can still show (at its own minimum).
            others = sum(self.sectionSize(i) for i in shown[:-1] if i != index)
            width = max(m, min(width, self.viewport().width() - others - self.min_width(last)))
        if width != new:
            self._clamping = True
            try:
                self.resizeSection(index, width)
            finally:
                self._clamping = False
        if self.keep_in_view:
            self.fill_last()

    def fill_last(self) -> None:
        """keep_in_view: give the last column exactly the room that's left."""
        shown = self._shown()
        if not shown:
            return
        last = shown[-1]
        want = max(self.min_width(last), self.viewport().width() - sum(self.sectionSize(i) for i in shown[:-1]))
        if self.sectionSize(last) != want:
            self._clamping = True
            try:
                self.resizeSection(last, want)
            finally:
                self._clamping = False

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self.keep_in_view:
            self.fill_last()
        cap = getattr(self, "_cap", None)
        if cap is not None and cap.height() != self.height():
            cap.setFixedHeight(self.height())

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        view = self.parentWidget()
        if getattr(self, "_cap", None) is None and isinstance(view, QAbstractScrollArea) \
                and self.orientation() == Qt.Horizontal:
            self._cap = _HeaderCap(self)
            self._cap.setFixedHeight(max(1, self.height()))
            view.addScrollBarWidget(self._cap, Qt.AlignTop)

    # ---- select-all checkbox
    def set_check(self, column: int, state, toggle) -> None:
        """Draw a tri-state checkbox in ``column``; clicking it calls ``toggle`` instead of sorting."""
        self._check = (column, state, toggle)
        self.setMouseTracking(True)
        self.viewport().update()

    def _check_hit(self, pos) -> bool:
        if self._check is None:
            return False
        col = self._check[0]
        if self.logicalIndexAt(pos) != col:
            return False
        x = pos.x() - self.sectionViewportPosition(col)
        return 0 <= x <= self._row_check_x() + Size.INDICATOR + Space.S

    def mousePressEvent(self, e) -> None:
        self._mins.clear()   # measured afresh for each drag (the rows may have changed)
        self._dragging = e.button() == Qt.LeftButton
        if e.button() == Qt.LeftButton and self._check_hit(e.position().toPoint()):
            self._check[2]()
            self.viewport().update()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        self._dragging = False
        if self._check_hit(e.position().toPoint()):
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseMoveEvent(self, e) -> None:
        pos = e.position().toPoint()
        hand = self._check_hit(pos)
        if hand != self._hand:  # only touch the cursor on change, so Qt's resize cursor still works
            self._hand = hand
            self.setCursor(Qt.PointingHandCursor) if hand else self.unsetCursor()
        self._hover_x = pos.x()
        self.viewport().update()
        super().mouseMoveEvent(e)

    def leaveEvent(self, e) -> None:
        self._hover_x = None
        if self._hand:
            self._hand = False
            self.unsetCursor()
        self.viewport().update()
        super().leaveEvent(e)

    def _resizable(self, index: int) -> bool:
        shown = [self.logicalIndex(v) for v in range(self.count()) if not self.isSectionHidden(self.logicalIndex(v))]
        last = shown[-1] if shown else -1
        return self.sectionResizeMode(index) == QHeaderView.Interactive and index != last

    def _divided(self, index: int) -> bool:
        """A separator follows every column but the checkbox column and the last one."""
        shown = [self.logicalIndex(v) for v in range(self.count()) if not self.isSectionHidden(self.logicalIndex(v))]
        return self.sectionResizeMode(index) != QHeaderView.Fixed and bool(shown) and index != shown[-1]

    def _draw_check(self, p: QPainter, rect) -> int:
        """Paint the checkbox at the row checkboxes' position; returns the width it used."""
        from PySide6.QtCore import QByteArray
        from PySide6.QtSvg import QSvgRenderer

        from .style import assets

        state = self._check[1]()
        key = {Qt.Checked: "box_checked", Qt.PartiallyChecked: "box_mixed"}.get(state, "box")
        with open(assets(tokens())[key], "rb") as f:
            renderer = QSvgRenderer(QByteArray(f.read()))
        size = Size.INDICATOR
        x = rect.left() + self._row_check_x()
        renderer.render(p, QRectF(x, rect.center().y() - size / 2 + 1, size, size))
        return x - rect.left() + size

    def _row_check_x(self) -> int:
        """Where the view draws row checkboxes inside a cell, so the header box lines up with them."""
        view = self.parentWidget()
        if view is None:
            return Space.S
        try:
            from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

            opt = QStyleOptionViewItem()
            opt.initFrom(view)
            opt.rect = QRect(0, 0, Size.ROW * 4, Size.ROW)
            opt.features |= QStyleOptionViewItem.HasCheckIndicator
            r = view.style().subElementRect(QStyle.SE_ItemViewItemCheckIndicator, opt, view)
            if r.isValid() and 0 <= r.left() < Size.ROW:
                return r.left()
        except Exception:  # noqa: BLE001
            pass
        return Space.S

    def paintSection(self, p: QPainter, rect, index: int) -> None:
        if not rect.isValid():
            return
        t = tokens()
        p.save()
        p.fillRect(rect, QColor(t.card))
        p.setPen(QPen(QColor(t.separator), 1))
        p.drawLine(rect.bottomLeft(), rect.bottomRight())
        if self._divided(index):
            # Column separator, always visible; the one under the pointer turns accent (drag to resize).
            on_it = self._resizable(index) and self._hover_x is not None and abs(self._hover_x - rect.right()) <= Space.XS + 1
            p.setPen(QPen(QColor(t.accent if on_it else t.separator), 2 if on_it else 1))
            x = rect.right() - (1 if on_it else 0)
            p.drawLine(x, rect.top() + Space.S, x, rect.bottom() - Space.S)
        model = self.model()
        label = str(model.headerData(index, Qt.Horizontal, Qt.DisplayRole) or "") if model else ""
        align = model.headerData(index, Qt.Horizontal, Qt.TextAlignmentRole) if model else None
        align = Qt.AlignmentFlag(int(align)) if align is not None else (Qt.AlignLeft | Qt.AlignVCenter)
        p.setFont(ui_font(Type.FOOTNOTE, QFont.DemiBold))
        p.setPen(QColor(t.text2))
        fm = p.fontMetrics()
        inner = rect.adjusted(Space.S, 0, -Space.S, 0)
        if self._check is not None and index == self._check[0]:
            inner.setLeft(rect.left() + self._draw_check(p, rect) + Space.S)
        sorted_here = self.isSortIndicatorShown() and self.sortIndicatorSection() == index and label
        arrow_w = Space.M if sorted_here else 0
        text_w = min(fm.horizontalAdvance(label), max(0, inner.width() - arrow_w - Space.XS))
        right = bool(align & Qt.AlignRight)
        if right:
            text_rect = QRectF(inner.right() - text_w, inner.top(), text_w, inner.height())
            arrow_x = text_rect.left() - Space.XS - arrow_w
        else:
            text_rect = QRectF(inner.left(), inner.top(), text_w, inner.height())
            arrow_x = text_rect.right() + Space.XS
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, fm.elidedText(label, Qt.ElideRight, int(text_w) + 1))
        if sorted_here:
            # Drawn as a vector at the screen's resolution (a scaled bitmap looked blurry).
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(t.text2), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            cx, cy, half = arrow_x + arrow_w / 2, rect.center().y() + 0.5, arrow_w * 0.28
            dy = -half / 2 if self.sortIndicatorOrder() == Qt.AscendingOrder else half / 2
            path = QPainterPath()
            path.moveTo(cx - half, cy - dy)
            path.lineTo(cx, cy + dy)
            path.lineTo(cx + half, cy - dy)
            p.drawPath(path)
        p.restore()
