"""Ask Everything (voidtools), when it's running, for the files under a folder: instant, and without the
administrator permission the drive's file table needs.

Everything keeps its own index of every NTFS drive. It answers questions sent as a WM_COPYDATA message to its
window (EVERYTHING_IPC_COPYDATA_QUERY2W, documented in its SDK's everything_ipc.h); the answer comes back
as a WM_COPYDATA message to a window of ours. Nothing is bundled or installed: when Everything isn't running,
or doesn't answer, the caller scans the usual way.

The answer is turned into the same listing the drive's file table gives (ntfs.Listing), so the scanner
applies exactly the same rules (hidden and program folders, cloud-only files, excluded names).
"""

from __future__ import annotations

import os
import struct
import time
from collections.abc import Callable, Iterable
from pathlib import Path

ENABLED = True          # Settings › "Use Everything when it's running"

_WNDCLASS = "EVERYTHING_TASKBAR_NOTIFICATION"
_WM_USER = 0x0400
_WM_COPYDATA = 0x004A
_IS_DB_LOADED = 401
_QUERY2W = 18
_REQ_FULL_PATH = 0x00000004
_REQ_SIZE = 0x00000010
_REQ_DATE_MODIFIED = 0x00000040
_REQ_ATTRIBUTES = 0x00000100
_FOLDER = 0x00000001
_ALL = 0xFFFFFFFF
_SORT_NAME = 1
_REPLY_ID = 0x434D5052          # "CMPR": the reply we wait for

_EPOCH_DIFF = 116444736000000000   # FILETIME (100 ns since 1601) -> Unix time


def _hwnd() -> int:
    if os.name != "nt" or not ENABLED:
        return 0
    import ctypes

    return int(ctypes.windll.user32.FindWindowW(_WNDCLASS, None) or 0)   # type: ignore[attr-defined]


def available() -> bool:
    """Everything is running and has its index loaded."""
    hwnd = _hwnd()
    if not hwnd:
        return False
    import ctypes

    return bool(ctypes.windll.user32.SendMessageW(hwnd, _WM_USER, _IS_DB_LOADED, 0))   # type: ignore[attr-defined]


def build_query(reply_hwnd: int, search: str) -> bytes:
    """EVERYTHING_IPC_QUERY2 followed by the search text (UTF-16, null-terminated)."""
    head = struct.pack("<7I", reply_hwnd & 0xFFFFFFFF, _REPLY_ID, 0, 0, _ALL,
                       _REQ_FULL_PATH | _REQ_SIZE | _REQ_DATE_MODIFIED | _REQ_ATTRIBUTES, _SORT_NAME)
    return head + (search + "\0").encode("utf-16-le")


def parse_reply(data: bytes) -> list[tuple[str, bool, int, float, int]]:
    """EVERYTHING_IPC_LIST2 -> (full path, is folder, size, modified (Unix time), attributes) per item."""
    total, num, _offset, flags, _sort = struct.unpack_from("<5I", data, 0)
    out = []
    for i in range(num):
        item_flags, at = struct.unpack_from("<2I", data, 20 + 8 * i)
        name = ""
        size, mtime, attrs = 0, 0.0, 0
        if flags & _REQ_FULL_PATH:
            (n,) = struct.unpack_from("<I", data, at)
            at += 4
            name = data[at:at + 2 * n].decode("utf-16-le", "replace")
            at += 2 * (n + 1)
        if flags & _REQ_SIZE:
            (size,) = struct.unpack_from("<q", data, at)
            at += 8
        if flags & _REQ_DATE_MODIFIED:
            (ft,) = struct.unpack_from("<Q", data, at)
            at += 8
            mtime = max(0.0, (ft - _EPOCH_DIFF) / 10_000_000) if ft else 0.0
        if flags & _REQ_ATTRIBUTES:
            (attrs,) = struct.unpack_from("<I", data, at)
            at += 4
        out.append((name, bool(item_flags & _FOLDER), max(0, size), mtime, attrs))
    return out


