"""Main application window: sidebar navigation, pages, shortcuts, tray."""

from __future__ import annotations

import json
import os
import time

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QDialog, QGridLayout, QHBoxLayout, QMainWindow, QMenu, QStackedWidget, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)

from .. import APP_NAME, __version__, tools
from ..formats import human
from ..log import log
from ..settings import Settings
from . import style
from .home_tab import HomeTab
from .compress_tab import CompressTab
from .history_tab import HistoryTab
from .dialogs import ask
from .style import Space, Type
from .watch_tab import WatchTab
from .sidebar import Sidebar
from .widgets import Card, button, refresh_icons, text
from .workers import EncoderProbeThread

MIN_W, MIN_H = 760, 540
SIDEBAR_COMPACT_BELOW = 1040   # window width at which the sidebar shows icons only

# (sequence, description, group) - shown in the Keyboard Shortcuts window
SHORTCUTS = [
    ("Ctrl+O", "Add files (to the page you're on)", "Files"),
    ("Ctrl+Shift+O", "Add a folder", "Files"),
    ("Ctrl+A / Ctrl+Shift+A", "Tick all / none (every list)", "Lists"),
    ("Space", "Tick or untick the row (Compress: preview)", "Lists"),
    ("Menu key, Shift+F10", "Actions for the selected files", "Lists"),
    ("Ctrl+Return", "Run the page's main action", "Lists"),
    ("Esc", "Stop the running job", "Lists"),
    ("↑ ↓, Home, End", "Move through the list", "Lists"),
    ("Ctrl+L", "Type a path", "Compress"),
    ("Ctrl+R / F5", "Rescan (Clean Up: scan this tool)", "Compress"),
    ("Space", "Preview the selected file (audio: play)", "Compress"),
    ("Delete / Insert", "Exclude / include the selected files", "Compress"),
    ("Ctrl+Shift+R", "Retry failed files", "Compress"),
    ("Ctrl+I", "Show or hide options", "Compress"),
    ("Ctrl+Z", "Undo the selected runs", "History"),
    ("F5", "Refresh", "History"),
    ("Ctrl+1 … 7", "Home, Compress, Convert, Metadata, Clean Up, Watch, History", "Navigation"),
    ("Ctrl+,", "Settings", "Navigation"),
    ("F1", "Keyboard shortcuts", "Navigation"),
    ("Ctrl+Q", "Quit", "Navigation"),
    ("← →", "Move the comparison line", "Preview"),
    ("+ −", "Zoom in / out", "Preview"),
    ("0 / 1", "Fit / actual size", "Preview"),
    ("S", "Side by side", "Preview"),
    (", .  Space", "Previous / next frame, play (videos)", "Preview"),
    ("Ctrl+Tab", "Next Clean Up tool", "Navigation"),
    ("Tab / Shift+Tab", "Move between controls", "Navigation"),
]


# Pages built the first time they're opened: attribute -> (module, class).
LAZY_PAGES = {"converter": (".convert_tab", "ConvertTab"), "metadata": (".metadata_tab", "MetadataTab"),
              "cleanup": (".cleanup_tab", "CleanupTab"), "app_settings": (".app_settings_tab", "AppSettingsTab")}


def open_qsettings() -> QSettings:
    """The app's settings store (an .ini file instead when COMPRESSOR_SETTINGS_FILE is set, e.g. for tests)."""
    path = os.environ.get("COMPRESSOR_SETTINGS_FILE")
    return QSettings(path, QSettings.IniFormat) if path else QSettings(APP_NAME, APP_NAME)


