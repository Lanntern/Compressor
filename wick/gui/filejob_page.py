"""Shared page for "add files → review → run" tools (Convert, Metadata)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFrame, QStackedWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from ..engine import CANCELLED, DONE, FAILED
from ..formats import KIND_LABEL, human
from ..plan import Plan
from ..scanner import FileEntry
from ..settings import Settings
from .dialogs import ask
from .filelist import (
    ListPlaceholder, SortItem, add_header_checkbox, click_to_check, created_time, file_menu, format_date, filling,
    keyboard_list, make_sortable, selected_or_clicked, set_sort_keys,
)
from .style import Size, Space, Type, tokens
from .thumbs import HoverPreviewer
from .widgets import (PROGRESS_ROLE, STATUS_ROLE, Card, DropZone, FlowLayout, Page, StatusDelegate, button, hbox,
                      set_compact, text)
from .jobstatus import JobStatus, PauseControl
from .settings_panel import BIN_NAME
from .workers import JobThread, ScanThread

if TYPE_CHECKING:
    from .metadata_scan import MetadataScan


ROLE_ROW = Qt.UserRole + 30
ROLE_OUT = Qt.UserRole + 31   # result path once done


class FileJobPage(Page):
    """Subclasses define: COLUMNS, STATUS_COL, build_options(card), plan(entry) -> Plan | None (None = skip,
    with self.skip_reason set), row_values(entry, plan) -> list[str], processor(), action_label, tool."""

    COLUMNS: list[str] = ["Name", "Folder", "Type", "Size", "Status"]
    STATUS_COL = 4
    action_label = "Run"
    tool = "job"
    confirm_title = "Run {n} files?"
    confirm_info = ""
    status_words = ("Working on", "done")       # JobStatus: "Converting photo.heic", "3 of 4 files converted"
    original_kept = True                          # what to say about the originals afterwards

    def __init__(self, window, title: str, subtitle: str, drop_title: str, drop_sub: str):
        super().__init__(title, subtitle)
        # Every list also has Created and Modified, before Status (off until chosen: right-click a heading).
        cols = type(self).COLUMNS
        assert type(self).STATUS_COL == len(cols) - 1
        self.COLUMNS = [*cols[:-1], "Created", "Modified", cols[-1]]
        self.STATUS_COL = len(self.COLUMNS) - 1
        self.C_CREATED, self.C_MODIFIED = self.STATUS_COL - 2, self.STATUS_COL - 1
        self.win = window
        self.removed: set[str] = set()      # files taken off the list by hand (normalized paths)
        self.paths: list[Path] = []
        self.entries: list[FileEntry] = []
        self.rows: dict[int, QTreeWidgetItem] = {}
        self._file_steps: dict[int, str] = {}
        self.scan_thread: ScanThread | MetadataScan | None = None
        self.job_thread: JobThread | None = None
        self._rescan = False

        self.add_files_btn = button("Add Files…", "", "files", "Add individual files", "")
        self.add_folder_btn = button("Add Folder…", "", "folder", "Add a folder")
        self.clear_btn = button("Clear", "", None, "Remove everything from the list")
        self.run_btn = button(self.action_label, "primary", None, "")
        self.run_btn.setEnabled(False)
        for b in (self.add_files_btn, self.add_folder_btn, self.clear_btn, self.run_btn):
            self.action_bar.addWidget(b)
        self.add_files_btn.clicked.connect(self.browse_files)
        self.add_folder_btn.clicked.connect(self.browse_folder)
        self.clear_btn.clicked.connect(self.clear)
        self.run_btn.clicked.connect(lambda: self.start())       # (clicked passes a bool)

        self.options = Card()
        self.build_options(self.options)
        self.body.addWidget(self.options)

        self.stack = QStackedWidget()
        self.drop = DropZone()
        self.drop.title.setText(drop_title)
        self.drop.sub.setText(drop_sub)
        self.drop.add_files.clicked.connect(self.browse_files)
        self.drop.add_folder.clicked.connect(self.browse_folder)
        self.stack.addWidget(self.drop)

        results = QWidget()
        rl = QVBoxLayout(results)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(Space.L)
        self.job_status = JobStatus(*self.status_words)
        self.job_status.stop_requested.connect(self.stop)
        self.pauser = PauseControl(self.job_status, window)
        rl.addWidget(self.job_status)
        card = Card(padding=0, spacing=0, inset=True)
        self.summary = text("", Type.BODY, secondary=True, wrap=True)
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setContentsMargins(Space.L, Space.M, Space.L, Space.S)
        card.lay.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setAccessibleName(f"{title} files")
        self.tree.setHeaderLabels(self.COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setFrameShape(QFrame.NoFrame)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setStyleSheet(f"QTreeView::item {{ height: {Size.ROW}px; }}")
        self.tree.setItemDelegateForColumn(self.STATUS_COL, StatusDelegate(self.tree))
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 260)
        for i in range(1, len(self.COLUMNS) - 1):
            self.tree.setColumnWidth(i, 150)
        self.tree.itemChanged.connect(self._item_changed)
        make_sortable(self.tree)
        self.tree.sortByColumn(-1, Qt.AscendingOrder)
        self.tree.header().remember(type(self).__name__)
        add_header_checkbox(self.tree, 0, self._item_changed)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        keyboard_list(self.tree, self._menu)
        click_to_check(self.tree, 0)
        self.tree.header().enable_column_menu(f"filejob/{self.tool}/{title}", list(range(1, self.STATUS_COL)),
                                              {self.C_CREATED, self.C_MODIFIED})
        self.tree.header().column_choice_changed = lambda: self.refresh_rows() \
            if not self.tree.isColumnHidden(self.C_CREATED) and self.entries and not self.busy() else None
        self.placeholder = ListPlaceholder(self.tree)
        card.lay.addWidget(self.tree, 1)
        rl.addWidget(card)
        self.stack.addWidget(results)
        self.body.addWidget(self.stack, 1)
        self.hover = HoverPreviewer(self.tree, self._hover_info, self)
        self.hover.enabled = window.qsettings.value("hover_preview", True, bool)
        self._update()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        # Icon-only Add buttons only when their labels really don't fit next to the title.
        buttons = (self.add_files_btn, self.add_folder_btn)
        for b in buttons:
            set_compact(b, False)
        needed = (self.title.sizeHint().width() + Space.XXL * 3 + Space.S * 4
                  + sum(b.sizeHint().width() for b in (*buttons, self.clear_btn, self.run_btn)))
        for b in buttons:
            set_compact(b, self.width() < needed)

    # ---- to override
    def build_options(self, card: Card) -> None: ...

    def plan(self, e: FileEntry) -> Plan | None: ...

    def row_values(self, e: FileEntry, plan: Plan | None) -> list[str]:
        return [e.path.name, e.path.parent.name, KIND_LABEL.get(e.kind, e.kind), human(e.size), ""]

    def processor(self):
        raise NotImplementedError

    def settings(self) -> Settings:
        return Settings(min_image_kb=0, min_video_mb=0)

    def accepts(self, path: Path) -> bool:
        return True

    def after_scan(self) -> None: ...

    # ---- inputs
    def add_paths(self, paths: list[str]) -> None:
        keys = {os.path.normcase(str(p)) for p in self.paths}
        for p in paths:
            p = os.path.normpath(p)
            if os.path.normcase(p) not in keys:
                self.paths.append(Path(p))
        self.scan()

    def _last_dir(self) -> str:
        """Where Add Folder / Add Files last were (else the home folder)."""
        last = str(self.win.qsettings.value("browse/last_dir", "", str)) if hasattr(self, "win") else ""
        return last if last and os.path.isdir(last) else str(Path.home())

    def _remember_dir(self, picked: str) -> None:
        if hasattr(self, "win"):
            self.win.qsettings.setValue("browse/last_dir", picked if os.path.isdir(picked) else os.path.dirname(picked))

    def browse_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Add files", self._last_dir())
        if files:
            self._remember_dir(files[0])
            self.add_paths(files)

    def browse_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Add a folder", self._last_dir())
        if d:
            self._remember_dir(d)
            self.add_paths([d])

    def clear(self) -> None:
        if self.busy():
            return
        self.paths, self.entries = [], []
        self.removed = set()
        self.tree.clear()
        self.job_status.hide()          # the last job's outcome goes with its list
        self._update()

    def busy(self) -> bool:
        return any(t is not None and t.isRunning() for t in (self.scan_thread, self.job_thread))

    # ---- scanning
    def scan(self) -> None:
        if self.busy():
            self._rescan = True
            return
        if not self.paths:
            self._update()
            return
        self.scan_thread = ScanThread(self.paths, self.settings(), self)
        self.scan_thread.progress.connect(lambda m, d, t: (self.job_status.busy(m, d, t), self.placeholder.show_loading(
            f"Reading file details… {d:,} of {t:,}" if t else "Looking for files…")))
        self.placeholder.show_loading("Looking for files…")
        self.scan_thread.result.connect(self._scanned)
        self.scan_thread.finished.connect(self._finished)
        self._busy(True, "Reading files…")
        self.scan_thread.start()

    def _scanned(self, result) -> None:
        self.entries = [e for e in result.entries if self.accepts(e.path)
                        and os.path.normcase(str(e.path)) not in self.removed]
        self.refresh_rows()
        self.after_scan()

    def refresh_rows(self) -> None:
        with filling(self.tree):
            self._fill_rows()
        self.tree.header().viewport().update()
        self._update()

    def _fill_rows(self) -> None:
        unticked = set()            # (keep what was left out when the list is rebuilt for new settings)
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            e, p = it.data(0, ROLE_ROW)
            if p is not None and it.checkState(0) != Qt.Checked:     # (excluded rows weren't the user's choice)
                unticked.add(id(e))
        self.tree.clear()
        self.rows = {}
        self._append_rows(self.entries, unticked)

    def _append_rows(self, entries, unticked=()) -> None:
        t = tokens()
        size_cols = [c for c, title in enumerate(self.COLUMNS) if title == "Size"]
        for e in entries:
            p = self.plan(e)
            vals = self.row_values(e, p)
            created = created_time(e.path) if not self.tree.isColumnHidden(self.C_CREATED) else None
            vals = [*vals[:-1], format_date(created), format_date(e.mtime), vals[-1]]
            it = SortItem(vals)
            set_sort_keys(it, {**{c: e.size for c in size_cols}, self.C_CREATED: created or 0.0,
                               self.C_MODIFIED: e.mtime})
            it.setData(0, ROLE_ROW, (e, p))
            it.setToolTip(0, str(e.path))
            for c, title in enumerate(self.COLUMNS):
                if title == "Size":
                    it.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
            if p is not None:
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(0, Qt.Unchecked if id(e) in unticked else Qt.Checked)
                it.setData(self.STATUS_COL, STATUS_ROLE, "ready")
            else:
                it.setData(self.STATUS_COL, STATUS_ROLE, "excluded")
                for c in range(len(self.COLUMNS)):
                    it.setForeground(c, QColor(t.text2))
            self.tree.addTopLevelItem(it)
            self.rows[id(e)] = it

    def _menu(self, pos) -> None:
        items = selected_or_clicked(self.tree, pos)
        if not items:
            return
        paths = [it.data(0, ROLE_ROW)[0].path for it in items]
        outputs = [Path(o) for it in items if (o := it.data(0, ROLE_OUT))]
        checkable = [it for it in items if it.flags() & Qt.ItemIsUserCheckable]
        extra: list = []
        if outputs:
            from .file_model import open_in_file_manager_many

            extra.append(("Show Result" if len(outputs) == 1 else f"Show {len(outputs)} Results",
                          lambda: open_in_file_manager_many(outputs)))
        if checkable and not self.busy():
            def check(state):
                for it in checkable:
                    it.setCheckState(0, state)
            n = "" if len(checkable) == 1 else f" {len(checkable)}"
            extra += [None, (f"Select{n}", lambda: check(Qt.Checked)), (f"Deselect{n}\tDelete", lambda: check(Qt.Unchecked))]
        if not self.busy():
            own = self.menu_entries([it.data(0, ROLE_ROW)[0] for it in items])
            if own:
                extra += [None, *own]
            n = "" if len(items) == 1 else f" {len(items)}"
            extra += [None, (f"Remove{n} from List", lambda: self.remove_items(items))]
        file_menu(self, paths, extra).exec(self.tree.viewport().mapToGlobal(pos))

    def remove_items(self, items) -> None:
        """Take files off the list (nothing happens to them); they stay off until the list is cleared."""
        if self.busy():
            return
        gone = {id(it.data(0, ROLE_ROW)[0]) for it in items}
        names = {os.path.normcase(str(it.data(0, ROLE_ROW)[0].path)) for it in items}
        self.removed |= names
        self.paths = [p for p in self.paths if os.path.normcase(str(p)) not in names]
        self.entries = [e for e in self.entries if id(e) not in gone]
        for it in items:
            self.rows.pop(id(it.data(0, ROLE_ROW)[0]), None)
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(it))
        self._update()

    def menu_entries(self, entries: list[FileEntry]) -> list:
        """The page's own context menu entries for these rows."""
        return []

    def selected(self) -> list[tuple[FileEntry, Plan]]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            e, p = it.data(0, ROLE_ROW)
            if p is not None and it.checkState(0) == Qt.Checked and not it.isHidden() \
                    and it.data(self.STATUS_COL, STATUS_ROLE) != "done":
                out.append((e, p))
        return out

    def _item_changed(self, *_):
        self._update()

    def selection_count(self) -> int:
        return len(self.selected())

    def _update(self) -> None:
        self.stack.setCurrentIndex(1 if (self.entries or self.busy()) else 0)
        n = self.selection_count() if self.entries else 0
        self.run_btn.setEnabled(n > 0 and not self.busy())
        self.run_btn.setText(f"{self.action_label} {n} File{'s' if n != 1 else ''}" if n else self.action_label)
        self.clear_btn.setEnabled(bool(self.entries) and not self.busy())
        self.update_summary()

    def update_summary(self) -> None:
        n = self.selection_count()
        self.summary.setText(f"{len(self.entries)} file{'s' if len(self.entries) != 1 else ''} · <b>{n} selected</b>")

    # ---- running
    def start(self, confirm: bool = True, only: set[str] | None = None) -> None:
        """Run the job on the ticked rows (``confirm``: ask first; not when the user just chose it elsewhere).
        ``only``: just these files (normcased paths), whatever else is listed."""
        items = [(e, p) for e, p in self.selected() if only is None or os.path.normcase(str(e.path)) in only]
        if not items or self.busy():
            return
        n = len(items)
        if confirm and not ask(self, self.confirm_title.format(n=n, s="s" if n != 1 else ""), self.confirm_info,
                               self.action_label):
            return
        self.job_thread = JobThread(items, self.settings(), self.paths[0] if self.paths else Path.home(), self,
                                    processor=self.processor(), tool=self.tool)
        self.job_thread.file_started.connect(self._started_one)
        self.job_thread.file_progress.connect(self._progress)
        self.job_thread.file_step.connect(self._step)
        self.job_thread.file_finished.connect(self._done_one)
        self.job_thread.done.connect(self._done)
        self.job_thread.error.connect(lambda m: self.job_status.failed(f"Stopped: {m}"))
        self.job_thread.finished.connect(self._finished)
        self._busy(True)
        self.job_status.start(n, sum(e.size for e, _ in items))
        self.job_thread.start()
        self.pauser.attach(self.job_thread)

    def _started_one(self, e) -> None:
        self._file_steps[id(e)] = "Starting"
        self._set_status(e, "running", "Working…")
        self.job_status.file_started(e)

    def _step(self, e, message: str) -> None:
        self._file_steps[id(e)] = message
        self._set_status(e, "running", f"{message}…")
        self.job_status.file_step(e, message)

    def _set_status(self, e, kind: str, msg: str, progress: float | None = None) -> None:
        it = self.rows.get(id(e))
        if it is None:
            return
        it.setData(self.STATUS_COL, STATUS_ROLE, kind)
        it.setData(self.STATUS_COL, PROGRESS_ROLE, progress)
        it.setText(self.STATUS_COL, msg)
        it.setToolTip(self.STATUS_COL, msg)

    def _progress(self, e, f: float) -> None:
        stage = self._file_steps.get(id(e), "Working")
        self._set_status(e, "running", f"{stage} · {f * 100:.0f}%", f)
        self.job_status.file_progress(e, f)

    def _done_one(self, r) -> None:
        self._file_steps.pop(id(r.entry), None)
        kind = {DONE: "done", FAILED: "failed", CANCELLED: "skipped"}.get(r.status, "skipped")
        msg = f"{r.message} (while {r.step[0].lower() + r.step[1:]})" if r.status == FAILED and r.step else r.message
        self._set_status(r.entry, kind, msg)
        self.job_status.file_finished(r)
        it = self.rows.get(id(r.entry))
        if it is not None and r.status == DONE:
            if r.output:
                it.setData(0, ROLE_OUT, str(r.output))
            it.setCheckState(0, Qt.Unchecked)
            it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable)

    def _done(self, results, run_id) -> None:
        self.win.history.refresh()
        many = len(results) > 1
        if self.settings().output_mode == "replace":
            note = (f"The original{'s are' if many else ' is'} in the {BIN_NAME}: empty it to free the space, or "
                    "undo it later.")
        else:
            note = f"The original{'s were' if many else ' was'} kept."
        self.job_status.finish(results, note,
                               on_undo=(lambda: self.win.history.undo_run(run_id)) if run_id else None)
        if not self.window().isActiveWindow():
            self.win.tray_message(self.job_status.title.text())
            QApplication.alert(self.window())

    def stop(self) -> None:
        for t in (self.scan_thread, self.job_thread):
            if t is not None and t.isRunning():
                t.cancel.set()
        if self.job_thread is not None and self.job_thread.isRunning():
            self.job_status.stopping()

    def _finished(self) -> None:
        self._busy(False)
        if self.entries:
            self.placeholder.clear()
        elif self.paths:
            self.placeholder.show_message("Nothing found here that this page can work on. Add other files or folders.")
        if self._rescan:
            self._rescan = False
            QTimer.singleShot(0, self.scan)

    def _busy(self, busy: bool, msg: str = "") -> None:
        if busy and msg:
            self.job_status.busy(msg)
        elif not busy and not self.job_status.stop_btn.isHidden():
            self.job_status.hide()          # a scan ended (a job's outcome stays until dismissed)
        for w in (self.options, self.add_files_btn, self.add_folder_btn, self.drop):
            w.setEnabled(not busy)
        self._update()

    def _hover_info(self, index):
        it = self.tree.itemFromIndex(index)
        data = it.data(0, ROLE_ROW) if it else None
        if not data:
            return None
        e = data[0]
        return (e.path, e.kind, e.path.name, f"{KIND_LABEL.get(e.kind, e.kind)} · {human(e.size)}") \
            if e.path.exists() else None


def set_visible(w: QWidget, on: bool) -> None:
    """Show/hide a control together with its row label."""
    w.setVisible(on)
    lab = w.property("rowLabel")
    if lab is not None:
        lab.setVisible(on)


def option_row(*pairs) -> FlowLayout:
    """Label/control pairs in a row that wraps on narrow windows (labels from the type scale)."""
    row = FlowLayout()
    for label, w in pairs:
        if isinstance(w, tuple):  # several controls that belong together (e.g. a field and its button)
            row.add_group(*w)
        elif label:
            lab = text(label, Type.BODY)
            lab.setBuddy(w)
            w.setProperty("rowLabel", lab)   # hide/show the label together with its control
            w.setAccessibleName(label)
            row.add_group(lab, w)
        else:
            row.add_group(w)
    return row


__all__ = ["FileJobPage", "option_row", "hbox"]
