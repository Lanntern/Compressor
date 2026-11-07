"""Wrong extensions: files whose contents are another type than their name says (a PNG called .jpg, a HEIC
photo called .jpg, an MP4 called .mov's opposite…). Apps that trust the name can refuse to open them."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

from ..formats import AUDIO_EXTS, DOCUMENT_EXTS, VIDEO_EXTS, _EXT_KIND, wrong_extension
from ..scanner import EXCLUDED_DIR_NAMES
from .common import Candidate, ScanResult

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
    items: list[Candidate] = []
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
                p = Path(e.path)
                if p.suffix.lower() not in CHECKED:
                    continue
                real = wrong_extension(p)
                if real is None:
                    continue
                size = e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            what = _NAMES.get(real, real.lstrip(".").upper())
            items.append(Candidate(p, 0, detail=f"A {what} file named {p.suffix}: rename to {p.stem}{real}",
                                   selected=True, extra={"real_ext": real, "size": size}))
    notes = [] if items else ["Every file's name matches what it is."]
    return ScanResult("extensions", items, notes, cancelled=bool(cancel is not None and cancel.is_set()))


__all__ = ["find_wrong_extensions", "corrected", "CHECKED"]
