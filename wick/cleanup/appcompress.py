"""Transparent WOF compression for programs and games, via Windows' compact.exe.

Files stay where they are and keep working; Windows decompresses them on the fly.
Undo runs ``compact /u``. Windows-only (needs NTFS).
"""

from __future__ import annotations

import os
import subprocess
import threading
import tempfile
import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path

from .caches import steam_libraries
from .common import Candidate, ProgressFn, ScanResult, cancelled
from ..formats import human

IS_WINDOWS = os.name == "nt"
ESTIMATED_RATIO = 0.62   # typical size after LZX for programs/games (shown as an estimate)
ALGORITHMS = {
    "xpress4k": ("XPRESS4K", "Fastest reads; smaller savings. Prefer for loading-sensitive games.", 0.80),
    "xpress8k": ("XPRESS8K", "Balanced reads and savings. Recommended starting point.", 0.74),
    "xpress16k": ("XPRESS16K", "Stronger compression; more work when reading files.", 0.68),
    "lzx": ("LZX", "Most compact; slower compression and more read overhead. Prefer for infrequently used apps.", 0.62),
}
DEFAULT_ALGORITHM = "xpress8k"
DEFAULT_SKIP = ".mp4,.mkv,.webm,.mp3,.ogg,.flac,.jpg,.jpeg,.png,.zip,.7z,.rar,.gz,.bz2,.xz,.bk2,.bik"
_NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def supported() -> bool:
    return IS_WINDOWS


def idle_seconds() -> float:
    if not IS_WINDOWS:
        return 0
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def algorithm(value: str) -> str:
    value = value.lower()
    if value not in ALGORITHMS:
        raise ValueError("Choose XPRESS4K, XPRESS8K, XPRESS16K or LZX")
    return value


def exclusions(value) -> set[str]:
    values = value.replace(";", ",").split(",") if isinstance(value, str) else value
    return {"." + str(v).strip().lower().lstrip(".*") for v in values if str(v).strip().lstrip(".*")}


def compression_records(include_undone: bool = False) -> dict:
    """Oldest measured pre-compression size and latest active settings, from local History."""
    from .. import journal

    records = {}
    for run in sorted(journal.list_runs(), key=lambda r: r.started):
        for rec in sorted(run.files, key=lambda r: r.trashed_at):
            if rec.mode != "compact":
                continue
            key = os.path.normcase(os.path.abspath(rec.original))
            old = records.get(key)
            settings = rec.compression or {"algorithm": "lzx", "skip_extensions": []}
            records[key] = {"path": Path(rec.original), "before": old["before"] if old else rec.original_size,
                            "after": rec.output_size, "when": rec.trashed_at, "settings": settings,
                            "run_id": run.run_id, "active": not rec.undone}
    return records if include_undone else {k: v for k, v in records.items() if v["active"]}


def volume_type(folder: Path) -> str:
    if not IS_WINDOWS:
        return ""
    import ctypes

    root, filesystem = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(64)
    k32 = ctypes.windll.kernel32
    if not k32.GetVolumePathNameW(str(folder.resolve()), root, len(root)):
        return "Unknown"
    if not k32.GetVolumeInformationW(root.value, None, 0, None, None, None, filesystem, len(filesystem)):
        return "Unknown"
    return filesystem.value


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
    ctypes.set_last_error(0)
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


def measure(folder: Path, cancel: threading.Event | None = None, seen: set[str] | None = None,
            by_extension: dict | None = None) -> tuple[int, int, int]:
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
                            if getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
                                continue
                            stack.append(Path(e.path))
                        else:
                            st = e.stat(follow_symlinks=False)
                            from .common import cloud_only

                            if cloud_only(st):
                                continue
                            logical += st.st_size
                            allocated = size_on_disk(Path(e.path))
                            disk += allocated
                            if by_extension is not None:
                                ext = Path(e.name).suffix.lower()
                                old = by_extension.get(ext, (0, 0))
                                by_extension[ext] = (old[0] + st.st_size, old[1] + allocated)
                            count += 1
                            if seen is not None and e.name.lower() in _DIRECTSTORAGE:
                                seen.add(e.name.lower())
                            if seen is not None and Path(e.name).suffix.lower() in (".ucas", ".utoc"):
                                seen.add("streaming-assets")
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
    out += [r["path"] for r in compression_records(True).values() if r["path"].is_dir()]
    return list(dict.fromkeys(out))


