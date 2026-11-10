"""Accessibility: every text colour meets WCAG AA (4.5:1) on the surfaces it's used on."""

import pytest

pytest.importorskip("PySide6")

from compressor.gui.style import DARK, LIGHT, contrast_ratio  # noqa: E402

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
    from compressor.gui import style
    from compressor.gui.widgets import button

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
def test_windows_accent_colours_stay_readable(accent, theme):
    """Any Windows accent (even pale yellow or near-black) gives buttons and accent text AA contrast."""
    from compressor.gui.style import DARK, LIGHT, with_accent

    base = LIGHT if theme == "light" else DARK
    t = with_accent(base, accent)
    assert contrast_ratio(t.on_accent, t.accent) >= 4.5
    assert contrast_ratio(t.accent_text, t.card) >= 4.5 and contrast_ratio(t.accent_text, t.window) >= 4.5
    assert t.accent_soft != t.card and t.window == base.window          # only accent colours change
