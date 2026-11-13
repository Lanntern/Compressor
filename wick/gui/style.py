"""Design system: color tokens, typography, icons and the application stylesheet.

Light/dark tokens follow Apple's Human Interface color roles. Every text color
used on its background meets WCAG AA (4.5:1) - see tests/test_style.py.
When Windows High Contrast is on, no custom styling is applied at all.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache

from PySide6.QtCore import QByteArray, QEvent, QObject, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLineEdit, QWidget


@dataclass(frozen=True)
class Tokens:
    dark: bool
    window: str        # page background
    sidebar: str
    card: str          # raised surfaces
    card_alt: str      # alternating rows, subtle fills
    border: str
    separator: str
    text: str
    text2: str         # secondary text (AA on card & window)
    text3: str         # placeholder / disabled
    accent: str        # buttons, focus, links
    accent_hover: str
    accent_pressed: str
    accent_text: str   # accent used as text on card (AA)
    on_accent: str
    accent_soft: str   # selection / soft fills
    control: str       # buttons & inputs
    control_hover: str
    control_border: str
    success: str
    success_soft: str
    danger: str
    danger_soft: str
    warning: str
    warning_soft: str
    neutral_soft: str
    canvas: str        # preview background


LIGHT = Tokens(
    dark=False, window="#F5F5F7", sidebar="#ECECEF", card="#FFFFFF", card_alt="#FAFAFC",
    border="#E3E3E8", separator="#EDEDF0", text="#1D1D1F", text2="#5E5E63", text3="#8E8E93",
    accent="#0066CC", accent_hover="#0071E3", accent_pressed="#00539F", accent_text="#0060C0",
    on_accent="#FFFFFF", accent_soft="#E3EEFB", control="#FFFFFF", control_hover="#F2F2F5",
    control_border="#D2D2D7", success="#1B7A33", success_soft="#E4F4E8", danger="#C4001A",
    danger_soft="#FDE8EA", warning="#9A4A00", warning_soft="#FFF1DC", neutral_soft="#EFEFF2",
    canvas="#E9E9ED",
)

DARK = Tokens(
    dark=True, window="#1C1C1E", sidebar="#232326", card="#2C2C2E", card_alt="#313134",
    border="#3A3A3D", separator="#38383A", text="#F5F5F7", text2="#B4B4BA", text3="#8E8E93",
    accent="#0A6CDB", accent_hover="#1A7AE8", accent_pressed="#085BB8", accent_text="#6CB4FF",
    on_accent="#FFFFFF", accent_soft="#173A63", control="#3A3A3C", control_hover="#444447",
    control_border="#4A4A4D", success="#4CD06D", success_soft="#1E3A26", danger="#FF6961",
    danger_soft="#4A2523", warning="#FFB340", warning_soft="#43341C", neutral_soft="#3A3A3C",
    canvas="#141415",
)

_current: Tokens = LIGHT
_system_accent = False      # Settings › Appearance › "Use the Windows accent color"


def set_system_accent(on: bool) -> None:
    """Use Windows' accent color instead of the app's blue (from the next apply_style)."""
    global _system_accent
    _system_accent = bool(on)


def windows_accent() -> str | None:
    """The accent color chosen in Windows' Settings › Personalization › Colors ("#RRGGBB"), or None. Asked of
    Windows' own UISettings API (the supported way), not read from its private registry values."""
    from .winrt import accent_color

    return accent_color()


def _mix(a: QColor, b: QColor, t: float) -> str:
    """``a`` moved ``t`` (0..1) of the way towards ``b``."""
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t)).name().upper()


def with_accent(t: Tokens, accent: str) -> Tokens:
    """``t`` with every accent color derived from ``accent``, each nudged until it meets the same contrast
    rules as the app's own blue (AA: 4.5:1 for text on buttons, and for accent-colored text on cards)."""
    import dataclasses

    base = QColor(accent)
    on = "#FFFFFF"                  # as Windows does with light accents: a deeper shade behind white text
    for _ in range(60):                                     # buttons: text on the accent stays readable
        if contrast_ratio(on, base.name()) >= 4.5:
            break
        base = base.darker(106) if base.value() > 20 else QColor(0, 0, 0)
    text = QColor(base)
    for _ in range(40):                                     # links and accent text on cards and the page
        if contrast_ratio(text.name(), t.card) >= 4.5 and contrast_ratio(text.name(), t.window) >= 4.5:
            break
        text = text.lighter(108) if t.dark else text.darker(108)
    background = QColor(t.card)
    return dataclasses.replace(
        t, accent=base.name().upper(), accent_hover=base.lighter(110).name().upper(),
        accent_pressed=base.darker(115).name().upper(), accent_text=text.name().upper(), on_accent=on,
        accent_soft=_mix(background, base, 0.35 if t.dark else 0.14))


def tokens() -> Tokens:
    return _current


# ---------------------------------------------------------------- accessibility

