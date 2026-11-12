"""Settings page: appearance, integration, data and privacy, encoders, diagnostics, about."""

from __future__ import annotations

from typing import cast

import os

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QVBoxLayout, QWidget

from .. import APP_NAME, __version__, index, tools
from ..fileops import BIN_NAME
from ..formats import human
from . import devmode, integration
from .dialogs import ask, inform, warn
from .style import Space, Type
from .theme import THEMES
from .widgets import Card, Page, ToggleSwitch, button, text

CONTENT_MAX_WIDTH = 760
HISTORY_CHOICES = [("Forever", 0), ("1 year", 365), ("6 months", 182), ("3 months", 91), ("1 month", 30),
                   ("1 week", 7)]


def setting_row(title: str, description: str, control: QWidget, help_text: str = "") -> QWidget:
    """Apple-style row: title and description on the left, the control on the right. ``help_text``: a
    "?" next to the title explains why the setting exists."""
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, Space.XS, 0, Space.XS)
    h.setSpacing(Space.L)
    left = QVBoxLayout()
    left.setSpacing(Space.XXS)
    if help_text:
        from .widgets import HelpTip

        head = QHBoxLayout()
        head.setSpacing(Space.S)
        head.addWidget(text(title, Type.BODY, bold=True))
        head.addWidget(HelpTip(help_text, title))
        head.addStretch(1)
        left.addLayout(head)
    else:
        left.addWidget(text(title, Type.BODY, bold=True))
    row.description = text(description, Type.FOOTNOTE, secondary=True, wrap=True)   # updatable later
    left.addWidget(row.description)
    if not description:
        # (only hide: showing a widget that has no parent yet opens it as a window of its own, briefly)
        row.description.hide()
    h.addLayout(left, 1)
    h.addWidget(control, 0, Qt.AlignVCenter)
    control.setAccessibleName(title)
    if description:
        control.setAccessibleDescription(description)
    return row


def separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setObjectName("separator")
    line.setFixedHeight(1)
    return line


