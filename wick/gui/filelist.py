"""Shared behavior for file lists: sortable columns, a select-all checkbox in the header,
and the context menu for files."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMenu, QTreeWidget, QTreeWidgetItem, QWidget

from .style import safe_filter

SORT_ROLE = Qt.UserRole + 50   # optional per-cell sort key (numbers, timestamps); otherwise the text sorts

SHOW_IN_FOLDER = ("Show in Explorer" if os.name == "nt" else
                  "Show in Finder" if sys.platform == "darwin" else "Show in Folder")


class SortItem(QTreeWidgetItem):
    """Tree item that sorts by SORT_ROLE when set (sizes, dates), else by text, case-insensitively."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        # Called ~16 times per row when sorting (40,000 rows on Home): keys are kept on the Python side,
        # which is several times faster than reading them back from Qt.
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else 0
        a = getattr(self, "_keys", {}).get(col)
        b = getattr(other, "_keys", {}).get(col)
        if a is not None and b is not None:
            try:
                return a < b
            except TypeError:
                pass
        return self.text(col).casefold() < other.text(col).casefold()


def set_sort_keys(item: QTreeWidgetItem, keys: dict[int, object]) -> None:
    for col, key in keys.items():
        item.setData(col, SORT_ROLE, key)
    item._keys = {**getattr(item, "_keys", {}), **keys}   # (read by SortItem.__lt__)


def make_sortable(tree: QTreeWidget, column: int = 0, order: Qt.SortOrder = Qt.AscendingOrder):
    """Clickable, sortable headers drawn in the app style. Returns the header."""
    from .header import Header

    old = tree.header()
    widths = [old.sectionSize(i) for i in range(old.count())]
    header = Header(tree)
    tree.setHeader(header)
    header.setStretchLastSection(True)
    for i, w in enumerate(widths):  # keep the column widths set before the swap
        header.resizeSection(i, w)
    tree._fitter = ColumnFitter(tree)
    tree._fitter.fit_soon()
    tree.setSortingEnabled(True)
    tree.sortByColumn(column, order)
    return header


@contextmanager
def filling(tree: QTreeWidget):
    """Add many items without re-sorting after each one (and without itemChanged noise)."""
    sorting = tree.isSortingEnabled()
    tree.setSortingEnabled(False)
    tree.blockSignals(True)
    try:
        yield
    finally:
        tree.blockSignals(False)
        tree.setSortingEnabled(sorting)
        fitter = getattr(tree, "_fitter", None)
        if fitter is not None:
            fitter.fit_soon()