def high_contrast_active() -> bool:
    """Windows High Contrast mode: the user's colors must win, so we don't style."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class HIGHCONTRAST(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD),
                        ("lpszDefaultScheme", wintypes.LPWSTR)]

        hc = HIGHCONTRAST()
        hc.cbSize = ctypes.sizeof(HIGHCONTRAST)
        if ctypes.windll.user32.SystemParametersInfoW(0x42, hc.cbSize, ctypes.byref(hc), 0):  # SPI_GETHIGHCONTRAST
            return bool(hc.dwFlags & 1)  # HCF_HIGHCONTRASTON
    except Exception:  # noqa: BLE001
        pass
    return False


def high_contrast_tokens(pal: QPalette) -> Tokens:
    """The app's colors taken from Windows' contrast theme (the user's own choices), for everything the app
    draws itself: icons, switches, the sidebar, charts. No color coding of its own: success, warning and
    danger use the theme's text color (the words and icons still say which is which)."""
    def c(role, group=QPalette.Active) -> str:
        return pal.color(group, role).name().upper()

    bg, fg = c(QPalette.Window), c(QPalette.WindowText)
    hi, on_hi = c(QPalette.Highlight), c(QPalette.HighlightedText)
    button, button_text = c(QPalette.Button), c(QPalette.ButtonText)
    return Tokens(
        dark=QColor(bg).lightness() < 128, window=bg, sidebar=bg, card=c(QPalette.Base), card_alt=c(QPalette.Base),
        border=fg, separator=fg, text=fg, text2=fg, text3=c(QPalette.WindowText, QPalette.Disabled),
        accent=hi, accent_hover=hi, accent_pressed=hi, accent_text=c(QPalette.Link), on_accent=on_hi,
        accent_soft=hi, control=button, control_hover=button, control_border=button_text,
        success=fg, success_soft=bg, danger=fg, danger_soft=bg, warning=fg, warning_soft=bg, neutral_soft=bg,
        canvas=bg)


def contrast_ratio(fg: str, bg: str) -> float:
    def lum(hex_color: str) -> float:
        c = QColor(hex_color)
        out = []
        for v in (c.redF(), c.greenF(), c.blueF()):
            out.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
        return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]

    a, b = lum(fg), lum(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


# ---------------------------------------------------------------- scales
# The ONLY sizes the UI uses. Everything else is derived from these.

class Type:
    """Type scale, as multiples of the user's system font size (Apple HIG roles)."""
    CAPTION = 0.85
    FOOTNOTE = 0.92
    BODY = 1.0
    HEADLINE = 1.0      # body size, semibold
    TITLE3 = 1.2
    TITLE2 = 1.45
    TITLE1 = 1.9
    DISPLAY = 2.4


class Space:
    """Spacing scale in device-independent pixels (8-point grid)."""
    XXS = 2
    XS = 4
    S = 8
    M = 12
    L = 16
    XL = 24
    XXL = 32


class Radius:
    SMALL = 6       # pills, tooltips, menu items
    CONTROL = 8     # buttons, inputs, list rows
    CARD = 12       # cards, tables
    LARGE = 16      # drop zone


class Size:
    """Component sizes in device-independent pixels at the platform's default text size.

    Text-bound sizes grow with the user's text size (Windows "Make text bigger", desktop
    font settings) via ``scale_to_text`` so labels never clip.
    """
    CONTROL = 32        # height of every button, field and dropdown
    ROW = 36            # table / list row height
    FORM_LABEL = 112    # label column in option forms
    INDICATOR = 16      # checkbox indicator
    SIDEBAR_COMPACT_W = 64
    SIDEBAR_ROW = 34
    SIDEBAR_W = 216
    TITLE_BAR = 40      # the window's own title bar (Windows): drag strip and caption buttons
    INSPECTOR_W = 368
    ICON_S = 16
    ICON_M = 20
    ICON_L = 48


# ---------------------------------------------------------------- typography

UI_FAMILIES = ["Segoe UI Variable Text", "Segoe UI", ".AppleSystemUIFont", "SF Pro Text",
               "Inter", "Cantarell", "Noto Sans", "Ubuntu", "DejaVu Sans"]


_BASE_PT: float | None = None


_TEXT_BOUND = ("CONTROL", "ROW", "FORM_LABEL", "SIDEBAR_ROW", "SIDEBAR_W", "INSPECTOR_W")
_REFERENCE_PT = {"win32": 9.0, "darwin": 13.0}.get(__import__("sys").platform, 10.0)
_scaled = False


def text_scale() -> float:
    """How much larger the user's text is than the platform default (never below 1)."""
    return max(1.0, base_point_size() / _REFERENCE_PT)


def scale_to_text() -> None:
    """Grow text-bound Size tokens with the text size. Call once, before building widgets."""
    global _scaled
    if _scaled:
        return
    _scaled = True
    f = text_scale()
    if f > 1.0:
        for name in _TEXT_BOUND:
            setattr(Size, name, int(round(getattr(Size, name) * f)))


def base_point_size() -> float:
    """The user's system font size (respects Windows text scaling / desktop settings).

    Read once, before our own font is applied, so the scale never compounds.
    """
    global _BASE_PT
    if _BASE_PT is None:
        from PySide6.QtWidgets import QApplication

        size = QApplication.font().pointSizeF()
        _BASE_PT = max(9.0, size if size > 0 else 10.0)
    return _BASE_PT


def pt(role: float) -> float:
    return round(base_point_size() * role, 1)


def ui_font(scale: float = Type.BODY, weight: QFont.Weight = QFont.Normal) -> QFont:
    f = QFont()
    f.setFamilies(UI_FAMILIES)
    f.setPointSizeF(pt(scale))
    f.setWeight(weight)
    return f


# ---------------------------------------------------------------- icons
# Simple 24x24 stroke icons (SF-Symbols-like). "currentColor" is replaced at render time.

_ICONS = {
    "compress": '<path d="M4 9h5V4M20 9h-5V4M4 15h5v5M20 15h-5v5"/>',
    "watch": '<path d="M2.5 12s3.5-6.5 9.5-6.5S21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "history": '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/><path d="M3.5 4.5v4h4"/><path d="M12 7.5V12l3 2"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "folder": '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2h9A1.5 1.5 0 0 1 21 9.5v8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z"/>',
    "files": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    "drop": '<path d="M12 3v11"/><path d="M7.5 9.5 12 14l4.5-4.5"/><path d="M4 15v3.5A2.5 2.5 0 0 0 6.5 21h11a2.5 2.5 0 0 0 2.5-2.5V15"/>',
    "sidebar": '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M15 4.5v15"/>',
    "eye": '<path d="M2.5 12s3.5-6.5 9.5-6.5S21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "refresh": '<path d="M20.5 12a8.5 8.5 0 1 1-2.5-6"/><path d="M20.5 4v4.5H16"/>',
    "retry": '<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6"/><path d="M3.5 4v4.5H8"/>',
    "undo": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "xmark": '<path d="M6 6l12 12M18 6 6 18"/>',
    "minus": '<path d="M6 12h12"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2.5"/>',
    "bolt": '<path d="M13 2.5 4.5 13.5h6.5l-1 8 8.5-11h-6.5z"/>',
    "keyboard": '<rect x="2.5" y="6" width="19" height="12" rx="2.5"/><path d="M6.5 10h1M10.5 10h1M14.5 10h1M6.5 14h11"/>',
    "chevron": '<path d="m9 6 6 6-6 6"/>',
    "chevron_down": '<path d="m6 9 6 6 6-6"/>',
    "broom": '<path d="M19.5 3.5 12 11"/><path d="M12.5 10.5c1.6 1.6 1.4 3.6.4 5.4L11 19.5c-.4.7-1.3.8-1.9.3l-4.9-4.9c-.6-.6-.4-1.5.3-1.9L8.1 11c1.8-1 3.8-1.2 4.4-.5z"/><path d="M7 14.5l2.5 2.5"/>',
    "home": '<path d="M3.5 11 12 4l8.5 7"/><path d="M5.5 9.5V20h13V9.5"/><path d="M10 20v-5h4v5"/>',
    "convert": '<path d="M4 8h13l-3.5-3.5"/><path d="M20 16H7l3.5 3.5"/>',
    "tag": '<path d="M3.5 12.5V4.5a1 1 0 0 1 1-1h8l8 8-9 9z"/><circle cx="8" cy="8" r="1.6"/>',
    "ellipsis": '<circle cx="6" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="18" cy="12" r="1.2"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6M5.5 7l1 12a2 2 0 0 0 2 2h7a2 2 0 0 0 2-2l1-12M9 7V4.5h6V7"/>',
    "lock": '<rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
    "heart": '<path d="M12 20s-7.5-4.6-7.5-10.2A4.3 4.3 0 0 1 12 7.2a4.3 4.3 0 0 1 7.5 2.6C19.5 15.4 12 20 12 20z"/>',
    "pause": '<rect x="7" y="5" width="3.2" height="14" rx="1"/><rect x="13.8" y="5" width="3.2" height="14" rx="1"/>',
    "play": '<path d="M8.5 5.5v13l10-6.5z"/>',
    "star": '<path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.8-5.2 2.8 1-5.8-4.3-4.1 5.9-.9z"/>',
}


def icon_svg(name: str, color: str, stroke: float = 1.8) -> bytes:
    body = _ICONS[name]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>').encode()


# Windows' own icon font (Windows 11; Windows 10 has the older Segoe MDL2 Assets): the default on Windows.
FLUENT_FONTS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
FLUENT_GLYPHS = {
    "compress": 0xE73F, "watch": 0xE7B3, "history": 0xE81C, "settings": 0xE713, "folder": 0xE8B7,
    "files": 0xE8A5, "drop": 0xE896, "sidebar": 0xE700, "eye": 0xE890, "refresh": 0xE72C, "retry": 0xE777,
    "undo": 0xE7A7, "check": 0xE73E, "xmark": 0xE711, "minus": 0xE738, "plus": 0xE710, "stop": 0xE71A,
    "bolt": 0xE945, "keyboard": 0xE765, "chevron": 0xE76C, "chevron_down": 0xE70D, "broom": 0xEA99,
    "home": 0xE80F, "convert": 0xE8AB, "tag": 0xE8EC, "ellipsis": 0xE712, "trash": 0xE74D, "lock": 0xE72E,
    "heart": 0xEB51, "pause": 0xE769, "play": 0xE768, "star": 0xE734,
}
_fluent = False


def use_fluent_icons(on: bool) -> None:
    """Draw icons with Windows' icon font where it has them (developer setting; the app's own icons otherwise)."""
    global _fluent
    _fluent = on
    _icon_pixmap.cache_clear()


@lru_cache(maxsize=1)
def _fluent_family() -> str:
    from PySide6.QtGui import QFontDatabase

    families = set(QFontDatabase.families())
    return next((f for f in FLUENT_FONTS if f in families), "")


def _fluent_char(name: str) -> str:
    """The icon font's character for ``name``, or "" when the font (or that glyph) isn't there."""
    if not _fluent or name not in FLUENT_GLYPHS or not _fluent_family():
        return ""
    from PySide6.QtGui import QFontMetrics

    code = FLUENT_GLYPHS[name]
    return chr(code) if QFontMetrics(QFont(_fluent_family())).inFontUcs4(code) else ""


def _paint_icon(p: QPainter, rect: QRectF, name: str, color: str, stroke: float) -> None:
    """Draw the icon into ``rect`` as vectors, so it's sharp at whatever scale the painter has."""
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    glyph = _fluent_char(name)
    if glyph:
        font = QFont(_fluent_family())
        font.setPixelSize(max(1, round(rect.height() * 0.84)))   # (the font's glyphs fill ~5/6 of the em)
        font.setHintingPreference(QFont.PreferNoHinting)
        p.setFont(font)
        p.setPen(QColor(color))
        p.drawText(rect, Qt.AlignCenter, glyph)
    else:
        QSvgRenderer(QByteArray(icon_svg(name, color, stroke))).render(p, rect)


def screen_scale() -> float:
    """The highest display scaling among the screens (125% -> 1.25), for pixmaps drawn ahead of time."""
    screens = QGuiApplication.screens() if QGuiApplication.instance() else []
    return max((sc.devicePixelRatio() for sc in screens), default=1.0) or 1.0


def icon_pixmap(name: str, color: str, size: int, dpr: float | None = None, stroke: float = 1.8) -> QPixmap:
    """Rendered once per name/color/size/scale (QPixmap copies are cheap and implicitly shared). ``dpr``:
    the display scaling to render for (default: the screens' highest, so it's sharp, not stretched)."""
    return QPixmap(_icon_pixmap(name, color, size, round(dpr or screen_scale(), 3), stroke))


@lru_cache(maxsize=512)
def _icon_pixmap(name: str, color: str, size: int, dpr: float, stroke: float) -> QPixmap:
    pm = QPixmap(max(1, round(size * dpr)), max(1, round(size * dpr)))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    try:
        _paint_icon(p, QRectF(0, 0, pm.width(), pm.height()), name, color, stroke)
    finally:
        p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


class _IconEngine(QIconEngine):
    """Draws the icon at exactly the size and display scaling it's shown at (Qt asks per screen: 100%, 150%,
    250%...), instead of stretching one pre-rendered pixmap. Colors per mode: normal, disabled, selected."""

    def __init__(self, name: str, colors: dict, stroke: float = 1.8):
        super().__init__()
        self._name, self._colors, self._stroke = name, colors, stroke

    def _color(self, mode) -> str:
        return self._colors.get(mode) or self._colors[QIcon.Normal]

    def paint(self, painter, rect, mode, state) -> None:
        _paint_icon(painter, QRectF(rect), self._name, self._color(mode), self._stroke)

    def scaledPixmap(self, size, mode, state, scale) -> QPixmap:
        side = min(size.width(), size.height())
        return icon_pixmap(self._name, self._color(mode), side, scale or 1.0, self._stroke)

    def pixmap(self, size, mode, state) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def actualSize(self, size, mode, state):
        return size

    def clone(self) -> QIconEngine:
        return _IconEngine(self._name, dict(self._colors), self._stroke)

    def key(self) -> str:
        return "wick-icon"


def icon(name: str, color: str | None = None, size: int = 20) -> QIcon:
    """The named icon in the theme's colors (``color`` for normal and selected, when given). ``size`` is
    unused: the icon is drawn at whatever size it's shown."""
    t = tokens()
    return QIcon(_IconEngine(name, {QIcon.Normal: color or t.text2, QIcon.Disabled: t.text3,
                                    QIcon.Selected: color or t.accent_text}))


# ---------------------------------------------------------------- stylesheet assets
# Qt stylesheets need files for arrows/indicators; they're generated per theme.

def _asset(name: str, svg: str) -> str:
    import hashlib

    from ..journal import data_dir

    folder = data_dir() / "ui"            # (in the app's own folder: removed with it, never left in %TEMP%)
    folder.mkdir(exist_ok=True)
    path = folder / f"{name}-{hashlib.sha1(svg.encode()).hexdigest()[:10]}.svg"
    if not path.exists():
        path.write_text(svg, encoding="utf-8")
    return path.as_posix()


def _stroke_svg(body: str, color: str, width: float = 2.0) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')


def _checkbox_svg(fill: str, border: str, check: str | None) -> str:
    mark = f'<path d="m6.5 12.5 3.5 3.5 7.5-8" stroke="{check}" stroke-width="2.6" fill="none" ' \
           f'stroke-linecap="round" stroke-linejoin="round"/>' if check else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" '
            f'rx="6" fill="{fill}" stroke="{border}" stroke-width="1.6"/>{mark}</svg>')


