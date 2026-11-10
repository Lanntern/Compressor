"""Home: quick overview of where space can come from (ranked by saving x confidence)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QTreeWidget, QWidget

from .. import overview
from ..fileops import BIN_NAME
from ..formats import KIND_LABEL, human, type_label
from ..mediainfo import cadence_note, describe
from .dialogs import inform
from .filelist import (
    ListPlaceholder, SortItem, add_header_checkbox, click_to_check, created_time, file_menu, filling, format_date,
    keyboard_list, make_sortable, selected_or_clicked, set_sort_keys,
)
from .style import Size, Space, Type, safe_filter
from .thumbs import HoverPreviewer
from .space_card import SpaceCard
from .widgets import (
    CARD_INSET, STATUS_ROLE, Card, ElidedLabel, Page, ProgressBar, StatTile, StatusDelegate, _BodyScroll, button, hbox,
    text,
)

C_FILE, C_TYPE, C_INFO, C_SIZE, C_SAVE, C_CONF, C_CREATED, C_MODIFIED, C_WHY = range(9)


class _Analyze(QThread):
    progress = Signal(str, int, int)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, roots, settings, parent=None, background: bool = False):
        super().__init__(parent)
        self.roots, self.settings = roots, settings
        self.background = background          # nobody's waiting (monthly check, start-up scan): low priority
        self.cancel = threading.Event()

    def run(self) -> None:
        from .. import power

        from contextlib import nullcontext

        try:
            with power.awake(), (power.background() if self.background else nullcontext()):
                ov = overview.analyze(self.roots, self.settings, self.progress.emit, self.cancel)
                if not ov.cancelled:
                    from .. import space

                    self.progress.emit(f"Measuring app caches and the {BIN_NAME}…", 0, 0)
                    ov.space = space.measure(ov, self.roots, self.cancel)
                self.done.emit(ov)
        except Exception as e:  # noqa: BLE001
            from ..log import log

            log.exception("Analysis stopped")
            self.failed.emit(str(e))


def default_roots() -> list[Path]:
    """Your whole user folder: Pictures, Music, OneDrive, Camera Roll and any folder you made. App data and
    other hidden folders, program and game folders, and rebuildable developer folders are skipped by the scan."""
    return [Path.home()]


class HomeTab(Page):
    analyzed = Signal(object)        # the Overview, once an analysis finishes

    def __init__(self, window):
        super().__init__("Home")
        self.win = window
        self.qs = window.qsettings
        self.worker: _Analyze | None = None
        saved = self.qs.value("home/folders", None)
        self.roots = [Path(p) for p in saved] if saved else default_roots()

        self.analyze_btn = button("Scan", "primary", "refresh", "Look through your folders for files that could be smaller",
                                  "Ctrl+R")
        self.analyze_btn.clicked.connect(self.analyze)
        self.action_bar.addWidget(self.analyze_btn)

        # folders
        fcard = Card(padding=Space.M)
        self.folder_label = ElidedLabel("", Type.BODY, secondary=True, mode=Qt.ElideRight)      # one line
        add = button("Add Folder…", "", "folder", "Include another folder")
        add.clicked.connect(self._add)
        reset = button("Reset", "", None, "Back to your whole user folder")
        reset.clicked.connect(self._reset)
        self.progress = ProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.progress.setMaximumWidth(Space.XXL * 6)
        folder_row = hbox(text("Looking in", Type.BODY, bold=True), self.folder_label, self.progress,
                                 add, reset, spacing=Space.M)
        folder_row.setStretchFactor(self.folder_label, 1)   # the list takes the room
        fcard.lay.addLayout(folder_row)
        self.body.addWidget(fcard)

        # tiles
        self.tile_save = StatTile("Could save", accent=True)
        self.tile_eff = StatTile("Already efficient")
        self.tile_files = StatTile("Analysed")
        self.tile_save.set("—", "Scan to find out")   # (before an analysis: nothing to show yet)
        self.body.addLayout(hbox(self.tile_save, self.tile_eff, self.tile_files, spacing=Space.L))
        self.space_card = SpaceCard()
        self.space_card.act.connect(self._space_action)
        self.body.addWidget(self.space_card)
        self._show_drive()

        # opportunities
        row = QHBoxLayout()
        row.setSpacing(Space.L)
        card = Card(padding=0, spacing=0, inset=True)
        head = hbox(text("Top savings", Type.HEADLINE, bold=True), None, margins=(Space.L, Space.M, Space.L, 0))
        self.select_high = button("Select High Confidence")
        self.select_high.clicked.connect(lambda: self._select(lambda c: c == overview.HIGH))
        self.select_none = button("Select None")
        self.select_none.clicked.connect(lambda: self._select(lambda c: False))
        self.send_btn = button("Compress Selected…", "primary", "compress",
                               "Open the selected files on the Compress page to review and compress")
        self.send_btn.clicked.connect(self._send)
        self._buttons = QWidget()          # beside the title, or on a line of their own when there's no room
        bl = QHBoxLayout(self._buttons)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(Space.S)
        for b in (self.select_high, self.select_none, self.send_btn):
            bl.addWidget(b)
        head.addWidget(self._buttons)
        card.lay.addLayout(head)
        self._below = hbox(None, margins=(Space.L, Space.S, Space.L, 0))
        card.lay.addLayout(self._below)
        self._head, self._head_title = head, head.itemAt(0).widget()
        self._head_card = card
        card.installEventFilter(self)
        self.hint = text("Ranked by how much each file would save.", Type.BODY,
                         secondary=True, wrap=True)
        self.hint.setContentsMargins(Space.L, Space.S, Space.L, Space.S)
        card.lay.addWidget(self.hint)
        self.tree = QTreeWidget()
        self.tree.setAccessibleName("Top savings, ranked by expected saving and confidence")
        self.tree.setHeaderLabels(["File", "Type", "Details", "Size", "Saves", "Confidence", "Created", "Modified", "Why"])
        self.tree.setRootIsDecorated(False)
        self.tree.setFrameShape(QFrame.NoFrame)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setStyleSheet(f"QTreeView::item {{ height: {Size.ROW}px; }}")
        self.tree.setItemDelegateForColumn(C_CONF, StatusDelegate(self.tree))
        self.tree.header().setStretchLastSection(True)
        for col, w in ((C_FILE, 240), (C_TYPE, 80), (C_INFO, 220), (C_SIZE, 90), (C_SAVE, 120), (C_CONF, 120)):
            self.tree.setColumnWidth(col, w)
        self.tree.itemChanged.connect(lambda *_: self._update_send())
        make_sortable(self.tree)
        self.tree._fitter.optional = [C_MODIFIED, C_CREATED, C_INFO, C_TYPE]   # a narrow card leaves these out
        # Right-click a heading to choose columns (dates are off until wanted).
        self.tree.header().enable_column_menu("home", [C_TYPE, C_INFO, C_SAVE, C_CONF, C_CREATED, C_MODIFIED],
                                              {C_CREATED, C_MODIFIED})
        self.tree.header().column_choice_changed = self._fill_created
        self.tree._fitter.last_min = Space.XXL * 7
        self.tree.sortByColumn(-1, Qt.AscendingOrder)  # ranked order until a column is clicked
        add_header_checkbox(self.tree, C_FILE, self._update_send)
        click_to_check(self.tree, C_FILE)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        keyboard_list(self.tree, self._menu)
        self.placeholder = ListPlaceholder(self.tree)
        self.placeholder.show_message("Scan to find the files that would save the most.")
        card.lay.addWidget(self.tree, 1)
        row.addWidget(card, 3)

        # already efficient + clean-up
        side = Card()
        side.setMaximumWidth(Space.XXL * 11)
        side.lay.addWidget(text("Already efficient", Type.HEADLINE, bold=True))
        self.eff_box = text("Files that are already as small as they sensibly get show here after an analysis.",
                            Type.BODY, secondary=True, wrap=True)
        self.eff_box.setTextFormat(Qt.RichText)
        side.lay.addWidget(self.eff_box)
        side.lay.addSpacing(Space.S)
        side.lay.addWidget(text("More space", Type.HEADLINE, bold=True))
        side.lay.addWidget(text("Duplicates, unzipped archives, caches, old installers and app compression are "
                                "on the Clean Up page.", Type.BODY, secondary=True, wrap=True))
        open_clean = button("Open Clean Up", "", "broom")
        open_clean.clicked.connect(lambda: self.win.go_to(self.win.cleanup))
        side.lay.addWidget(open_clean, 0, Qt.AlignLeft)
        side.lay.addStretch(1)
        row.addWidget(side, 1)
        self.body.addLayout(row, 1)
        self._opps, self._side = card, side

        self.hover = HoverPreviewer(self.tree, self._hover_info, self)
        self.hover.enabled = self.qs.value("hover_preview", True, bool)
        self._show_folders()
        self._update_send()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit()

    def _avail(self) -> int:
        """The width the page's content gets: the page's own width (always current, unlike the cards inside,
        which follow it) less its margins and, when the page scrolls, the scroll bar's gap."""
        body = self.body.contentsMargins()
        sc = self.findChild(_BodyScroll)
        bar = sc.verticalScrollBar() if sc is not None else None
        gutter = bar.width() if bar is not None and not bar.isHidden() else 0
        return self.width() - 2 * Space.XXL - body.left() - body.right() - gutter

    def _fit(self) -> None:
        # The side card is extra information: drop it unless the opportunities keep a comfortable width
        # beside it. Decided from the width the page really has (not from the cards, which follow it).
        avail = self._avail()
        # (the buttons on their own line, with that line's margins and the card's inset)
        opps = max(Space.XXL * 16, self._buttons.minimumSizeHint().width() + 2 * Space.L + 2 * CARD_INSET + Space.XS)
        self._side.setVisible(avail >= opps + Space.L + self._side.minimumSizeHint().width())
        self._fit_head()

    @safe_filter
    def eventFilter(self, obj, ev):  # noqa: N802
        from PySide6.QtCore import QEvent

        if obj is self._head_card and ev.type() == QEvent.Resize:
            self._fit_head()
        return super().eventFilter(obj, ev)

    def _fit_head(self) -> None:
        """Never squeeze the buttons: Select None goes first (the list's header checkbox does the same), then
        the buttons move to a line of their own below the title."""
        # From the page's width, not the card's (which the buttons themselves would keep wide).
        room = self._avail() - 2 * Space.L - 2 * CARD_INSET     # inside the card and its title line's margins
        if not self._side.isHidden():       # (its own state: isVisible() is False whenever Home isn't on screen)
            # its width as the 3:1 row will make it (its current one may be stale)
            share = (self._avail() - Space.L) // 4
            room -= min(max(self._side.minimumSizeHint().width(), share), self._side.maximumWidth()) + Space.L
        title = self._head_title.sizeHint().width() + Space.M    # (and the gap before the buttons)

        def buttons(with_none: bool) -> int:
            shown = (self.select_high, self.select_none, self.send_btn) if with_none else (self.select_high,
                                                                                         self.send_btn)
            return sum(b.sizeHint().width() for b in shown) + Space.S * (len(shown) - 1)

        if title + buttons(True) <= room:
            beside, none = True, True
        elif title + buttons(False) <= room:
            beside, none = True, False
        else:
            beside, none = False, buttons(True) <= room
        self.select_none.setVisible(none)
        target = self._head if beside else self._below
        if target.indexOf(self._buttons) < 0:
            target.addWidget(self._buttons)
        self._below.setContentsMargins(Space.L, Space.S if not beside else 0, Space.L, 0)

    # ---- folders
    def _show_folders(self) -> None:
        if hasattr(self, "space_card"):
            self._show_drive()
        home = Path.home()
        self.folder_label.setText(", ".join("Your user folder" if p == home else (p.name or str(p))
                                            for p in self.roots) or "No folders")
        self.folder_label.setToolTip("\n".join(str(p) for p in self.roots))

    def _add(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Include a folder", str(Path.home()))
        if d and Path(d) not in self.roots:
            self.roots.append(Path(os.path.normpath(d)))
            self.qs.setValue("home/folders", [str(p) for p in self.roots])
            self._show_folders()

    def _reset(self) -> None:
        self.roots = default_roots()
        self.qs.remove("home/folders")
        self._show_folders()

    # ---- analysis
    def busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def analyze(self, background: bool = False) -> None:
        if self.busy() or not self.roots:
            return
        s = self.win.compress.panel.settings()
        self.worker = _Analyze(list(self.roots), s, self, background)
        self.worker.progress.connect(self._progress)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(lambda m: inform(self, "The analysis stopped", m))
        self.worker.finished.connect(lambda: (self.progress.setVisible(False), self.analyze_btn.setEnabled(True)))
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.analyze_btn.setEnabled(False)
        self.hint.setText("Scanning…")
        if self.tree.topLevelItemCount() == 0:
            self.placeholder.show_loading("Looking through your folders…")
        self.worker.start()

    def stop(self) -> None:
        if self.busy():
            self.worker.cancel.set()
            self.worker.wait(30000)

    def _progress(self, msg: str, done: int, total: int) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        if self.placeholder.loading:
            self.placeholder.show_loading(f"Looking through your folders… {done:,} files" if done else
                                          "Looking through your folders…")

    def _show_drive(self) -> None:
        import shutil

        from ..space import _anchor

        root = _anchor(self.roots[0] if self.roots else Path.home())
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            self.space_card.hide()
            return
        self.space_card.show_drive(root.drive or str(root), usage.total, usage.free)

    def _space_action(self, key: str) -> None:
        from .. import space

        if key == space.CACHES:
            cu = self.win.cleanup
            self.win.go_to(cu)
            names = [cu.tabs.tabText(i) for i in range(cu.tabs.count())]
            if "Caches" in names:
                cu.tabs.setCurrentIndex(names.index("Caches"))
        elif key == space.BIN:
            if os.name == "nt":
                import subprocess

                subprocess.Popen(["explorer", "shell:RecycleBinFolder"])
            else:
                from .filelist import open_file

                xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
                open_file(Path(xdg) / "Trash" / "files")
        else:                       # photos, videos, music, documents: review them on Compress
            self.win.go_to(self.win.compress)
            self.win.compress.add_paths([str(r) for r in self.roots])

    def _done(self, ov: overview.Overview) -> None:
        self.tile_save.set(f"~{human(ov.likely)}" if ov.opportunities else "Nothing",
                           f"up to {human(ov.potential)} with current options" if ov.opportunities
                           else "with the current options")
        self.tile_eff.set(human(ov.efficient_size) if ov.efficient else "None",
                          f"{ov.efficient_count:,} file{'s' if ov.efficient_count != 1 else ''}" if ov.efficient
                          else "every file could shrink")
        self.tile_files.set(f"{ov.files:,} files", f"{human(ov.total_size)} of images, videos and audio")
        if ov.space is not None:
            self.space_card.show_breakdown(ov.space)
        self.analyzed.emit(ov)
        self.tree.sortByColumn(-1, Qt.AscendingOrder)
        with filling(self.tree):
            self._fill(ov)
        self.tree.header().viewport().update()
        if ov.opportunities:
            self.hint.setText(f"{len(ov.opportunities):,} files could be smaller, ranked by expected saving × "
                              f"confidence. High-confidence files are pre-selected.")
        else:
            self.hint.setText("Try other folders, or change the options on the Compress page.")
        if ov.opportunities:
            self.placeholder.clear()
        else:
            self.placeholder.show_message("Nothing worth compressing here with the current options.")
        groups = sorted(ov.efficient.values(), key=lambda g: g.size, reverse=True)
        self.eff_box.setText("<br><br>".join(
            f"<b>{g.label}</b><br>{g.count:,} file{'s' if g.count != 1 else ''} · {human(g.size)}<br>{g.explanation}"
            for g in groups) or "None found: every file could shrink.")
        self.qs.setValue("home/last", time.time())
        self._update_send()

    def _fill(self, ov) -> None:
        self.tree.clear()
        self._created_filled = False
        rank = {overview.HIGH: 3, overview.MEDIUM: 2, overview.LOW: 1}
        for o in ov.opportunities:
            e = o.entry
            it = SortItem([e.path.name, KIND_LABEL.get(e.kind, e.kind), describe(e), human(e.size),
                           f"−{human(o.saving)} ({100 * o.saving / max(1, e.size):.0f}%)",
                           o.confidence, "", "", o.why])
            it.setText(C_MODIFIED, format_date(e.mtime))
            set_sort_keys(it, {C_SIZE: e.size, C_SAVE: o.saving, C_CONF: rank.get(o.confidence, 0), C_MODIFIED: e.mtime})
            it.setData(C_FILE, Qt.UserRole, e)
            it.setData(C_CONF, STATUS_ROLE, o.confidence.lower())
            it.setToolTip(C_FILE, str(e.path))
            it.setToolTip(C_WHY, o.why)
            note = cadence_note(e.video) if e.video is not None else ""
            it.setToolTip(C_INFO, describe(e) + (f"\n{note}" if note else ""))
            for c in (C_SIZE, C_SAVE):
                it.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(C_FILE, Qt.Checked if o.confidence == overview.HIGH else Qt.Unchecked)
            self.tree.addTopLevelItem(it)
        self._fill_created()

    def _fill_created(self) -> None:
        """Creation dates cost a file read each, so they're only read once the column is shown."""
        if self.tree.isColumnHidden(C_CREATED) or getattr(self, "_created_filled", False):
            return
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            e = it.data(C_FILE, Qt.UserRole)
            ts = created_time(e.path) if e is not None else None
            it.setText(C_CREATED, format_date(ts))
            set_sort_keys(it, {C_CREATED: ts or 0.0})
        self._created_filled = self.tree.topLevelItemCount() > 0

    def _menu(self, pos) -> None:
        items = selected_or_clicked(self.tree, pos)
        if not items:
            return
        paths = [it.data(C_FILE, Qt.UserRole).path for it in items]

        def check(state):
            for it in items:
                it.setCheckState(C_FILE, state)
            self._update_send()

        def compress_these():
            c = self.win.compress
            c.clear()
            c.add_paths([str(p) for p in paths])
            self.win.go_to(c)

        n = "" if len(items) == 1 else f" {len(items)}"
        clicked = items[0].data(C_FILE, Qt.UserRole)
        label = type_label(clicked.path, clicked.kind)

        def by_type(state):
            self.tree.blockSignals(True)
            for i in range(self.tree.topLevelItemCount()):
                it = self.tree.topLevelItem(i)
                e = it.data(C_FILE, Qt.UserRole)
                if e is not None and type_label(e.path, e.kind) == label:
                    it.setCheckState(C_FILE, state)
            self.tree.blockSignals(False)
            self.tree.viewport().update()
            self.tree.header().viewport().update()
            self._update_send()

        file_menu(self, paths, [
            (f"Select{n}", lambda: check(Qt.Checked)),
            (f"Deselect{n}", lambda: check(Qt.Unchecked)),
            None,
            (f"Select All {label} Files", lambda: by_type(Qt.Checked)),
            (f"Deselect All {label} Files", lambda: by_type(Qt.Unchecked)),
            None,
            ("Compress This…" if len(items) == 1 else f"Compress These {len(items)}…", compress_these),
        ]).exec(self.tree.viewport().mapToGlobal(pos))

    # ---- selection
    def _checked(self) -> list[Path]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.checkState(C_FILE) == Qt.Checked:
                out.append(it.data(C_FILE, Qt.UserRole).path)
        return out

    def _select(self, pred) -> None:
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            it.setCheckState(C_FILE, Qt.Checked if pred(it.text(C_CONF)) else Qt.Unchecked)
        self.tree.blockSignals(False)
        self.tree.header().viewport().update()
        self._update_send()

    def _update_send(self) -> None:
        n = len(self._checked())
        self.send_btn.setEnabled(n > 0)
        self.send_btn.setText(f"Compress {n} Selected…" if n else "Compress Selected…")
        has = self.tree.topLevelItemCount() > 0
        self.select_high.setEnabled(has)
        self.select_none.setEnabled(has)

    def _send(self) -> None:
        paths = self._checked()
        if not paths:
            return
        c = self.win.compress
        c.clear()
        c.add_paths([str(p) for p in paths])
        self.win.go_to(c)

    def _hover_info(self, index):
        it = self.tree.itemFromIndex(index)
        e = it.data(C_FILE, Qt.UserRole) if it else None
        if e is None or not e.path.exists():
            return None
        return e.path, e.kind, e.path.name, f"{KIND_LABEL.get(e.kind, e.kind)} · {human(e.size)}"
