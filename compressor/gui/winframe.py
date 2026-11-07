"""The window's own title bar (Windows).

The sidebar and the page run to the top edge of the window, with the minimize, maximize and close buttons
drawn in the top-right corner, instead of a separate system caption strip that never quite matches the
app. Everything a real title bar does stays native, because Windows still sees a normal captioned window:
dragging (and Aero Snap), the Snap Layouts flyout on the maximize button (Windows 11), double-click to
maximize, the window menu (right-click the top strip or Alt+Space), resizing from the edges, the shadow,
rounded corners and the minimize/maximize animations.

How: the window is frameless to Qt (so Qt's geometry is the whole window), the caption and resize styles
are given back to it for Windows, ``WM_NCCALCSIZE`` makes the whole window client area, and
``WM_NCHITTEST`` tells Windows which parts are the caption, the edges and the maximize button.
Elsewhere (Linux) the desktop's own title bar is used.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QWidget

from .style import Size, tokens

CAPTION_W = 46          # Windows' caption button width
RESIZE_EDGE = 5         # logical px inside the window that resize it (the window has no outer frame)
RESIZE_CORNER = 14      # how far along an edge a corner grab reaches

_enabled: bool | None = None


def custom_frame() -> bool:
    """Does the main window draw its own title bar? (Windows; COMPRESSOR_CUSTOM_FRAME=1 forces the layout on
    other platforms, for screenshots; COMPRESSOR_NATIVE_FRAME=1 turns it off.)"""
    global _enabled
    if _enabled is None:
        if os.environ.get("COMPRESSOR_NATIVE_FRAME") == "1":
            _enabled = False
        elif os.environ.get("COMPRESSOR_CUSTOM_FRAME") == "1":
            _enabled = True
        else:
            _enabled = sys.platform == "win32" and QGuiApplication.platformName() == "windows"
    return _enabled


# ---------------------------------------------------------------- caption buttons

class CaptionButton(QAbstractButton):
    """Minimize, maximize/restore or close, drawn like Windows 11's (vector glyphs, crisp at any scale)."""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.nc_hover = False      # hovered as HTMAXBUTTON: Windows, not Qt, has the pointer then
        self.nc_down = False
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(CAPTION_W, Size.TITLE_BAR)
        self.setAttribute(Qt.WA_Hover)
        names = {"min": "Minimize", "max": "Maximize", "close": "Close"}
        self.setAccessibleName(names[kind])
        self.setToolTip(names[kind])

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(CAPTION_W, Size.TITLE_BAR)

    def set_nc(self, hover: bool | None = None, down: bool | None = None) -> None:
        changed = False
        if hover is not None and hover != self.nc_hover:
            self.nc_hover, changed = hover, True
        if down is not None and down != self.nc_down:
            self.nc_down, changed = down, True
        if changed:
            self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        t = tokens()
        win = self.window()
        hover = self.underMouse() or self.nc_hover
        down = (self.isDown() or self.nc_down) and hover
        p = QPainter(self)
        if self.kind == "close" and hover:
            p.fillRect(self.rect(), QColor("#C42B1C") if not down else QColor("#C83C31"))
            fg = QColor("#FFFFFF")
        else:
            if hover:
                bg = QColor(t.text)
                bg.setAlphaF(0.06 if down else 0.09)
                p.fillRect(self.rect(), bg)
            fg = QColor(t.text if win.isActiveWindow() else t.text3)
            if down:
                fg.setAlphaF(0.75)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(fg, 1.0)
        pen.setCosmetic(False)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        # A 10 px glyph centred on whole pixels, like Segoe Fluent Icons at 100 %.
        cx = round(self.width() / 2)
        cy = round(self.height() / 2)
        g = 10.0
        x0, y0 = cx - g / 2, cy - g / 2
        if self.kind == "min":
            p.drawLine(QPointF(x0, cy + 0.5), QPointF(x0 + g, cy + 0.5))
        elif self.kind == "close":
            dpr = self.devicePixelRatioF()
            if dpr == int(dpr):
                # Whole-number scaling: one solid pixel per row, as Windows draws it. Antialiasing a 1 px
                # diagonal spreads it over two pixels at partial strength (faint, and pink on red).
                p.setRenderHint(QPainter.Antialiasing, False)
                a, b = int(x0), int(x0 + g - 1)
                c, d = int(y0), int(y0 + g - 1)
                p.drawLine(a, c, b, d)
                p.drawLine(b, c, a, d)
            else:
                p.drawLine(QPointF(x0 + 0.5, y0 + 0.5), QPointF(x0 + g - 0.5, y0 + g - 0.5))
                p.drawLine(QPointF(x0 + g - 0.5, y0 + 0.5), QPointF(x0 + 0.5, y0 + g - 0.5))
        elif win.isMaximized():   # restore: a square with another behind it
            s = 8.0
            p.drawRoundedRect(QRectF(x0 + 0.5, y0 + 2.5, s, s), 1.2, 1.2)
            back = QPainterPath()
            back.moveTo(x0 + 2.5, y0 + 2.5)
            back.lineTo(x0 + 2.5, y0 + 1.7)
            back.quadTo(x0 + 2.5, y0 + 0.5, x0 + 3.7, y0 + 0.5)
            back.lineTo(x0 + 8.8, y0 + 0.5)
            back.quadTo(x0 + 10.5, y0 + 0.5, x0 + 10.5, y0 + 2.2)
            back.lineTo(x0 + 10.5, y0 + 7.3)
            back.quadTo(x0 + 10.5, y0 + 8.5, x0 + 9.3, y0 + 8.5)
            back.lineTo(x0 + 8.5, y0 + 8.5)
            p.drawPath(back)
        else:
            p.drawRoundedRect(QRectF(x0 + 0.5, y0 + 0.5, g - 1, g - 1), 1.2, 1.2)