def assets(t: "Tokens") -> dict[str, str]:
    return {
        "down": _asset("down", _stroke_svg('<path d="m7 10 5 5 5-5"/>', t.text2)),
        "down_disabled": _asset("down", _stroke_svg('<path d="m7 10 5 5 5-5"/>', t.text3)),
        "up_small": _asset("up", _stroke_svg('<path d="m8 14 4-4 4 4"/>', t.text2, 2.4)),
        "down_small": _asset("down", _stroke_svg('<path d="m8 10 4 4 4-4"/>', t.text2, 2.4)),
        "box": _asset("box", _checkbox_svg(t.control, t.control_border, None)),
        "box_hover": _asset("box", _checkbox_svg(t.control, t.accent, None)),
        "box_checked": _asset("box", _checkbox_svg(t.accent, t.accent, "#FFFFFF")),
        "box_mixed": _asset("box", _checkbox_svg(t.accent, t.accent, None).replace(
            "</svg>", '<path d="M7 12h10" stroke="#FFFFFF" stroke-width="2.6" stroke-linecap="round"/></svg>')),
        "box_disabled": _asset("box", _checkbox_svg(t.card_alt, t.border, None)),
        "box_checked_disabled": _asset("box", _checkbox_svg(t.neutral_soft, t.border, t.text3)),
    }


