"""Restoring a file we sent to the Recycle Bin (Windows) or Trash (Linux).

send2trash can delete but not restore, so this reads the bins directly:

* Windows: ``X:\\$Recycle.Bin\\<SID>\\$Ixxxxxx.ext`` holds the original path and
  deletion time; ``$Rxxxxxx.ext`` next to it is the file itself.
* freedesktop.org Trash: ``<trash>/info/<name>.trashinfo`` + ``<trash>/files/<name>``.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

_IS_WINDOWS = os.name == "nt"


@dataclass
class TrashedItem:
    original: Path
    deleted_at: float       # unix time
    data_path: Path         # the file content inside the bin
    meta_path: Path         # $I file / .trashinfo


# ---------------------------------------------------------------- Windows

def parse_windows_info(data: bytes) -> tuple[str, float] | None:
    """Parse a ``$I`` file. Returns (original path, deletion unix time)."""
    if len(data) < 24:
        return None
    version, _size, filetime = struct.unpack_from("<qqq", data, 0)
    if version == 1:
        raw = data[24:24 + 520]
    elif version == 2:
        if len(data) < 28:
            return None
        (nchars,) = struct.unpack_from("<I", data, 24)
        raw = data[28:28 + nchars * 2]
    else:
        return None
    path = raw.decode("utf-16-le", "replace").split("\x00", 1)[0]
    unix = (filetime - 116444736000000000) / 1e7
    return path, unix


def _windows_bins(original: Path) -> list[Path]:
    drive = os.path.splitdrive(str(original))[0]
    root = Path(drive + "\\") / "$Recycle.Bin"
    bins = []
    try:
        for d in os.scandir(root):
            if d.is_dir():
                bins.append(Path(d.path))
    except OSError:
        pass
    return bins


def _windows_candidates(original: Path, bins: list[Path]) -> list[TrashedItem]:
    want = str(original).lower()
    found = []
    for b in bins:
        try:
            entries = list(os.scandir(b))
        except OSError:
            continue  # other users' bins aren't readable
        for e in entries:
            if not e.name.startswith("$I"):
                continue
            try:
                with open(e.path, "rb") as f:
                    parsed = parse_windows_info(f.read(4096))
            except OSError:
                continue
            if not parsed or parsed[0].lower() != want:
                continue
            data = b / ("$R" + e.name[2:])
            if data.exists():
                found.append(TrashedItem(Path(parsed[0]), parsed[1], data, Path(e.path)))
    return found


# ---------------------------------------------------------------- freedesktop

def parse_trashinfo(text: str) -> tuple[str, float] | None:
    path = date = None
    for line in text.splitlines():
        if line.startswith("Path="):
            path = unquote(line[5:].strip())
        elif line.startswith("DeletionDate="):
            try:
                date = datetime.fromisoformat(line[13:].strip()).timestamp()
            except ValueError:
                date = 0.0
    if path is None:
        return None
    return path, date or 0.0


def _mount_point(p: Path) -> Path:
    p = p.resolve()
    while not os.path.ismount(p) and p.parent != p:
        p = p.parent
    return p


def _freedesktop_bins(original: Path) -> list[tuple[Path, Path | None]]:
    """(trash dir, topdir for relative paths or None)."""
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    bins: list[tuple[Path, Path | None]] = [(Path(xdg) / "Trash", None)]
    try:
        uid = os.getuid()
    except AttributeError:
        return bins
    top = _mount_point(original.parent)
    bins += [(top / ".Trash" / str(uid), top), (top / f".Trash-{uid}", top)]
    return bins


def _freedesktop_candidates(original: Path, bins: list[tuple[Path, Path | None]]) -> list[TrashedItem]:
    found = []
    want = os.path.normpath(str(original))
    for trash, top in bins:
        info_dir = trash / "info"
        try:
            entries = list(os.scandir(info_dir))
        except OSError:
            continue
        for e in entries:
            if not e.name.endswith(".trashinfo"):
                continue
            try:
                parsed = parse_trashinfo(Path(e.path).read_text("utf-8", "replace"))
            except OSError:
                continue
            if not parsed:
                continue
            path = parsed[0]
            if not os.path.isabs(path) and top is not None:
                path = str(top / path)
            if os.path.normpath(path) != want:
                continue
            data = trash / "files" / e.name[: -len(".trashinfo")]
            if data.exists():
                found.append(TrashedItem(Path(path), parsed[1], data, Path(e.path)))
    return found


# ---------------------------------------------------------------- API

def find_trashed(original: Path, not_before: float = 0.0, bins=None) -> TrashedItem | None:
    """The most recently trashed copy of ``original`` deleted at/after ``not_before``.

    ``bins`` overrides the bin locations (for tests).
    """
    original = Path(original)
    if _IS_WINDOWS:
        items = _windows_candidates(original, bins if bins is not None else _windows_bins(original))
    else:
        items = _freedesktop_candidates(original, bins if bins is not None else _freedesktop_bins(original))
    # Clock skew / second-granularity timestamps: allow a little slack.
    items = [i for i in items if i.deleted_at >= not_before - 120]
    return max(items, key=lambda i: i.deleted_at, default=None)


def _all_windows(bins: list[Path]) -> list[TrashedItem]:
    found = []
    for b in bins:
        try:
            entries = list(os.scandir(b))
        except OSError:
            continue
        for e in entries:
            if not e.name.startswith("$I"):
                continue
            try:
                with open(e.path, "rb") as f:
                    parsed = parse_windows_info(f.read(4096))
            except OSError:
                continue
            data = b / ("$R" + e.name[2:])
            if parsed and data.exists():
                found.append(TrashedItem(Path(parsed[0]), parsed[1], data, Path(e.path)))
    return found


def _all_freedesktop(bins: list[tuple[Path, Path | None]]) -> list[TrashedItem]:
    found = []
    for trash, top in bins:
        try:
            entries = list(os.scandir(trash / "info"))
        except OSError:
            continue
        for e in entries:
            if not e.name.endswith(".trashinfo"):
                continue
            try:
                parsed = parse_trashinfo(Path(e.path).read_text("utf-8", "replace"))
            except OSError:
                continue
            if not parsed:
                continue
            path = parsed[0] if os.path.isabs(parsed[0]) or top is None else str(top / parsed[0])
            data = trash / "files" / e.name[: -len(".trashinfo")]
            if data.exists() or data.is_symlink():
                found.append(TrashedItem(Path(path), parsed[1], data, Path(e.path)))
    return found


def index(originals, bins=None) -> dict[str, list[TrashedItem]]:
    """Everything in the bins that hold ``originals`` (each bin read once), by normalised original path.
    ``bins`` overrides the bin locations (for tests)."""
    originals = [Path(o) for o in originals]
    if bins is None:
        if _IS_WINDOWS:
            drives = {os.path.splitdrive(str(o))[0].upper(): o for o in originals}
            bins = [b for o in drives.values() for b in _windows_bins(o)]
        else:
            seen: dict = {}
            for o in originals:
                try:
                    for b in _freedesktop_bins(o):
                        seen.setdefault(str(b[0]), b)
                except OSError:
                    continue
            bins = list(seen.values())
    items = _all_windows(bins) if _IS_WINDOWS else _all_freedesktop(bins)
    out: dict[str, list[TrashedItem]] = {}
    for it in items:
        out.setdefault(os.path.normcase(os.path.normpath(str(it.original))), []).append(it)
    return out


def in_index(idx: dict[str, list[TrashedItem]], original, not_before: float = 0.0) -> bool:
    key = os.path.normcase(os.path.normpath(str(original)))
    return any(i.deleted_at >= not_before - 120 for i in idx.get(key, ()))


def restore(item: TrashedItem) -> None:
    if item.original.exists():
        raise FileExistsError(f"{item.original} already exists")
    item.original.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(item.data_path, item.original)
    except OSError:
        import shutil

        shutil.move(str(item.data_path), str(item.original))
    try:
        os.remove(item.meta_path)
    except OSError:
        pass


def purge(item: TrashedItem) -> None:
    """Delete one item we put in the bin for good (frees its space without emptying the whole bin)."""
    import shutil
    import stat

    def force(func, path, _exc):   # read-only files (Go's module cache, git objects): make writable, retry
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)

    data = item.data_path
    if data.is_dir() and not data.is_symlink():
        shutil.rmtree(data, onerror=force)
    elif data.exists() or data.is_symlink():
        try:
            os.remove(data)
        except PermissionError:
            os.chmod(data, stat.S_IWRITE | stat.S_IREAD)
            os.remove(data)
    try:
        os.remove(item.meta_path)
    except OSError:
        pass