class CaptionButtons(QWidget):
    """The three buttons, placed in the window's top-right corner by ``WindowFrame``."""

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.min = CaptionButton("min", self)
        self.max = CaptionButton("max", self)
        self.close = CaptionButton("close", self)
        for b in (self.min, self.max, self.close):
            lay.addWidget(b)
        self.min.clicked.connect(lambda: _syscommand(window, _SC_MINIMIZE, window.showMinimized))
        self.max.clicked.connect(lambda: toggle_maximized(window))
        self.close.clicked.connect(window.close)
        self.adjustSize()

    def refresh(self) -> None:
        self.max.setAccessibleName("Restore" if self.win.isMaximized() else "Maximize")
        self.max.setToolTip(self.max.accessibleName())
        for b in (self.min, self.max, self.close):
            b.update()


def toggle_maximized(window) -> None:
    if window.isMaximized():
        _syscommand(window, _SC_RESTORE, window.showNormal)
    else:
        _syscommand(window, _SC_MAXIMIZE, window.showMaximized)


# ---------------------------------------------------------------- Windows plumbing

_WM_NCCALCSIZE = 0x0083
_WM_NCHITTEST = 0x0084
_WM_NCMOUSEMOVE = 0x00A0
_WM_NCLBUTTONDOWN = 0x00A1
_WM_NCLBUTTONUP = 0x00A2
_WM_NCLBUTTONDBLCLK = 0x00A3
_WM_NCMOUSELEAVE = 0x02A2
_WM_SYSCOMMAND = 0x0112
_SC_MINIMIZE, _SC_MAXIMIZE, _SC_RESTORE = 0xF020, 0xF030, 0xF120
HTCLIENT, HTCAPTION, HTMAXBUTTON = 1, 2, 9
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 10, 11, 12, 13, 14, 15, 16, 17
_MESSAGES = frozenset((_WM_NCCALCSIZE, _WM_NCHITTEST, _WM_NCMOUSEMOVE, _WM_NCLBUTTONDOWN, _WM_NCLBUTTONUP,
                       _WM_NCLBUTTONDBLCLK, _WM_NCMOUSELEAVE))


def _syscommand(window, command: int, fallback) -> None:
    """Minimize/maximize/restore the way the system caption does (with its animation)."""
    if sys.platform == "win32" and QGuiApplication.platformName() == "windows":
        import ctypes

        ctypes.windll.user32.PostMessageW(ctypes.c_void_p(int(window.winId())), _WM_SYSCOMMAND, command, 0)
    else:
        fallback()


