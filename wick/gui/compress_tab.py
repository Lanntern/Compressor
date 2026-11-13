"""Compress page: add inputs, review the plan, preview, compress, retry."""

from __future__ import annotations

import copy
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QInputDialog,
    QScrollArea, QStackedWidget, QStyledItemDelegate, QTableView,
    QVBoxLayout, QWidget,
)

from .. import pending, tools
from ..engine import DONE
from ..formats import AUDIO, DOCUMENT, KIND_LABEL, VIDEO, human, type_label
from ..plan import plan_entry
from .file_model import (
    C_CHECK, C_CREATED, C_EST, C_FOLDER, C_INFO, C_MODIFIED, C_NAME, C_SIZE, C_STATUS, C_TYPE, FileFilterProxy,
    FileTableModel,
    open_in_file_manager,
    open_in_file_manager_many,
)
from .preview import PreviewDialog
from .thumbs import HoverPreviewer
from .settings_panel import BIN_NAME, SettingsPanel
from .dialogs import ask, inform, warn
from .filelist import ListPlaceholder, click_to_check, file_menu, keyboard_list, open_file
from .style import Size, Space, Type, safe_filter, text_scale
from .jobstatus import JobStatus, PauseControl, ResumeBanner
from .header import Header
from .widgets import (CARD_INSET, Card, DropZone, Page, StatTile, StatusDelegate, available_width, button,
                      hbox, page_top, set_compact, text)
from .workers import JobThread, ScanThread

LIST_COMFORT_W = 596   # the options panel starts hidden unless the list keeps this much room (× the text size)
COLUMN_WIDTHS = {C_FOLDER: 168, C_TYPE: 112, C_INFO: 250, C_SIZE: 92, C_EST: 136, C_CREATED: 136, C_MODIFIED: 136,
                 C_STATUS: 240}
NAME_MIN = 160                     # Name stretches; other columns hide (in this order) to keep it readable
HIDE_ORDER = (C_CREATED, C_MODIFIED, C_TYPE, C_FOLDER, C_EST, C_INFO)   # first to hide on narrow windows ... last


class _ElideRight(QStyledItemDelegate):
    def initStyleOption(self, option, index) -> None:  # noqa: N802
        super().initStyleOption(option, index)
        option.textElideMode = Qt.ElideRight


class CompressTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self.scan_thread: ScanThread | None = None
        self.job_thread: JobThread | None = None
        self.paths: list[Path] = []
        self.removed: set[str] = set()    # files taken off the list by hand (normalized paths)
        self.scan_roots: list[Path] = []
        self.skipped_dirs: list[Path] = []
        self.cloud_only = 0
        self._rescan_after = False
        self._inspector_user_choice: bool | None = None

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ------------------------------------------------ page
        self.page = Page("Compress")
        self.add_files_btn = button("Add Files…", "", "files", "Add individual files", "Ctrl+O")
        self.add_folder_btn = button("Add Folder…", "", "folder", "Add a folder", "Ctrl+Shift+O")
        self.options_btn = button("Options", "", "sidebar", "Show or hide compression options", "Ctrl+I")
        self.options_btn.setCheckable(True)
        self.compress_btn = button("Compress", "primary", "compress", "Compress the selected files", "Ctrl+Enter")
        self.compress_btn.setEnabled(False)
        for b in (self.add_files_btn, self.add_folder_btn, self.options_btn, self.compress_btn):
            self.page.action_bar.addWidget(b)

        self.stack = QStackedWidget()
        self.drop = DropZone()
        self.drop.add_folder.clicked.connect(self.browse_folder)
        self.drop.add_files.clicked.connect(self.browse_files)
        self.stack.addWidget(self.drop)

        results = QWidget()
        rl = QVBoxLayout(results)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(Space.L)
        self.tile_files = StatTile("Selected")
        self.tile_size = StatTile("Current size")
        self.tile_save = StatTile("Could save", accent=True)
        tiles = hbox(self.tile_files, self.tile_size, self.tile_save, spacing=Space.L)
        rl.addLayout(tiles)

        # progress while working; afterwards, what happened and where the result is
        self.job_status = JobStatus("Compressing", "compressed")
        self.job_status.stop_requested.connect(self.cancel)
        self.pauser = PauseControl(self.job_status, window)
        rl.addWidget(self.job_status)

        table_card = Card(padding=0, spacing=0, inset=True)
        bar = QHBoxLayout()
        bar.setContentsMargins(Space.M, Space.M, Space.M, Space.S)
        bar.setSpacing(Space.S)
        self.select_all = button("Select All", "", None, "Select every file that can be compressed", "Ctrl+A")
        self.select_none = button("Select None", "", None, "Deselect everything", "Ctrl+Shift+A")
        self.preview_btn = button("Preview", "", "eye", "Compare the selected file before and after (videos: a short "
                                                        "sample)", "Space")
        self.retry_btn = button("Retry Failed", "", "retry", "Run the failed files again", "Ctrl+Shift+R")
        self.retry_btn.setVisible(False)
        self.rescan_btn = button("Rescan", "", "refresh", "Scan the added folders again", "Ctrl+R")
        self.show_skipped = QCheckBox("Show skipped")
        self.show_skipped.setToolTip("Also list files that won't be compressed, with the reason")
        for w in (self.select_all, self.select_none, self.preview_btn, self.retry_btn):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.show_skipped)
        bar.addWidget(self.rescan_btn)
        self.clear_btn = button("Clear", "", "xmark", "Empty the list")
        bar.addWidget(self.clear_btn)
        table_card.lay.addLayout(bar)
        self._list_bar = bar

        self.model = FileTableModel(self)
        self.proxy = FileFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setAccessibleName("Files")
        self.table.setAccessibleDescription("Files found. Space previews, Delete excludes, Enter compresses.")
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setFrameShape(QFrame.NoFrame)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(Size.ROW)
        self.table.setItemDelegateForColumn(C_STATUS, StatusDelegate(self.table))
        self.table.setItemDelegateForColumn(C_INFO, _ElideRight(self.table))  # keep "1920×1080 · 30 fps" readable
        self.table.setHorizontalHeader(Header(self.table))
        self.table.setSortingEnabled(True)  # after the header swap, so the new header shows the indicator
        self.table.sortByColumn(C_SIZE, Qt.DescendingOrder)
        self.table.horizontalHeader().remember("compress")
        h = self.table.horizontalHeader()

        def toggle_all():
            self.model.set_all_checked(self.model.check_state() != Qt.Checked)
            self.update_summary()

        h.set_check(C_CHECK, self.model.check_state, toggle_all)
        self.model.dataChanged.connect(lambda *_: h.viewport().update())
        self.model.modelReset.connect(lambda: h.viewport().update())
        h.setHighlightSections(False)
        h.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        h.setSectionResizeMode(C_CHECK, QHeaderView.Fixed)
        self.table.setColumnWidth(C_CHECK, Space.XXL + Space.S)
        # Every column can be dragged; the last one (Status) fills the rest. (A stretching Name column
        # made dragging another column's edge move the columns beside it instead of the edge.)
        h.setSectionResizeMode(C_NAME, QHeaderView.Interactive)
        for col, w in COLUMN_WIDTHS.items():
            h.setSectionResizeMode(col, QHeaderView.Interactive)
            self.table.setColumnWidth(col, w)
        h.keep_in_view = True
        # Right-click a heading to choose columns (dates are off until wanted).
        h.enable_column_menu("compress", [C_FOLDER, C_TYPE, C_INFO, C_EST, C_CREATED, C_MODIFIED],
                             {C_CREATED, C_MODIFIED})
        h.column_choice_changed = self._fit_columns
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setTextElideMode(Qt.ElideMiddle)
        h.setMinimumSectionSize(Space.XXL)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        keyboard_list(self.table, self._table_menu)
        click_to_check(self.table, C_CHECK)
        self.placeholder = ListPlaceholder(self.table)
        self.table.doubleClicked.connect(self.preview_row)
        self.hover = HoverPreviewer(self.table, self._hover_info, self, column=C_NAME)
        self.table.viewport().installEventFilter(self)  # refit columns whenever the table's width changes
        table_card.lay.addWidget(self.table, 1)
        rl.addWidget(table_card, 1)
        self.stack.addWidget(results)
        self.resume_banner = ResumeBanner()
        self.resume_banner.resume.connect(self._resume_pending)
        self.resume_banner.discard.connect(self._discard_pending)
        self.page.body.addWidget(self.resume_banner)
        self._resuming = False
        QTimer.singleShot(0, self._check_pending)
        self.page.body.addWidget(self.stack, 1)
        root.addWidget(self.page, 1)

        # ------------------------------------------------ inspector (right)
        self.inspector = QFrame()
        self.inspector.setObjectName("inspector")
        self.inspector.setFixedWidth(Size.INSPECTOR_W)
        il = QVBoxLayout(self.inspector)
        il.setContentsMargins(0, page_top(), 0, 0)
        il.setSpacing(0)
        head = text("Options", Type.TITLE3, bold=True)
        head.setContentsMargins(Space.L, 0, Space.L, Space.XS)
        il.addWidget(head)
        self.panel = SettingsPanel()
        self.panel.setObjectName("inspectorBody")
        scroll = QScrollArea()
        scroll.setObjectName("inspectorScroll")
        scroll.setWidget(self.panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        il.addWidget(scroll, 1)
        root.addWidget(self.inspector)
        self.options_btn.setChecked(True)
        self.options_btn.toggled.connect(self._options_toggled)

        # ------------------------------------------------ wiring
        self.add_files_btn.clicked.connect(self.browse_files)
        self.add_folder_btn.clicked.connect(self.browse_folder)
        self.compress_btn.clicked.connect(lambda: self.start_job())
        self.preview_btn.clicked.connect(self.preview_selected)
        self.retry_btn.clicked.connect(self.retry_failed)
        self.rescan_btn.clicked.connect(self.start_scan)
        self.clear_btn.clicked.connect(self.clear)
        self.select_all.clicked.connect(lambda: (self.model.set_all_checked(True), self.update_summary()))
        self.select_none.clicked.connect(lambda: (self.model.set_all_checked(False), self.update_summary()))
        self.show_skipped.toggled.connect(self.proxy.set_show_skipped)
        self._summary_timer = QTimer(self, singleShot=True, interval=100)
        self._summary_timer.timeout.connect(self.update_summary)
        self.model.dataChanged.connect(lambda *_: self._summary_timer.start())
        self.model.modelReset.connect(self._summary_timer.start)
        self._replan_timer = QTimer(self, singleShot=True, interval=150)
        self._replan_timer.timeout.connect(self._replan)
        self.panel.changed.connect(self._replan_timer.start)
        self.update_summary()

    # ------------------------------------------------------------------ responsive layout
    def _options_toggled(self, on: bool) -> None:
        self._inspector_user_choice = on
        self.inspector.setVisible(on)
        QTimer.singleShot(0, self._fit_columns)

    def adapt_to_width(self, width: int) -> None:
        """Called on window resize: options panel, table columns and header buttons adapt."""
        if self._inspector_user_choice is None:
            show = width - Size.SIDEBAR_W - Size.INSPECTOR_W >= LIST_COMFORT_W * text_scale()
            self.inspector.setVisible(show)
            self.options_btn.blockSignals(True)
            self.options_btn.setChecked(show)
            self.options_btn.blockSignals(False)
        QTimer.singleShot(0, self._fit_columns)

    def _fit_columns(self) -> None:
        available = self.table.viewport().width()
        fixed = self.table.columnWidth(C_CHECK) + COLUMN_WIDTHS[C_SIZE] + COLUMN_WIDTHS[C_STATUS]
        off = self.table.horizontalHeader().user_hidden
        optional = [c for c in reversed(HIDE_ORDER) if c not in off]
        shown: list[int] = []
        for col in optional:  # add back in priority order while Name keeps its minimum
            if available - fixed - sum(COLUMN_WIDTHS[c] for c in shown) - COLUMN_WIDTHS[col] < NAME_MIN:
                break  # don't let a lower-priority column take the place of a more useful one
            shown.append(col)
        for col in HIDE_ORDER:
            self.table.setColumnHidden(col, col not in shown)
        # Name takes the room the window gives (columns keep the widths they were dragged to).
        h = self.table.horizontalHeader()
        middle = sum(self.table.columnWidth(c) for c in COLUMN_WIDTHS if c != C_STATUS and not self.table.isColumnHidden(c))
        name = available - self.table.columnWidth(C_CHECK) - middle - COLUMN_WIDTHS[C_STATUS]
        h._clamping = True
        try:
            self.table.setColumnWidth(C_NAME, max(NAME_MIN, name))
        finally:
            h._clamping = False
        h.fill_last()
        self._fit_toolbar()
        # Icon-only header buttons only when their labels really don't fit next to the title.
        buttons = (self.add_files_btn, self.add_folder_btn, self.options_btn)
        for b in buttons:
            set_compact(b, False)
        needed = (self.page.title.sizeHint().width() + Space.XXL * 3 + Space.S * 4
                  + sum(b.sizeHint().width() for b in (*buttons, self.compress_btn)))
        compact = self.page.width() < needed
        for b in buttons:
            set_compact(b, compact)

    def _fit_toolbar(self) -> None:
        """The list's toolbar never squeezes its buttons: first Select All/None go (the checkbox in the list's
        header does the same), then Preview, Retry and Rescan show only their icons."""
        room = min(self.table.width(), available_width(self.table) - 2 * CARD_INSET)
        selects = (self.select_all, self.select_none)
        iconic = (self.preview_btn, self.retry_btn, self.rescan_btn, self.clear_btn)

        def need() -> int:      # the row's real minimum (its margins, spacing and every shown control)
            self._list_bar.invalidate()
            return self._list_bar.minimumSize().width()

        for b in iconic:
            set_compact(b, False)
        for b in selects:
            b.setVisible(True)
        if need() > room:
            for b in selects:
                b.setVisible(False)
        if need() > room:
            for b in iconic:
                set_compact(b, True)

    @safe_filter
    def eventFilter(self, obj, ev):  # noqa: N802
        from PySide6.QtCore import QEvent

        if obj is self.table.viewport() and ev.type() == QEvent.Resize:
            self._fit_columns()
        return super().eventFilter(obj, ev)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        QTimer.singleShot(0, self._fit_columns)

    def toggle_options(self) -> None:
        self.options_btn.setChecked(not self.inspector.isVisible())

    # ------------------------------------------------------------------ inputs
    def input_paths(self) -> list[Path]:
        return list(self.paths)

    def set_inputs_text(self, value: str) -> None:
        parts = [p.strip().strip('"') for p in value.split(";")]
        self.paths = [Path(p) for p in parts if p]

    def inputs_text(self) -> str:
        return " ; ".join(str(p) for p in self.paths)

    def add_paths(self, paths: list[str], scan: bool = True) -> None:
        keys = {os.path.normcase(str(p)) for p in self.paths}
        for p in paths:
            p = os.path.normpath(p)
            if os.path.normcase(p) not in keys:
                self.paths.append(Path(p))
                keys.add(os.path.normcase(p))
        if scan:
            if self.busy():
                self._rescan_after = True
            else:
                self.start_scan()

    def browse_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Add a folder", self._last_dir())
        if d:
            self._remember_dir(d)
            self.add_paths([d])

    def _last_dir(self) -> str:
        """Where Add Folder / Add Files last were (else the home folder)."""
        last = str(self.win.qsettings.value("browse/last_dir", "", str)) if hasattr(self, "win") else ""
        return last if last and os.path.isdir(last) else str(Path.home())

    def _remember_dir(self, picked: str) -> None:
        if hasattr(self, "win"):
            self.win.qsettings.setValue("browse/last_dir", picked if os.path.isdir(picked) else os.path.dirname(picked))

    def browse_files(self) -> None:
        exts = "*.jpg *.jpeg *.jfif *.png *.apng *.gif *.webp *.tif *.tiff *.bmp *.heic *.heif *.avif " \
               "*.mp4 *.m4v *.mov *.mkv *.webm *.avi *.wmv *.flv *.mpg *.mpeg *.ts *.m2ts *.mts *.3gp"
        files, _ = QFileDialog.getOpenFileNames(self, "Add files", self._last_dir(),
                                                f"Images and videos ({exts});;All files (*)")
        if files:
            self._remember_dir(files[0])
            self.add_paths(files)

    def type_path(self) -> None:
        value, ok = QInputDialog.getText(self, "Add a path", "Folder or file path (separate several with ;):")
        if ok and value.strip():
            self.add_paths([p.strip().strip('"') for p in value.split(";") if p.strip()])

    def clear(self) -> None:
        if self.busy():
            return
        self.paths = []
        self.removed = set()
        self.scan_roots = []
        self.model.set_entries([], self.panel.settings())
        self.panel.set_file_kinds(None)
        self.job_status.hide()          # the last job's outcome goes with its list
        self.update_summary()

    # ------------------------------------------------------------------ scan
    def start_scan(self) -> None:
        if self.busy():
            self._rescan_after = True
            return
        paths = self.input_paths()
        missing = [str(p) for p in paths if not p.exists()]
        if not paths:
            self.update_summary()
            return
        if missing:
            warn(self, "Some items no longer exist", "\n".join(missing))
            self.paths = [p for p in self.paths if p.exists()]
            if not self.paths:
                self.update_summary()
                return
            paths = self.paths
        drives = [p for p in paths if p.is_dir() and p.anchor and Path(p.anchor) == p]
        if drives and not ask(self, f"Scan the whole drive {drives[0]}?",
                              "This can take a while. System, program and game folders are skipped.", "Scan"):
            return
        self.win.save_settings()
        self.scan_roots = paths
        self.model.set_entries([], self.panel.settings())
        self.scan_thread = ScanThread(paths, self.panel.settings(), self)
        self.scan_thread.progress.connect(self._scan_progress)
        self.scan_thread.result.connect(self._scan_done)
        self.scan_thread.error.connect(lambda m: self._fail(f"Scan failed: {m}"))
        self.scan_thread.finished.connect(self._thread_finished)
        self._set_busy(True, scanning=True)
        self.job_status.busy("Looking for images, videos and audio…")
        self.placeholder.show_loading("Looking for images, videos and audio…")
        self.scan_thread.start()

    def _scan_progress(self, msg: str, done: int, total: int) -> None:
        self.job_status.busy(msg, done, total)
        if self.placeholder.loading:
            self.placeholder.show_loading(f"Reading file details… {done:,} of {total:,}" if total else
                                          "Looking for images, videos and audio…")

    def _scan_done(self, result) -> None:
        kinds = {"video" if e.kind == VIDEO else "audio" if e.kind == AUDIO else
                 "document" if e.kind == DOCUMENT else "image" for e in result.entries}
        self.panel.set_file_kinds(kinds or None)  # only the options that matter for these files
        self.skipped_dirs = result.skipped_program_dirs
        self.cloud_only = result.cloud_only_files
        entries = [e for e in result.entries if os.path.normcase(str(e.path)) not in self.removed]
        self.model.set_entries(entries, self.panel.settings())
        self.update_summary()

    def _replan(self) -> None:
        self.win.save_settings()
        if self.busy() or not self.model.rows:
            return
        self.model.replan(self.panel.settings())
        self.proxy.invalidate()
        self.update_summary()

    # ------------------------------------------------------------------ summary
    def update_summary(self) -> None:
        has_rows = bool(self.model.rows)
        self.stack.setCurrentIndex(1 if (has_rows or self.busy()) else 0)
        n, size, est = self.model.totals()
        failed = self.model.failed_count()
        self.retry_btn.setVisible(failed > 0 and not self.busy())
        self.retry_btn.setProperty("fullText", None)        # (a compact toolbar keeps the new label)
        self.retry_btn.setText(f"Retry Failed ({failed})")
        self._fit_toolbar()
        self.rescan_btn.setEnabled(bool(self.paths) and not self.busy())
        self.clear_btn.setEnabled(bool(self.model.rows or self.paths) and not self.busy())
        self.compress_btn.setEnabled(n > 0 and not self.busy())
        self.compress_btn.setText(f"Compress {n} File{'s' if n != 1 else ''}" if n else "Compress")

        names = ", ".join(p.name or str(p) for p in self.paths[:3]) + (f" and {len(self.paths) - 3} more"
                                                                          if len(self.paths) > 3 else "")
        if not self.paths:
            self.page.subtitle.setText("")
        elif self.busy() and not has_rows:
            self.page.subtitle.setText(f"Scanning {names}…")
        else:
            convertible = sum(1 for r in self.model.rows if r.plan.convert)
            extra: list = []
            if self.skipped_dirs:
                extra.append(f"{len(self.skipped_dirs)} program/game folder(s) skipped")
            if self.cloud_only:
                extra.append(f"{self.cloud_only} cloud-only file(s) skipped")
            self.page.subtitle.setText(
                f"{names} · {len(self.model.rows)} file{'s' if len(self.model.rows) != 1 else ''} found, "
                f"{convertible} can be compressed"
                + (f" · {', '.join(extra)}" if extra else ""))

        done = [r for r in self.model.rows if r.result and r.result.status == DONE]
        saved = sum(r.result.saved for r in done)
        self.tile_files.set(f"{n:,}" if n else "0", f"of {self.model.selectable():,} possible")
        self.tile_size.set(human(size) if n else "—", f"about {human(est)} after" if n else "")
        if done and not n:
            self.tile_save.label.setText("Saved")
            self.tile_save.set(human(saved), f"{len(done)} file{'s' if len(done) != 1 else ''} compressed")
        else:
            self.tile_save.label.setText("Could save")
            pct = f"about {100 * (size - est) / size:.0f}% smaller" if size else ""
            self.tile_save.set(f"−{human(size - est)}" if n else "—", pct)

    # ------------------------------------------------------------------ preview
    def _row_at(self, proxy_index):
        return self.model.rows[self.proxy.mapToSource(proxy_index).row()]

    def preview_selected(self) -> None:
        idx = self.table.currentIndex()
        if not idx.isValid():
            if self.proxy.rowCount():
                idx = self.proxy.index(0, 0)
            else:
                return
        self.preview_row(idx)

    def preview_row(self, idx) -> None:
        r = self._row_at(idx)
        if r.entry.kind == AUDIO:  # nothing to look at: play it (the result once compressed)
            done = r.result and r.result.status == DONE and r.result.output
            open_file(r.result.output if done else r.entry.path)
            return
        if r.result and r.result.status == DONE:
            if r.entry.kind == VIDEO and r.result.output:
                open_in_file_manager(str(r.result.output))
            else:
                inform(self, "This file has already been compressed.")
            return
        dlg = PreviewDialog(r.entry, self.panel.settings(), self)
        dlg.quality_chosen.connect(lambda s, video=r.entry.kind == VIDEO: self._use_quality(s, video))
        dlg.compress_requested.connect(lambda s, e=r.entry: self.compress_one(e, s))
        dlg.save_as_requested.connect(lambda s, dest, e=r.entry: self.compress_one(e, s, dest))
        dlg.exec()

    def _use_quality(self, chosen, video: bool) -> None:
        """The preview's "Use for All Files": its quality choice goes into the options."""
        s = self.panel.settings()
        if video:
            s.video_quality, s.video_crf = chosen.video_quality, chosen.video_crf
        else:
            s.smart_quality, s.smart_target, s.image_quality = (chosen.smart_quality, chosen.smart_target,
                                                                chosen.image_quality)
        self.panel.load(s)

    def compress_one(self, entry, s, dest: Path | None = None) -> bool:
        """One file, with the preview's settings: per the output mode, or saved as ``dest`` (original kept)."""
        if self.busy():
            inform(self, "Another job is still running", "Wait for it to finish, then try again.")
            return False
        s = copy.deepcopy(s)
        if dest is not None:
            same = dest.exists() and os.path.samefile(dest, entry.path)
            # Saving over the original itself: the same as replacing it (it goes to the Trash first).
            s.output_mode = "replace" if same else "beside"
        plan = plan_entry(entry, s)
        if not plan.convert:
            inform(self, f"{entry.path.name} can't be compressed with these settings", plan.reason)
            return False
        if dest is not None and s.output_mode != "replace":
            plan.dest = dest
        return self.start_job(confirm=False, items=[(entry, plan)], settings=s)

    def _hover_info(self, proxy_index):
        """What the hover card shows for a row: the result if compressed, else the original."""
        r = self._row_at(proxy_index)
        e = r.entry
        path, size = e.path, e.size
        if r.result and r.result.status == DONE and r.result.output and r.result.output.exists():
            path, size = r.result.output, r.result.new_size
        if not path.exists():
            return None
        from ..mediainfo import describe

        meta = [KIND_LABEL.get(e.kind, e.kind), human(size), describe(e)]
        return path, e.kind, path.name, " · ".join(m for m in meta if m)

    def exclude_selected(self, include: bool = False) -> None:
        if self.busy():
            return
        for idx in self.table.selectionModel().selectedRows(C_CHECK):
            src = self.model.index(self.proxy.mapToSource(idx).row(), C_CHECK)
            self.model.setData(src, Qt.Checked if include else Qt.Unchecked, Qt.CheckStateRole)
        self.update_summary()

    def include_selected(self) -> None:
        self.exclude_selected(include=True)

    # ------------------------------------------------------------------ job
    def start_job(self, confirm: bool = True, items=None, settings=None) -> bool:
        """Compress the selected files (or ``items`` with ``settings``, e.g. one file from the preview)."""
        if self._replan_timer.isActive() and not self.busy():
            self._replan_timer.stop()       # options changed a moment ago: plan with them now, not after the job
            self._replan()
        items = self.model.selected_items() if items is None else items
        if not items or self.busy():
            return False
        s = settings or self.panel.settings()
        needed = {"ffmpeg", "ffprobe"} if any(e.kind in (VIDEO, AUDIO) for e, _ in items) else set()
        if any(p.route.startswith("jxl") for _, p in items):
            needed |= {"cjxl", "djxl"}
        missing = sorted(t for t in needed if not tools.find_tool(t))
        if missing:
            warn(self, "Missing tools: " + ", ".join(missing),
                 "Put them in the 'tools' folder next to Wick (see README).")
            return False
        size = sum(e.size for e, _ in items)
        where = {"replace": f"only then is the original moved to the {BIN_NAME}",
                 "folder": f"the result is saved in {s.output_dir} (originals untouched)",
                 }.get(s.output_mode, "the result is saved next to the original (originals untouched)")
        if confirm:
            info = f"Each file is checked after conversion; {where}. You can undo this later."
            jxl = sum(1 for _, p in items if p.target_ext == ".jxl")
            if jxl:
                from ..compat import jxl_note

                info += f"\n\n{jxl} file{'s' if jxl != 1 else ''} become{'s' if jxl == 1 else ''} JPEG XL. " + jxl_note()
            if not ask(self, f"Compress {len(items)} file{'s' if len(items) != 1 else ''} ({human(size)})?",
                       info, "Compress"):
                return False
        self.win.save_settings()
        self.model.locked = True
        self._job_settings = s
        pending.start("compress", s.to_dict(), str(self.scan_roots[0] if self.scan_roots else Path.cwd()),
                      [str(e.path) for e, _ in items])
        self.resume_banner.hide()
        self.job_thread = JobThread(items, s, self.scan_roots[0] if self.scan_roots else Path.cwd(), self)
        self.job_thread.file_started.connect(self._file_started)
        self.job_thread.file_progress.connect(self._file_progress)
        self.job_thread.file_finished.connect(self._file_finished)
        self.job_thread.file_step.connect(self._file_step)
        self.job_thread.done.connect(self._job_done)
        self.job_thread.error.connect(self._job_error)
        self.job_thread.finished.connect(self._thread_finished)
        self._set_busy(True)
        self.job_status.start(len(items), size)
        self.job_thread.start()
        self.pauser.attach(self.job_thread)
        return True

    def retry_failed(self) -> None:
        if self.busy():
            return
        n = self.model.select_failed_only()
        self.update_summary()
        if n:
            self.start_job()

    def _file_started(self, entry) -> None:
        self.model.mark_running(entry)
        self.job_status.file_started(entry)

    def _file_step(self, entry, message: str) -> None:
        self.model.set_step(entry, message)
        self.job_status.file_step(entry, message)

    def _file_progress(self, entry, fraction: float) -> None:
        self.model.set_progress(entry, fraction)
        self.job_status.file_progress(entry, fraction)

    def _file_finished(self, result) -> None:
        self.model.set_result(result)
        self.job_status.file_finished(result)
        pending.done("compress", str(result.entry.path))

    # ---- a job that didn't finish last time
    def _check_pending(self) -> None:
        p = pending.load("compress")
        if p is None:
            return
        if p.remaining and not self.busy():
            self._pending = p
            self.resume_banner.show_pending(p)
        elif not p.remaining:
            pending.finish("compress")

    def _resume_pending(self) -> None:
        p = getattr(self, "_pending", None)
        if p is None or self.busy():
            return
        from ..settings import Settings

        self.resume_banner.hide()
        self.panel.load(Settings.from_dict(p.settings))     # the options the job was started with
        self.clear()
        self._resuming = True
        self.add_paths(p.remaining)

    def _discard_pending(self) -> None:
        pending.finish("compress")
        self.resume_banner.hide()

    def _job_done(self, results, run_id) -> None:
        pending.finish("compress")          # it ended normally (finished, or stopped by the user)
        done = [r for r in results if r.status == DONE]
        self.win.history.refresh()
        self.win.notify_outputs([r.output for r in done if r.output])
        note = ""
        if done:
            many = len(results) > 1
            note = (f"The original{'s are' if many else ' is'} in the {BIN_NAME}: empty it to free the space, or "
                    "undo it later." if self._job_settings.output_mode == "replace"
                    else f"The original{'s were' if many else ' was'} kept.")
        self.job_status.finish(results, note, replaced=self._job_settings.output_mode == "replace")
        if not self.window().isActiveWindow():      # finished in the background: say so
            self.win.tray_message(self.job_status.title.text(), actions=[("Show in History", "history")],
                                  launch="compress")
            QApplication.alert(self.window())

    def _job_error(self, message: str) -> None:
        self.job_status.failed(f"Compression stopped: {message}")

    def cancel(self) -> None:
        for t in (self.scan_thread, self.job_thread):
            if t is not None and t.isRunning():
                t.cancel.set()
        self.job_status.stopping()

    def busy(self) -> bool:
        return any(t is not None and t.isRunning() for t in (self.scan_thread, self.job_thread))

    def _why_nothing(self) -> str:
        """Every file was skipped: say why (the commonest reasons), not just that."""
        from collections import Counter

        rows = self.model.rows
        reasons = Counter(r.plan.reason for r in rows if not r.plan.convert)
        hint = (" Change the minimum sizes in Options › Rules." if any(k.startswith("Smaller than") for k in reasons)
                else "")
        if len(rows) == 1:
            return f"{rows[0].entry.path.name} won't be compressed: {rows[0].plan.reason}.{hint}"
        top = "; ".join(f"{reason} ({n})" for reason, n in reasons.most_common(3))
        more = " Turn on Show skipped to see every file's reason." if len(reasons) > 3 else ""
        return f"Nothing here can be made smaller with the current options: {top}.{hint}{more}"

    def _thread_finished(self) -> None:
        self.model.locked = False
        self._set_busy(False)
        if self.proxy.rowCount():
            self.placeholder.clear()
        elif self.model.rows:
            self.placeholder.show_message(self._why_nothing())
        else:
            self.placeholder.clear()
        if self._rescan_after:
            self._rescan_after = False
            QTimer.singleShot(0, self.start_scan)
        elif self._resuming and not self.busy():
            self._resuming = False          # the unfinished job's files are listed again: carry on with them
            if self.model.selected_items():
                QTimer.singleShot(0, lambda: self.start_job(confirm=False))

    def _set_busy(self, busy: bool, scanning: bool = False) -> None:
        if scanning:
            self.job_status.busy("Looking for images, videos and audio…")
        elif not busy and not self.job_status.stop_btn.isHidden():
            self.job_status.hide()          # a scan ended (a job's outcome stays until dismissed)
        for w in (self.panel, self.select_all, self.select_none, self.preview_btn, self.add_files_btn,
                  self.add_folder_btn, self.drop):
            w.setEnabled(not busy)
        self.win.history.setEnabled(not busy)
        self.update_summary()
        if busy:
            self.compress_btn.setEnabled(False)
            self.retry_btn.setVisible(False)

    def _fail(self, msg: str) -> None:
        self.job_status.hide()
        warn(self, msg)

    def _open_row(self, proxy_index) -> None:
        r = self._row_at(proxy_index)
        path = r.result.output if r.result and r.result.output else r.entry.path
        open_in_file_manager(str(path))

    def _table_menu(self, pos) -> None:
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        sel = self.table.selectionModel()
        if not sel.isRowSelected(idx.row(), idx.parent()):
            self.table.selectRow(idx.row())
        rows = [self._row_at(i) for i in sel.selectedRows()] or [self._row_at(idx)]
        r = self._row_at(idx)
        extra: list = []
        if len(rows) == 1 and r.entry.kind == AUDIO:
            extra.append(("Play\tSpace", lambda: self.preview_row(idx)))
        elif len(rows) == 1 and not (r.result and r.result.status == DONE):
            extra.append(("Preview\tSpace", lambda: self.preview_row(idx)))
        outputs = [x.result.output for x in rows if x.result and x.result.status == DONE and x.result.output]
        if outputs:
            extra.append(("Show Result" if len(outputs) == 1 else f"Show {len(outputs)} Results",
                          lambda: open_in_file_manager_many(outputs)))
        pickable = [x for x in rows if x.plan.convert and not (x.result and x.result.status == DONE)]
        if pickable and not self.busy():
            def check(on: bool):
                for x in pickable:
                    src = self.model.index(self.model.rows.index(x), C_CHECK)
                    self.model.setData(src, Qt.Checked if on else Qt.Unchecked, Qt.CheckStateRole)
                self.update_summary()
            n = "" if len(pickable) == 1 else f" {len(pickable)}"
            extra += [None, (f"Select{n}", lambda: check(True)), (f"Deselect{n}\tDelete", lambda: check(False))]
        if not self.busy():
            label = type_label(r.entry.path, r.entry.kind)
            same = [x for x in self.model.rows if x.plan.convert and not (x.result and x.result.status == DONE)
                    and type_label(x.entry.path, x.entry.kind) == label]
            if len(same) > 1:
                def by_type(on: bool):
                    for x in same:
                        self.model.setData(self.model.index(self.model.rows.index(x), C_CHECK),
                                           Qt.Checked if on else Qt.Unchecked, Qt.CheckStateRole)
                    self.update_summary()
                extra += [None, (f"Select All {label} Files", lambda: by_type(True)),
                          (f"Deselect All {label} Files", lambda: by_type(False))]
        off = {x.plan.reason for x in rows if not x.plan.convert and x.plan.reason.endswith("conversion is off")}
        if off and not self.busy():
            extra.append(None)
            for reason in sorted(off):
                kind = reason.split()[0]
                extra.append((f"Turn On {kind} Compression", lambda k=kind: self._turn_on(k)))
        if not self.busy():
            n = "" if len(rows) == 1 else f" {len(rows)}"
            extra += [None, (f"Remove{n} from List", lambda: self.remove_rows(rows))]
        file_menu(self, [x.entry.path for x in rows], extra).exec(self.table.viewport().mapToGlobal(pos))

    def _turn_on(self, kind: str) -> None:
        """A kind of file was skipped because its compression is off: switch it on (the list is planned again)."""
        p = self.panel
        combo, value = {"Image": (p.image_format, "jxl"), "Video": (p.video_codec, "auto"),
                        "Audio": (p.audio_lossless, "flac")}[kind]
        i = combo.findData(value)
        if i >= 0:
            combo.setCurrentIndex(i)

    def remove_rows(self, rows) -> None:
        """Take files off the list (nothing happens to them). Files added one by one are forgotten too."""
        if self.busy():
            return
        names = {os.path.normcase(str(x.entry.path)) for x in rows}
        self.paths = [p for p in self.paths if os.path.normcase(str(p)) not in names]
        self.removed |= names             # (a file inside a listed folder: left out of later scans too)
        self.model.remove_rows(rows)
        self.update_summary()

