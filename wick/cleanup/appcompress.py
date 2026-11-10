"""Transparent NTFS compression (LZX) for programs and games, via Windows' compact.exe.

Files stay where they are and keep working; Windows decompresses them on the fly.
Undo runs ``compact /u``. Windows-only (needs NTFS).
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from .caches import steam_libraries
from .common import Candidate, ProgressFn, ScanResult, cancelled

IS_WINDOWS = os.name == "nt"
ESTIMATED_RATIO = 0.62   # typical size after LZX for programs/games (shown as an estimate)
_NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def supported() -> bool:
    return IS_WINDOWS


def size_on_disk(path: Path) -> int:
    """Allocated bytes for one file (compressed size when NTFS-compressed)."""
    if not IS_WINDOWS:
        try:
            return path.stat().st_blocks * 512
        except (OSError, AttributeError):
            return 0
    import ctypes
    from ctypes import wintypes

    high = wintypes.DWORD(0)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCompressedFileSizeW.restype = wintypes.DWORD
    low = k32.GetCompressedFileSizeW(str(path), ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.get_last_error() != 0:
        try:
            return path.stat().st_size
        except OSError:
            return 0
    return (high.value << 32) + low


# A game that streams its files with DirectStorage reads them straight from the drive (and decompresses on
# the GPU): Windows' file compression puts the CPU back in that path, so loading gets slower.
_DIRECTSTORAGE = {"dstorage.dll", "dstoragecore.dll"}


def measure(folder: Path, cancel: threading.Event | None = None, seen: set[str] | None = None
            ) -> tuple[int, int, int]:
    """(logical bytes, bytes on disk, file count). ``seen`` collects lower-case file names (to look for
    tell-tale files such as DirectStorage's)."""
    logical = disk = count = 0
    stack = [folder]
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
                            logical += e.stat(follow_symlinks=False).st_size
                            disk += size_on_disk(Path(e.path))
                            count += 1
                            if seen is not None and e.name.lower() in _DIRECTSTORAGE:
                                seen.add(e.name.lower())
                    except OSError:
                        continue
        except OSError:
            continue
    return logical, disk, count


def default_folders() -> list[Path]:
    """Installed games and programs that usually compress well."""
    out: list[Path] = []
    for lib in steam_libraries():
        common = lib / "steamapps" / "common"
        if common.is_dir():
            out += [p for p in common.iterdir() if p.is_dir()]
    if IS_WINDOWS:
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env)
            if base:
                epic = Path(base) / "Epic Games"
                if epic.is_dir():
                    out += [p for p in epic.iterdir() if p.is_dir() and p.name != "Launcher"]
                gog = Path(base) / "GOG Galaxy" / "Games"
                if gog.is_dir():
                    out += [p for p in gog.iterdir() if p.is_dir()]
    return out


def find_compressible(folders: list[Path], progress: ProgressFn | None = None,
                      cancel: threading.Event | None = None) -> ScanResult:
    result = ScanResult("appcompress")
    if not supported():
        result.notes.append("App compression uses NTFS and is only available on Windows.")
        return result
    for i, folder in enumerate(folders):
        if cancelled(cancel):
            result.cancelled = True
            break
        if progress:
            progress(f"Measuring {folder.name}", i, len(folders))
        names: set[str] = set()
        logical, disk, count = measure(folder, cancel, names)
        if logical < 50 * 1024 * 1024:
            continue
        est_after = int(logical * ESTIMATED_RATIO)
        saving = max(0, disk - est_after)
        state = "Already compressed" if disk < logical * 0.8 else "Not compressed"
        if state == "Already compressed" and saving >= logical * 0.05:
            state = "Partly compressed (an update added files)"
        warn = "Already compressed" if saving < logical * 0.05 else ""
        if names & _DIRECTSTORAGE:
            warn = "Uses DirectStorage: compressing it would make loading slower"
        writable = os.access(folder, os.W_OK)
        result.items.append(Candidate(
            folder, saving, title=folder.name,
            detail=f"{state} · {count:,} files · about {saving / max(1, disk) * 100:.0f}% smaller"
                   + ("" if writable else " · needs administrator"),
            is_dir=True, group="Games" if "steamapps" in str(folder).lower() or "games" in str(folder).lower()
            else "Programs", warning=warn,
            extra={"logical": logical, "disk": disk, "files": count, "admin": not writable}))
    return result


