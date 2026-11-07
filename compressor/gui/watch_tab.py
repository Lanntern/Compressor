"""Watch page: folders that are compressed automatically when new files appear."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QCheckBox, QComboBox, QFileDialog, QFrame, QLabel, QListWidget,
                               QListWidgetItem)

from .. import presets
from ..engine import DONE, FAILED, run_job
from ..formats import human
from ..plan import plan_entry
from ..scanner import scan
from ..settings import Settings
from .filelist import ListPlaceholder
from .style import Size, Space, Type, icon
from .watcher import FolderWatcher
from .widgets import Card, FlowLayout, Page, ToggleSwitch, button, hbox, text


class WatchJobThread(QThread):
    finished_files = Signal(list, int, int, int)   # outputs, done, failed, saved bytes
    log = Signal(str)

    def __init__(self, groups: list[tuple[list[str], Settings]], parent=None):
        super().__init__(parent)
        self.groups = groups  # files that share options (same watched folder preset)

    def run(self) -> None:
        from .. import power

        with power.background():               # new files in a watched folder: nobody's waiting
            self._run_all()

    def _run_all(self) -> None:
        outputs, done, failed, saved = [], 0, 0, 0
        for paths, s in self.groups:
            try:
                o, d, f, b = self._run_group(paths, s)
            except Exception as e:  # noqa: BLE001 - say so, and keep watching
                from ..log import log

                log.exception("Watched files failed")
                self.log.emit(f"Couldn't process {len(paths)} new file(s): {e}")
                o, d, f, b = [], 0, len(paths), 0
            outputs += o
            done, failed, saved = done + d, failed + f, saved + b
        self.finished_files.emit(outputs, done, failed, saved)

    def _run_group(self, paths: list[str], s: Settings) -> tuple[list[str], int, int, int]:
        result = scan([Path(p) for p in paths], s)
        items = []
        for e in result.entries:
            p = plan_entry(e, s)
            if p.convert:
                items.append((e, p))
            else:
                self.log.emit(f"Skipped {e.path.name}: {p.reason}")
        if not items:
            return [], 0, 0, 0
        results, _ = run_job(items, s, Path(paths[0]).parent, tool="watch")
        outputs = []
        for r in results:
            if r.status == DONE:
                outputs.append(str(r.output))
                self.log.emit(f"Compressed {r.entry.path.name} → {r.output.name}, saved {human(r.saved)}")
            else:
                self.log.emit(f"{'Failed' if r.status == FAILED else 'Skipped'}: {r.entry.path.name}: {r.message}")
        return (outputs, sum(r.status == DONE for r in results), sum(r.status == FAILED for r in results),
                sum(r.saved for r in results))


def _list(name: str) -> QListWidget:
    lw = QListWidget()
    lw.setObjectName("plainList")
    lw.setAccessibleName(name)
    lw.setFrameShape(QFrame.NoFrame)
    lw.setIconSize(QSize(Size.ICON_M, Size.ICON_M))
    lw.setSelectionMode(QAbstractItemView.ExtendedSelection)
    lw.setAlternatingRowColors(True)
    return lw


class WatchTab(Page):
    state_changed = Signal(bool)

    def __init__(self, window):
        super().__init__("Watch Folders")
        self.win = window
        self.qs: QSettings = window.qsettings
        self.watcher = FolderWatcher(self)
        self.watcher.files_ready.connect(self._files_ready)
        self.watcher.error.connect(lambda m: self._log(f"Watch error: {m}"))
        self.queue: list[str] = []
        self.job: WatchJobThread | None = None
        self.saved_total = 0

        self.add_btn = button("Add Folder…", "", "folder", "Watch another folder")
        self.add_btn.clicked.connect(self.add_folder)
        self.switch = ToggleSwitch("Watch folders")
        self.switch_label = text("Off", Type.BODY, bold=True)
        self.switch.toggled.connect(self.set_enabled)
        self.action_bar.addWidget(self.add_btn)
        self.action_bar.addSpacing(Space.M)
        self.action_bar.addWidget(self.switch_label)
        self.action_bar.addWidget(self.switch)

        folders_card = Card()
        folders_card.lay.addLayout(hbox(text("Folders", Type.HEADLINE, bold=True), None))
        folders_card.lay.addWidget(text("New images, videos and audio in these folders are compressed automatically, "
                                        "with the options from the Compress page or the folder's preset.",
                                        Type.FOOTNOTE, secondary=True, wrap=True))
        self.folders = _list("Watched folders")
        self.folders.setMinimumHeight(Size.ROW * 3)
        folders_card.lay.addWidget(self.folders, 1)
        self.folders_empty = ListPlaceholder(self.folders)
        self.recursive = QCheckBox("Include subfolders")
        self.recursive.setChecked(self.qs.value("watch_recursive", True, bool))
        self.recursive.toggled.connect(lambda v: (self.qs.setValue("watch_recursive", v), self._restart()))
        self.remove_btn = button("Remove", "", "minus", "Stop watching the selected folders", "Delete")
        self.remove_btn.clicked.connect(self.remove_folder)
        self.preset = QComboBox()
        self.preset.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)   # long preset names
        self.preset.setMinimumContentsLength(14)                                          # don't widen the page
        self.preset.setAccessibleName("Options for the selected folder")
        self.preset.setToolTip("Which options new files in the selected folder are compressed with")
        self.preset.activated.connect(self._preset_chosen)
        self.preset_label = QLabel("Options")
        self.preset_label.setBuddy(self.preset)
        options = FlowLayout()          # wraps instead of widening the page (large text, a narrow window)
        options.add_group(self.recursive)
        options.add_group(self.preset_label, self.preset)
        row = QHBoxLayout()
        row.setSpacing(Space.L)
        row.addLayout(options, 1)
        row.addWidget(self.remove_btn, 0, Qt.AlignBottom)
        folders_card.lay.addLayout(row)
        # Screenshots and recordings fill up fast and shrink well: offer them with one click.
        self.suggest_row = QHBoxLayout()
        self.suggest_row.setSpacing(Space.S)
        folders_card.lay.addLayout(self.suggest_row)
        self.folders.itemSelectionChanged.connect(self._selection_changed)
        self.body.addWidget(folders_card, 2)

        from .music_copy import MusicCopyCard

        self.music = MusicCopyCard(self.qs)
        self.music.synced.connect(self._log)
        self.body.addWidget(self.music)

        activity = Card()
        self.status = text("Not watching.", Type.FOOTNOTE, secondary=True)
        activity.lay.addLayout(hbox(text("Activity", Type.HEADLINE, bold=True), None, self.status))
        self.logw = _list("Activity log")
        self.logw.setSelectionMode(QAbstractItemView.NoSelection)
        activity.lay.addWidget(self.logw, 1)
        self.log_empty = ListPlaceholder(self.logw)
        self.log_empty.show_message("Files compressed while watching are listed here.")
        self.body.addWidget(activity, 3)

        try:
            self.folder_presets: dict[str, str] = dict(json.loads(self.qs.value("watch_presets", "{}", str)))
        except (ValueError, TypeError):
            self.folder_presets = {}
        for f in self.qs.value("watch_folders", [], list) or []:
            if f:
                self._add_item(f)
        self.refresh_presets()
        self.refresh_suggestions()
        if self.qs.value("watch_enabled", False, bool) and self.folders.count():
            self.switch.setChecked(True)
        self._update_empty()

    # ---- folders
    def _add_item(self, path: str) -> None:
        it = QListWidgetItem(icon("folder", None, Size.ICON_M), path)
        it.setData(Qt.UserRole, path)
        self.folders.addItem(it)
        self._label_item(it)

    def _label_item(self, it: QListWidgetItem) -> None:
        path = it.data(Qt.UserRole)
        name = self.folder_presets.get(path, "")
        it.setText(f"{path}   ·   {name}" if name else path)
        it.setToolTip(f"{path}\nOptions: {name or 'Compress page'}")

    def _update_empty(self) -> None:
        self.remove_btn.setEnabled(self.folders.count() > 0)
        if self.folders.count():
            self.folders_empty.clear()
        else:
            self.folders_empty.show_message("No folders yet. Add a folder to compress new files in it "
                                            "automatically.")
        self._selection_changed()

    def folder_paths(self) -> list[Path]:
        return [Path(self.folders.item(i).data(Qt.UserRole)) for i in range(self.folders.count())]

    def _save(self) -> None:
        self.qs.setValue("watch_folders", [str(p) for p in self.folder_paths()])
        kept = {str(p) for p in self.folder_paths()}
        self.folder_presets = {k: v for k, v in self.folder_presets.items() if k in kept and v}
        self.qs.setValue("watch_presets", json.dumps(self.folder_presets))
        self._update_empty()

    # ---- presets per folder
    def refresh_presets(self) -> None:
        """Rebuild the preset list (after presets are saved or deleted elsewhere)."""
        self.preset.blockSignals(True)
        self.preset.clear()
        self.preset.addItem("Compress page options", "")
        for name in presets.names():
            self.preset.addItem(name, name)
            self.preset.setItemData(self.preset.count() - 1, presets.description(name), Qt.ToolTipRole)
        self.preset.blockSignals(False)
        self.folder_presets = {k: v for k, v in self.folder_presets.items() if presets.get(v) is not None}
        for i in range(self.folders.count()):
            self._label_item(self.folders.item(i))
        self._selection_changed()

    def _selection_changed(self) -> None:
        sel = self.folders.selectedItems()
        self.preset.setEnabled(bool(sel))
        self.preset_label.setEnabled(bool(sel))
        names = {self.folder_presets.get(it.data(Qt.UserRole), "") for it in sel}
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(max(0, self.preset.findData(names.pop())) if len(names) == 1 else 0)
        self.preset.blockSignals(False)

    def _preset_chosen(self, _index: int) -> None:
        name = self.preset.currentData() or ""
        for it in self.folders.selectedItems():
            self.folder_presets[it.data(Qt.UserRole)] = name
            self._label_item(it)
        self._save()

    def settings_for(self, file_path: str) -> Settings:
        """Options for a new file: its watched folder's preset, else the Compress page's."""
        base = self.win.compress.panel.settings()
        p = os.path.normcase(os.path.abspath(file_path))
        best, best_len = "", -1
        for folder, name in self.folder_presets.items():
            f = os.path.normcase(os.path.abspath(folder))
            if (p == f or p.startswith(f.rstrip(os.sep) + os.sep)) and len(f) > best_len:
                best, best_len = name, len(f)
        if best and presets.get(best) is not None:
            return presets.apply(best, base)
        return base

    def add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Watch a folder", str(Path.home()))
        if d and os.path.normpath(d) not in [str(p) for p in self.folder_paths()]:
            self._add_item(os.path.normpath(d))
            self._save()
            self._restart()

    def refresh_suggestions(self) -> None:
        from ..suggest import watch_suggestions

        while self.suggest_row.count():
            w = self.suggest_row.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        found = watch_suggestions(self.folder_paths())
        if not found:
            return
        self.suggest_row.addWidget(text("Suggested:", Type.FOOTNOTE, secondary=True))
        for label, path, preset in found:
            b = button(label, "", "plus", f"Watch {path}" + (f" with the {preset} preset" if preset else ""))
            b.clicked.connect(lambda _=False, p=path, pr=preset: self._add_suggested(p, pr))
            self.suggest_row.addWidget(b)
        self.suggest_row.addStretch(1)

    def _add_suggested(self, path: Path, preset: str) -> None:
        p = os.path.normpath(str(path))
        if p not in [str(x) for x in self.folder_paths()]:
            if preset:
                self.folder_presets[p] = preset
            self._add_item(p)
            self._save()
            self._restart()
        self.refresh_suggestions()

    def remove_folder(self) -> None:
        for item in self.folders.selectedItems():
            self.folders.takeItem(self.folders.row(item))
        self._save()
        self._restart()
        self.refresh_suggestions()

    # ---- state
    def set_enabled(self, on: bool) -> None:
        if on and not self.folder_paths():
            self.switch.blockSignals(True)
            self.switch.setChecked(False)
            self.switch.blockSignals(False)
            self.status.setText("Add a folder first.")
            return
        self.qs.setValue("watch_enabled", on)
        self.switch_label.setText("On" if on else "Off")
        self.switch.setAccessibleDescription("on" if on else "off")
        if on:
            self.watcher.start(self.folder_paths(), self.recursive.isChecked())
            self.status.setText(f"Watching {len(self.folder_paths())} folder(s)")
            self._log("Started watching")
        else:
            self.watcher.stop()
            self.status.setText("Not watching.")
            self._log("Stopped watching")
        self.state_changed.emit(on)

    @property
    def watching(self) -> bool:
        return self.switch.isChecked()

    @property
    def toggle(self):  # compatibility for the tray menu
        return self.switch

    def _restart(self) -> None:
        if self.watching:
            if not self.folder_paths():
                self.switch.setChecked(False)
                return
            self.watcher.start(self.folder_paths(), self.recursive.isChecked())
            self.status.setText(f"Watching {len(self.folder_paths())} folder(s)")

    def ignore(self, paths) -> None:
        for p in paths:
            self.watcher.ignore(Path(p))

    def _files_ready(self, paths: list[str]) -> None:
        self.queue.extend(paths)
        self._next()

    def _next(self) -> None:
        if self.job is not None and self.job.isRunning() or not self.queue:
            return
        batch, self.queue = self.queue[:200], self.queue[200:]
        self._log(f"Compressing {len(batch)} new file(s)…")
        groups: dict[str, tuple[list[str], Settings]] = {}
        for path in batch:
            s = self.settings_for(path)
            key = json.dumps(s.to_dict(), sort_keys=True)
            groups.setdefault(key, ([], s))[0].append(path)
        self.job = WatchJobThread(list(groups.values()), self)
        self.job.log.connect(self._log)
        self.job.finished_files.connect(self._batch_done)
        self.job.finished.connect(self._next)
        self.job.start()

    def _batch_done(self, outputs: list, done: int, failed: int, saved: int) -> None:
        self.ignore(outputs)
        self.saved_total += saved
        self.status.setText(f"Watching {len(self.folder_paths())} folder(s) · saved {human(self.saved_total)} "
                            f"this session")
        self.win.history.refresh()
        if done or failed:
            self.win.tray_message(f"Compressed {done} new file(s), saved {human(saved)}"
                                  + (f"; {failed} failed" if failed else ""))

    def _log(self, line: str) -> None:
        self.logw.insertItem(0, f"{time.strftime('%H:%M')}   {line}")
        self.log_empty.clear()
        while self.logw.count() > 500:
            self.logw.takeItem(self.logw.count() - 1)

    def refresh(self) -> None:
        for i in range(self.folders.count()):
            self.folders.item(i).setIcon(icon("folder", None, Size.ICON_M))

    def shutdown(self) -> None:
        self.music.shutdown()
        self.watcher.stop()
        if self.job is not None:
            self.job.wait(30000)