def app_icon() -> QIcon:
    """The app icon: arrows squeezing a frame, on the accent colour."""
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        s = size / 64
        p.setBrush(QColor("#0A6CDB"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(2 * s, 2 * s, 60 * s, 60 * s), 15 * s, 15 * s)
        p.setPen(QPen(QColor("white"), max(1.5, 5 * s), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        path = QPainterPath()
        for x0, x1 in ((12, 26), (52, 38)):
            path.moveTo(x0 * s, 32 * s)
            path.lineTo(x1 * s, 32 * s)
            d = 6 if x1 > x0 else -6
            path.moveTo((x1 - d) * s, 26 * s)
            path.lineTo(x1 * s, 32 * s)
            path.lineTo((x1 - d) * s, 38 * s)
        p.drawPath(path)
        p.setPen(QPen(QColor("white"), max(1.0, 3 * s), Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(32 * s, 18 * s), QPointF(32 * s, 46 * s))
        p.end()
        icon.addPixmap(pm)
    return icon


def native(seq: str) -> str:
    return QKeySequence(seq).toString(QKeySequence.NativeText) if len(seq) > 1 and "/" not in seq and " " not in seq \
        else seq


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Space.XL, Space.XL, Space.XL, Space.XL)
        lay.setSpacing(Space.L)
        lay.addWidget(text("Keyboard Shortcuts", Type.TITLE2, bold=True))
        cols = QHBoxLayout()
        cols.setSpacing(Space.L)
        groups: dict[str, list] = {}
        for seq, desc, group in SHORTCUTS:
            groups.setdefault(group, []).append((seq, desc))
        left = QVBoxLayout()
        right = QVBoxLayout()
        for i, (group, rows) in enumerate(groups.items()):
            card = Card()
            card.lay.addWidget(text(group, Type.HEADLINE, bold=True))
            grid = QGridLayout()
            grid.setHorizontalSpacing(Space.L)
            grid.setVerticalSpacing(Space.S)
            for r, (seq, desc) in enumerate(rows):
                k = text(native(seq), Type.BODY, bold=True)
                k.setTextInteractionFlags(Qt.TextSelectableByMouse)
                grid.addWidget(k, r, 0)
                grid.addWidget(text(desc, Type.BODY, secondary=True), r, 1)
            grid.setColumnStretch(1, 1)
            card.lay.addLayout(grid)
            (left if i == 0 else right).addWidget(card)
        left.addStretch(1)
        right.addStretch(1)
        cols.addLayout(left, 1)
        cols.addLayout(right, 1)
        lay.addLayout(cols)
        close = button("Done", "primary")
        close.clicked.connect(self.accept)
        close.setDefault(True)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)


class MainWindow(QMainWindow):
    power_changed = Signal(bool)          # plugged in / on battery (see battery_pause)

    def __init__(self):
        style.scale_to_text()   # text-bound sizes follow the text size before anything is built (once)
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(int(MIN_W * style.text_scale()), int(MIN_H * style.text_scale()))
        self.resize(1320, 840)
        self.setAcceptDrops(True)
        self.qsettings = open_qsettings()
        self._housekeeping()
        self._quitting = False
        self._tray_hint_shown = False

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.sidebar = Sidebar(APP_NAME, [
            ("Home", "home", native("Ctrl+1")),
            ("Compress", "compress", native("Ctrl+2")),
            ("Convert", "convert", native("Ctrl+3")),
            ("Metadata", "tag", native("Ctrl+4")),
            ("Clean Up", "broom", native("Ctrl+5")),
            ("Watch Folders", "watch", native("Ctrl+6")),
            ("History", "history", native("Ctrl+7")),
            ("Settings", "settings", native("Ctrl+,")),
        ])
        root.addWidget(self.sidebar)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.setCentralWidget(central)
        from .winframe import WindowFrame, custom_frame

        self.frame = WindowFrame(self) if custom_frame() else None

        self.history = HistoryTab(self)
        self.compress = CompressTab(self)
        self.compress.panel.load(self.load_settings())
        self.watch = WatchTab(self)
        self.home = HomeTab(self)
        # Less-used pages are built the first time they're opened (faster start); see __getattr__.
        self._placeholders: dict[str, QWidget] = {name: QWidget() for name in LAZY_PAGES}
        for name in ("home", "compress", "converter", "metadata", "cleanup", "watch", "history", "app_settings"):
            self.pages.addWidget(self._placeholders.get(name) or getattr(self, name))
        self.sidebar.page_selected.connect(self._go)
        self.sidebar.list.setCurrentRow(0)
        self.qsettings.remove("last_inputs")   # (older versions reopened the last session's files: no longer)
        from . import support

        support.note_start(self.qsettings)
        self.sidebar.support_btn.clicked.connect(lambda: self.show_support())
        self.compress.hover.enabled = self.qsettings.value("hover_preview", True, bool)
        self.compress.update_summary()

        missing = [n for n, p in tools.tool_status().items() if not p and n not in tools.OPTIONAL_TOOLS]
        if missing:
            self.compress.page.subtitle.setText(
                "Some encoders are missing (" + ", ".join(missing) + "). See Settings → Encoders.")
        geo = self.qsettings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        if self.frame is not None:
            self.frame.attach()

        self._setup_shortcuts()
        self._setup_tray()
        self._setup_updates()
        self._setup_power()
        self._setup_monthly()
        self.watch.state_changed.connect(self._update_tray)
        self._update_tray(self.watch.watching)
        self.history.refresh()

        self._welcome_pending = not self.qsettings.value("welcome_done", False, bool)
        if self.qsettings.value("home/auto_analyze", True, bool) and not self._welcome_pending:
            # (low priority: nobody pressed Scan; the first time, the welcome offers it instead)
            QTimer.singleShot(800, lambda: self.home.analyze(background=True))
        # Start with focus on the main action, not the sidebar.
        QTimer.singleShot(0, lambda: self.home.analyze_btn.setFocus())

        self.probe = EncoderProbeThread(self)
        self.probe.result.connect(self.compress.panel.show_encoders)
        self.compress.panel.presets_changed.connect(self._presets_changed)
        from . import integration
        QTimer.singleShot(2000, integration.refresh_context_menu)  # keep the menu's preset list current
        QTimer.singleShot(200, self.probe.start)

    # ------------------------------------------------------------------ pages built on demand
    def __getattr__(self, name: str):
        # Only called for attributes that don't exist yet: build a lazy page on first use.
        if name in LAZY_PAGES and "_placeholders" in self.__dict__:
            return self._build_page(name)
        raise AttributeError(name)

    def built(self, name: str):
        """The page if it has been built, else None (for code that only needs to look at it)."""
        return self.__dict__.get(name)

    def _build_page(self, name: str) -> QWidget:
        import importlib

        module, cls = LAZY_PAGES[name]
        page = getattr(importlib.import_module(module, __package__), cls)(self)
        setattr(self, name, page)
        holder = self._placeholders.pop(name)
        index = self.pages.indexOf(holder)
        current = self.pages.currentIndex()
        self.pages.insertWidget(index, page)
        self.pages.removeWidget(holder)
        holder.deleteLater()
        self.pages.setCurrentIndex(current)
        self._page_shortcuts(name, page)
        return page

    def _go(self, index: int) -> None:
        if 0 <= index < self.pages.count():
            holder = self.pages.widget(index)
            lazy = next((n for n, w in self.__dict__.get("_placeholders", {}).items() if w is holder), None)
            if lazy is not None:
                self._build_page(lazy)
            self.pages.setCurrentIndex(index)
            if self.pages.widget(index) is self.history:
                self.history.refresh()
            if self.sidebar.list.currentRow() != index:
                self.sidebar.list.setCurrentRow(index)

    def go_to(self, page: QWidget) -> None:
        self._go(self.pages.indexOf(page))

    def focus_page(self) -> None:
        """After a keyboard page switch, put the focus where work happens: the page's list if it has rows,
        otherwise its first control (so the page's own shortcuts work straight away)."""
        page = self.pages.currentWidget()
        if page is self.built("cleanup"):
            page = page.stack.currentWidget()
        view = getattr(page, "table", None) or getattr(page, "tree", None)
        model = view.model() if view is not None else None
        if view is not None and view.isVisible() and model is not None and model.rowCount():
            view.setFocus(Qt.ShortcutFocusReason)
            return
        page.setFocus(Qt.ShortcutFocusReason)
        page.focusNextChild()

    def _setup_shortcuts(self) -> None:
        c = self.compress
        page_ctx = Qt.WidgetWithChildrenShortcut

        add = self._shortcut

        add("Ctrl+O", lambda: self._add_files(folder=False))
        add("Ctrl+Shift+O", lambda: self._add_files(folder=True))
        add("Ctrl+L", lambda: (self.go_to(c), c.type_path()))
        add("Ctrl+R", c.start_scan, c.page, page_ctx)
        add("F5", c.start_scan, c.page, page_ctx)
        add("Ctrl+Return", lambda: c.start_job(), c, page_ctx)
        add("Ctrl+Enter", lambda: c.start_job(), c, page_ctx)
        add("Esc", lambda: c.cancel() if c.busy() else None, c, page_ctx)
        add("Space", c.preview_selected, c.table, Qt.WidgetShortcut)
        add("Delete", c.exclude_selected, c.table, Qt.WidgetShortcut)
        add("Ctrl+A", c.select_all.click, c.page, page_ctx)
        add("Ctrl+Shift+A", c.select_none.click, c.page, page_ctx)
        add("Ctrl+Shift+R", c.retry_failed, c, page_ctx)
        add("Ctrl+I", lambda: (self.go_to(c), c.toggle_options()))
        add("Ctrl+Z", self.history.undo_selected, self.history, page_ctx)
        add("F5", self.history.reload, self.history, page_ctx)
        add("Delete", self.watch.remove_folder, self.watch.folders, Qt.WidgetShortcut)
        for n in range(1, 8):
            add(f"Ctrl+{n}", lambda i=n - 1: (self._go(i), self.focus_page()))
        add("Ctrl+R", self.home.analyze, self.home, page_ctx)
        add("Insert", c.include_selected, c.table, Qt.WidgetShortcut)
        # Home
        from .filelist import set_all_checked

        h = self.home
        add("Ctrl+A", lambda: set_all_checked(h.tree, True, 0, h._update_send), h, page_ctx)
        add("Ctrl+Shift+A", lambda: set_all_checked(h.tree, False, 0, h._update_send), h, page_ctx)
        add("Ctrl+Return", h._send, h, page_ctx)
        add("Ctrl+Enter", h._send, h, page_ctx)
        add("Ctrl+,", lambda: (self.go_to(self.app_settings), self.focus_page()))
        add("F1", self.show_shortcuts)
        add("Ctrl+/", self.show_shortcuts)
        add("Ctrl+Q", self.quit)

    def _shortcut(self, seq, slot, parent=None, ctx=Qt.WindowShortcut):
        sc = QShortcut(QKeySequence(seq), parent or self)
        sc.setContext(ctx)
        sc.activated.connect(slot)
        return sc

    def _page_shortcuts(self, name: str, page: QWidget) -> None:
        """Shortcuts that belong to a page built on demand (installed when it's built)."""
        from .filelist import set_all_checked

        add, page_ctx = self._shortcut, Qt.WidgetWithChildrenShortcut
        if name in ("converter", "metadata"):
            add("Ctrl+A", lambda p=page: set_all_checked(p.tree, True, 0, p._update), page, page_ctx)
            add("Ctrl+Shift+A", lambda p=page: set_all_checked(p.tree, False, 0, p._update), page, page_ctx)
            add("Ctrl+Return", lambda p=page: p.start(), page, page_ctx)
            add("Ctrl+Enter", lambda p=page: p.start(), page, page_ctx)
            add("Esc", lambda p=page: p.stop() if p.busy() else None, page, page_ctx)
        elif name == "cleanup":
            cu = page
            tool = lambda: cu.stack.currentWidget()  # noqa: E731
            add("Ctrl+R", lambda: tool().scan(), cu, page_ctx)
            add("F5", lambda: tool().scan(), cu, page_ctx)
            add("Ctrl+A", lambda: tool()._set_all(True), cu, page_ctx)
            add("Ctrl+Shift+A", lambda: tool()._set_all(False), cu, page_ctx)
            add("Ctrl+Return", lambda: tool().run_primary(), cu, page_ctx)
            add("Ctrl+Enter", lambda: tool().run_primary(), cu, page_ctx)
            add("Esc", lambda: tool().stop() if tool().busy() else None, cu, page_ctx)
            add("Ctrl+Tab", lambda: cu.tabs.setCurrentIndex((cu.tabs.currentIndex() + 1) % cu.tabs.count()), cu,
                page_ctx)
            add("Ctrl+Shift+Tab", lambda: cu.tabs.setCurrentIndex((cu.tabs.currentIndex() - 1) % cu.tabs.count()),
                cu, page_ctx)

    def _add_files(self, folder: bool) -> None:
        """Ctrl+O / Ctrl+Shift+O add to the page you're on (Convert, Metadata), else to Compress."""
        page = self.pages.currentWidget()
        if not hasattr(page, "browse_files"):
            page = self.compress
            self.go_to(page)
        (page.browse_folder if folder else page.browse_files)()

    def show_shortcuts(self) -> None:
        ShortcutsDialog(self).exec()

    def show_support(self, asked: bool = False) -> None:
        """The "star on GitHub / donate" window (the sidebar's Support link, or the one-time prompt)."""
        from . import support

        dlg = support.SupportDialog(int(self.qsettings.value(support.SAVED_MOST, 0, int)), self, asked=asked)
        dlg.show()
        self._support_dialog = dlg

    def note_saved_total(self, total: int) -> None:
        """History's total changed (or was read at start-up): maybe it's time for the one-time support prompt."""
        from . import support

        support.note_saved(self.qsettings, total)
        if support.due(self.qsettings):
            QTimer.singleShot(5000, self._maybe_ask_support)

    def _maybe_ask_support(self) -> None:
        """Ask once, and only at a quiet moment: the window on screen and nothing running."""
        from . import support

        lazy = [p for p in (self.built("cleanup"), self.built("converter"), self.built("metadata")) if p is not None]
        busy = self.compress.busy() or any(p.busy() for p in lazy)
        if not support.due(self.qsettings) or busy or not self.isVisible() or self.isMinimized() \
                or QApplication.activeModalWidget() is not None:
            return                          # (checked again the next time the History total changes)
        self.qsettings.setValue(support.ASKED, True)
        self.show_support(asked=True)

    def show_about(self) -> None:
        from .about_dialog import AboutDialog

        AboutDialog(self).exec()

    # ------------------------------------------------------------------ theme & layout
    def apply_theme(self, mode: str) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.setOverrideCursor(Qt.WaitCursor)   # restyling every widget takes about a second
        try:
            style.apply_style(mode)
            self.refresh_colors()
        finally:
            QApplication.restoreOverrideCursor()

    def refresh_colors(self) -> None:
        refresh_icons(self)
        self.sidebar.refresh()
        self.compress.drop.refresh()
        for sec in self.compress.panel.sections:
            sec.refresh()
        self.watch.refresh()
        self.compress.model._changed_all()
        self.compress.update_summary()
        self.history.refresh()
        if self.frame is not None:
            self.frame.set_dark(style.tokens().dark)
        self.update()

    # ------------------------------------------------------------------ own title bar (Windows)
    def nativeEvent(self, event_type, message):  # noqa: N802
        if getattr(self, "frame", None) is not None:
            handled, result = self.frame.native_event(message)
            if handled:
                return True, result
        return super().nativeEvent(event_type, message)

    def changeEvent(self, e) -> None:  # noqa: N802
        super().changeEvent(e)
        if getattr(self, "frame", None) is not None:
            self.frame.changed(e)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if getattr(self, "frame", None) is not None:
            self.frame.place()
        self.sidebar.set_compact(self.width() < SIDEBAR_COMPACT_BELOW)
        self.compress.adapt_to_width(self.width())

    # ------------------------------------------------------------------ settings
    def load_settings(self) -> Settings:
        raw = self.qsettings.value("settings", "", str)
        try:
            return Settings.from_dict(json.loads(raw) if raw else None)
        except ValueError:
            return Settings()

    def save_settings(self) -> None:
        self.qsettings.setValue("settings", json.dumps(self.compress.panel.settings().to_dict()))

    # ------------------------------------------------------------------ drag & drop / other instances
    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.compress.drop.set_hover(True)

    def dragLeaveEvent(self, e) -> None:
        self.compress.drop.set_hover(False)

    def dropEvent(self, e) -> None:
        self.compress.drop.set_hover(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            current = self.pages.currentWidget()
            if current is not None and current in (self.built("converter"), self.built("metadata")):
                current.add_paths(paths)
                return
            self.go_to(self.compress)
            self.compress.add_paths(paths)

    def _housekeeping(self) -> None:
        """Startup chores: file index on/off, history retention, stale index entries."""
        from .. import index, journal

        index.set_enabled(self.qsettings.value("index_enabled", True, bool))
        try:
            removed = journal.prune(self.qsettings.value("history_days", 0, int))
            if removed:
                log.info("Removed %d run(s) older than the history limit", removed)
        except OSError:
            pass
        QTimer.singleShot(5000, index.prune)
        self._recompress: QThread | None = None
        if self.qsettings.value("appcompress/recompress", True, bool):
            QTimer.singleShot(3 * 60 * 1000, self.recompress_updated_games)   # after start-up settles

    MONTHLY_DAYS = 30
    MONTHLY_MIN = 1024 ** 3          # only worth a notification from 1 GB

    def _setup_monthly(self) -> None:
        """Settings › Check for savings once a month (off by default): a quiet look through Home's folders,
        and one notification if there's a good amount to free. Nothing is ever changed by it."""
        self._monthly_running = False
        self._monthly_timer = QTimer(self, interval=60 * 60 * 1000)      # hourly: is a check due?
        self._monthly_timer.timeout.connect(self._monthly_due)
        self._monthly_timer.start()
        QTimer.singleShot(10 * 60 * 1000, self._monthly_due)              # ten minutes after start-up
        self.home.analyzed.connect(self._monthly_result)
        if self.tray is not None:
            self.tray.messageClicked.connect(self._message_clicked)

    def _monthly_due(self) -> bool:
        if not self.qsettings.value("monthly_check", False, bool) or self._monthly_running:
            return False
        if time.time() - self.qsettings.value("monthly/last", 0.0, float) < self.MONTHLY_DAYS * 86400:
            return False
        if self.home.busy() or self.compress.busy():
            return False                                 # busy now: the next hourly look will try again
        self._monthly_running = True
        self.home.analyze(background=True)
        return True

    def _monthly_result(self, ov) -> None:
        if not self._monthly_running:
            return
        self._monthly_running = False
        if ov.cancelled:
            return
        self.qsettings.setValue("monthly/last", time.time())
        extra = 0
        if ov.space is not None:
            from .. import space

            extra = ov.space.sizes.get(space.CACHES, 0) + ov.space.sizes.get(space.BIN, 0)
        total = ov.likely + extra
        if total < self.MONTHLY_MIN:
            return
        self._last_notice = "monthly"
        self.tray_message(f"About {human(total)} could be freed. Open Compressor to see where "
                          "(nothing has been changed).", force=True)

    def _message_clicked(self) -> None:
        if getattr(self, "_last_notice", "") == "monthly":
            self.show_window()
            self.go_to(self.home)

    def _setup_power(self) -> None:
        from .. import power

        power.ENABLED = self.qsettings.value("keep_awake", True, bool)
        from .. import scanner

        scanner.FAST_DRIVES = self.qsettings.value("fast_drive_scan", True, bool)
        from .. import everything

        everything.ENABLED = self.qsettings.value("use_everything", True, bool)
        self.on_battery = power.on_battery()
        self._power_timer = QTimer(self, interval=15_000)
        self._power_timer.timeout.connect(self._poll_power)
        self._power_timer.start()

    def _poll_power(self) -> None:
        from .. import power

        now = power.on_battery()
        if now != self.on_battery:
            self.on_battery = now
            self.power_changed.emit(bool(now))

    def battery_pause(self) -> bool:
        """Should jobs pause now? (On battery, and Settings › Pause on battery power is on.)"""
        return bool(self.on_battery) and self.qsettings.value("pause_on_battery", False, bool)

    def _setup_updates(self) -> None:
        """Show an update found earlier, and check again when Settings' frequency says so (in the background)."""
        self._update_check: QThread | None = None
        self._release = None
        self.sidebar.update_btn.clicked.connect(self._show_update)
        from .. import updates

        cached = self.qsettings.value("updates/available", "", str)
        if cached and updates.newer(cached):     # remembered from an earlier check: no network needed
            self.sidebar.set_update(cached, self.qsettings.value("updates/security", False, bool))
        last = self.qsettings.value("updates/last_check", 0.0, float)
        if updates.due(updates.frequency(self.qsettings), last, time.time()) \
                and os.environ.get("COMPRESSOR_NO_NETWORK") != "1":       # tests and offline builds
            QTimer.singleShot(30 * 1000, self.check_for_updates)   # once start-up has settled

    def check_for_updates(self, manual: bool = False) -> None:
        """Ask GitHub whether a newer version exists (in the background). ``manual``: from Check Now, so say
        what happened even when there's nothing new."""
        from PySide6.QtCore import QThread, Signal

        from .. import updates

        if self._update_check is not None and self._update_check.isRunning():
            return

        class Check(QThread):
            done = Signal(object, str)

            def run(self) -> None:
                try:
                    self.done.emit(updates.check(), "")
                except updates.CheckFailed as e:
                    self.done.emit(None, str(e))

        def finished(release, error: str) -> None:
            settings_page = self.built("app_settings")
            if error:
                log.info("Update check: %s", error)
                if settings_page is not None:
                    settings_page.show_update_status(f"Couldn't check: {error}.")
                return
            self.qsettings.setValue("updates/last_check", time.time())
            self.qsettings.setValue("updates/available", release.version if release else "")
            self.qsettings.setValue("updates/security", bool(release and release.security))
            self._release = release
            self.sidebar.set_update(release.version if release else None, bool(release and release.security))
            if settings_page is not None:
                settings_page.show_update_status()
            if release and not manual:
                self.tray_message(f"{APP_NAME} {release.version} is available"
                                  + (" with security fixes." if release.security else "."))
            if manual and release:
                self._show_update()

        self._update_check = Check(self)
        self._update_check.done.connect(finished)
        if manual and self.built("app_settings") is not None:
            self.app_settings.show_update_status("Checking…")
        self._update_check.start()

    def _show_update(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from .. import updates
        from .dialogs import choose

        version = self._release.version if self._release else self.qsettings.value("updates/available", "", str)
        if not version:
            return
        security = self._release.security if self._release else self.qsettings.value("updates/security", False, bool)
        url = self._release.url if self._release else updates.PAGE
        notes = (self._release.notes.strip() if self._release else "")[:600]
        info = ("It includes security fixes for components that open your files, so it's worth installing soon. "
                if security else "") + (f"\n\n{notes}" if notes else "")
        if choose(self, f"{APP_NAME} {version} is available (you have {__version__}).", info.strip(), "Later",
                  "Open Download Page"):
            QDesktopServices.openUrl(QUrl(url))

    def recompress_updated_games(self) -> None:
        """Games this app compressed get uncompressed files when they update: compress those again, in the
        background (games that are running, use DirectStorage or need administrator rights are skipped)."""
        import threading

        from PySide6.QtCore import QThread, Signal

        from ..cleanup import appcompress

        if not appcompress.supported() or (self._recompress is not None and self._recompress.isRunning()):
            return

        class Recompress(QThread):
            done = Signal(object, object)

            def __init__(self, parent=None):
                super().__init__(parent)
                self.cancel = threading.Event()

            def run(self) -> None:
                from .. import power

                try:
                    with power.background():
                        self.done.emit(*appcompress.recompress_updated(cancel=self.cancel))
                except Exception as e:  # noqa: BLE001 - a background nicety
                    log.warning("Re-compressing updated games failed: %s", e)

        def finished(done, notes) -> None:
            from ..formats import human

            if done:
                saved = sum(b for _, b in done)
                names = ", ".join(f.name for f, _ in done[:3]) + (" and more" if len(done) > 3 else "")
                log.info("Re-compressed %d updated game(s), saved %d bytes", len(done), saved)
                self.tray_message(f"Compressed {names} again after an update, saving {human(saved)}.")
                self.history.refresh()
            for note in notes:
                log.info(note)

        self._recompress = Recompress(self)
        self._recompress.done.connect(finished)
        self._recompress.start()

    def _presets_changed(self) -> None:
        from . import integration

        integration.refresh_context_menu()
        self.watch.refresh_presets()

    # --open=TARGET → the page's attribute name (see app.open_target).
    OPEN_PAGES = {"home": "home", "compress": "compress", "convert": "converter", "metadata": "metadata",
                  "cleanup": "cleanup", "watch": "watch", "history": "history", "settings": "app_settings"}

    def open_target(self, target: str) -> None:
        """Show a page (jump list, tray), or pick files on one: compress-files, convert-files."""
        self.show_window()
        page_name, _, action = target.partition("-")
        attr = self.OPEN_PAGES.get(page_name)
        if attr is None:
            return
        page = getattr(self, attr)
        self.go_to(page)
        if action == "files" and hasattr(page, "browse_files"):
            QTimer.singleShot(0, page.browse_files)        # once the window is in front

    def receive_paths(self, paths: list[str], show: bool = True, preset: str = "", convert: str = "",
                      open_target: str = "") -> None:
        if open_target:
            self.open_target(open_target)
        if show:
            self.show_window()
        if convert:                 # Explorer's "Convert with Compressor"
            self.go_to(self.converter)
            self.converter.quick_convert(paths, convert)
            return
        if preset and not self.compress.panel.apply_preset(preset):
            self.tray_message(f"There's no preset called “{preset}”; using the current options.")
        if paths:
            self.go_to(self.compress)
            # Explorer starts one process per selected file: collect them briefly before scanning.
            self.compress.add_paths(paths, scan=False)
            if not hasattr(self, "_scan_debounce"):
                self._scan_debounce = QTimer(self, singleShot=True, interval=600)
                self._scan_debounce.timeout.connect(self.compress.start_scan)
            self._scan_debounce.start()

    def notify_outputs(self, paths) -> None:
        self.watch.ignore(paths)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        from .. import power

        power.set_efficiency_mode(False)          # on screen: full speed
        if getattr(self, "_welcome_pending", False):
            self._welcome_pending = False
            QTimer.singleShot(300, self._welcome)       # once the window is on screen

    def _welcome(self) -> None:
        from .welcome import WelcomeDialog

        dlg = WelcomeDialog(self)
        self._welcome_dialog = dlg

        def closed(result: int) -> None:
            self.qsettings.setValue("welcome_done", True)
            if result == WelcomeDialog.Accepted:
                self.go_to(self.home)
                self.home.analyze()
        dlg.finished.connect(closed)
        dlg.open()                    # (window-modal, doesn't block the event loop)

    def show_window(self) -> None:
        from .. import power

        power.set_efficiency_mode(False)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------ tray
    def _setup_tray(self) -> None:
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu()
        open_app = menu.addAction(f"Open {APP_NAME}", self.show_window)
        menu.setDefaultAction(open_app)          # (bold: what clicking the icon does)
        menu.addSeparator()
        # The same shortcuts as the taskbar's jump list (jumplist.TASKS).
        menu.addAction("Compress Files…", lambda: self.open_target("compress-files"))
        menu.addAction("Convert Files…", lambda: self.open_target("convert-files"))
        menu.addAction("Clean Up", lambda: self.open_target("cleanup"))
        menu.addAction("History", lambda: self.open_target("history"))
        menu.addSeparator()
        self.tray_watch = QAction("Watch Folders", menu, checkable=True)
        self.tray_watch.setToolTip("Compress new files in your watched folders automatically")
        self.tray_watch.toggled.connect(lambda on: self.watch.switch.setChecked(on))
        menu.addAction(self.tray_watch)
        menu.addAction("Check for Updates", lambda: (self.open_target("settings"),
                                                     self.check_for_updates(manual=True)))
        menu.addAction(f"About {APP_NAME}", lambda: (self.show_window(), self.show_about()))
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.show_window() if reason == QSystemTrayIcon.Trigger else None)
        self._tray_menu = menu

    def _update_tray(self, watching: bool) -> None:
        if self.tray is None:
            return
        self.tray_watch.blockSignals(True)
        self.tray_watch.setChecked(watching)
        self.tray_watch.blockSignals(False)
        self.tray.setToolTip(f"{APP_NAME}: {'watching folders' if watching else 'idle'}")
        self.tray.setVisible(watching or not self.isVisible())

    def tray_message(self, message: str, force: bool = False) -> None:
        """A notification. ``force``: show the tray icon for it if it's hidden (the window is open)."""
        if self.tray is None:
            return
        if not self.tray.isVisible() and force:
            self.tray.setVisible(True)
            QTimer.singleShot(15_000, lambda: self._update_tray(self.watch.watching))
        if self.tray.isVisible():
            self.tray.showMessage(APP_NAME, message, app_icon(), 8000 if force else 4000)

    def quit(self) -> None:
        self._quitting = True
        self.close()

    def closeEvent(self, event) -> None:
        keep_running = (not self._quitting and self.watch.watching and self.tray is not None
                        and self.qsettings.value("close_to_tray", True, bool))
        if keep_running:
            self.save_settings()
            self.qsettings.setValue("geometry", self.saveGeometry())
            event.ignore()
            self.hide()
            self.tray.setVisible(True)
            from .. import power

            power.set_efficiency_mode(True)     # hidden, only watching: Windows' efficiency mode
            if not self._tray_hint_shown:
                self.tray_message("Still watching folders in the background. Quit from the tray icon.")
                self._tray_hint_shown = True
            return
        lazy = [p for p in (self.built("cleanup"), self.built("converter"), self.built("metadata")) if p is not None]
        if self.compress.busy() or any(p.busy() for p in lazy):
            if not ask(self, "Stop the running job and quit?", "The file being processed keeps its original.",
                       "Quit", destructive=True):
                event.ignore()
                self._quitting = False
                return
            self.compress.cancel()
            if self.built("cleanup") is not None:
                self.cleanup.stop_all()
            for page in [p for p in (self.built("converter"), self.built("metadata")) if p is not None]:
                page.stop()
                for th in (page.job_thread, page.scan_thread):
                    if th is not None:
                        th.wait(30000)
            for t in (self.compress.job_thread, self.compress.scan_thread):
                if t is not None:
                    t.wait(30000)
        if getattr(self, "_update_check", None) is not None and self._update_check.isRunning():
            self._update_check.wait(20000)       # a network request, at most its timeout
        if getattr(self, "_recompress", None) is not None and self._recompress.isRunning():
            self._recompress.cancel.set()
            self._recompress.wait(30000)
        self.watch.shutdown()
        self.home.stop()
        self.save_settings()
        self.qsettings.setValue("geometry", self.saveGeometry())
        if self.tray is not None:
            self.tray.hide()
        super().closeEvent(event)
        from PySide6.QtWidgets import QApplication

        QApplication.quit()


__all__ = ["MainWindow", "app_icon", "__version__"]
