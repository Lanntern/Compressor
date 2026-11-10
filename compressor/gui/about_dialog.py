"""About Compressor: version, privacy, credits and licences."""

from __future__ import annotations

import platform
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QAbstractItemView, QDialog, QFrame, QHBoxLayout, QLabel, QTreeWidget, QVBoxLayout

from .. import APP_NAME, __version__
from ..about import CREDITS, DONATE_URL, LICENCE_NOTE, PRIVACY
from .devmode import CLICKS
from .filelist import SortItem, make_sortable
from .style import Size, Space, Type
from .widgets import button, text

KIND = {"library": "Built in", "tool": "Separate tool", "code": "Adapted code", "download": "Downloaded when needed"}


def version_for(name: str, v: dict[str, str]) -> tuple[str, str]:
    """(version shown in the list, details for its tooltip) for a credits entry."""
    if name == "Python":
        return platform.python_version(), ""
    if name.startswith("Qt"):
        return v.get("Qt", ""), f"PySide6 {v.get('PySide6', '?')}"
    if name.startswith("pillow-heif"):
        return v.get("pillow-heif", ""), f"libheif {v.get('libheif', '?')} (HEIC decoding)"
    if name == "Pillow":
        libs = ", ".join(f"{k} {v[k]}" for k in ("libwebp", "libjpeg-turbo", "zlib") if k in v)
        return v.get("Pillow", ""), f"Includes {libs}" if libs else ""
    if name.startswith("libjxl"):
        return v.get("libjxl", "not found"), (f"cjpegli {v['jpegli']}" if "jpegli" in v else "")
    if name.startswith("x265"):
        return "in FFmpeg", "Built into the FFmpeg build listed above"
    if name.startswith("ssimulacra2 (Python)"):
        return "built in", ""
    if name == "Pandoc":
        return v.get("Pandoc", "not downloaded"), "Downloaded when you first convert documents"
    key = {"libimagequant": "imagequant", "oxipng / pyoxipng": "pyoxipng", "rawpy / LibRaw": "rawpy (LibRaw)"}.get(
        name, name)
    return v.get(key, "not found" if name in ("FFmpeg", "gifsicle") else ""), ""


class AboutDialog(QDialog):
    def eventFilter(self, obj, e) -> bool:
        from PySide6.QtCore import QEvent

        if obj is getattr(self, "_version", None) and e.type() == QEvent.MouseButtonRelease:
            self._clicks += 1
            if self._clicks == CLICKS:
                self._clicks = 0
                self._toggle_dev()
        return super().eventFilter(obj, e)

    def _toggle_dev(self) -> None:
        from PySide6.QtWidgets import QToolTip

        from . import devmode
        from .main_window import open_qsettings

        qs = open_qsettings()
        on = devmode.toggle(qs)
        devmode.apply(qs)
        win = self.parent()
        page = win.built("app_settings") if hasattr(win, "built") else None
        if page is not None:
            page.update_dev_section()
        if hasattr(win, "refresh_colors"):
            win.refresh_colors()
        QToolTip.showText(self._version.mapToGlobal(self._version.rect().bottomLeft()),
                          "Developer settings are on (Settings, at the bottom)." if on else
                          "Developer settings are hidden.", self._version)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.resize(860, 640)
        self.setMinimumWidth(560)     # (the height follows the text, which grows with the system text size)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Space.XL, Space.XL, Space.XL, Space.XL)
        lay.setSpacing(Space.M)

        head = QHBoxLayout()
        head.setSpacing(Space.L)
        logo = QLabel()
        from .main_window import app_icon

        logo.setPixmap(app_icon().pixmap(Size.ICON_L, Size.ICON_L))
        logo.setAccessibleName(f"{APP_NAME} icon")
        head.addWidget(logo, 0, Qt.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(Space.XXS)
        titles.addWidget(text(APP_NAME, Type.TITLE2, bold=True))
        from PySide6.QtCore import qVersion

        version = text(f"Version {__version__} · Python {platform.python_version()} · Qt {qVersion()}",
                       Type.FOOTNOTE, secondary=True)
        version.installEventFilter(self)      # (clicked seven times: developer settings, see devmode)
        self._version, self._clicks = version, 0
        titles.addWidget(version)
        titles.addWidget(text(PRIVACY, Type.BODY, wrap=True))
        head.addLayout(titles, 1)
        lay.addLayout(head)

        lay.addWidget(text("Credits and licences", Type.HEADLINE, bold=True, name="sectionTitle"))
        self.tree = QTreeWidget()
        self.tree.setAccessibleName("Components and their licences. Enter opens the project's website.")
        self.tree.setHeaderLabels(["Component", "Version", "Used for", "Licence", "Included as"])
        self.tree.setRootIsDecorated(False)
        self.tree.setFrameShape(QFrame.NoFrame)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setStyleSheet(f"QTreeView::item {{ height: {Size.ROW}px; }}")
        from ..components import versions

        found = versions()
        for c in CREDITS:
            version, detail = version_for(c.name, found)
            it = SortItem([c.name, version, c.purpose, c.licence, KIND.get(c.bundled, c.bundled)])
            it.setData(0, Qt.UserRole, c.url)
            it.setToolTip(0, c.url)
            if detail:
                it.setToolTip(1, detail)
            self.tree.addTopLevelItem(it)
        make_sortable(self.tree)
        self.tree.sortByColumn(-1, Qt.AscendingOrder)
        self.tree.itemActivated.connect(lambda it, _c: self._open(it))   # double-click or Enter
        self.tree.setCurrentItem(self.tree.topLevelItem(0))
        lay.addWidget(self.tree, 1)
        lay.addWidget(text(LICENCE_NOTE, Type.FOOTNOTE, secondary=True, wrap=True))

        buttons = QHBoxLayout()
        buttons.setSpacing(Space.S)
        site = button("Open Website", "", None, "Open the selected component's website", "Enter")
        site.clicked.connect(lambda: self._open(self.tree.currentItem()))
        buttons.addWidget(site)
        licences = self._licence_folder()
        if licences is not None:
            files = button("Licence Files", "", "folder", "Show the licence files of the bundled tools")
            files.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(licences))))
            buttons.addWidget(files)
        copy = button("Copy Versions", "", None, "Copy every component's version (useful when reporting a problem)")
        copy.clicked.connect(self._copy_versions)
        buttons.addWidget(copy)
        donate = button("Support on Ko-fi", "", None, "Compressor is free: a donation helps keep it going (opens Ko-fi)")
        donate.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(DONATE_URL)))
        buttons.addWidget(donate)
        buttons.addStretch(1)
        close = button("Close", "primary", None, "", "Esc")
        close.clicked.connect(self.accept)
        close.setDefault(False)
        buttons.addWidget(close)
        lay.addLayout(buttons)
        self.tree.setFocus()

    @staticmethod
    def _copy_versions() -> None:
        from PySide6.QtGui import QGuiApplication

        from ..components import report

        QGuiApplication.clipboard().setText(f"{report()}\nPython {platform.python_version()} · {platform.platform()}")

    @staticmethod
    def _licence_folder() -> Path | None:
        from ..tools import app_dir

        folder = app_dir() / "tools"
        return folder if folder.is_dir() and any(folder.glob("LICENSE*")) else None

    @staticmethod
    def _open(it) -> None:
        if it is not None and it.data(0, Qt.UserRole):
            QDesktopServices.openUrl(QUrl(it.data(0, Qt.UserRole)))


__all__ = ["AboutDialog"]
