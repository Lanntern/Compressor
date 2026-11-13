"""One Clean Up tool on screen: its options, a scan, the results list and its actions (and the workers that
run them in the background). The tools themselves are defined in cleanup_tools.py."""

from __future__ import annotations

from typing import TYPE_CHECKING

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, cfa
from ..cleanup import actions, appcompress, duplicates
from ..cleanup.common import Candidate, ScanResult, known_folder
from ..formats import classify, human
from ..log import log
from .dialogs import ask, choose, inform
from .filelist import (
    click_to_check,
    ListPlaceholder, SortItem, add_header_checkbox, file_menu, filling, keyboard_list, make_sortable,
    selected_or_clicked, set_sort_keys,
)
from .settings_panel import BIN_NAME
from .style import Size, Space, Type, tokens, ui_font
from .thumbs import HoverPreviewer
from .widgets import Card, ElidedLabel, FlowLayout, ProgressBar, button, hbox, text


if TYPE_CHECKING:
    from .cleanup_tab import CleanupTab

C_NAME, C_WHERE, C_SIZE, C_DETAIL = range(4)
OPEN_ROWS = 30_000     # groups open by themselves up to this many rows (opening 1,000 groups takes ~0.3 s)
ROLE_ITEM = Qt.UserRole + 20


@dataclass
class Tool:
    key: str
    tab: str
    description: str
    default_folders: Callable[[], list[Path]] | None      # None: this tool doesn't take folders
    scan: Callable[["ToolSection", Callable, threading.Event], ScanResult]
    actions: list[tuple[str, str, str]]                    # (action id, button label, kind)
    folder_label: str = "Look in"
    options: Callable[["ToolSection"], list[tuple[str, QWidget]]] = lambda s: []
    note: str = ""
    available: Callable[[], str] = lambda: ""              # returns a reason when unavailable
    group_title: Callable[[str, list[Candidate]], str] = lambda g, items: g
    menu: Callable[["ToolSection", list[Candidate]], list] = lambda s, items: []   # extra right-click entries
    extra: dict = field(default_factory=dict)


def _existing(*names: str) -> list[Path]:
    return [p for p in (known_folder(n) for n in names) if p is not None]


# ---------------------------------------------------------------- workers

class _Scan(QThread):
    progress = Signal(str, int, int)
    partial = Signal(str, object)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancel = threading.Event()
        self._pending: dict[str, list] = {}
        self._seen: set[tuple] = set()
        self._sent = 0.0
        self._lock = threading.RLock()

    def collect(self, key, items) -> None:
        import time

        with self._lock:
            for item in items:
                identity = (key, str(item.path), item.group, item.title)
                if identity not in self._seen:
                    self._seen.add(identity)
                    self._pending.setdefault(key, []).append(item)
            if time.monotonic() - self._sent >= 0.2 or sum(map(len, self._pending.values())) >= 200:
                self.flush()

    def flush(self) -> None:
        import time

        with self._lock:
            for key, items in self._pending.items():
                if items:
                    self.partial.emit(key, list(items))
            self._pending.clear()
            self._sent = time.monotonic()

    def run(self) -> None:
        from .. import power
        from ..cleanup.common import stream_results

        try:
            with power.awake(), stream_results(self.collect):
                result = self.fn(self.progress.emit, self.cancel)
                self.flush()
                self.done.emit(result)
        except Exception as e:  # noqa: BLE001
            self.flush()
            log.exception("Cleanup scan failed")
            self.failed.emit(str(e))


class _Apply(QThread):
    step = Signal(object, int, int)
    line = Signal(str)
    done = Signal(object, object)
    failed = Signal(str)

    def __init__(self, items, action, tool, root, parent=None):
        super().__init__(parent)
        self.items, self.action, self.tool, self.root = items, action, tool, root
        self.cancel = threading.Event()

    def run(self) -> None:
        from .. import power

        try:
            with power.awake():
                outcomes, run_id = actions.apply(self.items, self.action, self.tool, self.root, progress=self.step.emit,
                                                 cancel=self.cancel, on_line=self.line.emit)
        except Exception as e:  # noqa: BLE001
            log.exception("Clean-up stopped")
            self.failed.emit(str(e))
            return
        self.done.emit(outcomes, run_id)


class _Purge(QThread):
    step = Signal(str, int, int)
    done = Signal(int, int, object)
    failed = Signal(str)

    def __init__(self, outcomes, parent=None):
        super().__init__(parent)
        self.outcomes = outcomes
        self.cancel = threading.Event()

    def run(self) -> None:
        try:
            result = actions.empty_from_trash(self.outcomes, self.step.emit, self.cancel)
        except Exception as e:  # noqa: BLE001
            log.exception("Removing from the Trash stopped")
            self.failed.emit(str(e))
            return
        self.done.emit(*result)


# ---------------------------------------------------------------- one tool

_IDLE_SUMMARY = "Scan to see what can be cleaned up."


