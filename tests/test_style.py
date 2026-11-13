"""Accessibility: every text color meets WCAG AA (4.5:1) on the surfaces it's used on."""

import pytest

pytest.importorskip("PySide6")

from wick.gui.style import DARK, LIGHT, contrast_ratio  # noqa: E402

AA = 4.5


@pytest.mark.parametrize("t", [LIGHT, DARK], ids=["light", "dark"])
def test_text_contrast(t):
    pairs = {
        "text on window": (t.text, t.window), "text on card": (t.text, t.card),
        "secondary on window": (t.text2, t.window), "secondary on card": (t.text2, t.card),
        "secondary on sidebar": (t.text2, t.sidebar), "text on sidebar": (t.text, t.sidebar),
        "accent text on card": (t.accent_text, t.card), "accent text on soft": (t.accent_text, t.accent_soft),
        "button text": (t.on_accent, t.accent), "success pill": (t.success, t.success_soft),
        "danger pill": (t.danger, t.danger_soft), "neutral pill": (t.text2, t.neutral_soft),
        "text on selection": (t.text, t.accent_soft), "text on control": (t.text, t.control),
    }
    failing = {k: round(contrast_ratio(*v), 2) for k, v in pairs.items() if contrast_ratio(*v) < AA}
    assert not failing, failing


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_controls_share_one_height(mode):
    """Buttons (with/without icons, primary, focused), dropdowns, number and text fields: one height."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QComboBox, QGridLayout, QLineEdit, QSpinBox, QWidget

    app = QApplication.instance() or QApplication([])
    from wick.gui import style
    from wick.gui.widgets import button

    style.apply_style(mode)
    w = QWidget()
    g = QGridLayout(w)
    combo = QComboBox()
    combo.addItems(["Keep all", "Remove location"])
    controls = [button("Plain"), button("With icon", "", "files"), button("Primary", "primary", "compress"),
                combo, QSpinBox(), QLineEdit("text")]
    for i, c in enumerate(controls):
        g.addWidget(c, 0, i)
    w.show()
    controls[0].setFocus()
    app.processEvents()
    heights = {type(c).__name__ + (c.text() if hasattr(c, "text") and callable(c.text) else ""): c.height()
               for c in controls}
    assert set(heights.values()) == {style.Size.CONTROL}, heights
    w.close()


@pytest.mark.parametrize("accent", ["#0078D4", "#FFB900", "#E3008C", "#00CC6A", "#744DA9", "#FFFFFF", "#101010"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_windows_accent_colors_stay_readable(accent, theme):
    """Any Windows accent (even pale yellow or near-black) gives buttons and accent text AA contrast."""
    from wick.gui.style import DARK, LIGHT, with_accent

    base = LIGHT if theme == "light" else DARK
    t = with_accent(base, accent)
    assert contrast_ratio(t.on_accent, t.accent) >= 4.5
    assert contrast_ratio(t.accent_text, t.card) >= 4.5 and contrast_ratio(t.accent_text, t.window) >= 4.5
    assert t.accent_soft != t.card and t.window == base.window          # only accent colors change


def test_high_contrast_uses_the_users_colors():
    """In a Windows contrast theme, everything the app draws itself takes the theme's colors."""
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication

    from wick.gui.style import contrast_ratio, high_contrast_tokens

    QApplication.instance() or QApplication([])
    pal = QPalette()
    for role, color in ((QPalette.Window, "#000000"), (QPalette.Base, "#000000"), (QPalette.WindowText, "#FFFF00"),
                         (QPalette.Highlight, "#1AEBFF"), (QPalette.HighlightedText, "#000000"),
                         (QPalette.Button, "#000000"), (QPalette.ButtonText, "#FFFFFF"),
                         (QPalette.Link, "#75E9FC")):
        pal.setColor(QPalette.Active, role, QColor(color))
    t = high_contrast_tokens(pal)
    assert t.dark and t.window == "#000000" and t.text == t.text2 == "#FFFF00"
    assert t.accent == "#1AEBFF" and t.on_accent == "#000000" and t.accent_text == "#75E9FC"
    assert t.danger == t.success == t.text                       # no colors of the app's own
    assert contrast_ratio(t.text, t.window) >= 7 and contrast_ratio(t.on_accent, t.accent) >= 7


def test_focus_rings_only_for_keyboard_focus():
    """Like Windows: a button focused by a click or a window opening shows no ring; Tab (or a shortcut) does.
    (The filter is called directly: earlier tests may leave a QGuiApplication, which doesn't route widget events.)"""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QFocusEvent, QGuiApplication
    from PySide6.QtWidgets import QApplication, QPushButton

    from wick.gui import style

    if QGuiApplication.instance() is None:
        QApplication([])
    if not isinstance(QGuiApplication.instance(), QApplication):
        import pytest

        pytest.skip("an earlier test made a QGuiApplication (no widgets possible)")
    focus = style._FocusVisible()
    b = QPushButton("OK")
    for reason, ring in ((Qt.MouseFocusReason, False), (Qt.ActiveWindowFocusReason, False),
                         (Qt.OtherFocusReason, False), (Qt.TabFocusReason, True), (Qt.ShortcutFocusReason, True)):
        focus.eventFilter(b, QFocusEvent(QEvent.FocusIn, reason))
        assert (b.property("kbfocus") is True) == ring, reason
        focus.eventFilter(b, QFocusEvent(QEvent.FocusOut, Qt.OtherFocusReason))
        assert b.property("kbfocus") is not True


def test_tooltips_are_cards_under_the_control():
    """A tooltip is the app's own card, centered under the control it describes (not at the pointer), wrapped
    when long, and gone when asked."""
    from PySide6.QtCore import QEvent, QPoint
    from PySide6.QtGui import QGuiApplication, QHelpEvent
    from PySide6.QtWidgets import QApplication, QPushButton

    from wick.gui import style

    if QGuiApplication.instance() is None:
        QApplication([])
    if not isinstance(QGuiApplication.instance(), QApplication):
        import pytest

        pytest.skip("an earlier test made a QGuiApplication (no widgets possible)")
    b = QPushButton("1 task running")
    b.setToolTip("Home analysis: Working...")
    b.resize(300, 40)
    b.move(400, 300)
    b.show()
    tips = style._Tooltips()
    pointer = b.mapToGlobal(QPoint(290, 5))                    # near the button's right edge
    assert tips.eventFilter(b, QHelpEvent(QEvent.ToolTip, QPoint(290, 5), pointer)) is True
    card = style._tip_card
    assert card.isVisible() and card.label.text() == "Home analysis: Working..."
    below = b.mapToGlobal(QPoint(0, b.height()))
    assert card.y() >= below.y()                               # under the button, not over it
    assert abs(card.geometry().center().x() - b.mapToGlobal(b.rect().center()).x()) <= 2   # centered on it
    style.show_tip("word " * 80, b)
    assert card.label.wordWrap() and card.width() < 700        # long text wraps at a reading width
    style.hide_tip()
    assert not card.isVisible()
    b.setToolTip("")
    assert tips.eventFilter(b, QHelpEvent(QEvent.ToolTip, QPoint(5, 5), pointer)) is False
