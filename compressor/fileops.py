"""Filesystem helpers: Recycle Bin/Trash, timestamps, temp names."""

from __future__ import annotations

import os
import secrets
import shutil
import time
from functools import lru_cache
from pathlib import Path

from .formats import TEMP_PREFIX

BIN_NAME = "Recycle Bin" if os.name == "nt" else "Trash"     # what the person's system calls it

_IS_WINDOWS = os.name == "nt"


def temp_name(directory: Path, suffix: str, tag: str = "") -> Path:
    """An ASCII-only hidden temp file name in ``directory``.

    Encoders get bare ASCII file names and run with ``cwd=directory`` so that
    non-ASCII folder or user names (å, ä, ö...) can never trip them up.
    """
    return directory / f"{TEMP_PREFIX}{secrets.token_hex(6)}{tag}{suffix}"


def link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def remove_quietly(*paths: Path | None) -> None:
    for p in paths:
        if p is None:
            continue
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def final_path(original: Path, new_ext: str) -> Path:
    """Where the converted file goes: same name, new extension.

    If that name is taken by a different file, append " (1)", " (2)", ...
    Returns ``original`` itself when the extension doesn't change.
    """
    candidate = original.with_suffix(new_ext)
    if candidate == original or _same_path(candidate, original):
        return original
    n = 1
    while candidate.exists():
        candidate = original.with_name(f"{original.stem} ({n}){new_ext}")
        n += 1
    return candidate


def _same_path(a: Path, b: Path) -> bool:
    if _IS_WINDOWS:
        return str(a).lower() == str(b).lower()
    return a == b


def copy_times(st: os.stat_result, dst: Path) -> None:
    """Give ``dst`` the original's modified/accessed (and on Windows, created) time."""
    try:
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass
    if _IS_WINDOWS:
        created = getattr(st, "st_birthtime_ns", None) or st.st_ctime_ns
        _set_windows_creation_time(dst, created)


def _set_windows_creation_time(path: Path, ns: int) -> None:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(path), 0x100, 0x7, None, 3, 0x02000000, None  # FILE_WRITE_ATTRIBUTES, share all, OPEN_EXISTING, BACKUP_SEMANTICS
        )
        if handle in (None, wintypes.HANDLE(-1).value):
            return
        try:
            ft = ns // 100 + 116444736000000000
            ctime = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
            kernel32.SetFileTime(handle, ctypes.byref(ctime), None, None)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass


@lru_cache(maxsize=64)
def _drive_type(root: str) -> int:
    import ctypes

    return ctypes.windll.kernel32.GetDriveTypeW(root)  # type: ignore[attr-defined]


def trash_available(path: Path) -> bool:
    """Whether deleting ``path`` would land in a Recycle Bin/Trash.

    On Windows, removable and network drives have no Recycle Bin: deleting
    there is permanent, so we refuse to replace originals on them.
    """
    if not _IS_WINDOWS:
        return True
    drive = os.path.splitdrive(str(path.resolve()))[0]
    if not drive or drive.startswith("\\\\"):
        return False
    if _drive_type(drive + "\\") != 3:  # DRIVE_FIXED
        return False
    return recycle_bin_capacity(path) != 0   # 0: "Don't move files to the Recycle Bin" is set for the drive


class TooBigForTrash(OSError):
    """Windows would delete this permanently (it's larger than the drive's Recycle Bin), so we don't."""


_capacity_cache: dict[str, tuple[float, int | None]] = {}


def recycle_bin_capacity(path: Path) -> int | None:
    """Windows: the largest item the Recycle Bin on ``path``'s drive keeps, in bytes (0 when the bin is
    turned off for that drive). None elsewhere, or when it can't be read.

    Windows quietly deletes anything bigger *permanently* when asked to recycle it without a prompt (which
    is how send2trash asks), so callers must check this first."""
    if not _IS_WINDOWS:
        return None
    try:
        import ctypes
        import winreg

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        buf = ctypes.create_unicode_buffer(300)
        if not k32.GetVolumePathNameW(str(path), buf, 300):
            return None
        root = buf.value
        hit = _capacity_cache.get(root)
        if hit is not None and time.monotonic() - hit[0] < 30:
            return hit[1]
        name = ctypes.create_unicode_buffer(64)
        cap: int | None = None
        if k32.GetVolumeNameForVolumeMountPointW(root, name, 64):
            guid = name.value[name.value.find("{"):name.value.find("}") + 1]
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer"
                                    "\\BitBucket\\Volume\\" + guid) as key:
                    try:
                        if winreg.QueryValueEx(key, "NukeOnDelete")[0]:
                            cap = 0
                    except OSError:
                        pass
                    if cap is None:
                        cap = int(winreg.QueryValueEx(key, "MaxCapacity")[0]) * 1024 * 1024
            except OSError:
                cap = None
        if cap is None:   # not configured yet: Windows' default is about 5% of the drive
            cap = int(shutil.disk_usage(root).total * 0.05)
        _capacity_cache[root] = (time.monotonic(), cap)
        return cap
    except Exception:  # noqa: BLE001
        return None


