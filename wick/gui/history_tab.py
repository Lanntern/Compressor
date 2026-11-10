"""History page: past runs, totals and undo."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QAbstractItemView, QFrame, QMenu, QTreeWidget

from .. import journal
from ..formats import human
from .settings_panel import BIN_NAME
from .dialogs import ask, inform
from .filelist import (ListPlaceholder, SortItem, file_menu, filling, keyboard_list, make_sortable, selected_or_clicked,
                       set_sort_keys)
from .style import Size, Space, Type, tokens
from .widgets import Card, Page, StatTile, button, hbox, set_compact, text
from .workers import UndoThread


class _BinCheck(QThread):
    """Which originals can no longer be restored (the bin was emptied since): read each bin once."""

    done = Signal(object)        # {(run_id, original): reason}

    def __init__(self, runs, parent=None):
        super().__init__(parent)
        self.runs = runs

    def run(self) -> None:
        from .. import trash_restore
        from ..engine import cannot_undo

        records = [(run.run_id, rec) for run in self.runs for rec in run.files
                   if not rec.undone and rec.mode in ("replace", "removed", "deleted")]
        try:
            idx = trash_restore.index([rec.original for _, rec in records]) if records else {}
            out = {}
            for run_id, rec in records:
                reason = cannot_undo(rec, idx)
                if reason:
                    out[(run_id, rec.original)] = reason
        except Exception:  # noqa: BLE001 - a hint, never a reason to fail the page
            out = {}
        self.done.emit(out)


def run_summary(run) -> str:
    """What a run was, in a few words: the page or tool, and where its results went."""
    from .cleanup_tools import tools
    from .settings_panel import OUTPUT_CHOICES

    s = run.settings or {}
    tool = s.get("tool") or "job"
    names = {"job": "Compress", "compress": "Compress", "convert": "Convert", "metadata": "Metadata",
             "watch": "Watch Folders"}
    if tool in names:
        where = dict((v, k) for k, v in OUTPUT_CHOICES).get(s.get("output_mode", ""), "")
        return f"{names[tool]} · {where}" if where else names[tool]
    tabs = {t.key: t.tab for t in tools()}
    label = f"Clean Up · {tabs.get(tool, tool)}"
    return f"{label} (automatic)" if s.get("automatic") else label


class HistoryTab(Page):
    def __init__(self, window):
        super().__init__("History")
        self.win = window
        self.undo_thread: UndoThread | None = None
        self.blocked: dict = {}              # (run_id, original) -> why it can't be undone any more
        self._bin_check: _BinCheck | None = None
        self._bin_checked = 0.0

        self.report_btn = button("Export Report…", "", None, "Save the selected runs (or all of them) as a "
                                                           "spreadsheet (CSV): every file, its sizes and what was done")
        self.report_btn.clicked.connect(self.export_report)
        self.action_bar.addWidget(self.report_btn)
        self.refresh_btn = button("Refresh", "", "refresh", "Read the history again and check what can still be "
                                                            "undone", "F5")
        set_compact(self.refresh_btn, True)
        self.refresh_btn.setAccessibleName("Refresh")
        self.refresh_btn.clicked.connect(self.reload)
        self.action_bar.insertWidget(0, self.refresh_btn)
        self.delete_btn = button("Delete", "", "trash", "Remove runs from the history (names included). Your files "
                                                      "don't change.")
        menu = QMenu(self.delete_btn)
        self.delete_selected_action = menu.addAction("Delete Selected", self.delete_selected)
        self.delete_selected_action.setShortcut(QKeySequence.Delete)   # (shown in the menu; the key works in the list)
        self.delete_selected_action.setShortcutContext(Qt.WidgetShortcut)
        menu.addSeparator()
        menu.addAction("Delete All History and Logs…", self.delete_everything)
        menu.aboutToShow.connect(lambda: self.delete_selected_action.setEnabled(bool(self.tree.selectedItems())))
        self.delete_btn.setMenu(menu)
        self.action_bar.addWidget(self.delete_btn)
        self.undo_btn = button("Undo Selected", "", "undo", "Restore the originals of the selected runs or files",
                               "Ctrl+Z")
        self.undo_btn.clicked.connect(self.undo_selected)
        self.action_bar.addWidget(self.undo_btn)

        self.tile_total = StatTile("Saved in total", accent=True)
        self.tile_runs = StatTile("Runs")
        self.tile_files = StatTile("Files changed")
        self.body.addLayout(hbox(self.tile_total, self.tile_runs, self.tile_files, spacing=Space.L))

        card = Card(padding=0, spacing=0, inset=True)
        self.tree = QTreeWidget()
        self.tree.setAccessibleName("Past runs")
        self.tree.setHeaderLabels(["Run / file", "Folder", "Files", "Saved", "Details"])
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.setFrameShape(QFrame.NoFrame)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(Space.L + Space.XS)
        h = self.tree.header()
        h.setStretchLastSection(True)
        h.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.tree.setColumnWidth(0, 240)
        self.tree.setColumnWidth(1, 220)
        self.tree.setColumnWidth(2, 64)
        self.tree.setColumnWidth(3, 96)
        self.tree.setStyleSheet(f"QTreeView::item {{ height: {Size.ROW}px; }}")
        make_sortable(self.tree, 0, Qt.DescendingOrder)  # newest first
        self.placeholder = ListPlaceholder(self.tree)
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        keyboard_list(self.tree, self._menu)
        from PySide6.QtGui import QShortcut

        delete = QShortcut(QKeySequence.Delete, self.tree, self.delete_selected)
        delete.setContext(Qt.WidgetShortcut)
        card.lay.addWidget(self.tree, 1)
        self.body.addWidget(card, 1)
        self.hint = text(f"Undo works while originals are in the {BIN_NAME}. Lossless JPEG → JPEG XL files can always "
                         "be turned back into the exact original. Copies saved elsewhere are removed; hard links "
                         "get their own copy back; compressed apps are decompressed.",
                         Type.FOOTNOTE, secondary=True, wrap=True)
        if os.name == "nt":       # beside the note about it, not among the page's actions
            self.bin_btn = button("Open Recycle Bin", "link", None, "Open the Windows Recycle Bin")
            self.bin_btn.clicked.connect(lambda: subprocess.Popen(["explorer", "shell:RecycleBinFolder"]))
            self.body.addLayout(hbox(self.hint, self.bin_btn, spacing=Space.L))
            hint_row = self.body.itemAt(self.body.count() - 1).layout()
            hint_row.setStretchFactor(self.hint, 1)
            hint_row.setAlignment(self.bin_btn, Qt.AlignTop)
        else:
            self.body.addWidget(self.hint)
        self.refresh()

    def refresh(self, force: bool = False) -> None:
        """Update totals; rebuild the list only if the history changed, and only while it's on screen."""
        sig = journal.signature()
        if sig == getattr(self, "_sig", None) and not force:
            return
        self._sig = sig
        self.runs = journal.list_runs()
        total_saved = sum(r.saved_bytes for r in self.runs)
        files = sum(r.active_files for r in self.runs)
        self.tile_total.set(human(total_saved) if self.runs else "—")
        self.tile_runs.set(f"{len(self.runs):,}")
        self.tile_files.set(f"{files:,}")
        self.win.sidebar.set_saved(human(total_saved) if total_saved > 0 else None)
        self.win.note_saved_total(total_saved)
        self._stale = True
        if self.isVisible():
            self._build()
            self.check_bin(force=True)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if getattr(self, "_stale", True):
            self._build()
        self.check_bin()

    def check_bin(self, force: bool = False) -> None:
        """Look (in the background) for originals that can't be restored any more; mark them."""
        if self._bin_check is not None and self._bin_check.isRunning():
            return
        if not force and time.monotonic() - self._bin_checked < 10:
            return
        self._bin_checked = time.monotonic()
        self._bin_check = _BinCheck(list(getattr(self, "runs", []) or []), self)
        self._bin_check.done.connect(self._bin_checked_result)
        self._bin_check.start()

    def _bin_checked_result(self, blocked: dict) -> None:
        self.blocked = blocked
        self._mark_blocked()

    def _mark_blocked(self) -> None:
        from PySide6.QtGui import QColor

        muted = QColor(tokens().text3)
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            data = top.data(0, Qt.UserRole)
            if not data or data[0] != "run":
                continue
            run = data[1]
            n = sum(1 for rec in run.files if (run.run_id, rec.original) in self.blocked)
            undone = len(run.files) - run.active_files
            parts = [run_summary(run)] + ([f"{undone} undone"] if undone else [])
            if n:
                parts.append(f"{n} can't be undone any more")
            top.setText(4, " · ".join(parts))
            top.setToolTip(4, f"{n} original{'s are' if n != 1 else ' is'} no longer in the {BIN_NAME} "
                              "(it was emptied): those can't be restored." if n else top.text(4))
            for j in range(top.childCount()):
                child = top.child(j)
                cd = child.data(0, Qt.UserRole)
                reason = self.blocked.get((run.run_id, cd[2].original)) if cd else None
                if reason:
                    child.setText(4, f"Can't be undone: {reason[0].lower() + reason[1:]}")
                    child.setToolTip(4, child.text(4))
                    for c in range(5):
                        child.setForeground(c, muted)

    def _build(self) -> None:
        self._stale = False
        with filling(self.tree):
            self._fill()
        if self.blocked:
            self._mark_blocked()

    def _fill(self) -> None:
        self.tree.clear()
        runs = getattr(self, "runs", None)
        if runs is None:
            runs = self.runs = journal.list_runs()
        for run in runs:
            when = time.strftime("%a %d %b %Y, %H:%M", time.localtime(run.started))
            top = SortItem([when, Path(run.root).name or run.root, str(run.active_files),
                            human(run.saved_bytes), ""])
            set_sort_keys(top, {0: run.started, 2: run.active_files, 3: run.saved_bytes})
            top.setToolTip(1, run.root)
            top.setData(0, Qt.UserRole, ("run", run))
            undone = len(run.files) - run.active_files
            top.setText(4, run_summary(run) + (f" · {undone} undone" if undone else ""))
            top.setToolTip(4, top.text(4))
            # File rows are created when the run is expanded (a run can hold tens of thousands).
            top.setChildIndicatorPolicy(SortItem.ShowIndicator)
            self.tree.addTopLevelItem(top)
        if runs:
            self.placeholder.clear()
        elif not journal.ENABLED:
            self.placeholder.show_message("History is off: Wick doesn't write down what it changes. Turn it "
                                          "on in Settings › Data to be able to undo from here.")
        else:
            self.placeholder.show_message("Nothing compressed yet. Runs from Compress, Convert, Metadata and "
                                          "Watch Folders appear here, and can be undone.")

    def _expanded(self, top) -> None:
        data = top.data(0, Qt.UserRole)
        if not data or data[0] != "run" or top.childCount():
            return
        run = data[1]
        muted = tokens().text3
        from PySide6.QtGui import QColor

        children = []
        for rec in run.files:
            where = "" if rec.mode != "copy" else f" · copy in {Path(rec.output).parent.name}"
            child = SortItem([
                Path(rec.original).name, Path(rec.original).parent.name, "",
                "" if rec.undone else human(rec.original_size - rec.output_size),
                f"Undone: {rec.undo_note}" if rec.undone else f"{rec.method}{where}",
            ])
            set_sort_keys(child, {3: 0 if rec.undone else rec.original_size - rec.output_size})
            child.setToolTip(0, rec.original)
            child.setToolTip(4, child.text(4))
            child.setData(0, Qt.UserRole, ("file", run, rec))
            if rec.undone:
                for c in range(5):
                    child.setForeground(c, QColor(muted))
            children.append(child)
        sorting = self.tree.isSortingEnabled()
        self.tree.setSortingEnabled(False)
        top.addChildren(children)
        self.tree.setSortingEnabled(sorting)
        if self.blocked:
            self._mark_blocked()

    def _menu(self, pos) -> None:
        items = [it for it in selected_or_clicked(self.tree, pos) if it.data(0, Qt.UserRole)]
        if not items:
            return
        paths = []
        for it in items:
            data = it.data(0, Qt.UserRole)
            if data[0] == "run":
                paths.append(Path(data[1].root))
            else:
                rec = data[2]
                out = Path(rec.output)
                paths.append(out if not rec.undone and out.exists() else Path(rec.original))
        can_undo = any(not rec.undone for it in items
                       for rec in (it.data(0, Qt.UserRole)[1].files if it.data(0, Qt.UserRole)[0] == "run"
                                   else [it.data(0, Qt.UserRole)[2]]))
        extra = [("Undo…", self.undo_selected)] if can_undo else []
        extra.append(("Export Report…", self.export_report))
        extra.append(("Delete from History", self.delete_selected))
        file_menu(self, paths, extra).exec(self.tree.viewport().mapToGlobal(pos))

    def _selected_runs(self) -> list:
        runs, seen = [], set()
        for item in self.tree.selectedItems():
            data = item.data(0, Qt.UserRole)
            if data and data[1].run_id not in seen:
                seen.add(data[1].run_id)
                runs.append(data[1])
        return runs or list(getattr(self, "runs", []) or [])

    def reload(self) -> None:
        """Read the history again (runs deleted or added elsewhere) and re-check what's still in the bin."""
        self.refresh(force=True)
        self.check_bin(force=True)

    def delete_everything(self) -> None:
        from .. import log as applog
        from .. import pending

        if self.undo_thread is not None and self.undo_thread.isRunning():
            return
        if not ask(self, "Delete all history and logs?",
                   "Every record of what was changed and the troubleshooting log are removed from this computer. "
                   f"Nothing can be undone from here afterwards. Your files don't change; originals stay in the "
                   f"{BIN_NAME} until it's emptied.", "Delete All", destructive=True):
            return
        journal.clear()
        applog.delete_logs()
        for tool in ("compress", "convert", "metadata"):
            pending.finish(tool)
        self.refresh(force=True)

    def delete_selected(self) -> None:
        """Remove the selected runs, or single files from runs, from History; with nothing selected, all of it."""
        if self.undo_thread is not None and self.undo_thread.isRunning():
            return
        runs: set[str] = set()
        files: dict[str, set[str]] = {}
        for item in self.tree.selectedItems():
            data = item.data(0, Qt.UserRole)
            if not data:
                continue
            if data[0] == "run":
                runs.add(data[1].run_id)
            else:
                files.setdefault(data[1].run_id, set()).add(data[2].original)
        files = {r: f for r, f in files.items() if r not in runs}
        if not runs and not files:
            return                      # (nothing selected: "Delete All History and Logs…" is the way to clear it)
        everything = False
        n_files = sum(len(f) for f in files.values())
        what = ("the whole history" if everything else
                " and ".join(x for x in (f"{len(runs)} run{'s' if len(runs) != 1 else ''}" if runs else "",
                                         f"{n_files} file{'s' if n_files != 1 else ''}" if n_files else "") if x))
        if not ask(self, f"Delete {what}?",
                   "Their names and details are removed from this computer, and they can no longer be undone from "
                   f"here. Your files don't change, and originals stay in the {BIN_NAME} until it's emptied.",
                   "Delete", destructive=True):
            return
        if everything:
            journal.clear()
        else:
            journal.delete_runs(runs)
            for run_id, originals in files.items():
                journal.forget_files(run_id, originals)
        self.refresh(force=True)

    def export_report(self) -> None:
        from .. import report
        from .jobstatus import save_report

        runs = self._selected_runs()
        if not runs:
            inform(self, "Nothing in the history yet.")
            return
        save_report(self, lambda: report.runs_rows(runs))

    def undo_selected(self) -> None:
        if self.undo_thread is not None and self.undo_thread.isRunning():
            return
        by_run: dict[str, tuple[object, list]] = {}
        for item in self.tree.selectedItems():
            data = item.data(0, Qt.UserRole)
            if not data:
                continue
            run = data[1]
            recs = run.files if data[0] == "run" else [data[2]]
            entry = by_run.setdefault(run.run_id, (run, []))
            for rec in recs:
                if not rec.undone and rec not in entry[1]:
                    entry[1].append(rec)
        blocked = sum(1 for rid, (_, recs) in by_run.items() for rec in recs if (rid, rec.original) in self.blocked)
        jobs = [(rid, [rec for rec in recs if (rid, rec.original) not in self.blocked])
                for rid, (_, recs) in by_run.items()]
        jobs = [(rid, recs) for rid, recs in jobs if recs]
        if not jobs:
            inform(self, "Nothing here can be undone." if blocked else
                   "Select a run or files that haven't been undone.",
                   f"The originals are no longer in the {BIN_NAME} (it was emptied)." if blocked else "")
            return
        count = sum(len(r) for _, r in jobs)
        extra = (f"\n\n{blocked} other file{'s' if blocked != 1 else ''} can't be undone: "
                 f"{'their originals are' if blocked != 1 else 'its original is'} no longer in the {BIN_NAME}."
                 if blocked else "")
        if not ask(self, f"Restore {count} original file{'s' if count != 1 else ''}?",
                   "The compressed versions will be deleted." + extra, "Restore"):
            return
        self._run_undo_jobs(jobs, [0, 0], [])

    def _run_undo_jobs(self, jobs, totals, failures) -> None:
        if not jobs:
            self.refresh()
            self.undo_btn.setEnabled(True)
            inform(self, f"Restored {totals[0]} file{'s' if totals[0] != 1 else ''}.",
                   f"{totals[1]} could not be restored:\n\n" + "\n".join(failures[:15]) if totals[1] else "")
            return
        run_id, recs = jobs[0]
        self.undo_btn.setEnabled(False)
        self.undo_thread = UndoThread(run_id, recs, self)
        self.undo_thread.item_done.connect(
            lambda rec, ok, msg: None if ok else failures.append(f"{Path(rec.original).name}: {msg}"))

        def finished(ok, failed):
            totals[0] += ok
            totals[1] += failed
            self._run_undo_jobs(jobs[1:], totals, failures)

        self.undo_thread.done.connect(finished)
        self.undo_thread.start()