# ---------------------------------------------------------------- palette & stylesheet

def palette(t: Tokens) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.Window: t.window, QPalette.WindowText: t.text, QPalette.Base: t.card,
        QPalette.AlternateBase: t.card_alt, QPalette.ToolTipBase: t.card, QPalette.ToolTipText: t.text,
        QPalette.Text: t.text, QPalette.Button: t.control, QPalette.ButtonText: t.text,
        QPalette.BrightText: t.danger, QPalette.Highlight: t.accent, QPalette.HighlightedText: t.on_accent,
        QPalette.Link: t.accent_text, QPalette.PlaceholderText: t.text3, QPalette.Mid: t.border,
        QPalette.Midlight: t.separator, QPalette.Dark: t.control_border, QPalette.Light: t.card,
        QPalette.Shadow: "#000000",
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(t.text3))
    p.setColor(QPalette.Inactive, QPalette.Highlight, QColor(t.accent))
    return p


def stylesheet(t: Tokens) -> str:
    S, R, Z = Space, Radius, Size
    foot = pt(Type.FOOTNOTE)
    a = assets(t)
    ind = Z.INDICATOR
    h1, h2 = Z.CONTROL - 2, Z.CONTROL - 4   # content height with a 1px / 2px border
    bp = S.L - 2                             # horizontal button padding
    return f"""
* {{ outline: none; }}
QWidget {{ color: {t.text}; }}
QMainWindow, QDialog, #page {{ background: {t.window}; }}
QToolTip {{ background: {t.card}; color: {t.text}; border: 1px solid {t.border}; border-radius: {R.SMALL}px;
  opacity: 250;
  padding: {S.XS}px {S.S}px; font-size: {foot}pt; }}

/* ---- sidebar */
#sidebar {{ background: {t.sidebar}; border: none; border-right: 1px solid {t.separator}; }}
#updateLink {{ color: {t.accent_text}; background: transparent; border: none; padding: 0; text-align: left;
              font-weight: 600; }}
#updateLink:hover {{ text-decoration: underline; }}
#updateLink[kbfocus="true"]:focus {{ outline: none; border-bottom: 2px solid {t.accent}; }}
#supportLink {{ color: {t.text2}; background: transparent; border: none; padding: 0; text-align: left; }}
#supportLink:hover {{ color: {t.accent_text}; }}
#supportLink[kbfocus="true"]:focus {{ outline: none; border-bottom: 2px solid {t.accent}; }}
#sidebar QListWidget {{ background: transparent; border: none; padding: {S.XS}px {S.S}px; }}
#sidebar QListWidget::item {{ height: {Z.SIDEBAR_ROW}px; padding-left: {S.S}px; border-radius: {R.CONTROL}px;
  color: {t.text}; margin: 1px 0; }}
#sidebar QListWidget::item:hover {{ background: {t.control_hover}; }}
#sidebar QListWidget::item:selected {{ background: {t.accent_soft}; color: {t.accent_text}; }}
#sidebar QListWidget:focus {{ border: none; }}
QPushButton:focus, QToolButton:focus, QCheckBox:focus, QRadioButton:focus {{ outline: none; }}
#sidebar QListWidget[kbfocus="true"]::item:focus {{ border: 2px solid {t.accent}; }}

/* ---- text roles (sizes come from fonts set in code via Type) */
#secondary, #pageSubtitle {{ color: {t.text2}; }}
#sectionTitle {{ color: {t.text2}; }}
#statLabel {{ color: {t.text2}; }}
#statValueAccent {{ color: {t.accent_text}; }}

/* ---- cards */
#card {{ background: {t.card}; border: 1px solid {t.border}; border-radius: {R.CARD}px; }}
#inspector {{ background: {t.window}; border: none; border-left: 1px solid {t.separator}; }}
#inspectorScroll, #inspectorBody {{ background: transparent; border: none; }}
#separator {{ background: {t.separator}; border: none; }}

/* ---- buttons: every button is exactly Size.CONTROL tall; focus swaps 1px of padding for border */
QPushButton {{
  background: {t.control}; color: {t.text}; border: 1px solid {t.control_border};
  border-radius: {R.CONTROL}px; padding: 0 {bp}px; min-height: {h1}px; max-height: {h1}px;
}}
QPushButton:hover {{ background: {t.control_hover}; }}
QPushButton:pressed {{ background: {t.neutral_soft}; }}
QPushButton:disabled {{ color: {t.text3}; background: {t.card_alt}; border-color: {t.border}; }}
QPushButton[kbfocus="true"]:focus {{ border: 2px solid {t.accent}; padding: 0 {bp - 1}px; min-height: {h2}px; max-height: {h2}px; }}
QPushButton:checked {{ background: {t.accent_soft}; border-color: {t.accent}; color: {t.accent_text}; }}
QPushButton#primary {{ background: {t.accent}; color: {t.on_accent}; border-color: {t.accent}; font-weight: 600; }}
QPushButton#primary:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QPushButton#primary:pressed {{ background: {t.accent_pressed}; border-color: {t.accent_pressed}; }}
QPushButton#primary:disabled {{ background: {t.neutral_soft}; color: {t.text3}; border-color: {t.neutral_soft}; }}
QPushButton#primary[kbfocus="true"]:focus {{ border-color: {t.text}; }}
QPushButton#danger {{ color: {t.danger}; }}
QPushButton#link {{ background: transparent; border: 1px solid transparent; color: {t.accent_text}; padding: 0 {S.XS}px; }}
QPushButton#link:hover {{ text-decoration: underline; }}
QPushButton#link[kbfocus="true"]:focus {{ border: 2px solid {t.accent}; padding: 0 {S.XS - 1}px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: {R.CONTROL}px; padding: {S.XS + 1}px; }}
QToolButton:hover {{ background: {t.control_hover}; }}
QToolButton:checked {{ background: {t.accent_soft}; }}
QToolButton[kbfocus="true"]:focus {{ border: 2px solid {t.accent}; padding: {S.XS}px; }}
QToolButton#sectionToggle {{ border: 2px solid transparent; padding: {S.XS}px 0; font-weight: 600; color: {t.text}; }}
QToolButton#sectionToggle:hover, QToolButton#sectionToggle:checked {{ background: transparent; }}
QToolButton#sectionToggle:hover {{ color: {t.accent_text}; }}
QToolButton#sectionToggle[kbfocus="true"]:focus {{ border: 2px solid {t.accent}; padding: {S.XS}px 0; }}

/* ---- inputs: same height as buttons; text inset by Space.S */
QLineEdit, QComboBox, QSpinBox {{
  background: {t.control}; color: {t.text}; border: 1px solid {t.control_border}; border-radius: {R.CONTROL}px;
  min-height: {h1}px; max-height: {h1}px; selection-background-color: {t.accent}; selection-color: {t.on_accent};
}}
QLineEdit {{ padding: 0 {S.S}px; }}
QLineEdit:focus {{ border: 2px solid {t.accent}; padding: 0 {S.S - 1}px; min-height: {h2}px; max-height: {h2}px; }}
QComboBox {{ padding: 0 {S.XL + S.XS}px 0 {S.S}px; combobox-popup: 0; }}
QComboBox:focus {{ border: 2px solid {t.accent}; padding: 0 {S.XL + S.XS - 1}px 0 {S.S - 1}px; min-height: {h2}px; max-height: {h2}px; }}
QComboBox:hover {{ background: {t.control_hover}; }}
QComboBox:on {{ border: 2px solid {t.accent}; padding: 0 {S.XL + S.XS - 1}px 0 {S.S - 1}px; min-height: {h2}px; max-height: {h2}px; }}
QComboBox::drop-down {{ subcontrol-origin: border; subcontrol-position: center right; border: none; width: {S.XL + S.XS}px; }}
QComboBox::down-arrow {{ image: url({a["down"]}); width: {S.M}px; height: {S.M}px; }}
QComboBox::down-arrow:disabled {{ image: url({a["down_disabled"]}); }}
QSpinBox {{ padding: 0 {S.XL}px 0 {S.S}px; }}
QSpinBox:focus {{ border: 2px solid {t.accent}; padding: 0 {S.XL - 1}px 0 {S.S - 1}px; min-height: {h2}px; max-height: {h2}px; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: {t.text3}; background: {t.card_alt}; border-color: {t.border}; }}
QSpinBox::up-button, QSpinBox::down-button {{ subcontrol-origin: border; width: {S.L + S.XS}px; border: none;
  background: transparent; }}
QSpinBox::up-button {{ subcontrol-position: top right; margin: {S.XS}px {S.XS}px 0 0; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; margin: 0 {S.XS}px {S.XS}px 0; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {t.neutral_soft}; border-radius: {R.SMALL - 2}px; }}
QSpinBox::up-arrow {{ image: url({a["up_small"]}); width: {S.M - 2}px; height: {S.M - 2}px; }}
QSpinBox::down-arrow {{ image: url({a["down_small"]}); width: {S.M - 2}px; height: {S.M - 2}px; }}

/* ---- dropdown lists (combobox-popup: 0 = a normal list below the field) */
QComboBox QAbstractItemView {{
  background: {t.card}; color: {t.text}; border: 1px solid {t.border}; border-radius: {R.CONTROL}px;
  padding: {S.XS}px; outline: none; selection-background-color: {t.accent}; selection-color: {t.on_accent};
}}
QComboBox QAbstractItemView::item {{ min-height: {h2}px; padding: 0 {S.S}px; border-radius: {R.SMALL}px; color: {t.text}; }}
QComboBox QAbstractItemView::item:hover {{ background: {t.accent_soft}; color: {t.text}; }}
QComboBox QAbstractItemView::item:selected {{ background: {t.accent}; color: {t.on_accent}; }}
QCheckBox {{ spacing: {S.S}px; }}
QCheckBox[kbfocus="true"]:focus {{ color: {t.accent_text}; }}
QCheckBox::indicator, QTableView::indicator, QTreeView::indicator, QListView::indicator {{ width: {ind}px; height: {ind}px; }}
QCheckBox::indicator:unchecked, QTableView::indicator:unchecked, QTreeView::indicator:unchecked {{ image: url({a["box"]}); }}
QCheckBox::indicator:unchecked:hover, QTableView::indicator:unchecked:hover, QTreeView::indicator:unchecked:hover {{ image: url({a["box_hover"]}); }}
QCheckBox::indicator:checked, QTableView::indicator:checked, QTreeView::indicator:checked {{ image: url({a["box_checked"]}); }}
QTreeView::indicator:indeterminate {{ image: url({a["box_mixed"]}); }}
QCheckBox::indicator:unchecked:disabled, QTableView::indicator:unchecked:disabled, QTreeView::indicator:unchecked:disabled {{ image: url({a["box_disabled"]}); }}
QCheckBox::indicator:checked:disabled, QTableView::indicator:checked:disabled, QTreeView::indicator:checked:disabled {{ image: url({a["box_checked_disabled"]}); }}

/* ---- table & tree */
QTableView, QTreeWidget, QListWidget {{
  background: {t.card}; border: none; border-radius: {R.CARD}px; alternate-background-color: {t.card_alt};
  selection-background-color: {t.accent_soft}; selection-color: {t.text}; gridline-color: transparent;
}}
QTableView::item, QTreeWidget::item {{ padding: 0 {S.S}px; border: none; }}
QTableView::item:selected, QTreeWidget::item:selected, QListWidget::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
/* No ring around a whole list: keyboard focus shows on the current row instead. */
QTableView:focus, QTreeWidget:focus, QListWidget:focus {{ border: none; outline: none; }}
QTableView::item:focus, QTreeWidget::item:focus {{ border: none; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
  background: {t.card}; color: {t.text2}; border: none; border-bottom: 1px solid {t.separator};
  padding: {S.S}px; font-weight: 600; font-size: {foot}pt;
}}
QTableCornerButton::section {{ background: {t.card}; border: none; }}

/* ---- progress */
QProgressBar {{ background: {t.neutral_soft}; border: none; border-radius: {S.XS - 1}px; max-height: {S.XS + 2}px;
  min-height: {S.XS + 2}px; color: transparent; }}
QProgressBar::chunk {{ background: {t.accent}; border-radius: {S.XS - 1}px; }}

/* ---- scrollbars: a rounded pill inset from the edges, kept clear of rounded card corners */
QScrollBar:vertical {{ background: transparent; border: none; width: {S.M}px; margin: {R.CARD}px {S.XXS}px {R.CARD}px 0; }}
QScrollBar::handle:vertical {{ background: {t.control_border}; border: none; border-radius: {S.XS - 1}px; min-height: {S.XXL}px; margin: 0 {S.XXS}px; }}
QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed {{ background: {t.text3}; }}
QScrollBar:horizontal {{ background: transparent; border: none; height: {S.M}px; margin: 0 {R.CARD}px {S.XXS}px {R.CARD}px; }}
QScrollBar::handle:horizontal {{ background: {t.control_border}; border: none; border-radius: {S.XS - 1}px; min-width: {S.XXL}px; margin: {S.XXS}px 0; }}
QScrollBar::handle:horizontal:hover, QScrollBar::handle:horizontal:pressed {{ background: {t.text3}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar#edgeScrollBar:vertical {{ margin: {R.CARD}px 0 {R.CARD}px 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---- segmented control (QTabBar#segmented) */
QTabBar#segmented {{ background: transparent; }}
QTabBar#segmented::tab {{ background: {t.control}; color: {t.text}; border: 1px solid {t.control_border};
  border-right: none; padding: 0 {S.L}px; min-height: {h1}px; max-height: {h1}px; }}
QTabBar#segmented::tab:first {{ border-top-left-radius: {R.CONTROL}px; border-bottom-left-radius: {R.CONTROL}px; }}
QTabBar#segmented::tab:last {{ border-right: 1px solid {t.control_border}; border-top-right-radius: {R.CONTROL}px;
  border-bottom-right-radius: {R.CONTROL}px; }}
QTabBar#segmented::tab:only-one {{ border-right: 1px solid {t.control_border}; border-radius: {R.CONTROL}px; }}
QTabBar#segmented::tab:hover {{ background: {t.control_hover}; }}
QTabBar#segmented::tab:selected {{ background: {t.accent}; color: {t.on_accent}; border-color: {t.accent}; font-weight: 600; }}
QTabBar#segmented:focus {{ outline: none; }}
QTabBar#segmented[kbfocus="true"]::tab:focus {{ text-decoration: underline; }}
QTabBar QToolButton {{ background: {t.control}; border: 1px solid {t.control_border}; border-radius: {R.SMALL}px; }}

/* ---- menus, dialogs, sliders */
QSplitter::handle {{ background: transparent; }}
QMenu {{ background: {t.card}; border: 1px solid {t.border}; border-radius: {R.CONTROL}px; padding: {S.XS + 2}px; }}
QMenu::item {{ min-height: {h2}px; padding: 0 {S.XL}px 0 {S.M}px; border-radius: {R.SMALL}px; }}
QMenu::item:disabled {{ color: {t.text3}; }}
QMenu::item:selected {{ background: {t.accent}; color: {t.on_accent}; }}
QMenu::separator {{ height: 1px; background: {t.separator}; margin: {S.XS}px {S.S}px; }}
QMessageBox {{ background: {t.window}; }}
QSlider::groove:horizontal {{ height: {S.XS}px; background: {t.neutral_soft}; border-radius: {S.XXS}px; }}
QSlider::sub-page:horizontal {{ background: {t.accent}; border-radius: {S.XXS}px; }}
QSlider::handle:horizontal {{ background: {t.card}; border: 1px solid {t.control_border}; width: {S.L + 2}px;
  height: {S.L + 2}px; margin: -{S.S}px 0; border-radius: {S.S + 1}px; }}
QSlider {{ background: transparent; border: none; min-height: {S.XL}px; }}
QSlider[kbfocus="true"]::handle:horizontal:focus {{ border: 2px solid {t.accent}; }}
"""