def _compact_args(folder: Path, undo: bool) -> list[str]:
    mode = ["/u", "/exe"] if undo else ["/c", "/exe:lzx"]
    return ["compact.exe", *mode, f"/s:{folder}", "/i", "/a", "/f"]


def run_compact(folder: Path, undo: bool = False, on_line: Callable[[str], None] | None = None,
                cancel: threading.Event | None = None, elevate: bool = False) -> int:
    """Compress (or decompress) every file under ``folder``. Returns the exit code."""
    args = _compact_args(folder, undo)
    if elevate:
        # One UAC prompt; output isn't visible from an elevated child.
        ps_args = ",".join(f"'{a}'" for a in args[1:])
        cmd = ["powershell", "-NoProfile", "-Command",
               f"$p = Start-Process -FilePath compact.exe -ArgumentList {ps_args} -Verb RunAs -WindowStyle Hidden "
               f"-Wait -PassThru; exit $p.ExitCode"]
        return subprocess.run(cmd, creationflags=_NOWINDOW).returncode
    proc = subprocess.Popen(args, cwd=str(folder), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=_NOWINDOW, text=True, errors="replace")
    assert proc.stdout is not None
    for line in proc.stdout:
        if cancelled(cancel):
            proc.kill()
            break
        if on_line:
            on_line(line.rstrip())
    return proc.wait()


# ---------------------------------------------------------------- keeping games compressed after updates

def compressed_folders() -> list[tuple[Path, float]]:
    """Folders this app compressed (and that weren't decompressed again), with when: from History."""
    from .. import journal

    latest: dict[str, tuple[Path, float]] = {}
    for run in journal.list_runs():
        for rec in run.files:
            if rec.mode != "compact":
                continue
            key = os.path.normcase(rec.original)
            if rec.undone:
                latest.pop(key, None)
            elif key not in latest or latest[key][1] < rec.trashed_at:
                latest[key] = (Path(rec.original), rec.trashed_at)
    return [v for v in latest.values() if v[0].is_dir()]


def uncompressed_since(folder: Path, since: float, cancel: threading.Event | None = None) -> int:
    """Bytes in files changed after ``since`` that aren't compressed (what an update added). Files Windows
    can't compress well are small anyway; anything at 95 % or more of its size counts as uncompressed."""
    total = 0
    stack = [folder]
    while stack:
        if cancelled(cancel):
            break
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                            continue
                        st = e.stat(follow_symlinks=False)
                        if st.st_mtime <= since or st.st_size < 64 * 1024:
                            continue
                        if size_on_disk(Path(e.path)) >= st.st_size * 0.95:
                            total += st.st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def recompress_updated(min_bytes: int = 200 * 1024 * 1024, progress: Callable[[str], None] | None = None,
                       cancel: threading.Event | None = None) -> tuple[list[tuple[Path, int]], list[str]]:
    """Compress again the games this app compressed that an update has since added files to. Skips games
    that are running, use DirectStorage, or need administrator rights (no prompts in the background).
    Returns ([(folder, bytes freed)], [notes])."""
    from .. import journal
    from . import actions
    from .inuse import busy_projects

    done: list[tuple[Path, int]] = []
    notes: list[str] = []
    if not supported():
        return done, notes
    todo = []
    for folder, since in compressed_folders():
        if cancelled(cancel):
            break
        if uncompressed_since(folder, since, cancel) >= min_bytes:
            todo.append(folder)
    if not todo:
        return done, notes
    busy = busy_projects(todo)
    writer = journal.RunWriter(todo[0], {"tool": "appcompress", "action": actions.COMPACT, "automatic": True})
    for folder in todo:
        if cancelled(cancel):
            break
        names: set[str] = set()
        logical, disk, count = measure(folder, cancel, names)
        if folder in busy:
            notes.append(f"{folder.name} is running; it'll be compressed another time")
            continue
        if names & _DIRECTSTORAGE or not os.access(folder, os.W_OK):
            continue
        if progress:
            progress(f"Compressing {folder.name} again after an update")
        item = Candidate(folder, 0, title=folder.name, is_dir=True,
                         extra={"logical": logical, "disk": disk, "files": count})
        o = actions.compact_item(item, writer, cancel=cancel)
        if o.ok and o.freed:
            done.append((folder, o.freed))
    writer.close()
    if not writer.run.files:
        from ..fileops import remove_quietly

        remove_quietly(writer.path)
    return done, notes