def find_compressible(folders: list[Path], progress: ProgressFn | None = None,
                      cancel: threading.Event | None = None, algorithm_name: str = DEFAULT_ALGORITHM,
                      skip_extensions=DEFAULT_SKIP, per_folder: dict | None = None, sample: bool = False) -> ScanResult:
    result = ScanResult("appcompress")
    if not supported():
        result.notes.append("App compression uses NTFS and is only available on Windows.")
        return result
    default = algorithm(algorithm_name)
    tracked = compression_records(True)
    from . import game_results

    community = game_results.load()
    from .inuse import busy_projects

    running = busy_projects(folders)
    for i, folder in enumerate(dict.fromkeys(folders)):
        if cancelled(cancel):
            result.cancelled = True
            break
        if progress:
            progress(f"Measuring {folder.name}", i, len(folders))
        if folder.parent == folder:
            result.notes.append(f"{folder}: choose individual game/app folders, not an entire drive.")
            continue
        names: set[str] = set()
        extensions: dict = {}
        logical, disk, count = measure(folder, cancel, names, extensions)
        if cancelled(cancel):
            result.cancelled = True
            break
        key = os.path.normcase(os.path.abspath(folder))
        if logical < 50 * 1024 * 1024 and key not in tracked:
            continue
        settings = (per_folder or {}).get(key, {})
        selected_algorithm = algorithm(settings.get("algorithm", default))
        skipped = sorted(exclusions(settings.get("skip_extensions", skip_extensions)))
        skipped_logical = sum(extensions.get(ext, (0, 0))[0] for ext in skipped)
        skipped_disk = sum(extensions.get(ext, (0, 0))[1] for ext in skipped)
        public = community.get(game_results.steam_id(folder), {})
        public_mode = public.get("modes", {}).get(selected_algorithm)
        ratio = public_mode["ratio"] if public_mode else ALGORITHMS[selected_algorithm][2]
        estimate_source = (f"community estimate ({public_mode['samples']} samples, game versions may differ)"
                           if public_mode else "rough estimate")
        if sample and not public_mode:
            if progress:
                progress(f"Testing a temporary sample of {folder.name} ({selected_algorithm.upper()})", i, len(folders))
            try:
                ratio, samples = sample_ratio(folder, selected_algorithm, skipped, cancel)
                estimate_source = f"sample estimate ({samples} files; the full game can differ)"
            except Exception as error:
                if cancelled(cancel):
                    result.cancelled = True
                    break
                from ..log import log

                log.warning("Compression sample unavailable for %s: %s", folder, error)
                result.notes.append(f"{folder.name}: sample unavailable ({error}); using a rough estimate.")
        est_after = skipped_disk + int((logical - skipped_logical) * ratio)
        saving = max(0, disk - est_after)
        state = "Already compressed" if disk < logical * 0.8 else "Not compressed"
        if state == "Already compressed" and saving >= logical * 0.05:
            state = "Partly compressed (an update added files)"
        warn = ""
        if names & _DIRECTSTORAGE:
            warn = "DirectStorage DLLs found: may interfere with fast asset loading; compression is not suggested"
        elif "streaming-assets" in names:
            warn = "Streaming asset files found: load-time impact is uncertain; benchmark before keeping compression"
        if folder in running:
            warn = "This game/app is running; close it before compression"
        filesystem = volume_type(folder)
        if filesystem != "NTFS":
            warn = f"{filesystem or 'Unknown filesystem'}: WOF compression requires NTFS"
        history = tracked.get(key)
        if history and not history["active"]:
            state = "Previously compressed here; later decompressed"
        if history and warn == "Already compressed":
            warn = ""
        measured = (f"Before first compression: {human(history['before'])}; " if history else "")
        measured += f"On disk now: {human(disk)}; logical size: {human(logical)}"
        if history:
            measured += f"; last recorded algorithm: {history['settings'].get('algorithm', 'lzx').upper()}"
        writable = os.access(folder, os.W_OK)
        result.items.append(Candidate(
            folder, saving, title=folder.name,
            detail=f"{state} · {count:,} files · {measured} · {selected_algorithm.upper()} · "
                   f"{estimate_source}: {saving / max(1, disk) * 100:.0f}% smaller; actual savings vary"
                   + ("" if writable else " · needs administrator"),
            selected=not bool(history) and saving >= logical * 0.05,
            is_dir=True, group="Games" if "steamapps" in str(folder).lower() or "games" in str(folder).lower()
            else "Programs", warning=warn,
            extra={"logical": logical, "disk": disk, "files": count, "admin": not writable,
                   "algorithm": selected_algorithm, "skip_extensions": skipped,
                   "before_first": history["before"] if history else disk}))
    return result


def _compact_args(folder: Path, undo: bool, algorithm_name: str = DEFAULT_ALGORITHM) -> list[str]:
    mode = ["/u", "/exe"] if undo else ["/c", f"/exe:{algorithm(algorithm_name)}"]
    executable = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "compact.exe") \
        if IS_WINDOWS else "compact.exe"
    return [executable, *mode, f"/s:{folder}", "/i", "/a", "/f"]