class ToolSection(QWidget):
    def __init__(self, tool: Tool, page: "CleanupTab"):
        super().__init__()
        self.tool, self.page = tool, page
        self.qs: QSettings = page.win.qsettings
        self.worker: QThread | None = None
        self.result: ScanResult | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(Space.L)

        # ------------------------------------------------ setup card
        setup = Card()
        self.setup = setup
        setup.lay.addWidget(text(tool.description, Type.BODY, wrap=True))
        reason = tool.available()
        if tool.default_folders is not None:
            self.folder_key = f"cleanup/{tool.key}/{'sources' if tool.key == 'appcompress' else 'folders'}"
            saved = self.qs.value(self.folder_key, None)
            if saved is None and tool.key == "appcompress":
                legacy = self.qs.value("cleanup/appcompress/folders", None)
                if legacy:
                    saved = [str(p) for p in appcompress.migrate_sources([Path(p) for p in legacy])]
            self.folders = [Path(p) for p in saved] if saved is not None else tool.default_folders()
            self.folder_label = ElidedLabel("", Type.FOOTNOTE, secondary=True, mode=Qt.ElideRight)   # one line
            add = button("Manage Folders…", "", "folder", "View, add or remove scan sources")
            add.clicked.connect(self._manage_folders)
            reset = button("Reset", "", None, "Go back to the suggested folders")
            reset.clicked.connect(self._reset_folders)
            drives = button("Add Drives…", "", None, "Choose multiple connected drives to scan")
            drives.clicked.connect(self._add_drives)
            if os.name != "nt" or tool.key == "appcompress":
                drives.hide()
            folder_row = hbox(text(tool.folder_label, Type.BODY, bold=True), self.folder_label, add, drives, reset,
                                     spacing=Space.M)
            folder_row.setStretchFactor(self.folder_label, 1)   # the list takes the room
            setup.lay.addLayout(folder_row)
            self.folder_label.setSizePolicy(self.folder_label.sizePolicy().horizontalPolicy(),
                                            self.folder_label.sizePolicy().verticalPolicy())
            self._show_folders()
        else:
            self.folders = []
        opts = tool.options(self)
        self.scan_btn = button("Scan", "primary", "refresh", "Look for things to clean up")
        self.scan_btn.clicked.connect(self.scan)
        options = FlowLayout()        # wraps instead of squeezing (large text, narrow windows)
        for label, w in opts:
            if label:
                lab = text(label, Type.BODY)
                lab.setBuddy(w)
                options.add_group(lab, w)
                w.setAccessibleName(label)
            else:
                options.add_group(w)
        row = QHBoxLayout()
        row.setSpacing(Space.L)
        row.addLayout(options, 1)
        row.addWidget(self.scan_btn, 0, Qt.AlignBottom)
        setup.lay.addLayout(row)
        if tool.note:
            setup.lay.addWidget(text(tool.note, Type.FOOTNOTE, secondary=True, wrap=True))
        if reason:
            setup.lay.addWidget(text(reason, Type.BODY, bold=True, wrap=True))
            self.scan_btn.setEnabled(False)
        lay.addWidget(setup)

        # ------------------------------------------------ progress
        self.progress_card = Card(padding=Space.M, spacing=Space.S)
        self.status = text("", Type.BODY)
        self.stop_btn = button("Stop", "", "stop", "Stop", "Esc")
        self.stop_btn.clicked.connect(self.stop)
        self.bar = ProgressBar()
        self.bar.setTextVisible(False)
        self.progress_card.lay.addLayout(hbox(self.status, None, self.stop_btn))
        self.progress_card.lay.addWidget(self.bar)
        self.progress_card.setVisible(False)
        lay.addWidget(self.progress_card)

        # ------------------------------------------------ results
        results = Card(padding=0, spacing=0, inset=True)
        self.summary = text(_IDLE_SUMMARY, Type.BODY, secondary=True, wrap=True)
        self.summary.setContentsMargins(Space.L, Space.M, Space.L, Space.S)
        self.hide_unsuggested = QCheckBox("Hide not suggested")
        self.hide_unsuggested.setToolTip("Leave out what isn't suggested (each one's reason shows when it's listed)")
        self.hide_unsuggested.setChecked(self.qs.value("cleanup/hide_not_suggested", True, bool))
        self.hide_unsuggested.toggled.connect(self._hide_toggled)
        self.hide_unsuggested.hide()                   # (only when something isn't suggested)
        summary_row = QHBoxLayout()
        summary_row.setContentsMargins(0, 0, Space.L, 0)
        summary_row.addWidget(self.summary, 1)
        summary_row.addWidget(self.hide_unsuggested, 0, Qt.AlignVCenter)
        results.lay.addLayout(summary_row)
        self.tree = QTreeWidget()
        self.tree.setAccessibleName(f"{tool.tab} results")
        self.tree.setHeaderLabels(["Name", "Location", "Current size", "Before compression", "Est. saving",
                                   "Estimate", "Algorithm", "Status", "Notes"] if tool.key == "appcompress" else
                                  ["Name", "Location", "Size", "Details"])
        self.tree.setFrameShape(QFrame.NoFrame)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setStyleSheet(f"QTreeView::item {{ height: {Size.ROW}px; }}")
        h = self.tree.header()
        h.setStretchLastSection(True)
        h.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.tree.setColumnWidth(C_NAME, 280)
        self.tree.setColumnWidth(C_WHERE, 240)
        self.tree.setColumnWidth(C_SIZE, 120 if tool.key == "appcompress" else 96)
        if tool.key == "appcompress":
            for column, width in ((0, 220), (3, 160), (4, 160), (5, 110), (6, 130), (7, 180)):
                self.tree.setColumnWidth(column, width)
            self.tree.setColumnHidden(3, True)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemDoubleClicked.connect(self._open_item)
        self.tree.itemExpanded.connect(self._expand)
        make_sortable(self.tree)
        self.tree.sortByColumn(-1, Qt.AscendingOrder)
        add_header_checkbox(self.tree, C_NAME, self._update_summary)
        self.tree.header().set_expander(C_NAME, self._all_open, lambda: self.open_all(not self._all_open()))
        self.tree.itemExpanded.connect(lambda *_: self.tree.header().viewport().update())
        self.tree.itemCollapsed.connect(lambda *_: self.tree.header().viewport().update())
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        keyboard_list(self.tree, self._menu)
        click_to_check(self.tree, C_NAME)
        self.placeholder = ListPlaceholder(self.tree)
        self.placeholder.show_message("Press Scan to see what can be cleaned up.")
        results.lay.addWidget(self.tree, 1)
        self.hover = HoverPreviewer(self.tree, self._hover_info, self)
        self.hover.enabled = self.qs.value("hover_preview", True, bool)

        bar = QHBoxLayout()
        bar.setContentsMargins(Space.M, Space.S, Space.M, Space.M)
        bar.setSpacing(Space.S)
        self.select_all = button("Select Suggested", "", None, "Select everything that's verified safe")
        self.select_all.clicked.connect(lambda: self._set_all(None))
        self.select_none = button("Select None")
        self.select_none.clicked.connect(lambda: self._set_all(False))
        bar.addWidget(self.select_all)
        bar.addWidget(self.select_none)
        bar.addStretch(1)
        self.action_buttons: dict[str, object] = {}
        self._actions = QWidget()          # beside the selection buttons, or on a line of their own
        al = QHBoxLayout(self._actions)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(Space.S)
        for action_id, label, kind in tool.actions:
            b = button(label, kind)
            b.clicked.connect(lambda _=False, a=action_id: self.run_action(a))
            self.action_buttons[action_id] = b
            al.addWidget(b)
        bar.addWidget(self._actions)
        results.lay.addLayout(bar)
        self._below = hbox(None, margins=(Space.M, 0, Space.M, Space.M))
        results.lay.addLayout(self._below)
        self._bar, self._results = bar, results
        lay.addWidget(results, 1)
        self._update_buttons()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit_bar()

    def _room(self) -> int:
        """The width the page really has (this section itself is kept as wide as its buttons need)."""
        from PySide6.QtWidgets import QScrollArea

        p = self.parentWidget()
        while p is not None and not isinstance(p, QScrollArea):
            p = p.parentWidget()
        if p is None:
            return self.width()
        margins = p.widget().layout().contentsMargins()
        return p.viewport().width() - margins.left() - margins.right()

    def _fit_bar(self) -> None:
        """Never squeeze the buttons: Select None goes first (the list's header checkbox does the same), then
        the actions move to a line of their own."""
        room = self._room()
        if room == getattr(self, "_fitted_room", None) or getattr(self, "_fitting", False):
            return          # (moving the buttons resizes this section: don't chase our own change)
        self._fitted_room, self._fitting = room, True

        def need(beside: bool, none: bool) -> int:
            self.select_none.setVisible(none)
            target = self._bar if beside else self._below
            if target.indexOf(self._actions) < 0:
                target.addWidget(self._actions)
            for layout in (self._bar, self._below, self._actions.layout(), self._results.lay):
                layout.invalidate()          # (each level caches its size: refresh them, inside out)
            return self._results.lay.minimumSize().width()   # the results card, as it would be laid out

        try:
            for beside, none in ((True, True), (True, False), (False, True), (False, False)):
                if need(beside, none) <= room:
                    return
        finally:
            self._results.updateGeometry()   # let the page see the card's new size
            self._fitting = False

    # ---- folders
    def _show_folders(self) -> None:
        from .folders import summary

        label = appcompress.source_label if self.tool.key == "appcompress" else str
        self.folder_label.setText(summary(self.folders, label))
        self.folder_label.setToolTip("\n".join(label(p) for p in self.folders))

    def save_folders(self) -> None:
        self.qs.setValue(self.folder_key, [str(p) for p in self.folders])

    def _manage_folders(self) -> None:
        from .folders import FolderManager
        from PySide6.QtWidgets import QDialog

        is_apps = self.tool.key == "appcompress"
        dialog = FolderManager(self, self.folders, self.tool.default_folders, self.qs,
                               label=appcompress.source_label if is_apps else str,
                               note=("Steam, Epic and GOG library sources expand to individual games during a scan. "
                                     "Other selected folders are treated as individual apps/games." if is_apps else ""))
        if dialog.exec() == QDialog.Accepted:
            self.folders = dialog.paths()
            self.save_folders()
            self._show_folders()

    def _add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Add a folder", str(self.folders[0] if self.folders else Path.home()))
        if d and Path(d) not in self.folders:
            self.folders.append(Path(os.path.normpath(d)))
            self.save_folders()
            self._show_folders()

    def _reset_folders(self) -> None:
        self.folders = self.tool.default_folders() if self.tool.default_folders else []
        self.qs.remove(self.folder_key)
        if self.tool.key == "appcompress":
            self.qs.remove("cleanup/appcompress/folders")
        self._show_folders()

    def _add_drives(self) -> None:
        from ..cleanup.common import available_drives
        from PySide6.QtWidgets import QDialog, QDialogButtonBox

        dialog = QDialog(self)
        dialog.setWindowTitle("Drives to scan")
        layout = QVBoxLayout(dialog)
        layout.addWidget(text("Select drives to add. System folders, links and cloud-only files are skipped.",
                              Type.BODY, wrap=True))
        choices = []
        for drive in available_drives():
            box = QCheckBox(str(drive))
            layout.addWidget(box)
            choices.append((drive, box))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            self.folders += [p for p, box in choices if box.isChecked() and p not in self.folders]
            self.save_folders()
            self._show_folders()

    # ---- scanning
    def busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def scan(self) -> None:
        if self.busy():
            return
        if self.tool.key == "appcompress":
            self._app_scan_options = self.app_snapshot()
        if self.tool.default_folders is not None and not self.folders and self.tool.key != "caches":
            inform(self, "Add a folder to look in first.")
            return
        self.tree.clear()
        self.result = None
        self._scan_complete = False
        self.worker = _Scan(lambda progress, cancel: self.tool.scan(self, progress, cancel), self)
        self.worker.progress.connect(self._progress)
        self.worker.partial.connect(self._partial)
        self.worker.done.connect(self._scanned)
        self.worker.failed.connect(lambda m: inform(self, "The scan stopped", m))
        self.worker.finished.connect(self._finished)
        self._set_busy(True, "Scanning…")
        self.placeholder.show_loading("Scanning…")
        self.summary.setText(_IDLE_SUMMARY)
        self.worker.start()

    def _progress(self, msg: str, done: int, total: int) -> None:
        self.status.setText(msg if len(msg) < 100 else msg[:97] + "…")
        if self.placeholder.loading:
            self.placeholder.show_loading(msg if len(msg) < 70 else msg[:67] + "…")
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
        else:
            self.bar.setRange(0, 0)

    def _scanned(self, result: ScanResult) -> None:
        self._scan_complete = True
        self.result = result
        self._fill(result)
        if result.items:
            self.placeholder.clear()
        else:
            self.placeholder.show_message("Nothing found. " + " ".join(result.notes))

    def _partial(self, key: str, items) -> None:
        if key != self.tool.key or not items or getattr(self, "_scan_complete", False):
            return
        if self.result is None:
            self.result = ScanResult(key)
        self.result.items.extend(items)
        if self.tool.key == "appcompress" and any(c.extra.get("before_first") is not None for c in items):
            self.tree.setColumnHidden(3, False)
        with filling(self.tree):
            for candidate in items:
                row = self._row(candidate)
                row.setFlags(row.flags() & ~Qt.ItemIsUserCheckable)
                self.tree.addTopLevelItem(row)
            self._apply_hidden()
        self.placeholder.clear()
        self.summary.setText(f"{len(self.result.items):,} results so far · still scanning. You can browse now.")
        self._update_buttons()

    def _hide_toggled(self, on: bool) -> None:
        self.qs.setValue("cleanup/hide_not_suggested", on)
        self._apply_hidden()
        self._update_summary()

    def _apply_hidden(self) -> None:
        """Hide (and untick) what isn't suggested while "Hide not suggested" is on; groups left empty go too."""
        hide = self.hide_unsuggested.isChecked()
        any_warned = False
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            rows = [top.child(j) for j in range(top.childCount())] or [top]
            shown = 0
            for it in rows:
                c = it.data(C_NAME, ROLE_ITEM)
                warned = isinstance(c, Candidate) and bool(c.warning) and c.path != getattr(c, "keeper", None)
                any_warned |= warned
                gone = hide and warned
                it.setHidden(gone)
                if gone and it.flags() & Qt.ItemIsUserCheckable:
                    it.setCheckState(C_NAME, Qt.Unchecked)
                shown += not gone and bool(it.flags() & Qt.ItemIsUserCheckable)
            if top.childCount():
                top.setHidden(hide and shown == 0)
        self.tree.blockSignals(False)
        self.hide_unsuggested.setVisible(any_warned)

    def _fill(self, result: ScanResult) -> None:
        if self.tool.key == "appcompress":
            self.tree.setColumnHidden(3, not any(c.extra.get("before_first") is not None for c in result.items))
        with filling(self.tree):
            self._fill_items(result)
            self._apply_hidden()
        if result.rebuild is not None and self.tree.topLevelItemCount() <= 3:
            for i in range(self.tree.topLevelItemCount()):
                self.tree.topLevelItem(i).setExpanded(True)          # (folder sizes: the first level shows)
        self.tree.header().viewport().update()
        self._update_summary()

    def _fill_items(self, result: ScanResult) -> None:
        t = tokens()
        self.tree.clear()
        groups: dict[str, list[Candidate]] = {}
        for c in result.items:
            groups.setdefault(c.group or "", []).append(c)
        bold = ui_font(Type.BODY, QFont.DemiBold)
        parents: list[QTreeWidgetItem] = []
        for g, items in groups.items():
            parent = self.tree.invisibleRootItem()
            if g:
                if self.tool.key == "appcompress":
                    disk = sum(i.extra.get("disk", 0) for i in items)
                    before = (sum(i.extra["before_first"] for i in items)
                              if all(i.extra.get("before_first") is not None for i in items) else None)
                    saving = sum(i.size for i in items)
                    estimated = human(saving) if not any(i.extra.get("estimate_pending") for i in items) else "–"
                    parent = SortItem([self.tool.group_title(g, items), "", human(disk),
                                       human(before) if before is not None else "–", estimated, "", "", "", ""])
                    set_sort_keys(parent, {2: disk, 3: before or 0, 4: saving})
                else:
                    parent = SortItem([self.tool.group_title(g, items), "", human(sum(i.size for i in items)), ""])
                    set_sort_keys(parent, {C_SIZE: sum(i.size for i in items)})
                parent.setFont(C_NAME, bold)
                # The group's box ticks its removable rows (see _sync_groups). Duplicates: the copy that stays
                # has no box and is never ticked along. (Qt's own tri-state shows no box at all when one row
                # inside has none, so it's kept here.)
                parent.setFlags((parent.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsAutoTristate)
                parent.setCheckState(C_NAME, Qt.Unchecked)
                if items[0].keeper is not None:   # duplicates: show the copy that stays
                    try:
                        keep_size = items[0].keeper.stat().st_size   # similar photos differ in size
                    except OSError:
                        keep_size = items[0].size
                    keep = SortItem(parent, [f"Keep: {items[0].keeper.name}", self._where(items[0].keeper.parent),
                                             human(keep_size), "This copy stays"])
                    keep.setFlags(Qt.ItemIsEnabled)
                    keep.setToolTip(C_WHERE, str(items[0].keeper))
                    keep.setForeground(C_DETAIL, QColor(t.success))
                    keep.setData(C_NAME, ROLE_ITEM, Candidate(items[0].keeper, items[0].size, selected=False))
                self.tree.addTopLevelItem(parent)
            for c in items:
                it = self._row(c)
                if parent is self.tree.invisibleRootItem():
                    self.tree.addTopLevelItem(it)
                else:
                    parent.addChild(it)
            if g:
                parents.append(parent)
        # Groups open by themselves, unless there are so many rows that the list would be slow to scroll (then
        # the arrow above the rows opens them all).
        if parents and sum(p.childCount() + 1 for p in parents) <= OPEN_ROWS:
            for parent in parents:
                parent.setExpanded(True)

    def _row(self, c: Candidate) -> QTreeWidgetItem:
        where = ("Docker" if c.extra.get("docker") else
                 self._where(c.path if c.extra.get("location") else c.path.parent))
        detail = c.warning and f"Not suggested: {c.warning}. {c.detail}" or c.detail
        detail_column = C_DETAIL
        if self.tool.key == "appcompress":
            disk, before = c.extra.get("disk", 0), c.extra.get("before_first")
            source = c.extra.get("estimate_source", c.detail)
            estimate = ("Rescan" if c.extra.get("estimate_pending") else
                        "Community" if source.startswith("community") else
                        "Sample" if source.startswith("sample") else "Rough")
            notes = [c.warning] if c.warning else []
            if c.extra.get("admin"):
                notes.append("Needs administrator")
            if c.extra.get("outcome"):
                notes.append(c.extra["outcome"])
            detail = "; ".join(notes)
            saving = "–" if c.extra.get("estimate_pending") else f"{human(c.size)} ({c.size / max(1, disk):.0%})"
            it = SortItem([c.title, str(c.path.parent), human(disk), human(before) if before is not None else "–",
                           saving, estimate, c.extra.get("algorithm", "").upper(), c.extra.get("state", ""), detail])
            set_sort_keys(it, {2: disk, 3: before or 0, 4: c.size})
            it.setToolTip(2, f"Storage allocated on disk. File length: {human(c.extra.get('logical', disk))}. "
                            "These match for many uncompressed files; compressed files can use less disk space.")
            it.setToolTip(3, "Measured before Wick first compressed this folder; older game versions may differ.")
            it.setToolTip(5, "Scan again after changing settings" if c.extra.get("estimate_pending") else source)
            last = c.extra.get("last_algorithm", "")
            it.setToolTip(6, "Selected for the next compression" + (f"; last used: {last.upper()}" if last else ""))
            for column in (3, 4):
                it.setTextAlignment(column, Qt.AlignRight | Qt.AlignVCenter)
            detail_column = 8
        else:
            it = SortItem([c.title, where, human(c.size) if c.size else "–", detail])
            set_sort_keys(it, {C_SIZE: c.size})
        if c.extra.get("locked"):
            it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable)
        else:
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(C_NAME, Qt.Checked if c.extra.get("checked", c.selected) else Qt.Unchecked)
        it.setData(C_NAME, ROLE_ITEM, c)
        it.setToolTip(C_WHERE, str(c.path))
        it.setToolTip(detail_column, detail + (" Right-click to keep this copy instead." if c.keeper else ""))
        it.setTextAlignment(C_SIZE, Qt.AlignRight | Qt.AlignVCenter)
        if c.warning:
            it.setForeground(detail_column, QColor(tokens().warning))
        if c.extra.get("children"):
            it.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)    # (its rows are made when it opens)
        return it

    def _groups(self) -> list[QTreeWidgetItem]:
        """Top-level rows that are groups (duplicates, similar photos...): rows with rows inside."""
        if self.result is None or self.result.rebuild is not None:      # (folder sizes: a tree, not groups)
            return []
        items = (self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount()))
        return [it for it in items if it is not None and it.childCount() and not it.isHidden()]

    def _all_open(self) -> bool | None:
        """Whether every group is open (None: no groups, so no open-all arrow)."""
        groups = self._groups()
        return all(g.isExpanded() for g in groups) if groups else None

    def open_all(self, on: bool = True) -> None:
        self.tree.setUpdatesEnabled(False)
        try:
            for g in self._groups():
                g.setExpanded(on)
        finally:
            self.tree.setUpdatesEnabled(True)
        self.tree.header().viewport().update()

    def _expand(self, it: QTreeWidgetItem) -> None:
        """Folder sizes: make a folder's rows the first time it's opened."""
        c = it.data(C_NAME, ROLE_ITEM)
        if not isinstance(c, Candidate) or it.childCount() or not c.extra.get("children"):
            return
        with filling(self.tree):
            for k in c.extra["children"]():
                it.addChild(self._row(k))
        if not it.childCount():
            it.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)

    def _menu(self, pos) -> None:
        clicked = selected_or_clicked(self.tree, pos)
        items: list[QTreeWidgetItem] = []
        for it in clicked:            # a group row stands for its removable rows
            kids = [it.child(i) for i in range(it.childCount())]
            rows = [it] if isinstance(it.data(C_NAME, ROLE_ITEM), Candidate) else \
                [k for k in kids if k is not None and k.flags() & Qt.ItemIsUserCheckable]
            items += [r for r in rows if r not in items and isinstance(r.data(C_NAME, ROLE_ITEM), Candidate)]
        if not items:
            return
        paths = [it.data(C_NAME, ROLE_ITEM).path for it in items if not it.data(C_NAME, ROLE_ITEM).extra.get("docker")]
        checkable = [it for it in items if it.flags() & Qt.ItemIsUserCheckable]
        extra: list = []
        if checkable and not self.busy():
            def check(state):
                for it in checkable:
                    it.setCheckState(C_NAME, state)
                self._update_summary()
            n = "" if len(checkable) == 1 else f" {len(checkable)}"
            extra = [(f"Select{n}", lambda: check(Qt.Checked)), (f"Deselect{n}", lambda: check(Qt.Unchecked))]  # noqa: E501
        tool_entries = self.tool.menu(self, [it.data(C_NAME, ROLE_ITEM) for it in items]) if not self.busy() else []
        if tool_entries:
            extra += [None, *tool_entries]
        # The page's actions for just these rows, whatever is ticked (exceptions while going through a list).
        acting = [it.data(C_NAME, ROLE_ITEM) for it in checkable]
        if acting and not self.busy():
            what = "This File" if len(acting) == 1 else f"These {len(acting)}"
            extra.append(None)
            for action_id, label, _kind in self.tool.actions:
                if action_id != actions.TAKEOUT:
                    extra.append((f"{label}: {what}", lambda a=action_id: self.run_action(a, only=acting)))
        groups = self._groups()
        if groups:
            extra += [None, ("Open All Groups", lambda: self.open_all(True)),
                      ("Close All Groups", lambda: self.open_all(False))]
        first = items[0].data(C_NAME, ROLE_ITEM)
        if len(items) == 1 and first.keeper is not None and first.path != first.keeper and not self.busy():
            extra += [None, ("Keep This Copy Instead", lambda: self.keep_instead(first))]
        file_menu(self, paths, extra).exec(self.tree.viewport().mapToGlobal(pos))

    def keep_instead(self, c: Candidate) -> None:
        """Duplicates: make ``c`` the copy that stays; the previous one joins the removable copies."""
        if self.result is None:
            return
        self._remember_checks()
        group = [x for x in self.result.items if x.group == c.group]
        swapped = iter(duplicates.switch_keeper(group, c.path))
        self.result.items = [next(swapped) if x.group == c.group else x for x in self.result.items]
        self._fill(self.result)

    def _where(self, path: Path) -> str:
        """Location relative to the chosen folder ("Pictures › Phone backup"), or a shortened path."""
        for root in self.folders:
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            parts = [root.name or str(root), *rel.parts]
            return " › ".join(parts)
        home = Path.home()
        try:
            return "~ › " + " › ".join(path.relative_to(home).parts)
        except ValueError:
            return str(path)

    # ---- selection
    def _items(self, only_checked: bool = False) -> list[tuple[QTreeWidgetItem, Candidate]]:
        out = []
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            it = stack.pop(0)
            if it is None:
                continue
            c = it.data(C_NAME, ROLE_ITEM)
            checked = bool(it.flags() & Qt.ItemIsUserCheckable) and it.checkState(C_NAME) == Qt.Checked
            if isinstance(c, Candidate) and it.flags() & Qt.ItemIsUserCheckable and not it.isHidden():
                if not only_checked or checked:
                    out.append((it, c))
            if checked and isinstance(c, Candidate) and c.is_dir:
                continue                  # (what's inside a chosen folder goes with it: don't count it twice)
            stack.extend(it.child(i) for i in range(it.childCount()))
        return out

    def _set_all(self, value: bool | None) -> None:
        self.tree.blockSignals(True)
        for it, c in self._items():
            on = c.selected if value is None else value
            it.setCheckState(C_NAME, Qt.Checked if on else Qt.Unchecked)
        self.tree.blockSignals(False)
        self._update_summary()

    def _item_changed(self, it: QTreeWidgetItem | None = None, column: int = C_NAME):
        if it is not None and column == C_NAME and it.childCount() and it.flags() & Qt.ItemIsUserCheckable \
                and not isinstance(it.data(C_NAME, ROLE_ITEM), Candidate):
            state = it.checkState(C_NAME)       # a group's box: its removable rows follow
            if state != Qt.PartiallyChecked:
                self.tree.blockSignals(True)
                for i in range(it.childCount()):
                    child = it.child(i)
                    if child.flags() & Qt.ItemIsUserCheckable and not child.isHidden():
                        child.setCheckState(C_NAME, state)
                self.tree.blockSignals(False)
        self._update_summary()

    def _sync_groups(self) -> None:
        """Each group's box shows its removable rows: all ticked, some, or none."""
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            if g is None or not g.childCount() or isinstance(g.data(C_NAME, ROLE_ITEM), Candidate) \
                    or not g.flags() & Qt.ItemIsUserCheckable:
                continue
            states = [g.child(k).checkState(C_NAME) for k in range(g.childCount())
                      if g.child(k).flags() & Qt.ItemIsUserCheckable and not g.child(k).isHidden()]
            want = (Qt.Unchecked if not states or all(x == Qt.Unchecked for x in states) else
                    Qt.Checked if all(x == Qt.Checked for x in states) else Qt.PartiallyChecked)
            if g.checkState(C_NAME) != want:
                g.setCheckState(C_NAME, want)
        self.tree.blockSignals(False)

    def _update_summary(self) -> None:
        if self.result is None:
            return
        self._sync_groups()
        all_items = self._items()
        chosen = [c for _, c in self._items(True)]
        size = sum(c.size for c in chosen)
        total = sum(c.size for _, c in all_items)
        if self.result.rebuild is not None:          # folder sizes: rows inside rows, so no "found" count
            total = sum(c.size for c in self.result.items)
            msg = (f"{human(total)} in {self._names()}. Open a folder to see where its space goes. "
                   f"<b>{len(chosen):,} selected · {human(size)}</b>")
        elif self.tool.key == "appcompress" and self.result.items:
            hidden = sum(c.warning != "" for c in self.result.items) if self.hide_unsuggested.isChecked() else 0
            known_estimates = not any(c.extra.get("estimate_pending") for c in chosen)
            msg = (f"{len(self.result.items):,} folders · <b>{len(chosen):,} selected</b>"
                   + (f" · {hidden:,} not suggested, hidden" if hidden else "")
                   + (f" · Est. saving {human(size)}" if size and known_estimates else ""))
        elif not all_items:
            msg = "Nothing found. " + " ".join(self.result.notes)
        else:
            hidden = sum(1 for c in self.result.items if c.warning) if self.hide_unsuggested.isChecked() else 0
            msg = (f"Found {len(all_items):,} item{'s' if len(all_items) != 1 else ''} ({human(total)}). "
                   f"<b>{len(chosen):,} selected · {human(size)}</b>"
                   + (f" · {hidden:,} not suggested, hidden" if hidden else "")
                   + (f" · {' · '.join(self.result.notes)}" if self.result.notes else ""))
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setText(msg)
        self._update_buttons()

    def _names(self) -> str:
        names = [p.name or str(p) for p in self.folders]
        return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else (names[0] if names else "")

    def _update_buttons(self) -> None:
        chosen = [c for _, c in self._items(True)] if self.result else []
        size = sum(c.size for c in chosen)
        for action_id, label, _kind in self.tool.actions:
            b = self.action_buttons[action_id]
            b.setEnabled(bool(chosen) and not self.busy())
            if action_id == actions.TRASH and any(c.extra.get("docker") or c.extra.get("system") or c.extra.get("vdisk")
                                                  for c in chosen):
                label = "Clean Up"   # Docker's and Windows' own items don't go to the Recycle Bin: don't say so
            estimate = "~" if action_id == actions.COMPACT else ""
            show_size = (chosen and size and action_id != actions.DECOMPACT
                         and not any(c.extra.get("estimate_pending") for c in chosen))
            b.setText(f"{label} ({estimate}{human(size)})" if show_size else label)
        rows = bool(self._items()) if self.result is not None else False    # (nothing found: nothing to select)
        self.select_all.setEnabled(rows and not self.busy())
        self.select_all.setVisible(self.result is None or self.result.rebuild is None)   # (folder sizes: none are)
        self.select_none.setEnabled(rows and not self.busy())

    # ---- actions
    def run_primary(self) -> None:
        """The tool's main action (Ctrl+Return)."""
        primary = next((a for a in self.tool.actions if a[2] == "primary"), None)
        if primary is not None:
            self.run_action(primary[0])

    def run_action(self, action_id: str, only: list[Candidate] | None = None) -> None:
        """Run an action on the ticked rows, or on ``only`` (rows chosen from the right-click menu)."""
        items = only if only is not None else [c for _, c in self._items(True)]
        if not items or self.busy():
            return
        size = sum(c.size for c in items)
        n = len(items)
        if action_id == actions.TAKEOUT:
            from PySide6.QtWidgets import QFileDialog

            start = str(self.qs.value("cleanup/takeout/output", str(Path.home() / "Pictures"), str))
            out = QFileDialog.getExistingDirectory(self, "Merge the photos into", start)
            if not out:
                return
            self.qs.setValue("cleanup/takeout/output", out)
            photos = sum(c.extra.get("photos", 0) for c in items)
            if not ask(self, f"Merge {photos:,} photos and videos into {Path(out).name}?",
                       f"They're copied into {out}, in Google's folders (\"Photos from 2019\", your albums), with "
                       "the date each was taken. Your Takeout files aren't changed, and running it again picks up "
                       "where it stopped. You can remove the merged copy later.", "Merge"):
                return
            for c in items:
                c.extra.update(takeout_out=out, albums=self.takeout_albums.currentData(),
                               edited=self.takeout_edited.currentData())
            root = Path(out)
            self.worker = _Apply(items, action_id, self.tool.key, root, self)
            self.worker.step.connect(self._applied_one)
            self.worker.line.connect(lambda line: self.status.setText(line[-100:]) if line.strip() else None)
            self.worker.done.connect(self._applied)
            self.worker.failed.connect(self._stopped)
            self.worker.finished.connect(self._finished)
            self._set_busy(True, "Merging…")
            self.bar.setRange(0, 0)
            self.worker.start()
            return
        if action_id == actions.RENAME:
            q = (f"Rename {n} file{'s' if n != 1 else ''} to match what {'they are' if n != 1 else 'it is'}?",
                 "Only the extension changes (photo.jpg → photo.png). You can undo this later.", "Rename")
        elif action_id == actions.HARDLINK:
            other_drive = [c for c in items if not c.extra.get("same_volume", True)]
            if other_drive:
                inform(self, f"{len(other_drive)} duplicate(s) are on a different drive than the copy that stays",
                       "Hard links only work within one drive. Those will be skipped.")
            q = (f"Replace {n} duplicate{'s' if n != 1 else ''} with hard links?",
                 "Every file stays where it is, but identical contents are stored only once. Note: editing any "
                 "one of the linked files changes all of them. You can undo this later.", "Replace")
        elif action_id == actions.COMPACT:
            for candidate in items:
                candidate.extra.update(self.app_options(candidate.path))
            admin = any(c.extra.get("admin") for c in items)
            q = (f"Compress {n} folder{'s' if n != 1 else ''}?",
                 "Programs and games keep working; Windows decompresses files on the fly. This can take a while "
                 "for large games. " + ("Windows will ask for administrator permission. " if admin else "")
                 + "You can undo this later.", "Compress")
        elif action_id == actions.DECOMPACT:
            q = (f"Decompress {n} folder{'s' if n != 1 else ''}?",
                 "Files keep the same contents but may take more disk space. Close running games first. "
                 "To compress again afterward, choose an algorithm in Compress Apps.", "Decompress")
        elif any(c.extra.get("vdisk") for c in items):
            from ..cleanup import vdisk

            if vdisk.docker_desktop_running():
                inform(self, "Quit Docker Desktop first",
                       "Compacting needs Docker Desktop fully shut down, not just its window closed: right-click its "
                       "icon by the clock and choose Quit Docker Desktop, then try again.")
                return
            live = vdisk.running()
            stops = (f"Running now: {', '.join(live)}. " if live else "")
            q = ("Shut WSL down and compact its virtual disks?",
                 "This shuts down WSL completely, so everything must be fully shut down first. " + stops +
                 "Every Linux distro stops, with everything running inside it: terminals, servers, databases, "
                 "Docker containers, and VS Code or other editors connected to WSL. Save your work and close them "
                 "before you continue; anything unsaved in them is lost.\n\nNothing inside the disks is "
                 "deleted: only space Linux has already freed goes back to Windows. Windows asks for "
                 "administrator permission once, and compacting large disks can take several minutes.",
                 "Shut Down and Compact")
            if not ask(self, *q, destructive=True):
                return
            q = None
        elif any(c.extra.get("system") for c in items):
            sysi = [c for c in items if c.extra.get("system")]
            files = n - len(sysi)
            q = (f"Clean up {n} item{'s' if n != 1 else ''}?",
                 f"Windows removes {len(sysi)} of them with its own cleanup (Disk Cleanup, DISM). They don't go to "
                 f"the {BIN_NAME} and can't be put back. Windows asks for administrator permission once, and this "
                 "can take several minutes."
                 + (f" {files} other item{'s' if files != 1 else ''} go{'es' if files == 1 else ''} to the "
                    f"{BIN_NAME}." if files else ""), "Clean Up")
        elif any(c.extra.get("docker") for c in items):
            dk = [c for c in items if c.extra.get("docker")]
            files = n - len(dk)
            q = (f"Clean up {n} item{'s' if n != 1 else ''} ({human(size)})?",
                 (f"{files} folder{'s' if files != 1 else ''} go{'es' if files == 1 else ''} to the {BIN_NAME} "
                  "and can be put back. " if files else "")
                 + f"{len(dk)} Docker item{'s' if len(dk) != 1 else ''} "
                 f"({human(sum(c.size for c in dk))}) {'is' if len(dk) == 1 else 'are'} removed by Docker and can't "
                 f"be put back: Docker pulls or builds {'it' if len(dk) == 1 else 'them'} again when a project needs "
                 f"{'it' if len(dk) == 1 else 'them'}.", "Clean Up", )
        else:
            q = (f"Move {n} item{'s' if n != 1 else ''} ({human(size)}) to the {BIN_NAME}?",
                 f"Empty the {BIN_NAME} to delete them for good.",
                 "Move to " + BIN_NAME)
        if q is not None and not ask(self, *q):
            return
        root = self.folders[0] if self.folders else Path.home()
        self.worker = _Apply(items, action_id, self.tool.key, root, self)
        self.worker.step.connect(self._applied_one)
        self.worker.line.connect(lambda line: self.status.setText(line[-100:]) if line.strip() else None)
        self.worker.done.connect(self._applied)
        self.worker.failed.connect(self._stopped)
        self.worker.finished.connect(self._finished)
        self._set_busy(True, "Working…")
        self.bar.setRange(0, n)
        self.worker.start()

    def _applied_one(self, outcome, done: int, total: int) -> None:
        self.bar.setValue(done)
        self.status.setText(f"{outcome.message}: {outcome.item.title}")

    def _remember_checks(self) -> None:
        """Keep the user's ticks when the list is rebuilt (after an action, or a change of kept copy)."""
        for it, c in self._items():
            c.extra["checked"] = it.checkState(C_NAME) == Qt.Checked

    def _applied(self, outcomes, run_id) -> None:
        ok = [o for o in outcomes if o.ok]
        failed = [o for o in outcomes if not o.ok]
        if self.tool.key == "takeout":
            self.page.win.history.refresh()
            if ok:
                out = Path(ok[0].item.extra.get("takeout_out", ""))
                if choose(self, "Your photos are merged.", ok[0].message + f".\n\nThey're in {out}.", "Done",
                          "Show Folder"):
                    from .file_model import open_in_file_manager

                    open_in_file_manager(str(out))
            elif failed:
                inform(self, "The photos couldn't be merged.", failed[0].message)
            return
        done_ids = {id(o.item) for o in ok}
        self._remember_checks()
        if self.tool.key == "appcompress" and self.result is not None:
            for outcome in outcomes:
                item = outcome.item
                if "disk_after" in item.extra:
                    if item.extra.get("before_first") is None:
                        item.extra["before_first"] = item.extra["before_this_run"]
                    item.extra.update(disk=item.extra["disk_after"], checked=False, outcome=outcome.message,
                                      state=("Decompressed" if getattr(self.worker, "action", "") == actions.DECOMPACT else
                                             "Compressed") if outcome.ok else "Check result")
                    item.extra["estimate_pending"] = True
                    item.selected, item.size = False, 0
            self._fill(self.result)
        elif self.result is not None and self.result.rebuild is not None:
            for o in ok:
                if o.item.extra.get("forget"):
                    o.item.extra["forget"]()                     # (sizes above it shrink)
            self._refill_tree()
        elif self.result is not None:
            self.result.items = [c for c in self.result.items if id(c) not in done_ids]
            self._fill(self.result)
        self.page.win.history.refresh()
        if self.tool.key == "appcompress":
            self.page.win._refresh_game_watcher()
            inform(self, "App/game compression finished", "\n".join(f"{o.item.title}: {o.message}" for o in outcomes))
            return
        freed = sum(o.freed for o in ok)
        info = []
        blocked = [o for o in failed if o.blocked]
        others = [o for o in failed if not o.blocked]
        if blocked:
            n_b = f"{len(blocked)} item{'s' if len(blocked) != 1 else ''}"
            info.append(f"{n_b} {'are' if len(blocked) != 1 else 'is'} in folders that Windows protects. "
                        + cfa.HOW)
        if others:
            info.append(f"{len(others)} item(s) were skipped:\n" + "\n".join(
                f"• {o.item.title}: {o.message}" for o in others[:12]))
        if not ok:                           # nothing at all was done: say so, not "Freed 0 B from 0 items"
            title = (f"Nothing was changed: Windows' Controlled folder access blocked {APP_NAME}"
                     if blocked and not others else "Nothing was changed")
            if blocked:
                if choose(self, title, "\n\n".join(info), "OK", "Open Windows Security"):
                    cfa.open_settings()
            else:
                inform(self, title, "\n\n".join(info))
            return
        trashed = [o for o in ok if o.trashed_at]
        now = [o for o in ok if not o.trashed_at]
        if ok and not freed and not trashed:          # (renamed: nothing freed, nothing in the bin)
            inform(self, f"Done: {len(ok)} file{'s' if len(ok) != 1 else ''} renamed.", "\n\n".join(info))
            return
        if not trashed:
            n = f"{len(ok)} item{'s' if len(ok) != 1 else ''}"
            inform(self, f"Freed {human(freed)} from {n}.", "\n\n".join(info))
            return
        if now:
            info.insert(0, f"{human(sum(o.freed for o in now))} was freed right away "
                           f"({len(now)} Docker item{'s' if len(now) != 1 else ''}).")
        freed = sum(o.freed for o in trashed)
        n = f"{len(trashed)} item{'s' if len(trashed) != 1 else ''}"
        # Moving to the bin frees nothing yet: say so, and offer to free exactly this space now.
        info.insert(0, f"The space comes back when they leave the {BIN_NAME}. Until then you can put them back "
                       "from the history. Freeing it now deletes only these items; everything else in the "
                       f"{BIN_NAME} stays.")
        if choose(self, f"Moved {human(freed)} ({n}) to the {BIN_NAME}.", "\n\n".join(info), "Done",
                  f"Free {human(freed)} Now…"):
            self._purge(trashed)

    def _refill_tree(self) -> None:
        """Folder sizes after a removal: the rows again with the new sizes, opened where they were."""
        assert self.result is not None and self.result.rebuild is not None
        opened = set()
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            it = stack.pop()
            if it is not None and it.isExpanded():
                opened.add(str(it.data(C_NAME, ROLE_ITEM).path))
                stack.extend(it.child(i) for i in range(it.childCount()))
        bar = self.tree.verticalScrollBar().value()
        self.result.items = self.result.rebuild()
        self._fill(self.result)
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            it = stack.pop()
            c = it.data(C_NAME, ROLE_ITEM) if it is not None else None
            if isinstance(c, Candidate) and c.is_dir and str(c.path) in opened:
                it.setExpanded(True)
                stack.extend(it.child(i) for i in range(it.childCount()))
        self.tree.verticalScrollBar().setValue(bar)

    def _purge(self, outcomes) -> None:
        size = sum(o.freed for o in outcomes)
        if not ask(self, f"Delete these {len(outcomes)} item{'s' if len(outcomes) != 1 else ''} permanently?",
                   f"This frees {human(size)} now. They can't be put back afterwards; the rest of "
                   f"the {BIN_NAME} is left alone.", "Delete Permanently", destructive=True):
            return
        self.worker = _Purge(outcomes, self)
        self.worker.step.connect(lambda msg, d, t: (self.status.setText(msg[:100]), self.bar.setRange(0, t),
                                                    self.bar.setValue(d)))
        self.worker.done.connect(self._purged)
        self.worker.failed.connect(self._stopped)
        self.worker.finished.connect(self._finished)
        self._set_busy(True, "Freeing space…")
        self.worker.start()

    def _purged(self, count: int, freed: int, problems) -> None:
        self.page.win.history.refresh()
        title = f"Freed {human(freed)}." if count else "Nothing was freed."
        inform(self, title, ("Not freed:\n" + "\n".join(f"• {p}" for p in problems[:12])) if problems else "")

    def stop(self) -> None:
        if self.busy():
            self.worker.cancel.set()
            self.status.setText("Stopping…")

    def _stopped(self, message: str) -> None:
        from ..log import log_file

        inform(self, "The clean-up stopped", f"{message}\n\nDetails are in the log ({log_file()}).")

    def _finished(self) -> None:
        if self.result is not None and not getattr(self, "_scan_complete", True):
            self._scan_complete = True
            self.result.cancelled = True
            self.result.notes.append("Scan stopped; showing only the results found so far.")
            self._fill(self.result)
        self._set_busy(False)
        if self.placeholder.loading:   # stopped or failed before results
            self.placeholder.show_message("Press Scan to see what can be cleaned up.")

    def _set_busy(self, busy: bool, msg: str = "") -> None:
        self.setup.setEnabled(not busy)
        self.progress_card.setVisible(busy)
        if busy:
            self.status.setText(msg)
            self.bar.setRange(0, 0)
        self.scan_btn.setEnabled(not busy and not self.tool.available())
        self._update_buttons()

    # ---- hover & open
    def _hover_info(self, index):
        it = self.tree.itemFromIndex(index)
        c = it.data(C_NAME, ROLE_ITEM) if it else None
        if not isinstance(c, Candidate) or c.is_dir or not c.path.is_file():
            return None
        kind = classify(c.path)
        if kind is None:
            return None
        return c.path, kind, c.path.name, f"{human(c.path.stat().st_size)} · {c.path.parent}"

    def _open_item(self, it: QTreeWidgetItem, _col: int) -> None:
        c = it.data(C_NAME, ROLE_ITEM)
        if isinstance(c, Candidate):
            from .file_model import open_in_file_manager

            open_in_file_manager(str(c.path))

