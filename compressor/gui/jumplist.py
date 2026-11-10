"""Windows jump list: the tasks shown when you right-click Compressor on the taskbar or in Start, and as its
actions in Windows Search ("Compress files…", "Clean Up", …), plus the AppUserModelID that ties the running
window, the installer's shortcuts and that list together.

Plain ctypes calls to the shell's COM interfaces (ICustomDestinationList, IShellLinkW, IPropertyStore): Qt 6
has no jump list API and this avoids another dependency. Everything here does nothing off Windows, and failing
is harmless: the app works the same without a jump list.
"""

from __future__ import annotations

import ctypes
import os
import sys
import uuid

from ..log import log

APP_ID = "Lanntern.Compressor"   # also the installer's shortcuts (packaging/installer.iss, AppUserModelID)

# (title, --open target, description shown as its tooltip). Targets: gui/app.py open_target.
TASKS = (
    ("Compress files…", "compress-files", "Choose photos, videos or music to shrink"),
    ("Convert files…", "convert-files", "Choose files to change to another format"),
    ("Clean Up", "cleanup", "Find space Windows doesn't free for you"),
    ("Watch Folders", "watch", "Compress new files automatically"),
    ("History", "history", "See and undo past runs"),
)

IS_WINDOWS = os.name == "nt"