def _compact_files(folder, skip, cancel, changed_since=0):
    from .common import walk_files

    for path, st in walk_files([folder], skip_program_folders=False, include_hidden=True, cancel=cancel):
        if path.suffix.lower() in skip:
            continue
        if changed_since and size_on_disk(path) < st.st_size * 0.95:
            continue
        yield path


def sample_ratio(folder: Path, algorithm_name: str, skip_extensions=(), cancel=None) -> tuple[float, int]:
    """Bounded temporary copies, compressed with the chosen WOF algorithm. Originals are never modified."""
    selected = algorithm(algorithm_name)
    paths = []
    for path in _compact_files(folder, exclusions(skip_extensions), cancel):
        try:
            if path.stat().st_size >= 64 * 1024:
                paths.append(path)
            if len(paths) >= 64:
                break
        except OSError:
            continue
    if not paths:
        raise ValueError("No suitable files to sample")
    with tempfile.TemporaryDirectory(prefix="wick-compression-sample-") as directory:
        temporary = Path(directory)
        if volume_type(temporary) != "NTFS":
            raise ValueError("Temporary samples need an NTFS volume")
        if shutil.disk_usage(temporary).free < 64 * 1024 * 1024:
            raise ValueError("Not enough temporary disk space for a sample")
        count = 0
        for path in paths[::max(1, len(paths) // 8)][:8]:
            if cancelled(cancel):
                from ..tools import Cancelled

                raise Cancelled()
            try:
                size = path.stat().st_size
                out = temporary / f"sample-{count}.bin"
                with path.open("rb") as source, out.open("wb") as destination:
                    for at in (0, max(0, size // 2 - 256 * 1024), max(0, size - 512 * 1024)):
                        source.seek(at)
                        destination.write(source.read(min(size, 512 * 1024)))
                count += 1
            except OSError:
                continue
        logical = sum(p.stat().st_size for p in temporary.glob("*.bin"))
        if not logical:
            raise ValueError("Sample files could not be read")
        if run_compact(temporary, algorithm_name=selected, cancel=cancel) != 0:
            raise RuntimeError("Windows could not compress the temporary sample")
        disk = sum(size_on_disk(p) for p in temporary.glob("*.bin"))
        return max(0.01, min(2.0, disk / logical)), count


def _commands(folder, undo, algorithm_name, skip, cancel, changed_since):
    base = [arg for arg in _compact_args(folder, undo, algorithm_name) if not arg.startswith('/s:')]
    batch, length = [], sum(len(a) + 3 for a in base)
    for path in _compact_files(folder, set() if undo else skip, cancel, changed_since):
        name = str(path)
        if batch and (len(batch) >= 64 or length + len(name) + 3 > 7000):
            yield base + batch
            batch, length = [], sum(len(a) + 3 for a in base)
        batch.append(name)
        length += len(name) + 3
    if batch:
        yield base + batch


def _ps(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_compact(folder: Path, undo: bool = False, on_line: Callable[[str], None] | None = None,
                cancel: threading.Event | None = None, elevate: bool = False,
                algorithm_name: str = DEFAULT_ALGORITHM, skip_extensions=(), changed_since: float = 0) -> int:
    """One explicit folder; skip links/cloud files and exclusions. Elevated work prompts once."""
    from .. import tools

    selected = algorithm(algorithm_name)
    folder = folder.resolve()
    if not folder.is_dir():
        raise ValueError('Choose an existing game/app folder')
    protected = [Path(v).resolve() for k in ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData')
                 if (v := os.environ.get(k))]
    if folder.parent == folder or folder in protected:
        raise ValueError('Choose an individual game/app folder, not an entire drive or system location')
    windows = os.environ.get('SystemRoot')
    if windows and folder.is_relative_to(Path(windows).resolve()):
        raise ValueError('Windows system folders cannot be compressed here')
    if volume_type(folder) != 'NTFS':
        raise ValueError('App/game compression requires an NTFS volume')
    commands = _commands(folder, undo, selected, exclusions(skip_extensions), cancel, changed_since)
    if not elevate:
        code = 0
        for command in commands:
            if cancelled(cancel):
                return 1223
            if on_line:
                on_line(f"{'Decompressing' if undo else selected.upper()}: {command[-1]}")
            try:
                with tools.cancelling(cancel or threading.Event()):
                    result = tools.run(command, check=False)
            except tools.Cancelled:
                return 1223
            code = max(code, result.returncode)
            if on_line and result.stdout:
                on_line(result.stdout[-800:])
        return 1223 if cancelled(cancel) else code

    # Only argument data is stored on disk, never executable PowerShell. The fixed command runs bounded batches.
    fd, script_name = tempfile.mkstemp(prefix='wick-compact-', suffix='.json')
    script, marker = Path(script_name), Path(script_name + '.cancel')
    finished = threading.Event()
    watcher = None
    try:
        count = 0
        with os.fdopen(fd, 'w', encoding='utf-8-sig') as handle:
            handle.write('{"batches":[')
            for command in commands:
                if count:
                    handle.write(',')
                json.dump(command[1:], handle)
                count += 1
            handle.write(']}')
        if not count or cancelled(cancel):
            return 1223 if cancelled(cancel) else 0

        def watch():
            while not finished.wait(0.2):
                if cancelled(cancel):
                    marker.touch()
                    return

        watcher = threading.Thread(target=watch, daemon=True, name='compact-cancel')
        watcher.start()
        if on_line:
            on_line('Waiting for Windows administrator compression; Stop takes effect after the current batch')
        executable = _compact_args(folder, undo, selected)[0]
        child = (f"$ErrorActionPreference='Stop'; $failed=0; "
                 f"$data=Get-Content -Raw -LiteralPath {_ps(script)} | ConvertFrom-Json; "
                 f"foreach($batch in $data.batches) {{ if(Test-Path -LiteralPath {_ps(marker)}) {{exit 1223}}; "
                 f"$arguments=@($batch); & {_ps(executable)} @arguments; "
                 "if($LASTEXITCODE -ne 0){$failed=$LASTEXITCODE} }; exit $failed")
        arguments = subprocess.list2cmdline(['-NoProfile', '-Command', child])
        command = (f"$p=Start-Process -FilePath powershell.exe -ArgumentList {_ps(arguments)} "
                   '-Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode')
        return tools.run(['powershell', '-NoProfile', '-Command', command], check=False).returncode
    finally:
        finished.set()
        if watcher is not None:
            watcher.join(timeout=1)
        script.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)


# ---------------------------------------------------------------- keeping games compressed after updates

def compressed_folders() -> list[tuple[Path, float]]:
    """Folders this app compressed (and that weren't decompressed again), with when: from History."""
    return [(r["path"], r["when"]) for r in compression_records().values() if r["path"].is_dir()]


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
                            if getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
                                continue
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


def uncompressed_snapshot(folder: Path, skip_extensions=(), cancel=None) -> tuple[int, str]:
    """Metadata fingerprint of eligible, uncompressed files; handles updates preserving old timestamps."""
    total, signature, count = 0, 0, 0
    for path in _compact_files(folder, exclusions(skip_extensions), cancel):
        try:
            st = path.stat()
            if st.st_size < 64 * 1024 or size_on_disk(path) < st.st_size * 0.95:
                continue
            total += st.st_size
            count += 1
            stamp = f"{path.relative_to(folder)}|{st.st_size}|{st.st_mtime_ns}|{st.st_ctime_ns}"
            signature ^= int.from_bytes(hashlib.blake2b(stamp.encode(), digest_size=16).digest(), "big")
        except OSError:
            continue
    return total, f"{count}:{signature:032x}"


def recompress_updated(min_bytes: int = 200 * 1024 * 1024, progress: Callable[[str], None] | None = None,
                       cancel: threading.Event | None = None, preferences: dict | None = None
                       ) -> tuple[list[tuple[Path, int]], list[str]]:
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
    tracked = compression_records()
    for folder, since in compressed_folders():
        if cancelled(cancel):
            break
        key = os.path.normcase(os.path.abspath(folder))
        saved = {**tracked.get(key, {}).get("settings", {}), **(preferences or {}).get(key, {})}
        if progress:
            progress(f"Checking updated files in {folder.name}")
        amount, fingerprint = uncompressed_snapshot(folder, saved.get("skip_extensions", []), cancel)
        if amount >= min_bytes and fingerprint != saved.get("uncompressed_fingerprint"):
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
                         extra={"logical": logical, "disk": disk, "files": count,
                                **tracked.get(os.path.normcase(os.path.abspath(folder)), {}).get("settings", {}),
                                **(preferences or {}).get(os.path.normcase(os.path.abspath(folder)), {}),
                                "changed_since": tracked.get(os.path.normcase(os.path.abspath(folder)), {}).get("when", 0)})
        o = actions.compact_item(item, writer, cancel=cancel)
        if o.ok and o.freed:
            done.append((folder, o.freed))
    writer.close()
    if not writer.run.files:
        from ..fileops import remove_quietly

        remove_quietly(writer.path)
    return done, notes