class ColumnFitter(QObject):
    """Sizes columns to their content when the view is wide enough (full names and paths), and shares the
    space fairly when it isn't. Stops once the user drags a column themselves."""

    MIN = 64
    MAX = 720

    def __init__(self, tree: QTreeWidget):
        super().__init__(tree)
        self.tree = tree
        self.user_sized = False
        self.optional: list[int] = []   # columns to leave out (in this order) rather than cut the others short
        self.last_min = 0               # ...and the room the last column should keep before they're left out
        self._dropped: set[int] = set()
        self._fitting = False
        self._timer = QTimer(self, singleShot=True, interval=0)
        self._timer.timeout.connect(self.fit)
        tree.viewport().installEventFilter(self)
        tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # columns are fitted to the width instead
        # Cells filled in later (metadata read in the background) refit once updates pause: each change
        # restarts the timer, so a running job's status updates cost nothing (a fit is ~30 ms at 3,000 rows).
        self._later = QTimer(self, singleShot=True, interval=500)
        self._later.timeout.connect(self.fit)
        tree.model().dataChanged.connect(lambda *_: self._later.start())
        tree.header().sectionResized.connect(self._resized)
        tree.itemExpanded.connect(lambda *_: self.fit_soon())

    def _resized(self, *_):
        if not self._fitting and QApplication.mouseButtons() & Qt.LeftButton:
            self.user_sized = True  # the user is dragging a column edge: their widths, scroll if needed
            self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    @safe_filter
    def eventFilter(self, obj, ev):  # noqa: N802
        if ev.type() == QEvent.Resize:
            self.fit_soon()
        return False

    def _share(self, want: dict, available: int) -> dict:
        """Water-fill: narrow columns keep their size; wide ones share what's left equally."""
        if sum(want.values()) <= available:
            return dict(want)
        cap, rest = available, sorted(want.values())
        for i, w in enumerate(rest):
            share = (available - sum(rest[:i])) / (len(rest) - i)
            if w > share:
                cap = max(self.MIN, int(share))
                break
        return {c: min(w, cap) for c, w in want.items()}

    def fit_soon(self) -> None:
        self._timer.start()

    def fit(self) -> None:
        tree = self.tree
        if self.user_sized:      # (an empty list is fitted too: its headings must all show)
            return
        off = getattr(tree.header(), "user_hidden", set())        # turned off by the person: never brought back
        cols = [c for c in range(tree.columnCount())
                if c not in off and (not tree.isColumnHidden(c) or c in self._dropped)]
        if len(cols) < 2:
            return
        drop: list[int] = []
        for c in [None, *self.optional]:     # leave out optional columns only while the rest don't fit
            if c is not None:
                if c not in cols:
                    continue
                drop.append(c)
            fitted, too_wide = self._fitted([c for c in cols if c not in drop])
            if not too_wide:
                break
        for c in self.optional:
            if c in cols:
                tree.setColumnHidden(c, c in drop)
        self._dropped = set(drop)
        cols = [c for c in cols if c not in drop]
        tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded if too_wide else Qt.ScrollBarAlwaysOff)
        self._fitting = True
        try:
            for c in cols[:-1]:  # the last column stretches into what's left
                tree.header().resizeSection(c, fitted[c])
        finally:
            self._fitting = False

    def _fitted(self, cols: list[int]) -> tuple[dict, bool]:
        """The widths ``cols`` get in the view's width, and whether they still don't fit (at their minimums)."""
        tree, header = self.tree, self.tree.header()
        want = {}
        for c in cols:
            content = tree.sizeHintForColumn(c) if tree.topLevelItemCount() else 0
            head = header.sectionSizeFromContents(c).width()
            if header._check is not None and header._check[0] == c:
                head += 32
            want[c] = max(self.MIN, min(self.MAX, max(content, head) + 8))
        available = tree.viewport().width() - 2
        fitted = dict(want)
        if sum(want.values()) > available:
            first, others = cols[0], cols[1:]
            # The name column keeps its full width if the others can make room (down to MIN each);
            # otherwise everything shares the space fairly.
            room = available - want[first]
            fitted = {first: want[first], **self._share({c: want[c] for c in others}, room)} \
                if room >= self.MIN * len(others) else self._share(want, available)
        mins = getattr(header, "min_width", None)
        if mins is not None:   # never below a column's minimum (short values are never cut short)
            header.forget_min_widths()
            fitted = {c: max(w, mins(c)) for c, w in fitted.items()}
        last_min = max(self.MIN, mins(cols[-1])) if mins is not None else self.MIN
        if self.optional and any(c in cols for c in self.optional):
            last_min = max(last_min, self.last_min)
        return fitted, sum(fitted[c] for c in cols[:-1]) + last_min > available


_CHECKABLE = Qt.ItemIsUserCheckable.value | Qt.ItemIsEnabled.value
_TRISTATE = Qt.ItemIsAutoTristate.value


def checkable_items(tree: QTreeWidget, column: int = 0) -> list[QTreeWidgetItem]:
    out: list[QTreeWidgetItem] = []
    stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        it = stack.pop()
        if it is None:
            continue
        # (Flags as plain integers: Python's enum arithmetic made this the slowest part of a 40,000-row list.)
        flags, kids = it.flags().value, it.childCount()
        if flags & _CHECKABLE == _CHECKABLE and not it.isHidden():
            if not (flags & _TRISTATE and kids):  # group rows follow their children
                out.append(it)
        if kids:
            stack.extend(it.child(i) for i in range(kids))
    return out


def add_header_checkbox(tree: QTreeWidget, column: int = 0, on_change: Callable[[], None] | None = None) -> None:
    """A tri-state select-all checkbox in the header of ``column``, kept in sync with the rows."""
    header = tree.header()

    def state() -> Qt.CheckState:
        items = checkable_items(tree, column)
        if not items:
            return Qt.Unchecked
        checked = Qt.Checked
        n = sum(1 for it in items if it.checkState(column) == checked)
        return Qt.Checked if n == len(items) else Qt.Unchecked if n == 0 else Qt.PartiallyChecked

    def toggle() -> None:
        target = Qt.Unchecked if state() == Qt.Checked else Qt.Checked
        tree.blockSignals(True)
        for it in checkable_items(tree, column):
            it.setCheckState(column, target)
        tree.blockSignals(False)
        tree.viewport().update()
        if on_change:
            on_change()

    header.set_check(column, state, toggle)
    tree.itemChanged.connect(lambda *_: header.viewport().update())


