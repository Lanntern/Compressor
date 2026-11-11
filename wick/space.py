"""Where a drive's space goes: photos, videos, music and documents (from Home's look through your folders),
app caches, the Recycle Bin, everything else, and what's free. Measured alongside Home's analysis."""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .fileops import BIN_NAME

PHOTOS, VIDEOS, MUSIC, DOCUMENTS, CACHES, BIN = "photos", "videos", "music", "documents", "caches", "bin"
ORDER = (PHOTOS, VIDEOS, MUSIC, DOCUMENTS, CACHES, BIN)          # fixed: a category keeps its color
LABELS = {PHOTOS: "Photos", VIDEOS: "Videos", MUSIC: "Music", DOCUMENTS: "Documents", CACHES: "App caches",
          BIN: BIN_NAME}


@dataclass
class Breakdown:
    drive: str                       # "C:" / "/" (where the analyzed folders are)
    total: int
    free: int
    sizes: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def used(self) -> int:
        return self.total - self.free

    @property
    def other(self) -> int:
        return max(0, self.used - sum(self.sizes.values()))


def _anchor(p: Path) -> Path:
    return Path(Path(os.path.abspath(p)).anchor or "/")


def same_drive(a: Path, b: Path) -> bool:
    if os.name == "nt":
        return _anchor(a).drive.upper() == _anchor(b).drive.upper()
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def bin_size(drive: Path) -> int:
    """How much the Recycle Bin (Trash) holds for ``drive``."""
    if os.name == "nt":
        try:
            import ctypes

            class SHQUERYRBINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong),
                            ("i64NumItems", ctypes.c_longlong)]

            info = SHQUERYRBINFO()
            info.cbSize = ctypes.sizeof(info)
            if ctypes.windll.shell32.SHQueryRecycleBinW(str(drive), ctypes.byref(info)) == 0:
                return int(info.i64Size)
        except Exception:  # noqa: BLE001
            pass
        return 0
    from .cleanup.common import folder_size

    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    trash = Path(xdg) / "Trash" / "files"
    if not trash.is_dir() or not same_drive(trash, drive):
        return 0
    return folder_size(trash)[0]


def measure(overview, roots: list[Path], cancel: threading.Event | None = None) -> Breakdown | None:
    """The breakdown for the drive holding ``roots`` (Home's folders), from Home's ``overview``."""
    if not roots:
        return None
    drive = _anchor(roots[0])
    try:
        usage = shutil.disk_usage(drive)
    except OSError:
        return None
    b = Breakdown(drive.drive or str(drive), usage.total, usage.free)
    for key, (count, size) in getattr(overview, "by_kind", {}).items():
        b.sizes[key], b.counts[key] = size, count
    try:
        from .cleanup import caches

        found = caches.find_caches(cancel=cancel)
        on_drive = [c for c in found.items if same_drive(c.path, drive)]
        b.sizes[CACHES], b.counts[CACHES] = sum(c.size for c in on_drive), len(on_drive)
    except Exception:  # noqa: BLE001 - a breakdown without caches is still useful
        pass
    b.sizes[BIN] = bin_size(drive)
    return b


__all__ = ["Breakdown", "measure", "bin_size", "ORDER", "LABELS", "PHOTOS", "VIDEOS", "MUSIC", "DOCUMENTS",
           "CACHES", "BIN"]
