"""Clean Up page: duplicates, similar photos, extracted archives, installers, empty items, caches, developer
clutter, Windows, app compression and Google Takeout, one tab each (see cleanup_tools.py)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QSizePolicy, QStackedWidget, QTabBar, QWidget

from .cleanup_section import ToolSection, _Scan
from .cleanup_tools import tools
from .style import Space
from .widgets import Page, button, hbox, text
from .style import Type

# ---------------------------------------------------------------- page

class CleanupTab(Page):
    def __init__(self, window):
        super().__init__("Clean Up")
        self.win = window
        self.tabs = QTabBar()
        self.tabs.setObjectName("segmented")
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.setAccessibleName("Clean-up tools")
        self.tabs.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)   # its tabs' full height, never squeezed
        self.stack = QStackedWidget()
        # Each tool is built the first time it's shown (building all ten up front made opening the page hitch).
        self._tools = tools()
        self._built: dict[int, ToolSection] = {}
        self._hover: bool | None = None
        for tool in self._tools:
            self.tabs.addTab(tool.tab)
            self.stack.addWidget(QWidget())               # (a placeholder until the tool is first shown)
        self.tabs.currentChanged.connect(self._show_tool)
        self._show_tool(0)
        # Too narrow for every tab: the same choice as a drop-down (no scroll arrows, nothing cut off).
        self.picker = QComboBox()
        self.picker.setAccessibleName("Clean-up tool")
        for tool in tools():
            self.picker.addItem(tool.tab)
        self.picker.currentIndexChanged.connect(self.tabs.setCurrentIndex)
        self.tabs.currentChanged.connect(self.picker.setCurrentIndex)
        self.picker.hide()
        self.body.addWidget(self.tabs)
        self.body.addWidget(self.picker, 0, Qt.AlignLeft)
        common = button("Scan common checks…", "", "broom", "Run several checks with one shared folder listing")
        common.clicked.connect(self.scan_common)
        self.body.addLayout(hbox(common, text("Choose folders or drives, then review results in each tool.",
                                             Type.FOOTNOTE, secondary=True), None))
        self.body.addWidget(self.stack, 1)

    def section(self, index: int) -> ToolSection:
        """Tool ``index``'s section, built now if it hasn't been yet."""
        sec = self._built.get(index)
        if sec is None:
            from .widgets import _BodyScroll

            sec = ToolSection(self._tools[index], self)
            if self._hover is not None:
                sec.hover.enabled = self._hover
            current = self.stack.currentIndex()
            holder = self.stack.widget(index)
            self.stack.insertWidget(index, sec)
            self.stack.removeWidget(holder)
            holder.deleteLater()
            self.stack.setCurrentIndex(current)
            for scroll in sec.findChildren(_BodyScroll):
                scroll.verticalScrollBar().rangeChanged.connect(lambda *_: self._refit_soon())
            self._built[index] = sec
        return sec

    @property
    def sections(self) -> list[ToolSection]:
        """Every tool's section (building any that haven't been shown yet)."""
        return [self.section(i) for i in range(len(self._tools))]

    def _show_tool(self, index: int) -> None:
        """Only the tool on screen counts for the page's minimum size (a stack otherwise takes the widest of
        all ten, and every tool would be squeezed or cut off by the widest one's buttons)."""
        self.section(index)
        self.stack.setCurrentIndex(index)
        for i in range(self.stack.count()):
            w = self.stack.widget(i)
            policy = QSizePolicy.Preferred if i == index else QSizePolicy.Ignored
            w.setSizePolicy(policy, policy)
        self.stack.updateGeometry()
        self._refit_soon()

    def _refit_soon(self) -> None:
        """The room can change without this tool being resized (another tool was shown, the page's scroll bar
        came or went): check its buttons again once the layout has settled."""
        from PySide6.QtCore import QTimer

        section = self.stack.currentWidget()
        if section is not None and hasattr(section, "_fit_bar"):
            QTimer.singleShot(0, section._fit_bar)

    def event(self, e) -> bool:
        from PySide6.QtCore import QEvent

        if e.type() in (QEvent.Show, QEvent.StyleChange, QEvent.FontChange, QEvent.PolishRequest):
            # The tab bar learns its tabs' styled height only once polished, and doesn't tell the layout:
            # without this it keeps the height it had before (and its tabs are cut off at the bottom).
            self.tabs.updateGeometry()
        return super().event(e)

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        fits = self.tabs.sizeHint().width() <= self.width() - 2 * Space.XXL   # the page's side margins
        self.tabs.setVisible(fits)
        self.picker.setVisible(not fits)

    def busy(self) -> bool:
        return any(s.busy() for s in self._built.values())

    def set_hover(self, on: bool) -> None:
        self._hover = on
        for s in self._built.values():
            s.hover.enabled = on
            if not on:
                s.hover.hide()

    def stop_all(self) -> None:
        for s in self._built.values():
            s.stop()
            if s.worker is not None:
                s.worker.wait(30000)

    def scan_common(self) -> None:
        from ..cleanup.common import ScanResult, shared_inventory
        from .dialogs import inform
        from PySide6.QtWidgets import QVBoxLayout

        if self.busy():
            inform(self, "Wait for the current cleanup operation to finish, or stop it first.")
            return
        current = self.section(self.tabs.currentIndex())
        if not current.folders:
            inform(self, "Add folders or drives in a file tool first.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Scan common checks")
        layout = QVBoxLayout(dialog)
        layout.addWidget(text("Uses the current tool's folders for every selected check. Results stream into each "
                              "tool; nothing is changed. Compatible checks reuse the same folder listing.",
                              Type.BODY, wrap=True))
        choices = []
        keys = {"extensions", "large", "empty", "installers", "broken", "duplicates", "archives"}
        for i, tool in enumerate(self._tools):
            if tool.key in keys:
                box = QCheckBox(tool.tab)
                box.setChecked(tool.key in ("extensions", "large", "empty"))
                layout.addWidget(box)
                choices.append((i, box))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        sections = [self.section(i) for i, box in choices if box.isChecked()]
        if not sections:
            return
        roots = list(current.folders)

        def scan(progress, cancel):
            results = {}
            with shared_inventory():
                for sec in sections:
                    if cancel.is_set():
                        break
                    worker.active_key = sec.tool.key
                    progress(f"{sec.tool.tab}: starting", 0, 0)
                    results[sec.tool.key] = sec.tool.scan(
                        sec, lambda msg, done, total, sec=sec: progress(f"{sec.tool.tab}: {msg}", done, total), cancel)
            return results

        worker = _Scan(scan, self)
        worker.active_key = ""
        worker.combined = True
        self._batch_worker = worker
        for sec in sections:
            sec.folders = list(roots)
            sec.qs.setValue(f"cleanup/{sec.tool.key}/folders", [str(p) for p in roots])
            sec._show_folders()
            sec.result = None
            sec._scan_complete = False
            sec.tree.clear()
            sec.worker = worker
            sec._set_busy(True, "Waiting for shared scan…")
            worker.partial.connect(sec._partial)
            worker.finished.connect(sec._finished)

        def progressed(message, done, total):
            for sec in sections:
                if message.startswith(sec.tool.tab + ":"):
                    sec._progress(message, done, total)

        def finished(results):
            for sec in sections:
                sec._scanned(results.get(sec.tool.key, ScanResult(sec.tool.key, cancelled=True,
                                                               notes=["Stopped before this check finished."])))

        worker.progress.connect(progressed)
        worker.done.connect(finished)
        worker.failed.connect(lambda message: inform(self, "Shared scan stopped", message))
        worker.start()