def safe_filter(fn):
    """Event filters can still receive events while Qt tears objects down at exit; ignore those quietly
    instead of printing 'already deleted' errors."""
    import functools

    @functools.wraps(fn)
    def wrapper(self, obj, ev):
        try:
            return fn(self, obj, ev)
        except RuntimeError:
            return False
    return wrapper


_POLISH_CLASSES = {"QComboBoxPrivateContainer", "QTipLabel"}


@lru_cache(maxsize=1)
def _POLISH_TYPES():  # noqa: N802
    from PySide6.QtWidgets import QAbstractSpinBox, QComboBox, QMenu

    return (QComboBox, QAbstractSpinBox, QMenu)


def _polish_widget(obj: QWidget) -> None:
    """App-wide fixes applied as widgets are styled (covers Qt's own dialogs too).

    * popups (dropdown lists, menus, tooltips) get rounded corners without a dark rectangle behind them;
    * dropdowns get a styled-item delegate so their rows follow the stylesheet;
    * number fields get the same text inset as other fields;
    * the parts screen readers land on inside a dropdown (its list) or a number field (its text box) carry
      the control's own name, so Narrator says "Quality" rather than nothing.
    """
    cls = obj.metaObject().className()
    if cls not in _POLISH_CLASSES and not isinstance(obj, _POLISH_TYPES()):
        return   # nearly every widget: nothing to do
    from PySide6.QtWidgets import QAbstractSpinBox, QComboBox, QMenu, QStyledItemDelegate

    if isinstance(obj, QComboBox):
        if type(obj.itemDelegate()).__name__ != "QStyledItemDelegate":
            obj.setItemDelegate(QStyledItemDelegate(obj))
        obj.view().installEventFilter(_inner_names())
    elif isinstance(obj, QAbstractSpinBox):
        le = obj.findChild(QLineEdit)
        if le is not None:
            le.installEventFilter(_inner_names())
        css = f"background: transparent; border: none; margin: 0; padding: 0 0 0 {Space.S - 2}px;"
        if le is not None and le.styleSheet() != css:
            # Qt ignores the spin box's own padding for its text field; inset the field itself.
            le.setStyleSheet(css)
    if cls in ("QComboBoxPrivateContainer", "QTipLabel") or isinstance(obj, QMenu):
        if os.name == "nt":
            # Windows: translucent popup windows can render black outside the rounded border. Paint them
            # opaque and let the system round the window itself (Windows 11; see _native_round_corners).
            if cls == "QComboBoxPrivateContainer":
                css = f"QComboBoxPrivateContainer {{ background: {tokens().card}; border: none; }}"
                if obj.styleSheet() != css:
                    obj.setStyleSheet(css)
            if not obj.property("_rounding"):
                obj.setProperty("_rounding", True)
                obj.installEventFilter(_popup_filter())
        else:
            obj.setAttribute(Qt.WA_TranslucentBackground, True)
            css = "QComboBoxPrivateContainer { background: transparent; border: none; }"
            if cls == "QComboBoxPrivateContainer" and obj.styleSheet() != css:
                obj.setStyleSheet(css)


