"""Shared types and helpers for the clean-up tools."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..scanner import EXCLUDED_DIR_NAMES, PROGRAM_MARKER_EXTS, _CLOUD_ONLY_ATTRS

ProgressFn = Callable[[str, int, int], None]  # message, done, total


@dataclass
class Candidate:
    """One thing a tool suggests acting on."""
    path: Path
    size: int                      # bytes freed if acted on
    title: str = ""                # what to show (defaults to the file name)
    detail: str = ""               # why it's listed / what happens
    selected: bool = True          # pre-selected? (only when verified safe)
    is_dir: bool = False
    keeper: Path | None = None     # duplicates: the copy that stays
    warning: str = ""              # shown prominently; such items are never pre-selected
    group: str = ""                # items with the same group are shown together
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.title:
            self.title = self.path.name or str(self.path)
        if self.warning:
            self.selected = False


@dataclass
class ScanResult:
    tool: str
    items: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cancelled: bool = False
    rebuild: Callable[[], list[Candidate]] | None = None   # folder sizes: the rows again, after removing some

    @property
    def total(self) -> int:
        return sum(c.size for c in self.items)


def cancelled(cancel: threading.Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def cloud_only(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _CLOUD_ONLY_ATTRS)


def is_hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    if os.name == "nt":
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
            return bool(attrs & 0x2 or attrs & 0x4)
        except (OSError, AttributeError):
            return False
    return False


def walk_files(roots: list[Path], *, skip_program_folders: bool = True, include_hidden: bool = False,
               cancel: threading.Event | None = None, dirs_out: list[Path] | None = None
               ) -> Iterator[tuple[Path, os.stat_result]]:
    """All regular files under ``roots`` (no symlinks, no system/program folders, no cloud-only files)."""
    seen: set[str] = set()
    for root in roots:
        root = Path(os.path.abspath(root))
        if root.is_file():
            try:
                yield root, root.stat()
            except OSError:
                pass
            continue
        stack = [root]
        while stack:
            if cancelled(cancel):
                return
            d = stack.pop()
            key = os.path.normcase(str(d))
            if key in seen:
                continue
            seen.add(key)
            try:
                with os.scandir(d) as it:
                    children = list(it)
            except OSError:
                continue
            if skip_program_folders and d != root and any(
                    os.path.splitext(c.name)[1].lower() in PROGRAM_MARKER_EXTS for c in children):
                continue
            if dirs_out is not None:
                dirs_out.append(d)
            for c in children:
                try:
                    if c.is_symlink():
                        continue
                    if c.is_dir(follow_symlinks=False):
                        if c.name.lower() in EXCLUDED_DIR_NAMES or (not include_hidden and is_hidden(c)):
                            continue
                        stack.append(Path(c.path))
                    elif c.is_file(follow_symlinks=False):
                        if not include_hidden and is_hidden(c):
                            continue
                        st = c.stat(follow_symlinks=False)
                        if cloud_only(st):
                            continue
                        yield Path(c.path), st
                except OSError:
                    continue


def folder_size(path: Path, cancel: threading.Event | None = None) -> tuple[int, int]:
    """(bytes, file count) of everything under ``path`` (symlinks not followed)."""
    total = count = 0
    stack = [path]
    while stack:
        if cancelled(cancel):
            break
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count


def newest_mtime(path: Path, skip_names: set[str] | frozenset[str] = frozenset(), limit: int = 20000) -> float:
    """Most recent modification time inside ``path`` (bounded walk; skips heavy dependency folders)."""
    newest = 0.0
    stack = [path]
    seen = 0
    while stack and seen < limit:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    newest = max(newest, st.st_mtime)
                    if e.is_dir(follow_symlinks=False) and e.name not in skip_names and not e.name.startswith("."):
                        stack.append(Path(e.path))
        except OSError:
            continue
    return newest


def home() -> Path:
    return Path.home()


def known_folder(name: str) -> Path | None:
    """User folders such as Downloads, Pictures, Videos, Documents (Windows or XDG)."""
    if os.name == "nt":
        guids = {"Downloads": "{374DE290-123F-4565-9164-39C4925E467B}"}
        if name in guids:
            try:
                import ctypes
                from ctypes import wintypes

                class GUID(ctypes.Structure):
                    _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                                ("d", ctypes.c_ubyte * 8)]

                import uuid

                u = uuid.UUID(guids[name])
                g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
                ptr = ctypes.c_wchar_p()
                if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(ptr)) == 0 \
                        and ptr.value:
                    path = Path(ptr.value)
                    ctypes.windll.ole32.CoTaskMemFree(ptr)
                    if path.is_dir():
                        return path
            except Exception:  # noqa: BLE001
                pass
    p = home() / name
    return p if p.is_dir() else None
