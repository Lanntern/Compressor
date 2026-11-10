"""Walking folders (or taking individual files) and collecting compressible files."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .formats import (
    _EXT_KIND, AUDIO, AUDIO_EXTS, CONVERT_INPUTS, CONVERT_ONLY_KINDS, DOC, DOCUMENT, DOCUMENT_EXTS, HEIC, IMAGE_KINDS,
    ARCHIVE, COMIC, FONT, JXL, PDF, TEMP_PREFIX, VIDEO, VIDEO_EXTS,
    ImageInfo, classify, read_image_info, sniff,
)
from .probe import AudioInfo, VideoInfo, detect_cadence, probe_audio, probe_video
from .settings import Settings

# Folder names that are never scanned (case-insensitive). These hold OS,
# program or game data where converting files would break things.
EXCLUDED_DIR_NAMES = {
    "windows", "program files", "program files (x86)", "programdata", "appdata",
    "$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws",
    "recovery", "steamapps", "steamlibrary", "epic games", "riot games",
    "node_modules", ".git", ".svn", ".hg", "site-packages", "__pycache__",
    ".venv", "venv", ".cache", ".trash",
}

# A folder containing any of these files is treated as a program/game folder
# and skipped together with its subfolders (when skip_program_folders is on).
PROGRAM_MARKER_EXTS = {".dll", ".so", ".dylib"}

# Windows: OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS. Set on cloud-only
# OneDrive/Dropbox placeholders; reading them would download the file.
_CLOUD_ONLY_ATTRS = 0x1000 | 0x40000 | 0x400000


@dataclass
class FileEntry:
    path: Path
    size: int
    kind: str
    mtime: float
    root: Path | None = None        # the folder the user added (for "keep folder structure")
    image: ImageInfo | None = None
    video: VideoInfo | None = None
    audio: AudioInfo | None = None
    live_pair: bool = False         # photo or video half of an Apple Live Photo (same name, .HEIC/.JPG + .MOV)
    real_ext: str = ""              # its contents are another type than its name says (".png" for a PNG named .jpg)
    picked: bool = False            # chosen by itself (dropped or picked, not found in a folder): no size minimums

    @property
    def info(self):
        """The probed details for this kind of file (image, video or audio)."""
        return self.video if self.kind == VIDEO else self.audio if self.kind == AUDIO else self.image


@dataclass
class ScanResult:
    roots: list[Path]
    entries: list[FileEntry] = field(default_factory=list)
    skipped_program_dirs: list[Path] = field(default_factory=list)
    unreadable_dirs: list[Path] = field(default_factory=list)
    cloud_only_files: int = 0
    jxl_files: int = 0          # JPEG XL files seen (already efficient; not listed as entries)
    jxl_bytes: int = 0
    dirs_scanned: int = 0
    cancelled: bool = False

    @property
    def root(self) -> Path:
        return self.roots[0] if self.roots else Path.cwd()


ProgressFn = Callable[[str, int, int], None]  # (message, done, total)


def _is_hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    if os.name == "nt":
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
            return bool(attrs & 0x2 or attrs & 0x4)  # HIDDEN | SYSTEM
        except (OSError, AttributeError):
            return False
    return False


def _cloud_only(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _CLOUD_ONLY_ATTRS)


def scan(
    paths: Path | Iterable[Path],
    settings: Settings,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    probe: bool = True,
    picked: bool = False,
) -> ScanResult:
    """Scan folders and/or individual files. Files given directly are always included; with ``picked`` (someone
    chose them, rather than Watch Folders finding them) the size minimums don't apply to them either."""
    if isinstance(paths, (str, os.PathLike)):
        paths = [Path(paths)]
    roots = [Path(os.path.abspath(p)) for p in paths]
    result = ScanResult(roots=roots)
    seen: set[str] = set()

    for root in roots:
        if root.is_file():
            _add_file(result, root, None, seen, settings)
            if picked and result.entries and result.entries[-1].path == root:
                result.entries[-1].picked = True
        elif root.is_dir():
            listing = _fast_listing(root, progress, cancel)
            if listing is not None:
                walk_listing(result, root, listing, 0, settings, seen)
            else:
                _walk(result, root, settings, progress, cancel, seen)
        if cancel is not None and cancel.is_set():
            result.cancelled = True
            return result

    mark_live_photos(result.entries)
    if probe:
        probe_entries(result.entries, progress, cancel)
        if cancel is not None and cancel.is_set():
            result.cancelled = True
    result.entries.sort(key=lambda e: e.size, reverse=True)
    return result


_LIVE_PHOTO_EXTS = {".heic", ".heif", ".jpg", ".jpeg"}


def mark_live_photos(entries: list[FileEntry]) -> None:
    """Flag Apple Live Photos: a photo and a .MOV with the same name in the same folder belong together
    (Photos apps pair them by name; converting either one breaks the pair)."""
    for e in entries:
        ext = e.path.suffix.lower()
        if ext in _LIVE_PHOTO_EXTS:
            partners = [e.path.with_suffix(x) for x in (".MOV", ".mov")]
        elif ext == ".mov":
            partners = [e.path.with_suffix(x) for x in (".HEIC", ".heic", ".JPG", ".jpg", ".JPEG", ".jpeg")]
        else:
            continue
        e.live_pair = any(p.is_file() for p in partners)


