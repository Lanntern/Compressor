"""Large files: the biggest files in the chosen folders, to decide about yourself (none are pre-selected)."""

from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..formats import VIDEO_EXTS, human
from ..scanner import EXCLUDED_DIR_NAMES
from .common import Candidate, ScanResult

LIMIT = 500                    # the biggest this many are listed

DISK_IMAGES = {".iso", ".img", ".vhd", ".vhdx", ".vmdk", ".vdi", ".qcow2", ".dmg", ".wim", ".esd"}
ARCHIVES = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".cab"}
KINDS = {"all": "All files", "videos": "Videos", "images": "Disk images and archives", "other": "Everything else"}


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "videos"
    if ext in DISK_IMAGES or ext in ARCHIVES:
        return "images"
    return "other"


def _label(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "Video"
    if ext in DISK_IMAGES:
        return "Disk image"
    if ext in ARCHIVES:
        return "Archive"
    return (ext.lstrip(".").upper() + " file") if ext else "File"


def _walk(roots: list[Path], min_size: int, progress, cancel) -> list[tuple[Path, int, float]]:
    found: list[tuple[Path, int, float]] = []
    stack = list(roots)
    seen = 0
    while stack:
        if cancel is not None and cancel.is_set():
            break
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        seen += 1
        if progress and seen % 200 == 0:
            progress(f"Looking in {d}", seen, 0)
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    hidden = e.name.startswith(".") or bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN)
                    if not hidden and e.name.lower() not in EXCLUDED_DIR_NAMES:
                        stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    if st.st_size >= min_size:
                        found.append((Path(e.path), st.st_size, st.st_mtime))
            except OSError:
                continue
    return found


def _from_everything(roots: list[Path], min_size: int) -> list[tuple[Path, int, float]] | None:
    from .. import everything

    if not everything.available():
        return None
    out: list[tuple[Path, int, float]] = []
    if not any(r == Path.home() or (os.name == "nt" and r.anchor and Path(r.anchor) == r) for r in roots):
        return None                      # (a smaller folder is read directly: certain to be current)
    for root in roots:
        folder = str(everything.long_path(root)).rstrip("\\/") + "\\"
        items = everything.query(f'"{folder}" file: size:>={min_size}')
        if items is None:
            return None
        out += [(Path(n), s, m) for n, is_dir, s, m, a in items
                if not is_dir and s >= min_size and not a & 0x2]       # (hidden files left out, like the walk)
    return out


def find_large_files(roots: list[Path], min_mb: int, kind: str = "all",
                     progress: Callable | None = None, cancel: threading.Event | None = None) -> ScanResult:
    min_size = max(1, min_mb) * 1024 * 1024
    found = None
    if os.name == "nt":
        try:
            found = _from_everything(roots, min_size)
        except Exception:  # noqa: BLE001 - Everything's answer is a bonus: any problem, walk instead
            found = None
    if found is None:
        found = _walk(roots, min_size, progress, cancel)
    if kind != "all":
        found = [f for f in found if kind_of(f[0]) == kind]
    found.sort(key=lambda f: f[1], reverse=True)
    now = time.time()
    items = []
    for path, size, mtime in found[:LIMIT]:
        age = int((now - mtime) // 86400) if mtime else 0
        when = "changed today" if age < 1 else f"unchanged for {age:,} day{'s' if age != 1 else ''}"
        items.append(Candidate(path, size, detail=f"{_label(path)} · {when}", selected=False))
    notes = [f"Showing the largest {LIMIT}."] if len(found) > LIMIT else []
    if not items:
        notes.append(f"Nothing as large as {human(min_size)} here.")
    return ScanResult("large", items, notes, cancelled=bool(cancel is not None and cancel.is_set()))


__all__ = ["find_large_files", "KINDS", "kind_of", "LIMIT"]
