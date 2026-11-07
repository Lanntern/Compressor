"""Clean Up page: duplicates, similar photos, extracted archives, installers, empty items, caches, developer
clutter, Windows, app compression and Google Takeout, one tab each (see cleanup_tools.py)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QSizePolicy, QStackedWidget, QTabBar

from .cleanup_section import ToolSection
from .cleanup_tools import tools
from .style import Space
from .widgets import Page

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
        self.sections: list[ToolSection] = []
        for tool in tools():
            sec = ToolSection(tool, self)
            self.sections.append(sec)
            self.tabs.addTab(tool.tab)
            self.stack.addWidget(sec)
        self.tabs.currentChanged.connect(self._show_tool)
        from .widgets import _BodyScroll

        for scroll in self.findChildren(_BodyScroll):
            scroll.verticalScrollBar().rangeChanged.connect(lambda *_: self._refit_soon())
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
        self.body.addWidget(self.stack, 1)

    def _show_tool(self, index: int) -> None:
        """Only the tool on screen counts for the page's minimum size (a stack otherwise takes the widest of
        all ten, and every tool would be squeezed or cut off by the widest one's buttons)."""
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
        return any(s.busy() for s in self.sections)

    def set_hover(self, on: bool) -> None:
        for s in self.sections:
            s.hover.enabled = on
            if not on:
                s.hover.hide()

    def stop_all(self) -> None:
        for s in self.sections:
            s.stop()
            if s.worker is not None:
                s.worker.wait(30000)

