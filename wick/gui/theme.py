"""Theme helpers (thin layer over style.py tokens)."""

from __future__ import annotations

from PySide6.QtGui import QColor

from . import style

THEMES = (("System", "system"), ("Light", "light"), ("Dark", "dark"))


def apply_theme(mode: str) -> None:
    style.apply_style(mode)


def is_dark() -> bool:
    return style.tokens().dark


def success_color() -> QColor:
    return QColor(style.tokens().success)


def error_color() -> QColor:
    return QColor(style.tokens().danger)


def muted_color() -> QColor:
    return QColor(style.tokens().text2)


def css(color: QColor) -> str:
    return color.name()
