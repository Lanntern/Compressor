"""Wrong extensions: files whose contents are another type than their name says (a PNG called .jpg, a HEIC
photo called .jpg, an MP4 called .mov's opposite…). Apps that trust the name can refuse to open them."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..formats import AUDIO_EXTS, DOCUMENT_EXTS, VIDEO_EXTS, _EXT_KIND, wrong_extension
from ..scanner import EXCLUDED_DIR_NAMES
from .common import Candidate, ScanResult, cloud_only

CHECKED = {*_EXT_KIND, *VIDEO_EXTS, *AUDIO_EXTS, ".pdf", ".jxl", ".ico"} - (DOCUMENT_EXTS - {".pdf"})
_NAMES = {".jpg": "JPEG", ".png": "PNG", ".gif": "GIF", ".webp": "WebP", ".tif": "TIFF", ".bmp": "BMP",
          ".heic": "HEIC", ".avif": "AVIF", ".jxl": "JPEG XL", ".mp4": "MP4", ".mov": "QuickTime", ".mkv": "MKV",
          ".webm": "WebM", ".avi": "AVI", ".wmv": "WMV", ".m4v": "MP4", ".3gp": "3GP", ".mp3": "MP3",
          ".flac": "FLAC", ".ogg": "Ogg", ".opus": "Opus", ".wav": "WAV", ".m4a": "M4A", ".pdf": "PDF"}


def corrected(path: Path, ext: str) -> Path:
    """``path`` with the right extension, not taking an existing name ("photo (2).png")."""
    target = path.with_suffix(ext)
    n = 2
    while target.exists() and os.path.normcase(str(target)) != os.path.normcase(str(path)):
        target = path.with_name(f"{path.stem} ({n}){ext}")
        n += 1
    return target


def find_wrong_extensions(roots: list[Path], progress: Callable | None = None,
                          cancel: threading.Event | None = None) -> ScanResult:
    stopped = lambda: cancel is not None and cancel.is_set()  # noqa: E731
    # 1. The files to check, from the folder listings alone. Cloud-only files (OneDrive, iCloud...) are left out
    # here: reading even their first bytes would make the cloud app download them.
    files: list[tuple[Path, int]] = []
    stack = list(roots)
    seen = 0
    while stack and not stopped():
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        seen += 1
        if progress and seen % 100 == 0:
            progress(f"Looking in {d}", seen, 0)
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if not e.name.startswith(".") and e.name.lower() not in EXCLUDED_DIR_NAMES:
                        stack.append(Path(e.path))
                    continue
                if os.path.splitext(e.name)[1].lower() not in CHECKED:
                    continue
                st = e.stat(follow_symlinks=False)    # (from the listing on Windows: the file isn't opened)
                if cloud_only(st):
                    continue
                files.append((Path(e.path), st.st_size))
            except OSError:
                continue

    # 2. Their first bytes, read side by side: each read mostly waits on the disk, not the processor.
    def check(item: tuple[Path, int]) -> Candidate | None:
        if stopped():
            return None
        p, size = item
        try:
            real = wrong_extension(p)
        except OSError:
            return None
        if real is None:
            return None
        what = _NAMES.get(real, real.lstrip(".").upper())
        return Candidate(p, 0, detail=f"A {what} file named {p.suffix}: rename to {p.stem}{real}",
                         selected=True, extra={"real_ext": real, "size": size})

    items: list[Candidate] = []
    with ThreadPoolExecutor(max_workers=READERS, thread_name_prefix="sniff") as pool:
        for i, found in enumerate(pool.map(check, files), 1):
            if found is not None:
                items.append(found)
            if progress and i % 200 == 0:
                progress(f"Checked {i:,} of {len(files):,} files", i, len(files))
    notes = [] if items else ["Every file's name matches what it is."]
    return ScanResult("extensions", items, notes, cancelled=stopped())


READERS = 16      # files read at once (fast on SSDs; a hard disk just serves them in turn)


__all__ = ["find_wrong_extensions", "corrected", "CHECKED"]
