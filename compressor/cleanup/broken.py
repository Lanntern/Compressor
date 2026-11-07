"""Broken files: photos, videos, music, archives and PDFs that can't be opened, or whose end is missing
(an interrupted download or copy, a failing disk, a sync that never finished).

Each file is opened the way an app would open it: photos are decoded, archives have their contents checked,
videos and music are opened with ffmpeg and their last seconds played. Only files that can't be opened at all
are pre-selected: a damaged photo usually still shows most of the picture.
"""

from __future__ import annotations

import gzip
import os
import tarfile
import threading
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..formats import AUDIO, AUDIO_EXTS, AVIF, DOCUMENT, HEIC, IMAGE_KINDS, VIDEO, VIDEO_EXTS, _EXT_KIND, sniff
from .common import Candidate, ScanResult, cancelled, walk_files

ZIP_EXTS = {".zip", ".docx", ".docm", ".dotx", ".pptx", ".pptm", ".potx", ".ppsx", ".xlsx", ".xlsm", ".xltx",
            ".odt", ".ods", ".odp", ".odg", ".epub"}
TAR_EXTS = {".tar", ".tgz", ".gz"}
CHECKED = {*_EXT_KIND, *VIDEO_EXTS, *AUDIO_EXTS, *ZIP_EXTS, *TAR_EXTS, ".7z", ".pdf"}
FULL_CHECK_LIMIT = 512 * 1024 * 1024    # archives up to this size have every file's checksum verified

UNREADABLE = "unreadable"               # can't be opened at all: pre-selected
DAMAGED = "damaged"                     # opens, but part of it is missing or wrong

# ffmpeg's words for a file that stops early or has damaged data (seen when playing its end).
_MEDIA_ERRORS = ("partial file", "ended prematurely", "do not match", "packet corrupt", "corrupt input packet",
                 "decode_frame() failed", "decoding error", "invalid data found", "truncat", "error while decoding")


