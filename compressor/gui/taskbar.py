"""The taskbar button while a job runs (Windows): progress in the icon itself (green, yellow when paused, red
when something failed) and Pause / Stop buttons under the window's thumbnail when you hover the icon, the way
media players show Play / Next.

Every job page reports through its JobStatus panel; this keeps one taskbar state for the window. Both parts
can be turned off in Settings (taskbar/progress, taskbar/buttons). Plain ctypes COM (ITaskbarList3), nothing
to install; off Windows everything here does nothing.
"""

from __future__ import annotations

import ctypes
import os

from ..log import log

IS_WINDOWS = os.name == "nt"

# ITaskbarList3
CLSID_TaskbarList = "56fdf344-fd6d-11d0-958a-006097c9a090"
IID_ITaskbarList3 = "ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf"
_HR_INIT, _SET_VALUE, _SET_STATE, _THUMB_ADD, _THUMB_UPDATE = 3, 9, 10, 15, 16
STATES = {"none": 0, "indeterminate": 1, "normal": 2, "error": 4, "paused": 8}

# Thumbnail toolbar
_WM_COMMAND, _THBN_CLICKED = 0x0111, 0x1800
_THB_ICON, _THB_TOOLTIP, _THB_FLAGS = 0x2, 0x4, 0x8
_THBF_ENABLED, _THBF_DISABLED, _THBF_HIDDEN = 0x0, 0x1, 0x8
PAUSE_ID, STOP_ID = 1, 2


class _ThumbButton(ctypes.Structure):
    _fields_ = [("dwMask", ctypes.c_uint32), ("iId", ctypes.c_uint), ("iBitmap", ctypes.c_uint),
                ("hIcon", ctypes.c_void_p), ("szTip", ctypes.c_wchar * 260), ("dwFlags", ctypes.c_uint32)]