def _kind(p: Path, settings: Settings | None) -> str | None:
    if settings is not None and settings.convert_inputs and p.suffix.lower() == ".pdf":
        return PDF                                    # (Convert: its own kind, with its own choices)
    kind = classify(p)
    if kind is None and settings is not None:
        ext = p.suffix.lower()
        if settings.scan_jxl and ext == ".jxl":
            return JXL
        if settings.convert_inputs:
            from .archives import archive_ext

            return CONVERT_INPUTS.get(ext) or (ARCHIVE if archive_ext(p) else None)
    return kind


def _add_file(result: ScanResult, p: Path, root: Path | None, seen: set[str], settings: Settings | None = None) -> None:
    key = os.path.normcase(str(p))
    if key in seen or p.name.startswith(TEMP_PREFIX):
        return
    kind = _kind(p, settings)
    if kind is None:
        return
    try:
        st = p.stat()
    except OSError:
        return
    if _cloud_only(st):
        result.cloud_only_files += 1
        return
    seen.add(key)
    result.entries.append(FileEntry(p, st.st_size, kind, st.st_mtime, root))


# Every extension the scanner lists (and JPEG XL, counted as already efficient): what to ask Everything for.
_LISTED_EXTS = ({*_EXT_KIND, *VIDEO_EXTS, *AUDIO_EXTS, *DOCUMENT_EXTS, *CONVERT_INPUTS, ".zip", ".7z", ".tar", ".tgz",
                 ".gz", ".tbz2", ".bz2", ".txz", ".xz"})

FAST_DRIVES = True   # Settings › "Fast whole-drive scanning" (Windows, NTFS: reads the drive's file table)


def _is_drive_root(p: Path) -> bool:
    return os.name == "nt" and p.anchor != "" and Path(p.anchor) == p


def _big_scope(root: Path) -> bool:
    """Worth asking Everything: a whole drive or your whole user folder (where it saves minutes). A smaller
    folder is read directly: quick anyway, and certain to include a file saved a moment ago (Everything's
    index can trail the disk by a moment)."""
    try:
        return _is_drive_root(root) or os.path.normcase(os.path.abspath(root)) == \
            os.path.normcase(os.path.abspath(Path.home()))
    except OSError:
        return False


def _fast_listing(root: Path, progress: ProgressFn | None, cancel: threading.Event | None):
    """Windows: the list of files without asking the disk about every one. From Everything when it's running
    (any folder, instant), else for a whole drive from its NTFS file table (Windows asks for administrator
    permission). None: scan the usual way."""
    if os.name == "nt" and _big_scope(root):
        from . import everything

        try:
            listing = everything.listing_for(root, _LISTED_EXTS, progress)
        except Exception:  # noqa: BLE001 - Everything's answer is a bonus: any problem, the usual way
            listing = None
        if listing is not None:
            return listing
    if not (FAST_DRIVES and _is_drive_root(root)):
        return None
    from . import ntfs
    import tempfile

    if progress:
        progress(f"Reading the file table of {root} (Windows asks for permission)…", 0, 0)
    return ntfs.listing_for([root], lambda n: classify(Path(n)) is not None or n.lower().endswith(".jxl"),
                            Path(tempfile.gettempdir()), cancel)


def walk_listing(result: ScanResult, root: Path, listing, root_index: int, settings: Settings,
                 seen: set[str]) -> None:
    """The same rules as _walk, over a drive's file table (ntfs.Listing) instead of the disk."""
    from .ntfs import CLOUD_ONLY, HIDDEN, REPARSE, SYSTEM

    top = listing.roots.get(root_index)
    if top is None:
        return
    stack = [(top, root)]
    while stack:
        d, path = stack.pop()
        result.dirs_scanned += 1
        _parent, _attrs, marker, _name = listing.dirs[d]
        if settings.skip_program_folders and marker:
            result.skipped_program_dirs.append(path)
            continue
        subdirs = []
        for sub in listing.subdirs.get(d, []):
            _p, sattrs, _m, sname = listing.dirs[sub]
            if sattrs & REPARSE or sname.lower() in EXCLUDED_DIR_NAMES:
                continue
            if not settings.include_hidden and (sname.startswith(".") or sattrs & (HIDDEN | SYSTEM)):
                continue
            subdirs.append((sname, sub))
        for size, mtime, fattrs, fname in listing.files.get(d, []):
            if fname.startswith(TEMP_PREFIX) or fattrs & REPARSE:
                continue
            if not settings.include_hidden and (fname.startswith(".") or fattrs & (HIDDEN | SYSTEM)):
                continue
            p = path / fname
            kind = _kind(p, settings)
            if kind is None:
                if fname.lower().endswith(".jxl"):
                    result.jxl_files += 1
                    result.jxl_bytes += size
                continue
            if fattrs & CLOUD_ONLY:
                result.cloud_only_files += 1
                continue
            key = os.path.normcase(str(p))
            if key in seen:
                continue
            seen.add(key)
            result.entries.append(FileEntry(p, size, kind, mtime, root))
        stack.extend((sub, path / name) for name, sub in sorted(subdirs, reverse=True))