class AppSettingsTab(Page):
    index_counted = Signal(int, int)      # (files, bytes), from a background thread

    def __init__(self, window):
        super().__init__("Settings", scrolls=True, edge_to_edge=True)
        self.win = window
        self.qs: QSettings = window.qsettings

        content = QWidget()
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(Space.L)
        content.setMaximumWidth(CONTENT_MAX_WIDTH)

        # appearance
        col.addWidget(text("Appearance", Type.HEADLINE, bold=True, name="sectionTitle"))
        card = Card()
        self.theme = QComboBox()
        for label, value in THEMES:
            self.theme.addItem(label, value)
        self.theme.setCurrentIndex(max(0, self.theme.findData(self.qs.value("theme", "system", str))))
        self.theme.currentIndexChanged.connect(self._theme_changed)
        card.lay.addWidget(setting_row("Theme", "Follow the system, or always use light or dark.", self.theme))
        from . import style as _style

        if _style.windows_accent() is not None:
            card.lay.addWidget(separator())
            self.system_accent = ToggleSwitch("Use the Windows accent color")
            self.system_accent.setChecked(self.qs.value("accent/system", False, bool))
            self.system_accent.toggled.connect(self._accent_toggled)
            card.lay.addWidget(setting_row("Use the Windows accent color",
                                           "Buttons, links and selections take the color chosen in Windows' "
                                           "Personalization settings (adjusted where needed so text stays easy "
                                           "to read).", self.system_accent))
        card.lay.addWidget(separator())
        self.hover = ToggleSwitch("Preview on hover")
        self.hover.setChecked(self.qs.value("hover_preview", True, bool))
        self.hover.toggled.connect(self._hover_toggled)
        card.lay.addWidget(setting_row("Preview on hover", "Show a thumbnail when the pointer rests on a file. "
                                                           "Space previews the selected file from the keyboard.",
                                       self.hover))
        card.lay.addWidget(separator())
        self.auto = ToggleSwitch("Scan when Wick starts")
        self.auto.setChecked(self.qs.value("home/auto_analyze", True, bool))
        self.auto.toggled.connect(lambda on: self.qs.setValue("home/auto_analyze", on))
        card.lay.addWidget(setting_row("Scan when Wick starts", "Home looks through your folders in the "
                                                                         "background (reads file headers only).",
                                       self.auto))
        card.lay.addWidget(separator())
        self.monthly = ToggleSwitch("Check for savings once a month")
        self.monthly.setChecked(self.qs.value("monthly_check", False, bool))
        self.monthly.toggled.connect(lambda on: self.qs.setValue("monthly_check", on))
        card.lay.addWidget(setting_row("Check for savings once a month",
                                       "Scans Home's folders in the background and lets you know if 1 GB or "
                                       "more could be freed.", self.monthly))
        col.addWidget(card)

        # integration
        col.addWidget(text("Integration", Type.HEADLINE, bold=True, name="sectionTitle"))
        card = Card()
        self.menu = ToggleSwitch("Context menu")
        self.menu.setChecked(integration.context_menu_installed())
        self.menu.toggled.connect(self._menu_toggled)
        menu_desc = ("Adds “Compress with Wick” for files and folders in Explorer, with your current "
                     "options or any preset, and “Convert with Wick” for pictures, videos and music, with the "
                     "usual formats (the converted copy is saved next to the original). On Windows 11 they're in "
                     "the main menu as well as under Show more options." if integration.IS_WINDOWS
                     else "Adds Wick to your file manager's context menu (Open With).")
        card.lay.addWidget(setting_row("Context menu", menu_desc, self.menu))
        card.lay.addWidget(separator())
        self.autostart = ToggleSwitch("Start at sign-in")
        self.autostart.setChecked(integration.autostart_enabled())
        self.autostart.toggled.connect(self._autostart_toggled)
        card.lay.addWidget(setting_row("Start at sign-in", "Starts quietly in the tray so watched folders keep "
                                                           "working.", self.autostart))
        card.lay.addWidget(separator())
        self.close_to_tray = ToggleSwitch("Keep running in the tray")
        self.close_to_tray.setChecked(self.qs.value("close_to_tray", True, bool))
        self.close_to_tray.toggled.connect(lambda on: self.qs.setValue("close_to_tray", on))
        card.lay.addWidget(setting_row("Keep running in the tray", "When folders are being watched, closing the "
                                                                   "window keeps Wick in the tray.",
                                       self.close_to_tray))
        if integration.IS_WINDOWS:
            card.lay.addWidget(separator())
            self.taskbar_progress = ToggleSwitch("Progress on the taskbar")
            self.taskbar_progress.setChecked(self.qs.value("taskbar/progress", True, bool))
            self.taskbar_progress.toggled.connect(lambda on: self._taskbar_setting("taskbar/progress", on))
            card.lay.addWidget(setting_row("Progress on the taskbar", "Wick's taskbar icon fills up while a "
                                                                      "job runs: yellow when paused, red if "
                                                                      "something failed.", self.taskbar_progress))
            card.lay.addWidget(separator())
            self.taskbar_buttons = ToggleSwitch("Pause and Stop in the taskbar preview")
            self.taskbar_buttons.setChecked(self.qs.value("taskbar/buttons", True, bool))
            self.taskbar_buttons.toggled.connect(lambda on: self._taskbar_setting("taskbar/buttons", on))
            card.lay.addWidget(setting_row("Pause and Stop in the taskbar preview",
                                           "While a job runs, hovering Wick's taskbar icon shows Pause and "
                                           "Stop buttons under the window's preview.", self.taskbar_buttons))
            card.lay.addWidget(separator())
            self.native_notes = ToggleSwitch("Windows notifications")
            self.native_notes.setChecked(self.qs.value("notifications/native", True, bool))
            self.native_notes.toggled.connect(lambda on: self.qs.setValue("notifications/native", on))
            card.lay.addWidget(setting_row("Windows notifications",
                                           "Finished jobs and updates appear as Windows notifications, with "
                                           "buttons like Show in History, and wait in the notification center. "
                                           "Off: a small message from the tray icon instead.", self.native_notes))
        col.addWidget(card)

        # power
        col.addWidget(text("Power", Type.HEADLINE, bold=True, name="sectionTitle"))
        card = Card()
        self.keep_awake = ToggleSwitch("Keep the computer awake while working")
        self.keep_awake.setChecked(self.qs.value("keep_awake", True, bool))
        self.keep_awake.toggled.connect(self._keep_awake_toggled)
        card.lay.addWidget(setting_row("Keep the computer awake while working",
                                       "Long jobs aren't cut short by sleep. The screen can still turn off; the "
                                       "computer sleeps as usual once the work is done.", self.keep_awake))
        card.lay.addWidget(separator())
        self.battery_pause = ToggleSwitch("Pause on battery power")
        self.battery_pause.setChecked(self.qs.value("pause_on_battery", False, bool))
        self.battery_pause.toggled.connect(self._battery_toggled)
        card.lay.addWidget(setting_row("Pause on battery power",
                                       "On a laptop, a job pauses when you unplug (the file being worked on "
                                       "finishes first) and continues when you plug in again.", self.battery_pause))
        col.addWidget(card)

        # data
        col.addWidget(text("Data", Type.HEADLINE, bold=True, name="sectionTitle"))
        card = Card()
        self.index_on = ToggleSwitch("Remember file details")
        self.index_on.setChecked(self.qs.value("index_enabled", True, bool))
        self.index_on.toggled.connect(self._index_toggled)
        card.lay.addWidget(setting_row("Remember file details", "Scans skip reading files that haven't changed "
                                                                "since last time. Kept only on this computer.",
                                       self.index_on))
        if os.name == "nt":
            card.lay.addWidget(separator())
            self.fast_drives = ToggleSwitch("Fast whole-drive scanning")
            self.fast_drives.setChecked(self.qs.value("fast_drive_scan", True, bool))
            self.fast_drives.toggled.connect(self._fast_drives_toggled)
            card.lay.addWidget(setting_row(
                "Fast whole-drive scanning", "When you scan a whole drive (like C:), Wick reads its file "
                "table directly: seconds instead of minutes. Windows asks for administrator permission each time; "
                "if you say no, it scans the usual way.", self.fast_drives))
            card.lay.addWidget(separator())
            self.use_everything = ToggleSwitch("Use Everything when it's running")
            self.use_everything.setChecked(self.qs.value("use_everything", True, bool))
            self.use_everything.toggled.connect(self._everything_toggled)
            card.lay.addWidget(setting_row(
                "Use Everything when it's running", "If the Everything search app is running, scans ask it for "
                "the list of files: instant, with no permission prompt.", self.use_everything))
        card.lay.addWidget(separator())
        self.index_clear = button("Clear", "", None, "Forget all remembered file details")
        self.index_clear.clicked.connect(self._index_clear)
        self.index_row = setting_row("Remembered files", "", self.index_clear)
        self.index_desc = text("", Type.FOOTNOTE, secondary=True, wrap=True)
        self.index_row.layout().itemAt(0).layout().addWidget(self.index_desc)
        card.lay.addWidget(self.index_row)
        card.lay.addWidget(separator())
        self.history_on = ToggleSwitch("Keep a history and log")
        self.history_on.setChecked(self.qs.value("history_enabled", True, bool))
        self.history_on.toggled.connect(self._history_toggled)
        card.lay.addWidget(setting_row(
            "Keep a history and log",
            "Records what was changed, so it can be undone and an interrupted job can be resumed, "
            "and keeps a log for troubleshooting. Off: nothing about your files is written down.",
            self.history_on))
        card.lay.addWidget(separator())
        self.history_keep = QComboBox()
        for label, days in HISTORY_CHOICES:
            self.history_keep.addItem(label, days)
        self.history_keep.setCurrentIndex(max(0, self.history_keep.findData(self.qs.value("history_days", 0, int))))
        self.history_keep.activated.connect(self._history_days_chosen)
        self.history_keep.setAccessibleName("Delete history older than")
        self.history_keep_row = setting_row(
            "Delete older history", "Runs older than this are deleted automatically and can no longer be undone "
                                    f"from the history. Originals stay in the {BIN_NAME} until it's emptied.",
            self.history_keep)
        self.history_keep_row.setEnabled(self.history_on.isChecked())
        card.lay.addWidget(self.history_keep_row)
        col.addWidget(card)

        # encoders
        col.addWidget(text("Encoders", Type.HEADLINE, bold=True, name="sectionTitle"))
        card = Card()
        status = tools.tool_status()
        for i, (name, path) in enumerate(status.items()):
            if path:
                state = text("Installed", Type.FOOTNOTE, secondary=True)
                state.setToolTip(path)
            else:
                state = text("Optional, not installed" if name in tools.OPTIONAL_TOOLS else "Missing",
                             Type.FOOTNOTE, secondary=True)
            desc = {"ffmpeg": "Video encoding (AV1, HEVC)", "ffprobe": "Reads video details",
                    "cjxl": "JPEG XL encoder", "djxl": "JPEG XL decoder (verification, undo)",
                    "cjpegli": "Best-in-class lossy JPEG encoder", "gifsicle": "GIF optimizer",
                    "ssimulacra2": "Fast image quality metric for Smart quality (a built-in one is used "
                                   "otherwise)",
                    "butteraugli_main": "Butteraugli, an optional quality measure for videos"}.get(name, "")
            card.lay.addWidget(setting_row(name, desc, state))
            if i < len(status) - 1:
                card.lay.addWidget(separator())
        col.addWidget(card)

        # diagnostics
        col.addWidget(text("Diagnostics", Type.HEADLINE, bold=True, name="sectionTitle"))
        card = Card()
        open_logs = button("Open Folder", "", "folder", "Show the log files")
        open_logs.clicked.connect(self._open_logs)
        card.lay.addWidget(setting_row("Log", "Wick notes runs, failures and crashes in a small log file on "
                                              "this computer.", open_logs))
        card.lay.addWidget(separator())
        copy = button("Copy", "", None, "Copy diagnostics to the clipboard")
        copy.clicked.connect(self._copy_diagnostics)
        card.lay.addWidget(setting_row("Copy diagnostics", "Versions, encoders and the latest log lines, for a bug "
                                                           "report. Log lines can include file names, so have a look "
                                                           "before sharing.", copy))
        col.addWidget(card)

        # about
        col.addWidget(text("About", Type.HEADLINE, bold=True, name="sectionTitle"))
        card = Card()
        about = button("About Wick", "", None, "Version, privacy, credits and licenses")
        about.clicked.connect(self.win.show_about)
        card.lay.addWidget(setting_row(f"{APP_NAME} {__version__}", "Private by design: no accounts, no telemetry; "
                                                                    "your files never leave your computer.", about))
        card.lay.addWidget(separator())
        from .. import updates

        self.update_frequency = QComboBox()
        for key, (label, _days) in updates.FREQUENCIES.items():
            self.update_frequency.addItem(label, key)
        self.update_frequency.setCurrentIndex(self.update_frequency.findData(updates.frequency(self.qs)))
        self.update_frequency.activated.connect(
            lambda _i: self.qs.setValue("updates/frequency", self.update_frequency.currentData()))
        self.update_frequency.setAccessibleName("Check for updates")
        card.lay.addWidget(setting_row("Check for updates",
                                       "Asks GitHub whether a newer version exists. Nothing is downloaded or "
                                       "installed by itself.", self.update_frequency, updates.HELP))
        card.lay.addWidget(separator())
        self.check_btn = button("Check Now", "", "refresh", "Ask GitHub whether a newer version exists")
        self.check_btn.clicked.connect(lambda: self.win.check_for_updates(manual=True))
        updates_row = setting_row("Updates", "", self.check_btn)
        self.update_status = updates_row.description
        card.lay.addWidget(updates_row)
        self.show_update_status()
        card.lay.addWidget(separator())
        keys = button("Keyboard Shortcuts", "", "keyboard", "Every shortcut in one place", "F1")
        keys.clicked.connect(self.win.show_shortcuts)
        card.lay.addWidget(setting_row("Keyboard shortcuts", "Every page can be used without a mouse.", keys))
        col.addWidget(card)

        # developer (hidden unless switched on: see devmode)
        self.dev_section = QWidget()
        dl = QVBoxLayout(self.dev_section)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(col.spacing())
        dl.addWidget(text("Developer", Type.HEADLINE, bold=True, name="sectionTitle"))
        card = Card()
        self.fluent_icons = ToggleSwitch("Use Windows' icons")
        self.fluent_icons.setChecked(self.qs.value("dev/fluent_icons", devmode.fluent_default(), bool))
        self.fluent_icons.toggled.connect(self._fluent_toggled)
        card.lay.addWidget(setting_row("Use Windows' icons", "Draw icons with Segoe Fluent Icons (Windows 11) or "
                                       "Segoe MDL2 Assets (Windows 10) where they have one; Wick's own "
                                       "icons otherwise.", self.fluent_icons))
        dl.addWidget(card)
        col.addWidget(self.dev_section)
        self.update_dev_section()
        col.addSpacing(Space.XL)   # the page's bottom margin, inside the scroll area
        col.addStretch(1)

        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(Space.XXL, 0, Space.XXL, 0)
        hl.addWidget(content, 1)
        hl.addStretch(0)
        from .scroll_effects import EdgeScrollArea

        holder.setObjectName("inspectorBody")
        self.settings_scroll = EdgeScrollArea(holder)
        self.body.addWidget(self.settings_scroll, 1)

    def _everything_toggled(self, on: bool) -> None:
        from .. import everything

        self.qs.setValue("use_everything", on)
        everything.ENABLED = on

    def _fast_drives_toggled(self, on: bool) -> None:
        from .. import scanner

        self.qs.setValue("fast_drive_scan", on)
        scanner.FAST_DRIVES = on

    def _keep_awake_toggled(self, on: bool) -> None:
        from .. import power

        self.qs.setValue("keep_awake", on)
        power.ENABLED = on

    def _battery_toggled(self, on: bool) -> None:
        self.qs.setValue("pause_on_battery", on)
        self.win.power_changed.emit(bool(self.win.on_battery))     # apply to a running job now

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._update_index_stats()
        self.update_dev_section()

    def _update_index_stats(self) -> None:
        """Count what the file index holds, off the window's thread: the count waits for the index's lock, which
        a scan in the background (Home's analysis) can hold for a while, and the page mustn't freeze meanwhile."""
        if getattr(self, "_counting", False):
            return
        self._counting = True
        if not hasattr(self, "_counted_connected"):
            self.index_counted.connect(self._show_index_stats)
            self._counted_connected = True

        def count() -> None:
            try:
                n, size = index.stats()
            except Exception:  # noqa: BLE001 - only a description
                n, size = 0, 0
            self.index_counted.emit(n, size)

        import threading

        threading.Thread(target=count, daemon=True, name="index-stats").start()

    def _show_index_stats(self, n: int, size: int) -> None:
        self._counting = False
        self.index_desc.setText(f"{n:,} files · {human(size)}" if n else "Nothing remembered yet.")
        self.index_clear.setEnabled(n > 0)

    def _history_days_chosen(self, _i: int = 0) -> None:
        from .. import journal

        days = self.history_keep.currentData()
        self.qs.setValue("history_days", days)
        journal.prune(days)                   # now, not only at the next start
        self.win.history.refresh()

    def _history_toggled(self, on: bool) -> None:
        from .. import journal, log as applog, pending

        self.qs.setValue("history_enabled", on)
        journal.ENABLED = on
        applog.set_enabled(on)
        self.history_keep_row.setEnabled(on)
        if not on:
            for tool in ("compress", "convert", "metadata"):
                pending.finish(tool)           # (the resume lists name files too)
            if journal.list_runs() or applog.log_file().exists():
                if ask(self, "Delete the existing history and log too?",
                       "From now on nothing is written down. What's already there can be deleted as well: "
                       "those changes then can't be undone.", "Delete", destructive=True):
                    journal.clear()
                    applog.delete_logs()
        self.win.history.refresh()

    def _index_toggled(self, on: bool) -> None:
        self.qs.setValue("index_enabled", on)
        index.set_enabled(on)
        if not on:
            index.clear()
        self._update_index_stats()

    def _index_clear(self) -> None:
        if ask(self, "Forget remembered file details?", "The next scan reads every file again.", "Clear"):
            index.clear()
            self._update_index_stats()

    def show_update_status(self, message: str = "") -> None:
        import time

        self.update_status.setVisible(True)
        if message:
            self.update_status.setText(message)
            return
        last = cast(float, self.qs.value("updates/last_check", 0.0, float))
        latest = cast(str, self.qs.value("updates/available", "", str))
        when = time.strftime("%d %b %Y, %H:%M", time.localtime(last)) if last else "never"
        from .. import updates

        state = (f"Version {latest} is available." if latest and updates.newer(latest) else
                 "You have the newest version." if last else "")
        self.update_status.setText(f"Last checked: {when}. {state}".strip())

    def _open_logs(self) -> None:
        from ..log import log_dir, log_file
        from .file_model import open_in_file_manager

        open_in_file_manager(str(log_file() if log_file().exists() else log_dir()))

    def _copy_diagnostics(self) -> None:
        from ..log import diagnostics

        QGuiApplication.clipboard().setText(diagnostics())
        inform(self, "Diagnostics copied", "Paste them into an email or bug report.")

    def _taskbar_setting(self, key: str, on: bool) -> None:
        self.qs.setValue(key, on)
        self.win.taskbar._apply()             # show or hide it at once (during a job)

    def update_dev_section(self) -> None:
        from . import devmode

        self.dev_section.setVisible(devmode.enabled(self.qs))

    def _fluent_toggled(self, on: bool) -> None:
        from . import devmode

        self.qs.setValue("dev/fluent_icons", on)
        devmode.apply(self.qs)
        self.win.refresh_colors()

    def _accent_toggled(self, on: bool) -> None:
        from . import style as _style

        self.qs.setValue("accent/system", on)
        _style.set_system_accent(on)
        self.win.apply_theme(self.qs.value("theme", "system", str))

    def _theme_changed(self) -> None:
        mode = self.theme.currentData()
        self.qs.setValue("theme", mode)
        self.win.apply_theme(mode)

    def _hover_toggled(self, on: bool) -> None:
        self.qs.setValue("hover_preview", on)
        self.win.compress.hover.enabled = on
        if self.win.built("cleanup") is not None:
            self.win.cleanup.set_hover(on)
        for page in (self.win.home, self.win.built("converter"), self.win.built("metadata")):
            if page is not None:
                page.hover.enabled = on
        if not on:
            self.win.compress.hover.hide()

    def _menu_toggled(self, on: bool) -> None:
        try:
            integration.install_context_menu() if on else integration.uninstall_context_menu()
        except OSError as e:
            warn(self, "Couldn't change the context menu", str(e))

    def _autostart_toggled(self, on: bool) -> None:
        try:
            integration.set_autostart(on)
        except OSError as e:
            warn(self, "Couldn't change the start-up setting", str(e))