def set_process_app_id() -> None:
    """Group this process's windows under APP_ID (call before the first window is shown)."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # type: ignore[attr-defined]
    except (AttributeError, OSError) as e:
        log.info("AppUserModelID not set: %s", e)


# ---------------------------------------------------------------------------------------------- COM plumbing

class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def of(cls, text: str) -> _GUID:
        g = cls()
        ctypes.memmove(g.data, uuid.UUID(text).bytes_le, 16)
        return g


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


class _PROPVARIANT(ctypes.Structure):
    # vt, three reserved WORDs, then the value (a wide-string pointer here); padded to PROPVARIANT's size.
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("pwszVal", ctypes.c_wchar_p), ("pad", ctypes.c_void_p)]


CLSID_DestinationList = "77f10cf0-3db5-4966-b520-b7c54fd35ed6"
IID_ICustomDestinationList = "6332debf-87b5-4670-90c0-5e57b408a49e"
CLSID_EnumerableObjectCollection = "2d3468c1-36a7-43b6-ac24-d3f02fd9607a"
IID_IObjectCollection = "5632b1a4-e38a-400a-928a-d4cd63230295"
IID_IObjectArray = "92ca9dcd-5622-4bba-a805-5e9f541bd8c9"
CLSID_ShellLink = "00021401-0000-0000-c000-000000000046"
IID_IShellLinkW = "000214f9-0000-0000-c000-000000000046"
IID_IPropertyStore = "886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"
PKEY_Title = ("f29f85e0-4ff9-1068-ab91-08002b27b3d9", 2)
VT_LPWSTR = 31
CLSCTX_INPROC_SERVER = 1


class _Com:
    """A COM interface pointer: call vtable slot ``i`` with ctypes argument types."""

    def __init__(self, ptr: ctypes.c_void_p):
        self.ptr = ptr

    def call(self, index: int, argtypes: tuple, *args) -> int:
        vtable = ctypes.cast(self.ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])  # type: ignore[attr-defined]
        hr = fn(self.ptr, *args)
        if hr < 0:
            raise OSError(f"COM call {index} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
        return hr

    def query(self, iid: str) -> _Com:
        out = ctypes.c_void_p()
        self.call(0, (ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)), ctypes.byref(_GUID.of(iid)),
                  ctypes.byref(out))
        return _Com(out)

    def release(self) -> None:
        if self.ptr:
            vtable = ctypes.cast(self.ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(self.ptr)  # type: ignore[attr-defined]
            self.ptr = ctypes.c_void_p()


CO_E_NOTINITIALIZED = -0x7FFBFE10        # 0x800401F0: COM hasn't been started on this thread


def _create(clsid: str, iid: str) -> _Com:
    ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    out = ctypes.c_void_p()

    def attempt() -> int:
        return ole32.CoCreateInstance(ctypes.byref(_GUID.of(clsid)), None, CLSCTX_INPROC_SERVER,
                                      ctypes.byref(_GUID.of(iid)), ctypes.byref(out))

    hr = attempt()
    if hr == CO_E_NOTINITIALIZED:
        # (Qt starts COM on the window's thread only once something needs it, e.g. drag and drop: start it here,
        # in the same single-threaded mode Qt uses, and leave it running for the app's lifetime.)
        ole32.CoInitializeEx(None, 2)
        hr = attempt()
    if hr < 0:
        raise OSError(f"CoCreateInstance {clsid} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    return _Com(out)


def _task_link(exe: str, args: str, title: str, description: str) -> _Com:
    """An IShellLinkW for one task: runs ``exe args``, shown as ``title``."""
    link = _create(CLSID_ShellLink, IID_IShellLinkW)
    w = ctypes.c_wchar_p
    link.call(20, (w,), exe)                      # SetPath
    link.call(11, (w,), args)                     # SetArguments
    link.call(7, (w,), description)               # SetDescription (the tooltip)
    link.call(17, (w, ctypes.c_int), exe, 0)      # SetIconLocation: the app's own icon
    store = link.query(IID_IPropertyStore)
    try:
        key = _PROPERTYKEY(_GUID.of(PKEY_Title[0]), PKEY_Title[1])
        value = _PROPVARIANT(vt=VT_LPWSTR, pwszVal=title)
        store.call(6, (ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT)),
                   ctypes.byref(key), ctypes.byref(value))                             # SetValue (copies)
        store.call(7, ())                                                              # Commit
    finally:
        store.release()
    return link


def update(exe: str | None = None, args_prefix: str = "") -> bool:
    """Replace the jump list's tasks with TASKS. ``exe``: the program the tasks start (default: this app when
    packaged; nothing is done when running from source, where tasks would start python.exe). True if it worked."""
    if not IS_WINDOWS:
        return False
    if exe is None:
        if not getattr(sys, "frozen", False):
            return False
        exe = sys.executable
    ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    hr_init = ole32.CoInitializeEx(None, 2)    # apartment-threaded; S_FALSE / changed mode: already initialised
    dest = tasks = None
    links: list[_Com] = []
    try:
        dest = _create(CLSID_DestinationList, IID_ICustomDestinationList)
        dest.call(3, (ctypes.c_wchar_p,), APP_ID)                                       # SetAppID
        slots, removed = ctypes.c_uint(), ctypes.c_void_p()
        dest.call(4, (ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)),
                  ctypes.byref(slots), ctypes.byref(_GUID.of(IID_IObjectArray)), ctypes.byref(removed))  # BeginList
        _Com(removed).release()
        tasks = _create(CLSID_EnumerableObjectCollection, IID_IObjectCollection)
        for title, target, description in TASKS:
            link = _task_link(exe, f"{args_prefix}--open={target}".strip(), title, description)
            links.append(link)
            tasks.call(5, (ctypes.c_void_p,), link.ptr)                                 # AddObject
        array = tasks.query(IID_IObjectArray)
        try:
            dest.call(7, (ctypes.c_void_p,), array.ptr)                                 # AddUserTasks
        finally:
            array.release()
        dest.call(8, ())                                                                # CommitList
        return True
    except OSError as e:
        log.info("Jump list not updated: %s", e)
        if dest is not None:
            try:
                dest.call(11, ())                                                       # AbortList
            except OSError:
                pass
        return False
    finally:
        for link in links:
            link.release()
        for com in (tasks, dest):
            if com is not None:
                com.release()
        if hr_init in (0, 1):
            ole32.CoUninitialize()


def clear() -> bool:
    """Remove the jump list Windows keeps for this app (the uninstaller's clean-up)."""
    if not IS_WINDOWS:
        return False
    ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    hr_init = ole32.CoInitializeEx(None, 2)
    dest = None
    try:
        dest = _create(CLSID_DestinationList, IID_ICustomDestinationList)
        dest.call(10, (ctypes.c_wchar_p,), APP_ID)                                     # DeleteList
        return True
    except OSError:
        return False
    finally:
        if dest is not None:
            dest.release()
        if hr_init in (0, 1):
            ole32.CoUninitialize()


__all__ = ["APP_ID", "TASKS", "set_process_app_id", "update", "clear"]