class _PopupFilter(QObject):
    """Windows: round each popup's native window when it's shown (only popups get this filter)."""

    @safe_filter
    def eventFilter(self, obj, ev):  # noqa: N802
        if ev.type() == QEvent.Show:
            _native_round_corners(obj, small=obj.metaObject().className() == "QTipLabel")
        return False


class _InnerNames(QObject):
    """A dropdown's list and a number field's text box take their control's name when they're shown or
    focused (names are often set after the widget is styled, so this is done when it matters)."""

    @safe_filter
    def eventFilter(self, obj, ev):  # noqa: N802
        if ev.type() in (QEvent.Show, QEvent.FocusIn) and not obj.accessibleName():
            from PySide6.QtWidgets import QAbstractSpinBox, QComboBox

            owner = obj.parent()
            while owner is not None and not isinstance(owner, (QComboBox, QAbstractSpinBox)):
                owner = owner.parent()
            if owner is not None and owner.accessibleName():
                obj.setAccessibleName(owner.accessibleName())
        return False


_inner_names_obj: _InnerNames | None = None


def _inner_names() -> _InnerNames:
    global _inner_names_obj
    if _inner_names_obj is None:
        _inner_names_obj = _InnerNames()
    return _inner_names_obj


_popup_filter_obj: _PopupFilter | None = None


