"""Wrong extensions: files whose contents are another type than their name says (a PNG called .jpg, a HEIC
photo called .jpg, an MP4 called .mov's opposite…). Apps that trust the name can refuse to open them."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..formats import AUDIO_EXTS, DOCUMENT_EXTS, VIDEO_EXTS, _EXT_KIND, wrong_extension
from .common import Candidate, ScanResult, publish, walk_files

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
    # Check bounded batches as folders are discovered, rather than waiting for the entire drive listing.
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
    checked = 0
    with ThreadPoolExecutor(max_workers=READERS, thread_name_prefix="sniff") as pool:
        batch = []
        last_flush = time.monotonic()

        def flush():
            nonlocal checked, last_flush
            for found in pool.map(check, batch):
                checked += 1
                if found is not None:
                    items.append(found)
                    publish("extensions", [found])
            batch.clear()
            last_flush = time.monotonic()
            if progress:
                progress(f"Checked {checked:,} files; looking for more", checked, 0)

        for path, st in walk_files(roots, cancel=cancel):
            if path.suffix.lower() in CHECKED:
                batch.append((path, st.st_size))
                if len(batch) >= 128:
                    flush()
            if batch and time.monotonic() - last_flush >= 0.2:
                flush()
        if batch:
            flush()
    notes = [] if items else ["Every file's name matches what it is."]
    return ScanResult("extensions", items, notes, cancelled=stopped())


READERS = 16      # files read at once (fast on SSDs; a hard disk just serves them in turn)


__all__ = ["find_wrong_extensions", "corrected", "CHECKED"]