class ListPlaceholder(QWidget):
    """Shown over an empty list: a spinner and a message while it's being filled ("Looking through your
    folders… 1,234 files"), or just a message when there's nothing to show."""

    def __init__(self, view):
        super().__init__(view.viewport())
        self.view = view
        self.message = ""
        self.loading = False
        from PySide6.QtCore import QElapsedTimer

        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        # Driven by the clock, not by counting ticks: when the app is briefly busy the ring keeps its pace
        # instead of slowing down and catching up in jumps.
        self.clock = QElapsedTimer()
        self.clock.start()
        self.timer = QTimer(self, interval=16, timerType=Qt.PreciseTimer)
        self.timer.timeout.connect(self._spin)
        view.viewport().installEventFilter(self)
        self.hide()

    def show_loading(self, message: str) -> None:
        self.loading, self.message = True, message
        self.timer.start()
        self._place()

    def show_message(self, message: str) -> None:
        self.loading, self.message = False, message
        self.timer.stop()
        self._place()

    def clear(self) -> None:
        self.loading = False
        self.timer.stop()
        self.hide()

    def _place(self) -> None:
        self.setGeometry(self.view.viewport().rect())
        self.show()
        self.raise_()
        self.update()

    def _ring_rect(self):
        from PySide6.QtCore import QRect

        from .style import Space

        r = Space.L + 3
        cx, cy = self.width() // 2, self.height() // 2 - Space.L
        return QRect(cx - r, cy - r, 2 * r, 2 * r)

    def _spin(self) -> None:
        self.update(self._ring_rect())          # only the ring: the message doesn't change 60 times a second

    @safe_filter
    def eventFilter(self, obj, ev):  # noqa: N802
        if ev.type() == QEvent.Resize and not self.isHidden():   # also while its page isn't shown yet
            self.setGeometry(self.view.viewport().rect())
        return False

    def paintEvent(self, _e) -> None:
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QPainter, QPen

        from .style import Space, Type, tokens, ui_font

        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2 - Space.L
        if self.loading:
            import math

            r = Space.L
            ms = self.clock.elapsed()
            angle = (ms * 0.36) % 360                       # one turn a second
            sweep = 70 + 110 * (0.5 - 0.5 * math.cos(ms / 1300 * 2 * math.pi))   # the arc grows and shrinks
            p.setPen(QPen(QColor(t.separator), 3))
            p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
            p.setPen(QPen(QColor(t.accent), 3, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), int(-angle * 16), int(sweep * 16))
            cy += r + Space.L
        p.setPen(QColor(t.text2))
        p.setFont(ui_font(Type.BODY))
        p.drawText(QRectF(Space.L, cy - Space.S, self.width() - 2 * Space.L, Space.XXL * 2),
                   Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, self.message)


def set_all_checked(tree: QTreeWidget, on: bool, column: int = 0, on_change: Callable[[], None] | None = None) -> None:
    tree.blockSignals(True)
    for it in checkable_items(tree, column):
        it.setCheckState(column, Qt.Checked if on else Qt.Unchecked)
    tree.blockSignals(False)
    tree.viewport().update()
    tree.header().viewport().update()
    if on_change:
        on_change()