def _popup_filter() -> _PopupFilter:
    global _popup_filter_obj
    if _popup_filter_obj is None:
        _popup_filter_obj = _PopupFilter()
    return _popup_filter_obj


def _app_style():
    """Fusion with our widget fixes. Qt calls polish() once per widget, instead of a filter that would
    see every event in the app (mouse moves, paints...)."""
    from PySide6.QtWidgets import QProxyStyle, QStyleFactory

    class AppStyle(QProxyStyle):
        def polish(self, arg):  # noqa: D401 - Qt override (QWidget / QPalette / QApplication overloads)
            if isinstance(arg, QWidget):
                _polish_widget(arg)
            return super().polish(arg)

    return AppStyle(QStyleFactory.create("Fusion"))


def _native_round_corners(w: QWidget, small: bool = False) -> None:
    """Windows 11: round a popup window the way system menus are (no-op on older Windows)."""
    try:
        import ctypes

        hwnd = ctypes.c_void_p(int(w.winId()))
        dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined]
        pref = ctypes.c_int(3 if small else 2)          # DWMWCP_ROUNDSMALL (4 px) / DWMWCP_ROUND (8 px)
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))   # CORNER_PREFERENCE
        none = ctypes.c_uint(0xFFFFFFFE)                # DWMWA_COLOR_NONE: our own 1 px border stays
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(none), ctypes.sizeof(none))   # BORDER_COLOR
    except Exception:  # noqa: BLE001
        pass


_style_obj = None


def _rich(value: str) -> bool:
    from PySide6 import QtGui

    return bool(QtGui.Qt.mightBeRichText(value))


def keyboard_focus(w: QWidget) -> bool:
    """Whether ``w`` has the focus and got it from the keyboard (Tab, a shortcut, or a key pressed while focused):
    only then is the focus ring drawn, as in Windows. Clicks and windows opening don't show it."""
    return w.hasFocus() and w.property("kbfocus") is True


class _FocusVisible(QObject):
    """Marks the focused widget ``kbfocus`` when the focus came from the keyboard (what the stylesheet's focus
    rings and keyboard_focus() go by), and unmarks it when the focus leaves. Also gives dialogs' title bars the
    dialog's colors on Windows 11 (see _tint_title_bar)."""

    _KEYBOARD = (Qt.TabFocusReason, Qt.BacktabFocusReason, Qt.ShortcutFocusReason)
    _IGNORE = (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta)

    @staticmethod
    def _mark(w: QWidget, on: bool) -> None:
        if (w.property("kbfocus") is True) != on:
            w.setProperty("kbfocus", on)
            w.style().unpolish(w)
            w.style().polish(w)
            w.update()

    @safe_filter
    def eventFilter(self, obj, ev):  # noqa: N802
        kind = ev.type()
        if kind == QEvent.FocusIn and isinstance(obj, QWidget):
            self._mark(obj, ev.reason() in self._KEYBOARD)
        elif kind == QEvent.FocusOut and isinstance(obj, QWidget):
            self._mark(obj, False)
        elif kind == QEvent.Show and os.name == "nt" and isinstance(obj, QWidget) and obj.isWindow() \
                and obj.inherits("QDialog") and not obj.property("_titleTinted"):
            obj.setProperty("_titleTinted", True)
            _tint_title_bar(obj)
        elif kind == QEvent.KeyPress and isinstance(obj, QWidget) and obj.hasFocus() \
                and ev.key() not in self._IGNORE:
            self._mark(obj, True)          # (arrow keys or Space on something clicked: now it's the keyboard's)
        return False


_focus_visible: _FocusVisible | None = None


def _tint_title_bar(w: QWidget) -> None:
    """Windows 11: a dialog's title bar in the dialog's own colors. Windows would otherwise paint it in the
    accent color (when "Show accent color on title bars" is on), unlike the app's own window, which draws its
    own title bar."""
    try:
        import ctypes

        if sys.getwindowsversion().build < 22000:  # type: ignore[attr-defined]
            return
        def colorref(c: QColor) -> ctypes.c_uint:
            return ctypes.c_uint(c.red() | c.green() << 8 | c.blue() << 16)

        hwnd = ctypes.c_void_p(int(w.winId()))
        dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined]
        for attribute, color in ((35, w.palette().color(QPalette.Window)),      # DWMWA_CAPTION_COLOR
                                 (36, QColor(tokens().text))):                  # DWMWA_TEXT_COLOR
            value = colorref(color)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:  # noqa: BLE001 - the default title bar is fine too
        pass