def _zeros(path: Path, size: int) -> bool:
    """Only zero bytes at both ends: space a copy or sync reserved and never filled."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
            if head.strip(b"\0"):
                return False
            if size > 8192:
                f.seek(-4096, os.SEEK_END)
                return not f.read().strip(b"\0")
            return True
    except OSError:
        return False


def _check_image(path: Path, kind: str) -> tuple[str, str] | None:
    from PIL import Image, UnidentifiedImageError

    if kind in (HEIC, AVIF):
        from ..formats import register_heif

        register_heif()
    try:
        im = Image.open(path)
    except UnidentifiedImageError:
        return UNREADABLE, "Not a picture any app can open"
    except (OSError, ValueError, SyntaxError) as e:
        return UNREADABLE, f"Can't be opened ({_short(e)})"
    try:
        with im:
            if im.format == "JPEG":
                im.draft("RGB", (max(1, im.width // 8), max(1, im.height // 8)))   # (reads it all, faster)
            im.load()
            for i in range(1, min(getattr(im, "n_frames", 1), 500)):
                im.seek(i)
                im.load()
    except (OSError, ValueError, SyntaxError, EOFError, MemoryError) as e:
        msg = str(e).lower()
        if "truncated" in msg or "end of file" in msg or isinstance(e, EOFError):
            return DAMAGED, "Part of the picture is missing (the file stops early)"
        return DAMAGED, f"Damaged picture data ({_short(e)})"
    except Image.DecompressionBombError:
        return None
    return None


def _check_zip(path: Path, size: int) -> tuple[str, str] | None:
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if any(i.flag_bits & 0x1 for i in infos):
                return None                                    # (password-protected: can't be checked)
            if size <= FULL_CHECK_LIMIT:
                bad = z.testzip()
                if bad is not None:
                    return DAMAGED, f"Damaged inside: {bad} doesn't match its checksum"
    except zipfile.BadZipFile:
        return UNREADABLE, "Can't be opened: the end of the file is missing or damaged"
    except (OSError, EOFError, ValueError, NotImplementedError, RuntimeError) as e:
        if isinstance(e, NotImplementedError):
            return None                                        # (a compression method Python can't read)
        return DAMAGED, f"Damaged inside ({_short(e)})"
    return None


def _check_7z(path: Path, size: int) -> tuple[str, str] | None:
    try:
        import py7zr
    except ImportError:
        return None
    try:
        with py7zr.SevenZipFile(path, "r") as z:
            if z.needs_password():
                return None
            if size <= FULL_CHECK_LIMIT and z.testzip() is not None:
                return DAMAGED, "Damaged inside: a file doesn't match its checksum"
    except py7zr.exceptions.Bad7zFile:
        return UNREADABLE, "Can't be opened: not a complete 7-Zip archive"
    except (OSError, EOFError, ValueError, KeyError, IndexError, TypeError) as e:
        return DAMAGED, f"Damaged inside ({_short(e)})"
    except Exception:  # noqa: BLE001 - py7zr raises many kinds for unsupported methods: not proof of damage
        return None
    return None


def _check_tar(path: Path, size: int) -> tuple[str, str] | None:
    if size > FULL_CHECK_LIMIT:
        return None
    try:
        if path.suffix.lower() == ".gz" and not path.name.lower().endswith(".tar.gz"):
            with gzip.open(path, "rb") as g:
                while g.read(1 << 20):
                    pass
            return None
        with tarfile.open(path) as t:
            for m in t:
                if m.isfile():
                    f = t.extractfile(m)
                    if f is not None:
                        while f.read(1 << 20):
                            pass
    except (tarfile.ReadError, gzip.BadGzipFile) as e:
        if "empty" in str(e).lower():
            return None
        return UNREADABLE, "Can't be opened: not a complete archive"
    except (EOFError, OSError, tarfile.TarError, ValueError) as e:
        return DAMAGED, f"The end of the archive is missing ({_short(e)})"
    return None


def _check_pdf(path: Path, size: int) -> tuple[str, str] | None:
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
            f.seek(max(0, size - 8192))
            tail = f.read()
    except OSError:
        return None
    if b"%PDF" not in head:
        return UNREADABLE, "Not a PDF any app can open"
    if b"%%EOF" not in tail:
        return DAMAGED, "The end of the PDF is missing (an interrupted download?)"
    return None


def _check_media(path: Path, thorough: bool) -> tuple[str, str] | None:
    from .. import tools

    ffprobe, ffmpeg = tools.find_tool("ffprobe"), tools.find_tool("ffmpeg")
    if not ffprobe or not ffmpeg:
        return None
    try:
        p = tools.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                      timeout=60, check=False)
    except Exception:  # noqa: BLE001 - (timeout, or ffprobe missing): not proof of damage
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return UNREADABLE, "Can't be played: " + (_ff_reason(p.stderr) or "no sound or picture found")
    args = [ffmpeg, "-nostdin", "-v", "warning"] + ([] if thorough else ["-sseof", "-3"]) + \
        ["-i", str(path), "-map", "0:v:0?", "-map", "0:a:0?", "-f", "null", "-"]
    try:
        p = tools.run(args, timeout=3600 if thorough else 60, check=False)
    except Exception:  # noqa: BLE001
        return None
    low = (p.stderr or "").lower()
    if any(w in low for w in _MEDIA_ERRORS):
        where = "has damaged parts" if thorough else "stops early or is damaged near the end"
        return DAMAGED, f"Plays, but {where}"
    return None


def _ff_reason(stderr: str) -> str:
    low = (stderr or "").lower()
    if "moov atom not found" in low:
        return "the end of the file is missing"
    if "invalid data" in low:
        return "the data isn't a video or song"
    return ""


def _short(e: BaseException) -> str:
    s = str(e).strip().rstrip(".") or type(e).__name__
    return s[:80]


def check_file(path: Path, size: int, thorough: bool = False) -> tuple[str, str] | None:
    """(UNREADABLE or DAMAGED, why) for a broken file; None when it's fine or can't be judged."""
    if size == 0:
        return None                                  # (empty files are on the Empty Items tab)
    if _zeros(path, size):
        return UNREADABLE, "Contains nothing but zeros: the copy or download never finished"
    ext = path.suffix.lower()
    found = sniff(path)
    expected = _EXT_KIND.get(ext) or (VIDEO if ext in VIDEO_EXTS else AUDIO if ext in AUDIO_EXTS else
                                      DOCUMENT if ext == ".pdf" else None)
    if found is not None and expected is not None and found[0] != expected \
            and not (found[0] in (VIDEO, AUDIO) and expected in (VIDEO, AUDIO)) \
            and not (found[0] in IMAGE_KINDS and expected in IMAGE_KINDS):
        return None                                  # (another type of file: the Wrong Extensions tab)
    if ext in _EXT_KIND:
        return _check_image(path, found[0] if found else _EXT_KIND[ext])
    if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
        return _check_media(path, thorough)
    if ext == ".pdf":
        return _check_pdf(path, size)
    if ext in ZIP_EXTS:
        return _check_zip(path, size)
    if ext == ".7z":
        return _check_7z(path, size)
    if ext in TAR_EXTS:
        return _check_tar(path, size)
    return None


def find_broken(roots: list[Path], thorough: bool = False, progress: Callable | None = None,
                cancel: threading.Event | None = None) -> ScanResult:
    files = []
    for p, st in walk_files(roots, cancel=cancel):
        if p.suffix.lower() in CHECKED:
            files.append((p, st.st_size))
            if progress and len(files) % 500 == 0:
                progress(f"Found {len(files):,} files to check", 0, 0)
    items: list[Candidate] = []
    done = 0
    lock = threading.Lock()

    def one(entry):
        nonlocal done
        path, size = entry
        if cancelled(cancel):
            return
        try:
            found = check_file(path, size, thorough)
        except Exception:  # noqa: BLE001 - an unexpected error is not proof the file is broken
            found = None
        with lock:
            done += 1
            if found is not None:
                how, why = found
                items.append(Candidate(path, size, detail=why, selected=how == UNREADABLE,
                                       extra={"how": how}))
            if progress and done % 20 == 0:
                progress(f"Checking {path.name}", done, len(files))

    workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, files))
    items.sort(key=lambda c: (c.extra["how"] != UNREADABLE, str(c.path).lower()))
    notes = [] if items else [f"All {len(files):,} files open properly." if files else "Nothing here to check."]
    return ScanResult("broken", items, notes, cancelled=cancelled(cancel))


__all__ = ["find_broken", "check_file", "CHECKED", "UNREADABLE", "DAMAGED"]