def _walk(result: ScanResult, root: Path, settings: Settings, progress: ProgressFn | None,
          cancel: threading.Event | None, seen: set[str]) -> None:
    stack = [root]
    while stack:
        if cancel is not None and cancel.is_set():
            return
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                children = list(it)
        except OSError:
            result.unreadable_dirs.append(d)
            continue
        result.dirs_scanned += 1

        if settings.skip_program_folders and any(
            c.is_file(follow_symlinks=False)
            and os.path.splitext(c.name)[1].lower() in PROGRAM_MARKER_EXTS
            for c in children
        ):
            result.skipped_program_dirs.append(d)
            continue

        subdirs = []
        for c in children:
            try:
                if c.is_symlink():
                    continue
                if c.is_dir(follow_symlinks=False):
                    if c.name.lower() in EXCLUDED_DIR_NAMES:
                        continue
                    if not settings.include_hidden and _is_hidden(c):
                        continue
                    subdirs.append(Path(c.path))
                    continue
                if not c.is_file(follow_symlinks=False) or c.name.startswith(TEMP_PREFIX):
                    continue
                if not settings.include_hidden and _is_hidden(c):
                    continue
                p = Path(c.path)
                kind = _kind(p, settings)
                if kind is None:
                    if c.name.lower().endswith(".jxl"):
                        result.jxl_files += 1
                        result.jxl_bytes += c.stat(follow_symlinks=False).st_size
                    continue
                st = c.stat(follow_symlinks=False)
                if _cloud_only(st):
                    result.cloud_only_files += 1
                    continue
                key = os.path.normcase(c.path)
                if key in seen:
                    continue
                seen.add(key)
                result.entries.append(FileEntry(p, st.st_size, kind, st.st_mtime, root))
            except OSError:
                continue
        stack.extend(sorted(subdirs, reverse=True))
        if progress and result.dirs_scanned % 25 == 0:
            progress(f"Scanning {d}", result.dirs_scanned, 0)


_preloaded = False
_heif_loaded = False


def preload_codecs(heif: bool = True) -> None:
    """Import image libraries on the calling thread before worker threads use them.

    Importing them for the first time from several threads at once can deadlock
    in a frozen (PyInstaller) app. HEIC support (which brings a large x265 library) only loads when
    HEIC files are involved.
    """
    global _preloaded, _heif_loaded
    if not _preloaded:
        _preloaded = True
        from PIL import Image

        Image.init()
        for mod in ("mozjpeg_lossless_optimization", "oxipng", "imagequant"):
            try:
                __import__(mod)
            except Exception:  # noqa: BLE001 - reported when a file actually needs it
                pass
    if heif and not _heif_loaded:
        _heif_loaded = True
        from .formats import register_heif

        register_heif()


def probe_entries(entries: list[FileEntry], progress: ProgressFn | None = None,
                  cancel: threading.Event | None = None) -> None:
    """Read image headers and video info (in parallel)."""
    from . import index

    todo = []
    for e in entries:
        if e.info is not None or e.kind in (DOCUMENT, DOC, PDF, FONT, COMIC, ARCHIVE):     # (nothing to read ahead)
            continue
        cached = index.lookup(e.path, e.size, e.mtime, e.kind)
        if cached is None:
            todo.append(e)
        elif e.kind == VIDEO:
            e.video = cached
        elif e.kind == AUDIO:
            e.audio = cached
        else:
            e.image = cached
    if not todo:
        return
    preload_codecs(heif=any(e.kind == HEIC for e in entries))
    done = 0
    lock = threading.Lock()

    def work(e: FileEntry) -> None:
        nonlocal done
        if cancel is not None and cancel.is_set():
            return
        if e.kind not in (VIDEO, AUDIO, *CONVERT_ONLY_KINDS):   # (a RAW photo is a TIFF inside, and so on)
            # Images are named by whoever saved them (phones save HEIC as .jpg, sites PNGs as .jpg): read what
            # the file really is, so it's decoded and compressed as that.
            real = sniff(e.path)
            if real is not None and real[0] != e.kind and real[0] in (*IMAGE_KINDS, VIDEO, AUDIO):
                e.kind, e.real_ext = real
        if e.kind == VIDEO:
            e.video = probe_video(e.path)
            if e.video is not None:
                e.video.cadence = detect_cadence(e.path, e.video)
        elif e.kind == AUDIO:
            e.audio = probe_audio(e.path)
        else:
            e.image = read_image_info(e.path, e.kind)
        with lock:
            done += 1
            if progress and (done % 20 == 0 or e.kind in (VIDEO, AUDIO)):
                progress(f"Reading file info: {e.path.name}", done, len(todo))

    with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2))) as pool:
        list(pool.map(work, todo))
    index.store([(e.path, e.size, e.mtime, e.kind, e.info) for e in todo if e.info is not None])
