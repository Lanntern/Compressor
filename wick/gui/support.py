"""Asking, once, for a GitHub star or a donation: only after Wick has been around for a day and has saved
at least 10 GB, never while something is running, and never again after that (the sidebar's "Support
Wick" link opens the same window any time)."""

from __future__ import annotations

import time

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout

from .. import APP_NAME
from ..about import DONATE_URL
from ..formats import human
from .style import Space, Type
from .widgets import button, text

GITHUB_URL = "https://github.com/Lanntern/Wick"
MIN_AGE = 24 * 3600            # seconds since Wick first ran
MIN_SAVED = 10 * 1024 ** 3     # bytes saved: "10 GB" as the app shows sizes (formats.human counts in 1024s)

# QSettings keys
FIRST_SEEN = "support/first_seen"     # unix time of the first start with this feature
ASKED = "support/asked"               # the one-time prompt has been shown
SAVED_MOST = "support/saved_most"     # the most the History has ever shown as saved (pruning can lower it later)


def note_start(qs, now: float | None = None) -> None:
    """Remember when Wick first ran (call at every start; only the first one counts)."""
    if not qs.contains(FIRST_SEEN):
        qs.setValue(FIRST_SEEN, time.time() if now is None else now)


def note_saved(qs, total: int) -> int:
    """Record History's current total; returns the most ever saved (history can be pruned or undone later)."""
    most = max(int(qs.value(SAVED_MOST, 0, int)), int(total))
    qs.setValue(SAVED_MOST, most)
    return most


def due(qs, now: float | None = None) -> bool:
    """Time to ask: not asked before, around for a day, and 10 GB or more saved."""
    if qs.value(ASKED, False, bool):
        return False
    now = time.time() if now is None else now
    first = float(qs.value(FIRST_SEEN, now, float))
    return now - first >= MIN_AGE and int(qs.value(SAVED_MOST, 0, int)) >= MIN_SAVED


class SupportDialog(QDialog):
    """A small window, not modal: it never blocks the app, and closing it is always fine."""

    def __init__(self, saved: int, parent=None, asked: bool = False):
        """``asked``: opened by the one-time prompt (not from the sidebar)."""
        super().__init__(parent)
        self.setWindowTitle(f"Support {APP_NAME}")
        self.setWindowModality(Qt.NonModal)
        self.setAttribute(Qt.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Space.XL, Space.XL, Space.XL, Space.XL)
        lay.setSpacing(Space.M)
        heading = (f"{APP_NAME} has saved you {human(saved)}" if saved >= 1024 ** 3
                   else f"Enjoying {APP_NAME}?")
        lay.addWidget(text(heading, Type.TITLE3, bold=True, wrap=True))
        lay.addWidget(text(
            f"{APP_NAME} is free and open source. If you'd like to support it, a star on GitHub or a tip on Ko-fi "
            "helps.", Type.BODY, wrap=True))
        if asked:
            lay.addWidget(text(f"This won't show again. It's under “Support {APP_NAME}” in the sidebar.",
                               Type.FOOTNOTE, secondary=True, wrap=True))
        row = QHBoxLayout()
        row.setSpacing(Space.S)
        self.star = button("Star on GitHub", "", "star", "Open Wick's GitHub page")
        self.star.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(GITHUB_URL)))
        self.donate = button("Ko-fi", "", "heart", "Open Wick's Ko-fi page")
        self.donate.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(DONATE_URL)))
        self.close_btn = button("Close", "", None, "", "Esc")
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.star)
        row.addWidget(self.donate)
        row.addStretch(1)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        self.setMinimumWidth(420)
        # Enter or Esc just closes: a reflex key press shouldn't open a browser.
        self.close_btn.setDefault(True)
        self.close_btn.setFocus()


__all__ = ["SupportDialog", "GITHUB_URL", "note_start", "note_saved", "due", "MIN_AGE", "MIN_SAVED"]
