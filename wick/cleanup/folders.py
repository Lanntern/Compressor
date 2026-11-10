"""Folder sizes: how much each folder holds, biggest first, opened one level at a time.

One walk measures everything under the chosen folders (hidden folders and program folders too: they take
space as well). Links are not followed and files that are only in the cloud don't count. Each folder keeps
its subfolders and its largest files; the rest of its files show as one line. Rows are built only when a
folder is opened, so even a whole drive stays quick to show.
"""

from __future__ import annotations

import heapq
import os
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .common import Candidate, ScanResult, cancelled, cloud_only

TOP_FILES = 30                       # largest files listed per folder
# Folders apps and the system need as they are: shown and measured, but not offered for the Recycle Bin.
LOCKED = {"appdata", "library", "windows", "program files", "program files (x86)", "programdata",
          "$recycle.bin", "system volume information", ".local", ".config", "users", "recovery", "boot",
          "onedrive", "icloud drive", "dropbox", "google drive"}


@dataclass
class _Dir:
    path: str
    parent: int
    size: int = 0                     # own files, then everything below
    files: int = 0
    subdirs: list[int] = field(default_factory=list)
    top: list[tuple[int, str]] = field(default_factory=list)   # (size, name): a min-heap of the largest


def _measure(roots: list[Path], progress, cancel) -> list[_Dir]:
    dirs: list[_Dir] = []
    seen: set[str] = set()
    for root in roots:
        stack = [(str(root), -1)]
        while stack:
            if cancelled(cancel):
                break
            path, parent = stack.pop()
            key = os.path.normcase(path)
            if key in seen:
                continue
            seen.add(key)
            me = len(dirs)
            node = _Dir(path, parent)
            dirs.append(node)
            if parent >= 0:
                dirs[parent].subdirs.append(me)
            if progress and me % 500 == 0:
                progress(f"Measuring {path}", me, 0)
            try:
                with os.scandir(path) as it:
                    entries = list(it)
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_symlink():
                        continue
                    st = e.stat(follow_symlinks=False)
                    if getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        continue              # (junctions and mount points: measured where they really are)
                    if e.is_dir(follow_symlinks=False):
                        stack.append((e.path, me))
                    elif not cloud_only(st):
                        node.size += st.st_size
                        node.files += 1
                        if len(node.top) < TOP_FILES:
                            heapq.heappush(node.top, (st.st_size, e.name))
                        elif st.st_size > node.top[0][0]:
                            heapq.heapreplace(node.top, (st.st_size, e.name))
                except OSError:
                    continue
    for node in reversed(dirs):           # (children always come after their parent)
        if node.parent >= 0:
            dirs[node.parent].size += node.size
            dirs[node.parent].files += node.files
    return dirs


def _pct(part: int, whole: int) -> str:
    if whole <= 0:
        return ""
    p = 100 * part / whole
    return f"{p:.0f}%" if p >= 1 else "<1%"


def _forget(dirs: list[_Dir], parent: int, size: int, files: int, folder: int = -1, name: str = "") -> Callable:
    """Take a removed folder (or file) out of the measurements."""
    def forget() -> None:
        node = dirs[parent]
        if folder >= 0 and folder in node.subdirs:
            node.subdirs.remove(folder)
        elif name:
            node.top = [t for t in node.top if t[1] != name]
            heapq.heapify(node.top)
        k = parent
        while k >= 0:
            dirs[k].size -= size
            dirs[k].files -= files
            k = dirs[k].parent
    return forget


def _children(dirs: list[_Dir], i: int) -> Callable[[], list[Candidate]]:
    def make() -> list[Candidate]:
        node = dirs[i]
        name = Path(node.path).name or node.path
        out = [_folder(dirs, k, node.size, name) for k in sorted(node.subdirs, key=lambda k: -dirs[k].size)]
        top = sorted(node.top, reverse=True)
        for size, fname in top:
            out.append(Candidate(Path(node.path) / fname, size, detail=f"{_pct(size, node.size)} of {name}",
                                 selected=False, extra={"forget": _forget(dirs, i, size, 1, name=fname)}))
        own = node.size - sum(dirs[k].size for k in node.subdirs)
        rest = own - sum(s for s, _ in top)
        others = node.files - sum(dirs[k].files for k in node.subdirs) - len(top)
        if others > 0:
            out.append(Candidate(Path(node.path), rest, title=f"{others:,} smaller file{'s' if others != 1 else ''}",
                                 detail=f"{_pct(rest, node.size)} of {name}", selected=False,
                                 extra={"locked": True, "location": True}))
        out.sort(key=lambda c: -c.size)
        return out
    return make


def _folder(dirs: list[_Dir], i: int, whole: int, parent_name: str) -> Candidate:
    node = dirs[i]
    p = Path(node.path)
    locked = p.name.lower() in LOCKED or node.parent < 0
    detail = f"{node.files:,} file{'s' if node.files != 1 else ''}"
    if whole:
        detail = f"{_pct(node.size, whole)} of {parent_name} · {detail}"
    if locked and node.parent >= 0:
        detail += " · apps and Windows need this folder as it is"
    extra: dict = {"children": _children(dirs, i) if node.subdirs or node.files else None, "locked": locked,
                   "forget": _forget(dirs, node.parent, node.size, node.files, folder=i) if node.parent >= 0 else None}
    return Candidate(p, node.size, title=p.name or str(p), detail=detail, selected=False, is_dir=True, extra=extra)


def find_folder_sizes(roots: list[Path], progress: Callable | None = None,
                      cancel: threading.Event | None = None) -> ScanResult:
    dirs = _measure(roots, progress, cancel)
    tops = [i for i, d in enumerate(dirs) if d.parent < 0]

    def rows() -> list[Candidate]:
        out = []
        for i in tops:
            root = _folder(dirs, i, 0, "")
            root.detail = f"{dirs[i].files:,} files"
            out.append(root)
        return out

    notes = [] if sum(dirs[i].size for i in tops) else ["These folders are empty."]
    return ScanResult("folders", rows(), notes, cancelled=cancelled(cancel), rebuild=rows)


__all__ = ["find_folder_sizes", "LOCKED"]
