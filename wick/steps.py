"""What a file is going through right now ("Encoding JPEG XL", "Checking the result"...).

Deep conversion code calls ``report()``; the job running that file (on that thread) passes it on to the
interface, and keeps the last step so a failure can say where it happened.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

_local = threading.local()

_NAMES = {".jxl": "JPEG XL", ".jpg": "JPEG", ".jpeg": "JPEG", ".heic": "HEIC", ".webp": "WebP", ".avif": "AVIF",
          ".png": "PNG", ".gif": "GIF", ".tif": "TIFF", ".tiff": "TIFF", ".mp4": "MP4", ".mkv": "MKV",
          ".m4a": "AAC", ".opus": "Opus", ".mp3": "MP3", ".flac": "FLAC", ".pdf": "PDF"}


def format_name(ext: str) -> str:
    return _NAMES.get(ext.lower(), ext.lstrip(".").upper())


def report(message: str) -> None:
    from .log import log

    log.info("Stage: %s", message)
    _local.last = message
    cb = getattr(_local, "cb", None)
    if cb is not None:
        try:
            cb(message)
        except Exception:  # noqa: BLE001 - progress is never worth failing a file over
            log.exception("Could not report progress for stage: %s", message)


def last() -> str:
    return getattr(_local, "last", "")


@contextmanager
def reporting(cb: Callable[[str], None] | None) -> Iterator[None]:
    prev_cb, prev_last = getattr(_local, "cb", None), getattr(_local, "last", "")
    _local.cb, _local.last = cb, ""
    try:
        yield
    finally:
        _local.cb, _local.last = prev_cb, prev_last


__all__ = ["report", "last", "reporting", "format_name"]
