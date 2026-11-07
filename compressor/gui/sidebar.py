"""The app's sidebar: brand, pages, the update notice and what's been saved so far."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .style import Size, Space, Type, icon, ui_font
from .widgets import text


class Sidebar(QFrame):
    page_selected = Signal(int)

    def __init__(self, app_name: str, items: list[tuple[str, str, str]], parent=None):
        """items: (label, icon name, shortcut text)."""
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(Size.SIDEBAR_W)
        self.items = items
        from .winframe import custom_frame

        lay = QVBoxLayout(self)
        # With the app's own title bar the name sits in that bar, level with the window buttons.
        self._in_title_bar = custom_frame()
        lay.setContentsMargins(0, 0 if self._in_title_bar else Space.L, 0, Space.L)
        lay.setSpacing(Space.S)
        brand = QHBoxLayout()
        brand.setContentsMargins(*self._brand_margins(False))
        brand.setSpacing(Space.S)
        self.logo = QLabel()
        brand.addWidget(self.logo)
        self.brand_name = text(app_name, Type.TITLE3, bold=True)
        self.brand_name.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)   # the name is never cut short
        brand.addWidget(self.brand_name)
        brand.addStretch(1)
        self.brand = brand
        if self._in_title_bar:
            box = QWidget()
            box.setFixedHeight(Size.TITLE_BAR)
            box.setLayout(brand)
            lay.addWidget(box)
        else:
            lay.addLayout(brand)
        self.list = QListWidget()
        self.list.setAccessibleName("Navigation")
        self.list.setIconSize(QSize(Size.ICON_M, Size.ICON_M))
        self.list.setFont(ui_font(Type.BODY))
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.compact = False
        for label, _icon, shortcut in items:
            it = QListWidgetItem(label)
            it.setData(Qt.AccessibleTextRole, label)
            it.setToolTip(f"{label}  ({shortcut})")
            it.setSizeHint(QSize(0, Size.SIDEBAR_ROW))
            self.list.addItem(it)
        self.list.currentRowChanged.connect(self.page_selected.emit)
        lay.addWidget(self.list, 1)
        foot = QVBoxLayout()
        foot.setContentsMargins(Space.L, 0, Space.L, 0)
        foot.setSpacing(Space.XXS)
        self.foot_label = text("Saved so far", Type.FOOTNOTE, secondary=True)
        self.foot_value = text("", Type.TITLE3, bold=True)
        foot.addWidget(self.foot_label)
        foot.addWidget(self.foot_value)
        self.update_btn = QPushButton()
        self.update_btn.setObjectName("updateLink")
        self.update_btn.setCursor(Qt.PointingHandCursor)
        self.update_btn.setFlat(True)
        self.update_btn.hide()
        foot.addWidget(self.update_btn, 0, Qt.AlignLeft)
        lay.addLayout(foot)
        self._saved: str | None = None
        self._update_foot()
        self.refresh()

    def refresh(self) -> None:
        from .main_window import app_icon

        self.logo.setPixmap(app_icon().pixmap(Size.ICON_M + 4, Size.ICON_M + 4))
        for i, (_label, name, _s) in enumerate(self.items):
            self.list.item(i).setIcon(icon(name, None, Size.ICON_M))

    def set_compact(self, compact: bool) -> None:
        if compact == self.compact:
            return
        self.compact = compact
        self.setFixedWidth(Size.SIDEBAR_COMPACT_W if compact else Size.SIDEBAR_W)
        self.brand_name.setVisible(not compact)
        self.brand.setContentsMargins(*self._brand_margins(compact))
        self._update_foot()
        for i, (label, _icon, _s) in enumerate(self.items):
            self.list.item(i).setText("" if compact else label)

    def _brand_margins(self, compact: bool) -> tuple[int, int, int, int]:
        return (Space.L + (Space.XS if compact else 0), 0, Space.L, 0 if self._in_title_bar else Space.S)

    def set_update(self, version: str | None, security: bool = False) -> None:
        """Show (or hide, with None) that a newer version is available."""
        if not version:
            self.update_btn.hide()
            return
        short = "Security update available" if security else "Update available"
        label = f"Version {version} is available" + (" with security fixes" if security else "")
        self.update_btn.setText("Update" if self.compact else short)
        self.update_btn.setToolTip(label + ". Click for details.")
        self.update_btn.setAccessibleName(label)
        self.update_btn.show()

    def set_saved(self, value: str | None) -> None:
        """Total saved; None hides the footer until something has actually been saved."""
        self._saved = value
        self.foot_value.setText(value or "")
        self.foot_value.setAccessibleName(f"Saved so far: {value}" if value else "")
        self._update_foot()

    def _update_foot(self) -> None:
        show = bool(getattr(self, "_saved", None)) and not self.compact
        self.foot_label.setVisible(show)
        self.foot_value.setVisible(show)


# ---------------------------------------------------------------- table header
