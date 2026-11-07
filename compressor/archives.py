"""Repacking archives (ZIP, 7-Zip, TAR) into 7-Zip or a better-compressed ZIP. Only when asked for: it's an
option on Convert, never a default anywhere, because it reads and writes the whole archive for a saving that's
usually small (most archives hold already-compressed photos and videos).

The archive is unpacked into a private temporary folder (every name checked to stay inside it; links skipped),
packed again, and every file's checksum compared with the new archive's before it's used.
Password-protected archives are left alone.
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import threading
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path, PurePosixPath

ARCHIVE_EXTS = (".zip", ".7z", ".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz")


def archive_ext(path: Path) -> str:
    name = path.name.lower()
    return next((e for e in sorted(ARCHIVE_EXTS, key=len, reverse=True) if name.endswith(e)), "")


def _safe(root: Path, name: str) -> Path | None:
    """Where ``name`` goes inside ``root``, or None for a name that would land outside it."""
    parts = [p for p in PurePosixPath(name.replace("\\", "/")).parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or parts[0].endswith(":") or name.startswith(("/", "\\")):
        return None
    target = root.joinpath(*parts)
    return target if os.path.commonpath([root, target]) == str(root) else None


def _unpack(src: Path, root: Path, cancel: threading.Event | None) -> dict[str, int]:
    """Every file into ``root``: {archive name: CRC-32}."""
    ext = archive_ext(src)
    crcs: dict[str, int] = {}

    def put(name: str, data: bytes, mtime: float | None) -> None:
        if cancel is not None and cancel.is_set():
            raise InterruptedError()
        target = _safe(root, name)
        if target is None:
            raise ValueError(f"The archive holds a name that points outside it ({name}): not repacked")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if mtime:
            os.utime(target, (mtime, mtime))
        crcs[target.relative_to(root).as_posix()] = zlib.crc32(data) & 0xFFFFFFFF

    if ext == ".zip":
        with zipfile.ZipFile(src) as z:
            if any(i.flag_bits & 0x1 for i in z.infolist()):
                raise ValueError("The archive has a password: left as it is")
            import time

            for i in z.infolist():
                if not i.is_dir():
                    put(i.filename, z.read(i), time.mktime((*i.date_time, 0, 0, -1)))
    elif ext == ".7z":
        import py7zr

        with py7zr.SevenZipFile(src) as z:
            if z.needs_password():
                raise ValueError("The archive has a password: left as it is")
            names = [f.filename for f in z.list() if not f.is_directory]
            for name, data in (z.read(names) or {}).items():
                put(name, data.read(), None)
    else:
        with tarfile.open(src) as t:
            for m in t.getmembers():
                if m.isfile():
                    f = t.extractfile(m)
                    if f is not None:
                        put(m.name, f.read(), m.mtime)
    return crcs


def repack(src: Path, dest: Path, fmt: str, progress: Callable[[float], None] | None = None,
           cancel: threading.Event | None = None) -> None:
    """``src`` packed again as ``fmt`` ("7z": LZMA2 at its strongest; "zip": Deflate at level 9) at ``dest``."""
    from .images import VerificationFailed

    work = Path(tempfile.mkdtemp(prefix="compressor-repack-"))
    try:
        if progress:
            progress(0.1)
        crcs = _unpack(src, work, cancel)
        if not crcs:
            raise ValueError("The archive is empty")
        if progress:
            progress(0.5)
        files = sorted(crcs)
        if fmt == "7z":
            import py7zr

            with py7zr.SevenZipFile(dest, "w", filters=[{"id": py7zr.FILTER_LZMA2, "preset": 9}]) as z:
                for name in files:
                    z.write(work / name, name)
            with py7zr.SevenZipFile(dest) as z:
                got = {f.filename: f.crc32 for f in z.list() if not f.is_directory}
        else:
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
                for name in files:
                    z.write(work / name, name)
            with zipfile.ZipFile(dest) as z:
                got = {i.filename: i.CRC for i in z.infolist() if not i.is_dir()}
        if got != crcs:
            raise VerificationFailed("The new archive doesn't hold the same files")
        if progress:
            progress(1.0)
    finally:
        shutil.rmtree(work, ignore_errors=True)


__all__ = ["repack", "archive_ext", "ARCHIVE_EXTS"]