def query(search: str, timeout: float = 30.0) -> list[tuple[str, bool, int, float, int]] | None:
    """Send ``search`` to Everything and wait for the answer. None: no answer (not running, or too slow)."""
    target = _hwnd()
    if not target:
        return None
    import ctypes
    from ctypes import wintypes

    # Our own handles to the DLLs, so the declarations below don't change anyone else's calls.
    user32 = ctypes.WinDLL("user32")       # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32")   # type: ignore[attr-defined]
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class COPYDATASTRUCT(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    # Handles are pointer-sized: without these, ctypes passes them as 32-bit ints and a module loaded above
    # 2 GB fails ("int too long to convert") or is cut short.
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = LRESULT
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.UnregisterClassW.restype = wintypes.BOOL
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                    wintypes.UINT]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.MsgWaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                                 wintypes.DWORD]
    user32.MsgWaitForMultipleObjects.restype = wintypes.DWORD
    reply: list[bytes] = []

    def proc(hwnd, msg, wparam, lparam):
        if msg == _WM_COPYDATA:
            cds = ctypes.cast(lparam, ctypes.POINTER(COPYDATASTRUCT)).contents
            if cds.dwData == _REPLY_ID:
                reply.append(ctypes.string_at(cds.lpData, cds.cbData))   # (only valid during this message)
                return 1
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    callback = WNDPROC(proc)
    name = f"WickEverythingReply{os.getpid()}"
    wc = WNDCLASSW()
    wc.lpfnWndProc = callback
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = name
    if not user32.RegisterClassW(ctypes.byref(wc)):
        return None
    HWND_MESSAGE = wintypes.HWND(-3)
    hwnd = user32.CreateWindowExW(0, name, name, 0, 0, 0, 0, 0, HWND_MESSAGE, None, wc.hInstance, None)
    try:
        if not hwnd:
            return None
        try:
            # Running elevated, Windows drops messages from programs with fewer rights (Everything usually
            # runs as you): let its reply through, to this window only.
            user32.ChangeWindowMessageFilterEx.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.DWORD,
                                                           ctypes.c_void_p]
            user32.ChangeWindowMessageFilterEx(hwnd, _WM_COPYDATA, 1, None)   # MSGFLT_ALLOW
        except Exception:  # noqa: BLE001 - older Windows: not needed
            pass
        payload = build_query(int(hwnd), search)
        buf = ctypes.create_string_buffer(payload, len(payload))
        cds = COPYDATASTRUCT(_QUERY2W, len(payload), ctypes.cast(buf, ctypes.c_void_p))
        if not user32.SendMessageW(target, _WM_COPYDATA, hwnd, ctypes.addressof(cds)):
            return None
        msg = wintypes.MSG()
        end = time.monotonic() + timeout
        while not reply and time.monotonic() < end:
            while user32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, 1):    # PM_REMOVE
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if not reply:
                user32.MsgWaitForMultipleObjects(0, None, False, 50, 0x04FF)   # QS_ALLINPUT
        return parse_reply(reply[0]) if reply else None
    finally:
        if hwnd:
            user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(name, wc.hInstance)


def search_for(root: Path, extensions: Iterable[str]) -> str:
    """Folders and the files with ``extensions`` anywhere under ``root``."""
    folder = str(root).rstrip("\\/") + "\\"
    exts = ";".join(sorted({e.lstrip(".").lower() for e in extensions}))
    return f'"{folder}" <folder:|ext:{exts}>'


def to_listing(root: Path, items, marker_exts: Iterable[str] = (".dll", ".so", ".dylib")):
    """Everything's answer as the scanner's ntfs.Listing (root index 0). Folders get records; files are filed
    under their folder; a folder holding a program library is marked like the file table marks it."""
    from .ntfs import Listing

    markers = {e.lower() for e in marker_exts}
    root_key = os.path.normcase(str(root).rstrip("\\/"))
    out = Listing()
    out.roots[0] = 0
    out.dirs[0] = (-1, 0, False, root.name or str(root))
    ids = {root_key: 0}
    folders = sorted((name for name, is_dir, *_ in items if is_dir), key=lambda n: n.count("\\"))
    attrs_of = {os.path.normcase(n): a for n, d, _s, _m, a in items if d}
    for name in folders:
        key = os.path.normcase(name)
        parent = ids.get(os.path.normcase(os.path.dirname(name)))
        if parent is None or key in ids:
            continue                       # (outside the root, or its parent was left out)
        rec = len(ids)
        ids[key] = rec
        out.dirs[rec] = (parent, attrs_of.get(key, 0), False, os.path.basename(name))
        out.subdirs.setdefault(parent, []).append(rec)
    for name, is_dir, size, mtime, attrs in items:
        if is_dir:
            continue
        parent = ids.get(os.path.normcase(os.path.dirname(name)))
        if parent is None:
            continue
        base = os.path.basename(name)
        if os.path.splitext(base)[1].lower() in markers:
            p, a, _m, n = out.dirs[parent]
            out.dirs[parent] = (p, a, True, n)
            continue
        out.files.setdefault(parent, []).append((size, mtime, attrs, base))
    return out


def long_path(p: Path) -> Path:
    """The full name of ``p`` (Everything stores full names: C:\\Users\\RUNNER~1 would match nothing)."""
    if os.name != "nt":
        return p
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetLongPathNameW(str(p), buf, len(buf))   # type: ignore[attr-defined]
        return Path(buf.value) if 0 < n < len(buf) else p
    except Exception:  # noqa: BLE001
        return p


def listing_for(root: Path, extensions: Iterable[str], progress: Callable | None = None):
    """The listing for ``root`` from Everything, or None (not running, no answer, or nothing indexed there)."""
    if not available():
        return None
    if progress:
        progress("Asking Everything for the file list…", 0, 0)
    root = long_path(root)
    exts = set(extensions) | {".dll", ".so", ".dylib"}
    items = query(search_for(root, exts))
    if not items:
        return None
    return to_listing(root, items)


__all__ = ["ENABLED", "available", "query", "listing_for", "build_query", "parse_reply", "to_listing", "search_for"]