def _hicon(name: str, size: int, light_taskbar: bool):
    """A Windows icon handle from one of the app's line icons (white on a dark taskbar, dark on a light one)."""
    from ctypes import wintypes

    from PySide6.QtGui import QImage

    from .style import icon_pixmap

    img = icon_pixmap(name, "#202020" if light_taskbar else "#FFFFFF", size, 1.0, 2.0).toImage() \
        .convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    bits = bytes(img.constBits())[: w * h * 4]           # BGRA, top-down

    class BitmapInfoHeader(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class IconInfo(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                    ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]

    gdi, user = ctypes.windll.gdi32, ctypes.windll.user32  # type: ignore[attr-defined]
    gdi.CreateDIBSection.restype = wintypes.HBITMAP
    gdi.CreateBitmap.restype = wintypes.HBITMAP
    user.CreateIconIndirect.restype = wintypes.HICON
    header = BitmapInfoHeader(ctypes.sizeof(BitmapInfoHeader), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = ctypes.c_void_p()
    color = gdi.CreateDIBSection(None, ctypes.byref(header), 0, ctypes.byref(pixels), None, 0)
    if not color:
        return None
    ctypes.memmove(pixels, bits, len(bits))
    mask = gdi.CreateBitmap(w, h, 1, 1, None)
    info = IconInfo(True, 0, 0, mask, color)
    handle = user.CreateIconIndirect(ctypes.byref(info))
    gdi.DeleteObject(color)
    gdi.DeleteObject(mask)
    return handle


def _light_taskbar() -> bool:
    """Whether Windows' taskbar (the "system" mode) is light."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
    except OSError:
        return False


class Taskbar:
    """One window's taskbar button. ``report`` from job panels; ``native_event`` from the window."""

    def __init__(self, window, qsettings):
        self.window, self.qs = window, qsettings
        self._list = None
        self._failed = False
        self.error = ""                      # why the taskbar couldn't be used (for the log and tests)
        self._buttons_added = False
        self._jobs: dict[int, tuple[object, str, float]] = {}   # id(panel) -> (panel, state, fraction)
        self._created_msg = 0
        if IS_WINDOWS:
            try:
                self._created_msg = ctypes.windll.user32.RegisterWindowMessageW("TaskbarButtonCreated")  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------- settings
    @property
    def show_progress(self) -> bool:
        return bool(self.qs.value("taskbar/progress", True, bool))

    @property
    def show_buttons(self) -> bool:
        return bool(self.qs.value("taskbar/buttons", True, bool))

    # ------------------------------------------------------------- COM
    def _taskbar(self):
        if not IS_WINDOWS or self._failed:
            return None
        if self._list is None:
            try:
                from .jumplist import _create

                tb = _create(CLSID_TaskbarList, IID_ITaskbarList3)
                tb.call(_HR_INIT, ())
                self._list = tb
            except OSError as e:
                log.warning("Taskbar progress unavailable: %s", e)
                self.error = str(e)
                self._failed = True
                return None
        return self._list

    def _hwnd(self) -> int:
        return int(self.window.winId())

    # ------------------------------------------------------------- progress
    def report(self, panel, state: str, fraction: float = 0.0) -> None:
        """A job panel's state: "normal" (with ``fraction`` 0..1), "indeterminate", "paused", "error" or "none"
        (finished: forget it)."""
        if state == "none":
            self._jobs.pop(id(panel), None)
        else:
            self._jobs[id(panel)] = (panel, state, max(0.0, min(1.0, fraction)))
        self._apply()

    def clear_error(self) -> None:
        """The window was looked at: a finished job's red bar has done its job."""
        if any(state == "error" for _p, state, _f in self._jobs.values()):
            self._jobs = {k: v for k, v in self._jobs.items() if v[1] != "error"}
            self._apply()

    def active_panel(self):
        """The panel of the job the taskbar shows (the one the buttons act on)."""
        running = [p for p, state, _f in self._jobs.values() if state in ("normal", "indeterminate", "paused")]
        return running[-1] if running else None

    def _apply(self) -> None:
        tb = self._taskbar()
        if tb is None:
            return
        hwnd = ctypes.c_void_p(self._hwnd())
        try:
            if not self._jobs or not self.show_progress:
                tb.call(_SET_STATE, (ctypes.c_void_p, ctypes.c_int), hwnd, STATES["none"])
            else:
                # Several jobs at once: the most important state wins (error > paused > running).
                states = [s for _p, s, _f in self._jobs.values()]
                state = next(s for s in ("error", "paused", "normal", "indeterminate") if s in states)
                fractions = [f for _p, s, f in self._jobs.values() if s == state]
                frac = 1.0 if state == "error" else sum(fractions) / len(fractions)
                if state != "indeterminate":
                    tb.call(_SET_VALUE, (ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64), hwnd,
                            int(frac * 1000), 1000)
                tb.call(_SET_STATE, (ctypes.c_void_p, ctypes.c_int), hwnd, STATES[state])
            self._update_buttons()
        except Exception as e:  # noqa: BLE001 - the taskbar is a nicety: never let it break a job
            log.warning("Taskbar progress turned off for this session: %s", e)
            self.error = str(e)
            self._failed = True

    # ------------------------------------------------------------- thumbnail buttons
    def _button_array(self):
        panel = self.active_panel()
        visible = panel is not None and self.show_buttons
        paused = visible and getattr(panel, "_paused_reason", "")
        light = _light_taskbar()
        size = ctypes.windll.user32.GetSystemMetrics(49) or 16  # type: ignore[attr-defined]  # SM_CXSMICON
        arr = (_ThumbButton * 2)()
        for i, (bid, name, tip) in enumerate((
                (PAUSE_ID, "play" if paused else "pause", "Resume" if paused else "Pause after the current file"),
                (STOP_ID, "stop", "Stop after the current file"))):
            b = arr[i]
            b.dwMask = _THB_ICON | _THB_TOOLTIP | _THB_FLAGS
            b.iId = bid
            b.hIcon = _hicon(name, size, light)
            b.szTip = tip
            stop_ok = panel is not None and panel.stop_btn.isEnabled()
            b.dwFlags = _THBF_HIDDEN if not visible else (_THBF_ENABLED if bid == PAUSE_ID or stop_ok
                                                          else _THBF_DISABLED)
        return arr

    def _update_buttons(self) -> None:
        tb = self._taskbar()
        if tb is None:
            return
        if not self._buttons_added and self.active_panel() is None:
            return                  # nothing to show yet: add them with the first job
        arr = self._button_array()
        hwnd = ctypes.c_void_p(self._hwnd())
        index = _THUMB_UPDATE if self._buttons_added else _THUMB_ADD
        try:
            tb.call(index, (ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p), hwnd, 2, ctypes.byref(arr))
            self._buttons_added = True
        finally:
            for b in arr:
                if b.hIcon:
                    ctypes.windll.user32.DestroyIcon(ctypes.c_void_p(b.hIcon))  # type: ignore[attr-defined]

    # ------------------------------------------------------------- window messages
    def native_event(self, msg) -> bool:
        """A Windows message for the window (``msg``: a wintypes.MSG). True when it was a thumbnail button."""
        if msg.message == self._created_msg and self._created_msg:
            self._buttons_added = False          # Explorer (re)started: the buttons have to be added again
            self._apply()
            return False
        if msg.message == _WM_COMMAND and (msg.wParam >> 16) & 0xFFFF == _THBN_CLICKED:
            self.clicked(msg.wParam & 0xFFFF)
            return True
        return False

    def clicked(self, button_id: int) -> None:
        panel = self.active_panel()
        if panel is None:
            return
        if button_id == PAUSE_ID:
            panel.pause_btn.click()
        elif button_id == STOP_ID and panel.stop_btn.isVisible() and panel.stop_btn.isEnabled():
            panel.stop_btn.click()


__all__ = ["Taskbar", "STATES", "PAUSE_ID", "STOP_ID"]
