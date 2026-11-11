"""Calling Windows Runtime (WinRT) classes from plain ctypes: the supported, documented way to reach modern Windows
features (notifications, the accent color) without PowerShell or another dependency.

Only what this app uses: activate a class or get its statics/factory, call methods by vtable slot (via
jumplist._Com), pass strings as HSTRINGs. Interface IDs and slot numbers come from Windows' own metadata (as
published in Microsoft's windows-rs bindings); slot 6 is the first method after IInspectable's six.
"""

from __future__ import annotations

import ctypes
import os
from contextlib import contextmanager

IS_WINDOWS = os.name == "nt"
_combase = None


def _api():
    """combase.dll with every function typed (handles and HSTRINGs are 64-bit: never let ctypes guess)."""
    global _combase
    if _combase is None:
        from ctypes import wintypes

        dll = ctypes.WinDLL("combase")  # type: ignore[attr-defined]
        dll.WindowsCreateString.argtypes = (wintypes.LPCWSTR, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))
        dll.WindowsCreateString.restype = ctypes.c_long
        dll.WindowsDeleteString.argtypes = (ctypes.c_void_p,)
        dll.WindowsDeleteString.restype = ctypes.c_long
        dll.RoGetActivationFactory.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        dll.RoGetActivationFactory.restype = ctypes.c_long
        dll.RoActivateInstance.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        dll.RoActivateInstance.restype = ctypes.c_long
        dll.RoInitialize.argtypes = (ctypes.c_int,)
        dll.RoInitialize.restype = ctypes.c_long
        dll.RoInitialize(0)        # (single-threaded, like Qt's GUI thread; already-started is fine)
        _combase = dll
    return _combase


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


@contextmanager
def hstring(text: str):
    """A WinRT string for the duration of a call."""
    api = _api()
    h = ctypes.c_void_p()
    _check(api.WindowsCreateString(text, len(text), ctypes.byref(h)), "WindowsCreateString")
    try:
        yield h
    finally:
        api.WindowsDeleteString(h)


def factory(class_name: str, iid: str):
    """A class's statics or factory interface (``iid``), as a jumplist._Com."""
    from .jumplist import _GUID, _Com

    out = ctypes.c_void_p()
    with hstring(class_name) as name:
        _check(_api().RoGetActivationFactory(name, ctypes.byref(_GUID.of(iid)), ctypes.byref(out)),
               f"RoGetActivationFactory({class_name})")
    return _Com(out)


def activate(class_name: str):
    """A new instance of a class with a default constructor (its IInspectable, as a jumplist._Com)."""
    from .jumplist import _Com

    out = ctypes.c_void_p()
    with hstring(class_name) as name:
        _check(_api().RoActivateInstance(name, ctypes.byref(out)), f"RoActivateInstance({class_name})")
    return _Com(out)


def call_out(com, slot: int, argtypes: tuple = (), *args):
    """Call a method whose last parameter receives an interface pointer; returns that interface."""
    from .jumplist import _Com

    out = ctypes.c_void_p()
    com.call(slot, (*argtypes, ctypes.POINTER(ctypes.c_void_p)), *args, ctypes.byref(out))
    return _Com(out)


class Color(ctypes.Structure):
    _fields_ = [("A", ctypes.c_ubyte), ("R", ctypes.c_ubyte), ("G", ctypes.c_ubyte), ("B", ctypes.c_ubyte)]


IID_IUISettings3 = "03021be4-5254-4781-8194-5168f7d06d7b"
UICOLOR_ACCENT = 5


def accent_color() -> str | None:
    """The accent color chosen in Windows' Settings (UISettings.GetColorValue(Accent)) as "#RRGGBB", or None."""
    if not IS_WINDOWS:
        return None
    settings = ui = None
    try:
        settings = activate("Windows.UI.ViewManagement.UISettings")
        ui = settings.query(IID_IUISettings3)
        color = Color()
        ui.call(6, (ctypes.c_int, ctypes.POINTER(Color)), UICOLOR_ACCENT, ctypes.byref(color))   # GetColorValue
        return f"#{color.R:02X}{color.G:02X}{color.B:02X}"
    except Exception:  # noqa: BLE001 - no accent: the app's own color is used
        return None
    finally:
        for com in (ui, settings):
            if com is not None:
                com.release()


__all__ = ["hstring", "factory", "activate", "call_out", "accent_color", "IS_WINDOWS"]