class _TipCard(QWidget):
    """The app's tooltip: a small card that fades in under the thing it describes. (Qt's own tooltip opens at
    the pointer, over what you're looking at, and Windows' tooltip animation flashes it as an empty outline.)"""

    FADE_MS = 110
    GAP = 6

    def __init__(self):
        from PySide6.QtCore import QPropertyAnimation, QTimer
        from PySide6.QtWidgets import QLabel, QVBoxLayout

        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.label = QLabel(self)
        self.label.setTextFormat(Qt.AutoText)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Space.M, Space.S, Space.M, Space.S)
        lay.addWidget(self.label)
        self.anchor = None                       # QRect (global): the tip goes away when the pointer leaves it
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(self.FADE_MS)
        self._watch = QTimer(self, interval=120)
        self._watch.timeout.connect(self._check_pointer)

    def paintEvent(self, _e) -> None:  # noqa: N802
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(t.card))
        p.setPen(QColor(t.border))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), Radius.SMALL, Radius.SMALL)

    def show_for(self, tip: str, anchor, x: int | None = None) -> None:
        """Show ``tip`` under ``anchor`` (a global QRect), centered on it, or on ``x`` for wide things such as
        list rows; above it when there's no room below; always kept on the screen."""
        from PySide6.QtCore import QPoint

        t = tokens()
        self.label.setFont(ui_font(Type.FOOTNOTE))
        self.label.setStyleSheet(f"color: {t.text}; background: transparent;")
        max_w = int(Space.XXL * 11 * text_scale())
        self.label.setWordWrap(False)
        self.label.setText(tip)
        if self.label.sizeHint().width() > max_w:      # long: wrapped at a reading width
            self.label.setWordWrap(True)
            self.label.setFixedWidth(max_w)
        else:
            self.label.setMinimumWidth(0)
            self.label.setMaximumWidth(16777215)
        self.adjustSize()
        screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        cx = anchor.center().x() if x is None else x
        left = max(area.left() + 4, min(cx - self.width() // 2, area.right() - self.width() - 4))
        top = anchor.bottom() + self.GAP
        if top + self.height() > area.bottom():
            top = anchor.top() - self.GAP - self.height()
        self.anchor = anchor
        was_shown = self.isVisible()
        self.move(QPoint(left, top))
        if not was_shown:                      # (moving to another row: no second fade)
            self.setWindowOpacity(0.0)
            self.show()
            self._fade.stop()
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()
        self._watch.start()

    def dismiss(self) -> None:
        self._watch.stop()
        self._fade.stop()
        self.anchor = None
        self.hide()

    def _check_pointer(self) -> None:
        from PySide6.QtGui import QCursor

        if self.anchor is None or not self.anchor.adjusted(-2, -2, 2, 2).contains(QCursor.pos()):
            self.dismiss()


_tip_card: _TipCard | None = None


def show_tip(tip: str, widget: QWidget, rect=None, x: int | None = None) -> None:
    """Show the app's tooltip for ``widget`` (or for ``rect``, a part of it in its own coordinates)."""
    from PySide6.QtCore import QRect

    global _tip_card
    if not tip:
        hide_tip()
        return
    if _tip_card is None:
        _tip_card = _TipCard()
    r = rect if rect is not None else widget.rect()
    _tip_card.show_for(tip, QRect(widget.mapToGlobal(r.topLeft()), r.size()), x)


def hide_tip() -> None:
    if _tip_card is not None and _tip_card.isVisible():
        _tip_card.dismiss()


class _Tooltips(QObject):
    """Shows every tooltip as the app's card (see _TipCard): under the control, or under the row for lists."""

    WIDE = 420      # wider than this (rows, long fields): centered on the pointer instead of the whole thing

    def eventFilter(self, obj, ev):  # noqa: N802
        kind = ev.type()
        if kind == QEvent.ToolTip and isinstance(obj, QWidget):
            from PySide6.QtWidgets import QAbstractItemView

            view = obj.parentWidget()
            if isinstance(view, QAbstractItemView) and view.viewport() is obj:
                index = view.indexAt(ev.pos())
                tip = index.data(Qt.ToolTipRole) if index.isValid() else ""
                if tip:
                    show_tip(str(tip), obj, view.visualRect(index), ev.globalPos().x())
                else:
                    hide_tip()
                return True
            tip = obj.toolTip()
            if not tip:
                hide_tip()
                return False
            show_tip(tip, obj, x=ev.globalPos().x() if obj.width() > self.WIDE else None)
            return True
        if kind in (QEvent.MouseButtonPress, QEvent.KeyPress, QEvent.Wheel, QEvent.WindowDeactivate):
            hide_tip()
        return False


_tooltip_wrap: _Tooltips | None = None
_system_palette: QPalette | None = None


def apply_style(mode: str) -> Tokens:
    """Apply light/dark/system styling app-wide. Returns the active tokens."""
    global _current
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    scale_to_text()
    global _tooltip_wrap
    if _tooltip_wrap is None:
        _tooltip_wrap = _Tooltips(app)
        app.installEventFilter(_tooltip_wrap)
    global _focus_visible
    if _focus_visible is None:
        _focus_visible = _FocusVisible(app)
        app.installEventFilter(_focus_visible)
    hints = app.styleHints()
    if hasattr(hints, "setColorScheme"):
        scheme = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(mode, Qt.ColorScheme.Unknown)
        hints.setColorScheme(scheme)
    if mode == "system":
        system_dark = hasattr(hints, "colorScheme") and hints.colorScheme() == Qt.ColorScheme.Dark
        dark = system_dark
    else:
        dark = mode == "dark"
    _current = DARK if dark else LIGHT
    accent = windows_accent() if _system_accent else None
    if accent:
        _current = with_accent(_current, accent)
    global _system_palette
    if _system_palette is None:
        _system_palette = QPalette(app.palette())          # (Windows' own colors, before ours are applied)
    if high_contrast_active():
        # The user's contrast theme wins: Windows draws the controls in its colors, and what the app draws
        # itself (icons, switches, charts) takes the same colors.
        app.setStyleSheet("")
        app.setPalette(_system_palette)
        _current = high_contrast_tokens(_system_palette)
        return _current
    global _style_obj
    if _style_obj is None:
        _style_obj = _app_style()
        app.setStyle(_style_obj)  # Fusion everywhere, for consistent rendering; the stylesheet does the rest
    app.setPalette(palette(_current))
    app.setFont(ui_font())
    app.setStyleSheet(stylesheet(_current))
    return _current


def is_mac() -> bool:
    return sys.platform == "darwin"

