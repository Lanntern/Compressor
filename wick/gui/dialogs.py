"""Consistent alert dialogs: app icon, readable width, a clear primary action."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QMessageBox, QWidget

from .. import APP_NAME
from .style import Size, Space

DIALOG_TEXT_WIDTH = Space.XXL * 12   # 384 px: comfortable line length


def _role(button, name: str) -> None:
    """Give a dialog button a style role (re-polish: Qt styled it before the name was set)."""
    button.setObjectName(name)
    button.style().unpolish(button)
    button.style().polish(button)


def _box(parent: QWidget | None, text: str, info: str = "") -> QMessageBox:
    from .main_window import app_icon

    box = QMessageBox(parent)
    box.setWindowTitle(APP_NAME)
    box.setIconPixmap(app_icon().pixmap(Size.ICON_L, Size.ICON_L))
    box.setText(text)
    if info:
        box.setInformativeText(info)
    for lab in box.findChildren(QLabel):
        if lab.objectName() in ("qt_msgbox_label", "qt_msgbox_informativelabel"):
            lab.setMinimumWidth(DIALOG_TEXT_WIDTH)
            lab.setMaximumWidth(int(DIALOG_TEXT_WIDTH * 1.4))
            lab.setWordWrap(True)
    return box


def ask(parent: QWidget | None, text: str, info: str = "", action: str = "OK", destructive: bool = False) -> bool:
    """Confirm an action. The action button is the default (Enter); Esc cancels."""
    box = _box(parent, text, info)
    cancel = box.addButton("Cancel", QMessageBox.RejectRole)
    go = box.addButton(action, QMessageBox.DestructiveRole if destructive else QMessageBox.AcceptRole)
    _role(go, "danger" if destructive else "primary")
    box.setDefaultButton(go)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is go


def choose(parent: QWidget | None, text: str, info: str, default: str, other: str) -> bool:
    """A message with a default button and one other choice. True when ``other`` was chosen."""
    box = _box(parent, text, info)
    alt = box.addButton(other, QMessageBox.ActionRole)
    _role(alt, "")
    ok = box.addButton(default, QMessageBox.AcceptRole)
    _role(ok, "primary")
    box.setDefaultButton(ok)
    box.setEscapeButton(ok)
    box.exec()
    return box.clickedButton() is alt


def inform(parent: QWidget | None, text: str, info: str = "") -> None:
    box = _box(parent, text, info)
    ok = box.addButton("OK", QMessageBox.AcceptRole)
    _role(ok, "primary")
    box.setDefaultButton(ok)
    box.exec()


def warn(parent: QWidget | None, text: str, info: str = "") -> None:
    inform(parent, text, info)