class WindowFrame:
    """Gives ``window`` its own title bar. Call ``native_event`` from the window's ``nativeEvent``, and
    ``place`` from its ``resizeEvent``; ``set_dark`` when the theme changes."""

    def __init__(self, window):
        self.win = window
        self.native = sys.platform == "win32" and QGuiApplication.platformName() == "windows"
        window.setWindowFlag(Qt.FramelessWindowHint, True)
        self.buttons = CaptionButtons(window)
        self._leave = QTimer(window, interval=120)
        self._leave.timeout.connect(self._check_leave)
        if self.native:
            import ctypes
            from ctypes import wintypes

            self._ct, self._wt = ctypes, wintypes
            self._user32 = ctypes.windll.user32
            self._user32.DefWindowProcW.restype = ctypes.c_ssize_t
            self._user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                                    wintypes.LPARAM)

    # ---- set-up, once the native window exists
    def attach(self) -> None:
        self.place()
        if not self.native:
            return
        ct, user32 = self._ct, self._user32
        hwnd = ct.c_void_p(int(self.win.winId()))
        gwl_style = -16
        ws = 0x00C00000 | 0x00040000 | 0x00020000 | 0x00010000 | 0x00080000   # CAPTION THICKFRAME MIN MAX SYSMENU
        get = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        put = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        get.restype = ct.c_ssize_t
        put.argtypes = (ct.c_void_p, ct.c_int, ct.c_ssize_t)
        put(hwnd, gwl_style, get(hwnd, gwl_style) | ws)

        class MARGINS(ct.Structure):  # type: ignore[name-defined]  # (ctypes, loaded on Windows only)
            _fields_ = [("l", ct.c_int), ("r", ct.c_int), ("t", ct.c_int), ("b", ct.c_int)]

        try:
            dwm = ct.windll.dwmapi
            m = MARGINS(-1, -1, -1, -1)                  # the DWM shadow (the window paints opaque on top)
            dwm.DwmExtendFrameIntoClientArea(hwnd, ct.byref(m))
            pref = ct.c_int(2)                           # DWMWCP_ROUND: Windows 11 corners
            dwm.DwmSetWindowAttribute(hwnd, 33, ct.byref(pref), ct.sizeof(pref))
        except Exception:  # noqa: BLE001 - older Windows: no shadow or rounding, still works
            pass
        self.set_dark(tokens().dark)
        # Recalculate the frame now that it has a caption (SWP_NOMOVE|NOSIZE|NOZORDER|NOACTIVATE|FRAMECHANGED).
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
        handle = self.win.windowHandle()
        if handle is not None:
            handle.screenChanged.connect(
                lambda *_: user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020))

    def set_dark(self, dark: bool) -> None:
        """Dark border and shadow for the dark theme (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
        self.buttons.refresh()
        if not self.native:
            return
        ct = self._ct
        try:
            on = ct.c_int(1 if dark else 0)
            ct.windll.dwmapi.DwmSetWindowAttribute(ct.c_void_p(int(self.win.winId())), 20, ct.byref(on),
                                                   ct.sizeof(on))
        except Exception:  # noqa: BLE001
            pass

    def place(self) -> None:
        b = self.buttons
        b.adjustSize()
        b.move(self.win.width() - b.width(), 0)
        b.raise_()

    def changed(self, event) -> None:
        """From the window's changeEvent: restore/maximize glyph, inactive colours."""
        if event.type() in (QEvent.WindowStateChange, QEvent.ActivationChange):
            self.buttons.refresh()

    # ---- Windows messages
    def native_event(self, message) -> tuple[bool, int]:
        if not self.native:
            return False, 0
        msg = self._wt.MSG.from_address(int(message))
        m = msg.message
        if m not in _MESSAGES:
            return False, 0
        if m == _WM_NCCALCSIZE:
            return self._calc_size(msg)
        if m == _WM_NCHITTEST:
            ht = self.hit_test(_signed(msg.lParam & 0xFFFF), _signed((msg.lParam >> 16) & 0xFFFF))
            self.buttons.max.set_nc(hover=ht == HTMAXBUTTON)
            if ht == HTMAXBUTTON:
                self._leave.start()
            return True, ht
        is_max = msg.wParam == HTMAXBUTTON
        if m == _WM_NCMOUSEMOVE:
            self.buttons.max.set_nc(hover=is_max)
            return False, 0
        if m == _WM_NCMOUSELEAVE:
            self.buttons.max.set_nc(hover=False, down=False)
            return False, 0
        if m in (_WM_NCLBUTTONDOWN, _WM_NCLBUTTONDBLCLK) and is_max:
            self.buttons.max.set_nc(down=True)
            return True, 0
        if m == _WM_NCLBUTTONUP and is_max:
            was_down = self.buttons.max.nc_down
            self.buttons.max.set_nc(down=False)
            if was_down:
                toggle_maximized(self.win)
            return True, 0
        if m == _WM_NCLBUTTONUP:
            self.buttons.max.set_nc(down=False)
        return False, 0

    def _check_leave(self) -> None:
        b = self.buttons.max
        if not b.rect().contains(b.mapFromGlobal(QCursor.pos())):
            b.set_nc(hover=False, down=False)
            self._leave.stop()

    def _calc_size(self, msg) -> tuple[bool, int]:
        """The whole window is client area; when maximized, keep it inside the monitor's work area (Windows
        maximizes a captioned window a frame's width past the screen edges)."""
        ct, wt, user32 = self._ct, self._wt, self._user32
        rect = wt.RECT.from_address(msg.lParam)   # NCCALCSIZE_PARAMS starts with the proposed window rect
        hwnd = ct.c_void_p(msg.hWnd)
        if user32.IsZoomed(hwnd):

            class MONITORINFO(ct.Structure):  # type: ignore[name-defined]
                _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                            ("dwFlags", wt.DWORD)]

            user32.MonitorFromWindow.restype = ct.c_void_p
            mon = user32.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
            mi = MONITORINFO()
            mi.cbSize = ct.sizeof(MONITORINFO)
            if mon and user32.GetMonitorInfoW(ct.c_void_p(mon), ct.byref(mi)):
                work = mi.rcWork
                rect.left = max(rect.left, work.left)
                rect.top = max(rect.top, work.top)
                rect.right = min(rect.right, work.right)
                rect.bottom = min(rect.bottom, work.bottom)
                edge = _autohide_taskbar_edge(mi.rcMonitor)
                if edge is not None:      # leave a sliver so an auto-hidden taskbar can still be revealed
                    if edge == 0:
                        rect.left += 2
                    elif edge == 1:
                        rect.top += 2
                    elif edge == 2:
                        rect.right -= 2
                    else:
                        rect.bottom -= 2
        return True, 0

    def hit_test(self, sx: int, sy: int) -> int:
        """What's at screen point (sx, sy), in physical pixels."""
        ct, wt = self._ct, self._wt
        pt = wt.POINT(sx, sy)
        self._user32.ScreenToClient(ct.c_void_p(int(self.win.winId())), ct.byref(pt))
        dpr = self.win.devicePixelRatioF() or 1.0
        x, y = pt.x / dpr, pt.y / dpr
        return self.area_at(x, y)

    def area_at(self, x: float, y: float) -> int:
        """What's at window point (x, y), in logical pixels (the platform-independent part)."""
        w, h = self.win.width(), self.win.height()
        if not self.win.isMaximized() and not self.win.isFullScreen():
            e, c = RESIZE_EDGE, RESIZE_CORNER
            left, right, top, bottom = x < e, x >= w - e, y < e, y >= h - e
            near_l, near_r, near_t, near_b = x < c, x >= w - c, y < c, y >= h - c
            if (top and near_l) or (left and near_t):
                return HTTOPLEFT
            if (top and near_r) or (right and near_t):
                return HTTOPRIGHT
            if (bottom and near_l) or (left and near_b):
                return HTBOTTOMLEFT
            if (bottom and near_r) or (right and near_b):
                return HTBOTTOMRIGHT
            if left:
                return HTLEFT
            if right:
                return HTRIGHT
            if top:
                return HTTOP
            if bottom:
                return HTBOTTOM
        bx = x - self.buttons.x()
        if 0 <= y < self.buttons.height() and 0 <= bx < self.buttons.width():
            return HTMAXBUTTON if self.buttons.max.geometry().contains(int(bx), int(y)) else HTCLIENT
        if y < Size.TITLE_BAR:
            target = self.win.childAt(int(x), int(y))
            if target is None or not _interactive(target, self.win):
                return HTCAPTION
        return HTCLIENT


