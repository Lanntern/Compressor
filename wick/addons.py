"""Tools that are downloaded only when first needed, instead of making every install bigger.

Pandoc (document conversion) is 236 MB unpacked: on Windows it's downloaded the first time documents are
converted, after asking. The version and its SHA-256 are pinned here, so nothing else is ever installed; it
goes in this app's data folder, which ``tools.find_tool`` also looks in. Elsewhere Pandoc comes from the
system's package manager.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Addon:
    tool: str
    version: str
    url: str
    sha256: str
    size: int                  # bytes to download
    member: str                # the program inside the archive
    license: str               # its license file inside the archive
    what: str                  # what it's for, in a sentence


PANDOC = Addon(
    "pandoc", "3.12.1",
    "https://github.com/jgm/pandoc/releases/download/3.12.1/pandoc-3.12.1-windows-x86_64.zip",
    "6ef431cb20b2c24a0371fc19200dc49d452be54b1b5c9239f3462841ef53e587", 42_154_914,
    "pandoc-3.12.1/pandoc.exe", "pandoc-3.12.1/COPYRIGHT.txt",
    "Pandoc converts documents (Word, OpenDocument, Markdown, web pages, EPUB, RTF). It's free software "
    "(GPL) by John MacFarlane.")


def folder() -> Path:
    from .journal import data_dir

    d = data_dir() / "tools"
    d.mkdir(parents=True, exist_ok=True)
    return d


def can_download(addon: Addon) -> bool:
    """Only the Windows build is downloaded (elsewhere it comes from the system's packages)."""
    return os.name == "nt" and os.environ.get("WICK_NO_NETWORK") != "1"


def install_hint(addon: Addon) -> str:
    if os.name == "nt":
        return f"Wick can download {addon.tool.capitalize()} for you ({addon.size / 1e6:.0f} MB)."
    if sys.platform == "darwin":
        return f"Install it with Homebrew (brew install {addon.tool}), then add the files again."
    return f"Install it from your distribution's packages (for example: sudo apt install {addon.tool}), then " \
           "add the files again."


class Cancelled(Exception):
    pass


def download(addon: Addon, progress: Callable[[int, int], None] | None = None,
             cancel: threading.Event | None = None, url: str | None = None) -> Path:
    """Download, check the fingerprint, and unpack the program. Returns its path."""
    import tempfile
    import urllib.request

    from . import tools

    dest = folder()
    exe = dest / Path(addon.member).name
    fd, tmp_name = tempfile.mkstemp(prefix=".download-", suffix=".zip", dir=dest)
    tmp = Path(tmp_name)
    try:
        h = hashlib.sha256()
        done = 0
        req = urllib.request.Request(url or addon.url, headers={"User-Agent": "Wick"})
        with os.fdopen(fd, "wb") as f, urllib.request.urlopen(req, timeout=60) as r:
            while chunk := r.read(1 << 20):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, addon.size)
        if h.hexdigest() != addon.sha256:
            raise ValueError(f"The download of {addon.tool} isn't the expected file (its fingerprint differs), so "
                             "it wasn't used.")
        with zipfile.ZipFile(tmp) as z:
            part = exe.with_name(exe.name + ".part")
            with z.open(addon.member) as src, open(part, "wb") as out:
                while chunk := src.read(1 << 20):
                    out.write(chunk)
            os.replace(part, exe)
            try:
                (dest / f"LICENSE-{addon.tool}.txt").write_bytes(z.read(addon.license))
            except KeyError:
                pass
        tools.find_tool.cache_clear()
        return exe
    finally:
        tmp.unlink(missing_ok=True)


__all__ = ["PANDOC", "Addon", "download", "folder", "can_download", "install_hint", "Cancelled"]