class _KeyboardList(QObject):
    """Keyboard use of a list: the context menu opens for the current row (Menu key, Shift+F10), and tabbing
    into the list highlights a row so it's clear where the focus is."""

    def __init__(self, view, open_menu: Callable):
        super().__init__(view)
        self.view, self.open_menu = view, open_menu
        view.viewport().installEventFilter(self)
        view.installEventFilter(self)
        for seq in ("Shift+F10", QKeySequence(Qt.Key_Menu)):
            sc = QShortcut(QKeySequence(seq), view)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(self.menu_for_current)

    def menu_for_current(self) -> None:
        idx = self.view.currentIndex()
        if not idx.isValid():
            return
        self.view.scrollTo(idx)
        rect = self.view.visualRect(idx)
        self.open_menu(QPoint(min(rect.center().x(), self.view.viewport().width() // 2), rect.center().y()))

    @safe_filter
    def eventFilter(self, obj, ev):  # noqa: N802
        if ev.type() == QEvent.ContextMenu and ev.reason() == ev.Reason.Keyboard:
            self.menu_for_current()
            return True
        if obj is self.view and ev.type() == QEvent.FocusIn and ev.reason() in (
                Qt.TabFocusReason, Qt.BacktabFocusReason, Qt.ShortcutFocusReason):
            model, sel = self.view.model(), self.view.selectionModel()
            if model is not None and model.rowCount() and sel is not None and not sel.hasSelection():
                idx = self.view.currentIndex()
                if not idx.isValid():
                    idx = model.index(0, 0)
                self.view.setCurrentIndex(idx)
                sel.select(idx, sel.SelectionFlag.ClearAndSelect | sel.SelectionFlag.Rows)
        return False


def format_date(ts: float | None) -> str:
    """2026-11-06 14:05 (ISO order: sorts and reads the same everywhere)."""
    if not ts:
        return ""
    import time

    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def created_time(path) -> float | None:
    """When the file was created (Windows and macOS keep this; most Linux file systems don't report it)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    birth = getattr(st, "st_birthtime", None)
    if birth:
        return float(birth)
    return float(st.st_ctime) if os.name == "nt" else None


class _ClickToCheck(QObject):
    """A plain click anywhere on a row ticks or unticks it, not only a click on the small box. Ctrl/Shift-clicks
    still just select. A click on the box itself is left to Qt (it already toggled: compared before/after)."""

    def __init__(self, view, column: int):
        super().__init__(view)
        self.view, self.column = view, column
        self._before: tuple | None = None
        if isinstance(view, QTreeWidget):
            view.itemPressed.connect(self._tree_pressed)
            view.itemClicked.connect(self._tree_clicked)
        else:
            view.pressed.connect(self._pressed)
            view.clicked.connect(self._clicked)

    def _tree_pressed(self, item, _col) -> None:
        self._before = (item, item.checkState(self.column))

    def _tree_clicked(self, item, _col) -> None:
        before, self._before = self._before, None
        if before is None or before[0] is not item or QApplication.keyboardModifiers() & (Qt.ControlModifier |
                                                                                         Qt.ShiftModifier):
            return
        flags = item.flags()
        if not (flags & Qt.ItemIsUserCheckable and flags & Qt.ItemIsEnabled) or item.checkState(self.column) != before[1]:
            return
        item.setCheckState(self.column, Qt.Unchecked if before[1] == Qt.Checked else Qt.Checked)

    def _pressed(self, index) -> None:
        cell = index.siblingAtColumn(self.column)
        self._before = (index.row(), cell.data(Qt.CheckStateRole))

    def _clicked(self, index) -> None:
        before, self._before = self._before, None
        if before is None or before[0] != index.row() or QApplication.keyboardModifiers() & (Qt.ControlModifier |
                                                                                            Qt.ShiftModifier):
            return
        cell = index.siblingAtColumn(self.column)
        state = cell.data(Qt.CheckStateRole)
        if state is None or not (cell.flags() & Qt.ItemIsUserCheckable) or state != before[1]:
            return
        checked = Qt.CheckState(state) == Qt.Checked if not isinstance(state, Qt.CheckState) else state == Qt.Checked
        self.view.model().setData(cell, Qt.Unchecked if checked else Qt.Checked, Qt.CheckStateRole)


def click_to_check(view, column: int = 0) -> None:
    view._click_to_check = _ClickToCheck(view, column)


def keyboard_list(view, open_menu: Callable) -> None:
    view._keyboard = _KeyboardList(view, open_menu)


# ---------------------------------------------------------------- context menu

def open_file(path: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def copy_paths(paths: Iterable[Path]) -> None:
    QGuiApplication.clipboard().setText("\n".join(str(p) for p in paths))


def file_menu(parent: QWidget, paths: list[Path],
              extra: Iterable[tuple[str, Callable[[], None] | list] | None] = ()) -> QMenu:
    """Open · Show in Explorer · Copy Path, then page-specific actions (None = separator)."""
    from .file_model import open_in_file_manager_many

    m = QMenu(parent)
    existing = [p for p in paths if p.exists()]
    if len(paths) == 1:
        a = m.addAction("Open", lambda: open_file(paths[0]))
        a.setEnabled(bool(existing))
    if paths:   # none: rows that aren't files (Docker images...) get only the page's own actions
        a = m.addAction(SHOW_IN_FOLDER, lambda: open_in_file_manager_many(existing))
        a.setEnabled(bool(existing))
        m.addAction("Copy Path" if len(paths) == 1 else f"Copy {len(paths)} Paths", lambda: copy_paths(paths))
    extra = list(extra)
    while extra and extra[0] is None:
        extra.pop(0)
    if extra:
        if paths:
            m.addSeparator()
        for e in extra:
            if e is None:
                m.addSeparator()
            elif isinstance(e[1], list):            # a submenu: (title, [(label, action) or None...])
                sub = m.addMenu(e[0])
                for x in e[1]:
                    if x is None:
                        sub.addSeparator()
                    else:
                        sub.addAction(x[0], x[1])
            else:
                m.addAction(e[0], e[1])
    return m


def selected_or_clicked(tree: QTreeWidget, pos) -> list[QTreeWidgetItem]:
    """Items a right-click applies to: the selection if the click was on it, else the clicked row."""
    it = tree.itemAt(pos)
    if it is None:
        return []
    if not it.isSelected():
        tree.setCurrentItem(it)
    return tree.selectedItems() or [it]


__all__ = ["ListPlaceholder", "keyboard_list", "set_all_checked", "ColumnFitter", "SORT_ROLE", "SortItem",
           "set_sort_keys", "make_sortable", "filling", "add_header_checkbox", "checkable_items", "file_menu",
           "open_file", "copy_paths", "selected_or_clicked", "SHOW_IN_FOLDER"]