def _interactive(w: QWidget | None, top: QWidget) -> bool:
    """Is ``w`` (or a parent below ``top``) something you click, rather than background or a label?"""
    from PySide6.QtWidgets import QAbstractItemView, QAbstractSlider, QAbstractSpinBox, QComboBox, QLineEdit

    while w is not None and w is not top:
        if isinstance(w, (QAbstractButton, QAbstractItemView, QAbstractSlider, QAbstractSpinBox, QComboBox,
                          QLineEdit)):
            return True
        w = w.parentWidget()
    return False


def _signed(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def _autohide_taskbar_edge(monitor_rect) -> int | None:
    """0 left, 1 top, 2 right, 3 bottom: where an auto-hide taskbar sits on this monitor, else None."""
    import ctypes
    from ctypes import wintypes

    class APPBARDATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uCallbackMessage", wintypes.UINT),
                    ("uEdge", wintypes.UINT), ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]

    try:
        shell32 = ctypes.windll.shell32
        shell32.SHAppBarMessage.restype = ctypes.c_size_t
        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(APPBARDATA)
        if not shell32.SHAppBarMessage(0x4, ctypes.byref(abd)) & 0x1:   # ABM_GETSTATE & ABS_AUTOHIDE
            return None
        for edge in range(4):
            abd.uEdge = edge
            abd.rc = monitor_rect
            if shell32.SHAppBarMessage(0xB, ctypes.byref(abd)):          # ABM_GETAUTOHIDEBAREX
                return edge
    except Exception:  # noqa: BLE001
        return None
    return None


__all__ = ["custom_frame", "WindowFrame", "CaptionButtons", "toggle_maximized"]