def _size_of(path: Path) -> int:
    if not path.is_dir() or path.is_symlink():
        return path.lstat().st_size
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


_TRASH_LOCK = __import__("threading").Lock()


def move_to_trash(path: Path, size: int | None = None) -> None:
    """Send to the Recycle Bin/Trash. Serialised: parallel workers would otherwise race to
    create the Trash folders (Linux) or overlap shell file operations (Windows).

    Raises TooBigForTrash instead of letting Windows delete something permanently because it's larger
    than the Recycle Bin (``size``: bytes, if already known)."""
    from send2trash import send2trash

    cap = recycle_bin_capacity(path)
    if cap is not None:
        size = _size_of(path) if size is None else size
        if cap == 0 or size > cap * 0.95:
            from .formats import human

            raise TooBigForTrash(
                f"Too big for the Recycle Bin on this drive ({human(size)}; it keeps items up to {human(cap)}), "
                "so Windows would delete it permanently. Left as it is. You can raise the limit in the "
                "Recycle Bin's Properties.")
    with _TRASH_LOCK:
        send2trash(str(path))


def read_zone_mark(path: Path) -> bytes | None:
    """Windows: the "downloaded from the internet" mark on ``path`` (its Zone.Identifier stream), or None."""
    if not _IS_WINDOWS:
        return None
    try:
        with open(f"{path}:Zone.Identifier", "rb") as f:
            return f.read(64 * 1024)
    except OSError:
        return None                             # no mark (or not NTFS)


def write_zone_mark(path: Path, mark: bytes | None) -> None:
    """Give ``path`` a mark read by read_zone_mark, so SmartScreen and Office's Protected View treat a converted
    download the same as the original."""
    if not mark or not _IS_WINDOWS:
        return
    try:
        with open(f"{path}:Zone.Identifier", "wb") as f:
            f.write(mark)
    except OSError:
        pass


_SHCNE_CREATE, _SHCNE_DELETE, _SHCNE_UPDATEITEM, _SHCNE_UPDATEDIR = 0x2, 0x4, 0x2000, 0x1000
_SHCNF_PATHW, _SHCNF_FLUSHNOWAIT = 0x5, 0x3000


def notify_shell(created=(), updated=(), deleted=()) -> None:
    """Windows: tell Explorer which files changed, so open windows show new thumbnails and icons straight away
    instead of stale ones until F5 (Explorer caches thumbnails by name, and a file replaced under the same name
    often keeps its old one)."""
    if not _IS_WINDOWS:
        return
    try:
        import ctypes

        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        folders = set()
        for event, paths in ((_SHCNE_CREATE, created), (_SHCNE_UPDATEITEM, updated), (_SHCNE_DELETE, deleted)):
            for p in paths:
                shell32.SHChangeNotify(event, _SHCNF_PATHW | _SHCNF_FLUSHNOWAIT, ctypes.c_wchar_p(str(p)), None)
                folders.add(str(Path(p).parent))
        for d in folders:
            shell32.SHChangeNotify(_SHCNE_UPDATEDIR, _SHCNF_PATHW | _SHCNF_FLUSHNOWAIT, ctypes.c_wchar_p(d), None)
    except Exception:  # noqa: BLE001 - cosmetic: never fails a job
        pass


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` so that a crash or power cut leaves either the old file or the new one,
    never half of one: written to a temporary file beside it, flushed to disk, then renamed over it."""
    path = Path(path)
    tmp = path.with_name(f"{TEMP_PREFIX}{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        remove_quietly(tmp)


def set_aside(path: Path) -> Path | None:
    """Keep a damaged file (renamed with the time) instead of writing over it; returns the new name."""
    path = Path(path)
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        os.replace(path, dest)
        return dest
    except OSError:
        return None
