"""Archives that have already been extracted next to themselves.

An archive is only suggested when every file in it exists in an extracted folder
with the same size *and* the same contents (CRC-32 for zip/7z, SHA-256 for tar).
Anything less is listed with a warning and never pre-selected.
"""

from __future__ import annotations

import hashlib
import tarfile
import threading
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .common import Candidate, ProgressFn, ScanResult, cancelled, walk_files

ARCHIVE_EXTS = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")


@dataclass
class Entry:
    path: str          # posix-style relative path inside the archive
    size: int
    crc: int | None    # CRC-32 when the format stores it


def archive_ext(path: Path) -> str | None:
    name = path.name.lower()
    for ext in sorted(ARCHIVE_EXTS, key=len, reverse=True):
        if name.endswith(ext):
            return ext
    return None


def stem(path: Path) -> str:
    ext = archive_ext(path) or path.suffix
    return path.name[: -len(ext)] if ext else path.stem


class Unverifiable(Exception):
    pass


def list_entries(path: Path) -> list[Entry]:
    ext = archive_ext(path)
    if ext == ".zip":
        with zipfile.ZipFile(path) as z:
            out = []
            for i in z.infolist():
                if i.is_dir():
                    continue
                if i.flag_bits & 0x1:
                    raise Unverifiable("password-protected")
                out.append(Entry(i.filename, i.file_size, i.CRC))
            return out
    if ext == ".7z":
        import py7zr

        with py7zr.SevenZipFile(path, "r") as z:
            if z.needs_password():
                raise Unverifiable("password-protected")
            return [Entry(f.filename, f.uncompressed, f.crc32 if f.crc32 is not None else None)
                    for f in z.list() if not f.is_directory]
    if ext == ".rar":
        raise Unverifiable("RAR contents can't be checked without the RAR tools")
    with tarfile.open(path) as t:
        return [Entry(m.name, m.size, None) for m in t.getmembers() if m.isfile()]


def _crc32(path: Path, cancel) -> int | None:
    crc = 0
    try:
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                if cancelled(cancel):
                    return None
                crc = zlib.crc32(chunk, crc)
    except OSError:
        return None
    return crc & 0xFFFFFFFF


def _tar_matches(archive: Path, base: Path, cancel) -> bool:
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            if not m.isfile():
                continue
            if cancelled(cancel):
                return False
            f = t.extractfile(m)
            if f is None:
                return False
            h1, h2 = hashlib.sha256(), hashlib.sha256()
            while chunk := f.read(1 << 20):
                h1.update(chunk)
            try:
                with open(base / Path(*PurePosixPath(m.name).parts), "rb") as g:
                    while chunk := g.read(1 << 20):
                        h2.update(chunk)
            except OSError:
                return False
            if h1.digest() != h2.digest():
                return False
    return True


def _candidate_bases(archive: Path, entries: list[Entry]) -> list[Path]:
    parent, s = archive.parent, stem(archive)
    bases = [parent / s]
    for n in range(1, 4):
        bases.append(parent / f"{s} ({n})")
    tops = {PurePosixPath(e.path).parts[0] for e in entries if len(PurePosixPath(e.path).parts) > 1}
    if len(tops) == 1:  # archive has one top-level folder: it may sit directly next to the archive
        bases.append(parent)
    bases.append(parent)  # extracted in place
    seen, out = set(), []
    for b in bases:
        if b not in seen and b.is_dir():
            seen.add(b)
            out.append(b)
    return out


def _match(base: Path, entries: list[Entry]) -> tuple[int, int]:
    """(entries present with the right size, entries checked)."""
    ok = 0
    for e in entries:
        p = base / Path(*PurePosixPath(e.path).parts)
        try:
            if p.is_file() and p.stat().st_size == e.size:
                ok += 1
        except OSError:
            pass
    return ok, len(entries)


def find_extracted_archives(roots: list[Path], *, progress: ProgressFn | None = None,
                            cancel: threading.Event | None = None) -> ScanResult:
    result = ScanResult("archives")
    archives = [(p, st) for p, st in walk_files(roots, cancel=cancel) if archive_ext(p)]
    for i, (path, st) in enumerate(archives):
        if cancelled(cancel):
            result.cancelled = True
            return result
        if progress:
            progress(f"Checking {path.name}", i, len(archives))
        try:
            entries = list_entries(path)
        except Unverifiable as e:
            bases = [b for b in (path.parent / stem(path),) if b.is_dir()]
            if bases:
                result.items.append(Candidate(path, st.st_size, detail=f"Extracted folder: {bases[0].name}",
                                              warning=f"Can't verify: {e}"))
            continue
        except Exception:  # noqa: BLE001 - damaged archive
            continue
        if not entries:
            continue
        best = None
        for base in _candidate_bases(path, entries):
            ok, total = _match(base, entries)
            if ok and (best is None or ok > best[1]):
                best = (base, ok, total)
        if best is None:
            continue
        base, ok, total = best
        where = base.name if base != path.parent else "this folder"
        if ok < total:
            result.items.append(Candidate(path, st.st_size, detail=f"Extracted into {where}",
                                          warning=f"Only {ok} of {total} files were found extracted"))
            continue
        # Same names and sizes: now prove the contents are identical.
        if archive_ext(path) in (".zip", ".7z"):
            same = True
            for member in entries:
                if member.crc is None:
                    same = False
                    break
                c = _crc32(base / Path(*PurePosixPath(member.path).parts), cancel)
                if c != member.crc:
                    same = False
                    break
        else:
            same = _tar_matches(path, base, cancel)
        if cancelled(cancel):
            result.cancelled = True
            return result
        if same:
            result.items.append(Candidate(path, st.st_size,
                                          detail=f"All {total} file{'s' if total != 1 else ''} are identical in {where}",
                                          extra={"extracted_to": str(base)}))
        else:
            result.items.append(Candidate(path, st.st_size, detail=f"Extracted into {where}",
                                          warning="Some extracted files were changed after extracting"))
    return result
