"""Empty folders and zero-byte files."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .common import Candidate, ProgressFn, ScanResult, cancelled, walk_files

# Zero-byte files that mean something (package markers, placeholders, settings).
KEEP_EMPTY_FILES = {"__init__.py", ".gitkeep", ".keep", ".nomedia", "desktop.ini", ".placeholder", "py.typed",
                    ".npmignore", ".gitignore", "__init__.pyi", "thumbs.db", "lock", ".lock"}
KEEP_EMPTY_EXTS = {".lock", ".pid", ".sock", ".marker", ".flag", ".keep"}


def _real_size(path: Path) -> int | None:
    """Size from the file itself. Directory listings on Windows can say 0 for files that are open for
    writing (logs, databases) or changed through another hard link."""
    try:
        return os.stat(path, follow_symlinks=False).st_size
    except OSError:
        return None


def truly_empty(folder: Path) -> bool:
    """No files at all below ``folder``: hidden and system files count, links and junctions count."""
    stack = [folder]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for c in it:
                    if c.is_symlink() or getattr(c, "is_junction", lambda: False)():
                        return False
                    if c.is_dir(follow_symlinks=False):
                        stack.append(Path(c.path))
                    else:
                        return False
        except OSError:
            return False
    return True


def still_empty(path: Path, is_dir: bool) -> bool:
    """Re-checked right before removal."""
    if is_dir:
        return path.is_dir() and truly_empty(path)
    return _real_size(path) == 0


def find_empty(roots: list[Path], progress: ProgressFn | None = None,
               cancel: threading.Event | None = None) -> ScanResult:
    result = ScanResult("empty")
    dirs: list[Path] = []
    files_in: dict[str, int] = {}
    n = 0
    for path, st in walk_files(roots, cancel=cancel, dirs_out=dirs):
        n += 1
        if progress and n % 500 == 0:
            progress(f"Looking at files… {n:,}", n, 0)
        files_in[os.path.normcase(str(path.parent))] = files_in.get(os.path.normcase(str(path.parent)), 0) + 1
        if st.st_size == 0 and path.name.lower() not in KEEP_EMPTY_FILES \
                and path.suffix.lower() not in KEEP_EMPTY_EXTS and _real_size(path) == 0:
            result.items.append(Candidate(path, 0, detail="Zero-byte file", selected=False, group="Zero-byte files"))
    if cancelled(cancel):
        result.cancelled = True
        return result
    root_keys = {os.path.normcase(os.path.abspath(r)) for r in roots}
    empty: set[str] = set()
    # Deepest first: a folder is empty if it has no files and only empty subfolders.
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        key = os.path.normcase(str(d))
        if key in root_keys or files_in.get(key):
            continue
        try:
            children = list(os.scandir(d))
        except OSError:
            continue
        if all(c.is_dir(follow_symlinks=False) and os.path.normcase(c.path) in empty for c in children):
            empty.add(key)
    # Only list the top-most empty folder of each empty tree.
    by_key = {os.path.normcase(str(d)): d for d in dirs}
    for key in sorted(empty, key=len):
        parent = os.path.dirname(key)
        if parent in empty:
            continue
        p = Path(by_key[key])
        if not truly_empty(p):
            continue
        result.items.append(Candidate(p, 0, detail="Empty folder", is_dir=True, group="Empty folders"))
    return result
