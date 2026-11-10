"""Thumbnails from Windows itself (IShellItemImageFactory): the same pictures Explorer shows, straight from its
thumbnail cache when Explorer has seen the file before, which is instant even for videos, HEIC or camera RAW
(with their codecs installed). Asked first for hover previews; our own decoders (thumbs.py) are the fallback,
and Windows may then still make one for a type we can't read.

Plain ctypes COM; returns None off Windows or whenever Windows can't help.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IID_IShellItemImageFactory = "bcc18b79-ba16-442f-80c4-8a59c30c463b"
SIIGBF_BIGGERSIZEOK, SIIGBF_THUMBNAILONLY, SIIGBF_INCACHEONLY = 0x01, 0x08, 0x10


class _Size(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


def thumbnail(path: Path, max_px: int, cache_only: bool = True):
    """A QImage of ``path``'s Windows thumbnail (at most ``max_px`` on its longer side), or None. ``cache_only``:
    only if Windows already has it cached (never slow)."""
    if not IS_WINDOWS:
        return None
    from ctypes import wintypes

    from .jumplist import _GUID, _Com

    ole32, shell32, gdi32 = ctypes.windll.ole32, ctypes.windll.shell32, ctypes.windll.gdi32  # type: ignore[attr-defined]
    hr_init = ole32.CoInitializeEx(None, 2)          # (a worker thread: its own apartment)
    factory = None
    hbitmap = wintypes.HBITMAP()
    try:
        out = ctypes.c_void_p()
        hr = shell32.SHCreateItemFromParsingName(ctypes.c_wchar_p(str(path)), None,
                                                 ctypes.byref(_GUID.of(IID_IShellItemImageFactory)),
                                                 ctypes.byref(out))
        if hr < 0 or not out.value:
            return None
        factory = _Com(out)
        flags = SIIGBF_THUMBNAILONLY | SIIGBF_BIGGERSIZEOK | (SIIGBF_INCACHEONLY if cache_only else 0)
        try:
            factory.call(3, (_Size, ctypes.c_int, ctypes.POINTER(wintypes.HBITMAP)),
                         _Size(max_px, max_px), flags, ctypes.byref(hbitmap))          # GetImage
        except OSError:
            return None                               # not cached / no thumbnail provider for this type
        return _to_qimage(hbitmap, max_px, gdi32)
    except Exception:  # noqa: BLE001 - a preview is a nicety
        return None
    finally:
        if hbitmap:
            gdi32.DeleteObject(hbitmap)
        if factory is not None:
            factory.release()
        if hr_init in (0, 1):
            ole32.CoUninitialize()


def _to_qimage(hbitmap, max_px: int, gdi32):
    """The bitmap's pixels as a QImage (32-bit, top-down), scaled to fit ``max_px``."""
    from ctypes import wintypes

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    class Bitmap(ctypes.Structure):
        _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                    ("bmWidthBytes", ctypes.c_long), ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    class BitmapInfoHeader(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    bm = Bitmap()
    if not gdi32.GetObjectW(hbitmap, ctypes.sizeof(bm), ctypes.byref(bm)) or bm.bmWidth <= 0 or bm.bmHeight <= 0:
        return None
    w, h = bm.bmWidth, abs(bm.bmHeight)
    header = BitmapInfoHeader(ctypes.sizeof(BitmapInfoHeader), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    get_dc = user32.GetDC
    get_dc.restype = wintypes.HDC                      # (64-bit handle: the default int would cut it short)
    dc = wintypes.HDC(get_dc(None))
    try:
        if gdi32.GetDIBits(dc, hbitmap, 0, h, buf, ctypes.byref(header), 0) != h:
            return None
    finally:
        user32.ReleaseDC(None, dc)
    data = buf.raw
    # Thumbnails with transparency carry premultiplied alpha; plain ones have 0 in every alpha byte.
    has_alpha = any(data[3::4])
    fmt = QImage.Format_ARGB32_Premultiplied if has_alpha else QImage.Format_RGB32
    img = QImage(data, w, h, w * 4, fmt).copy()       # (copy: own the pixels before the buffer goes)
    if max(w, h) > max_px:
        img = img.scaled(max_px, max_px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return img


__all__ = ["thumbnail"]
