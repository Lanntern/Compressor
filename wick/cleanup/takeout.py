"""Google Photos Takeout: merge the archives into one folder of photos with their real dates.

A Takeout export is split over many .zip/.tgz files, keeps each photo's date in a separate JSON file (the
files themselves get the export date), repeats every album photo in the year folders, and names the JSON
files inconsistently: ``IMG_1.JPG.json``, ``IMG_1.JPG.supplemental-metadata.json`` (also cut short:
``...supplemen.json``), long names truncated, ``IMG_1(1).JPG`` ↔ ``IMG_1.JPG(1).json``, and ``-edited``
copies sharing the original's JSON.

One pass copies every photo and video into the output folder (Google's folder layout: "Photos from 2019",
albums), computing a checksum on the way, so album copies of a photo become hard links (no extra space).
A second pass matches each file to its JSON and gives it the date the photo was taken. Files are never
re-encoded or rewritten: contents stay byte-identical. Running it again resumes: files already in place
are skipped.
"""

from __future__ import annotations

import functools
import json
import os
import re
import tarfile
import threading
import zipfile
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..formats import classify
from .common import Candidate, ProgressFn, ScanResult, cancelled

ALBUMS = ("link", "copy", "skip")           # album copies: hard links | real copies | leave out
EDITED = ("both", "edited", "original")     # keep both | only the edited version | only the original
_EDITED_SUFFIXES = ("-edited", "-bearbeitet", "-modifié", "-editado", "-modificato", "-bewerkt", "-redigerad",
                    "-redigeret", "-muokattu", "-edytowane", "-upraveno", "-編集済み", "-편집됨", "-已修改")
_YEAR = re.compile(r"^Photos from \d{4}$")
_SUPPLEMENTAL = ".supplemental-metadata"
_NUMBERED = re.compile(r"^(?P<stem>.*?)\((?P<n>\d+)\)(?P<ext>\.[^.]*)$")


# ---------------------------------------------------------------- reading the archives

@dataclass
class Member:
    """One file inside a Takeout: where it is in "Google Photos/", and how to read it."""
    rel: str                       # "Photos from 2019/IMG_1.JPG" (relative to "Google Photos")
    size: int
    open: Callable[[], object]     # -> binary file object
    crc: int | None = None         # zip archives store it; saves reading the file to spot duplicates
    source: str = ""


_UNSAFE_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')     # not allowed in Windows names (":" also means a drive)


def _safe_parts(parts: list[str]) -> list[str] | None:
    """Path parts that stay inside the output folder: "." / ".." refused (an archive could otherwise write
    anywhere), empty parts dropped, characters Windows can't store replaced. None if unsafe or empty."""
    out = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == ".." or part.strip(" .") == "":
            return None
        out.append(_UNSAFE_CHARS.sub("_", part))
    return out or None


def _photos_rel(name: str) -> str | None:
    """The path below "Google Photos" (any language's Takeout folder), or None if it isn't in there (or
    would lead outside it)."""
    parts = name.replace("\\", "/").split("/")
    for i, part in enumerate(parts[:-1]):
        if part in ("Google Photos", "Google Fotos", "Google Foto", "Google Photos ", "Google 相簿", "Google フォト"):
            safe = _safe_parts(parts[i + 1:])
            return "/".join(safe) if safe else None
    return None


def _zip_members(path: Path) -> Iterator[Member]:
    zf = zipfile.ZipFile(path)
    for info in zf.infolist():
        if info.is_dir():
            continue
        rel = _photos_rel(info.filename)
        if rel:
            yield Member(rel, info.file_size, functools.partial(zf.open, info), info.CRC, str(path))


def _tar_members(path: Path) -> Iterator[Member]:
    """.tgz can only be read front to back: each member must be read before moving on."""
    with tarfile.open(path, "r|*") as tf:
        for info in tf:
            if not info.isfile():
                continue
            rel = _photos_rel(info.name)
            if rel:
                f = tf.extractfile(info)
                yield Member(rel, info.size, functools.partial(lambda x: x, f), None, str(path))


def _folder_members(path: Path) -> Iterator[Member]:
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = _photos_rel(os.path.relpath(full, path.parent))
            if rel:
                yield Member(rel, os.path.getsize(full), functools.partial(open, full, "rb"), None, full)


def find_sources(roots: list[Path]) -> list[Path]:
    """Takeout archives (takeout-*.zip / .tgz) and extracted "Takeout" folders under ``roots``."""
    out: list[Path] = []
    for root in roots:
        root = Path(root)
        if root.is_file():
            out.append(root)
            continue
        for dirpath, dirs, files in os.walk(root):
            for name in files:
                low = name.lower()
                if low.startswith("takeout") and low.endswith((".zip", ".tgz", ".tar.gz")):
                    out.append(Path(dirpath) / name)
            for d in list(dirs):
                if d == "Takeout" and any((Path(dirpath) / d / g).is_dir() for g in ("Google Photos", "Google Fotos")):
                    out.append(Path(dirpath) / d)
                    dirs.remove(d)
    return sorted(set(out))


def members(source: Path) -> Iterator[Member]:
    low = source.name.lower()
    if source.is_dir():
        return _folder_members(source)
    if low.endswith(".zip"):
        return _zip_members(source)
    return _tar_members(source)


# ---------------------------------------------------------------- matching photos to their JSON

def _edited_base(name: str) -> str | None:
    """"IMG_1-edited.JPG" → "IMG_1.JPG" (also localised "-bearbeitet"...); None if not an edited copy."""
    stem, ext = os.path.splitext(name)
    for suffix in _EDITED_SUFFIXES:
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)] + ext
    return None


def _json_key(json_name: str) -> str:
    """What a JSON file describes: "IMG_1.JPG.supplemental-meta.json" → "IMG_1.JPG",
    "IMG_1.JPG.supplemental-metadata(1).json" → "IMG_1.JPG(1)" (paired with "IMG_1(1).JPG" by ``find``)."""
    base = json_name[:-5] if json_name.lower().endswith(".json") else json_name
    num = ""
    m = re.match(r"^(.*)\((\d+)\)$", base)
    if m:
        base, num = m.group(1), f"({m.group(2)})"
    dot = base.rfind(".")
    if dot > 0:
        tail = base[dot:].lower()
        if len(tail) >= 2 and _SUPPLEMENTAL.startswith(tail):    # ".supplemental-metadata", or cut short
            base = base[:dot]
    return base + num


class JsonIndex:
    """All JSON sidecars of a folder, and the rules for finding a photo's."""

    def __init__(self):
        self.by_folder: dict[str, dict[str, dict]] = {}

    def add(self, rel: str, data: dict) -> None:
        folder, _, name = rel.rpartition("/")
        self.by_folder.setdefault(folder, {})[_json_key(name)] = data

    def find(self, rel: str) -> dict | None:
        folder, _, name = rel.rpartition("/")
        jsons = self.by_folder.get(folder, {})
        if not jsons:
            return None
        names = [name]
        base = _edited_base(name)
        if base:
            names.append(base)                     # edited copies use the original's JSON
        for n in list(names):
            m = _NUMBERED.match(n)                 # "IMG(1).JPG" ↔ "IMG.JPG(1).json"
            if m:
                names.append(f"{m['stem']}{m['ext']}({m['n']})")
        for n in names:
            if n in jsons:
                return jsons[n]
        for n in names:                            # long names: Google cut the JSON's name short
            for key, data in jsons.items():
                if len(key) >= 20 and n.startswith(key):
                    return data
        for n in names:                            # last resort: the original name recorded inside
            for data in jsons.values():
                if data.get("title") == n:
                    return data
        return None


def taken_time(data: dict) -> float | None:
    for key in ("photoTakenTime", "creationTime"):
        try:
            ts = float(data[key]["timestamp"])
            if ts > 0:
                return ts
        except (KeyError, TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------- scan (what the list shows)

@dataclass
class Summary:
    photos: int = 0
    size: int = 0
    jsons: int = 0
    albums: dict[str, int] = field(default_factory=dict)
    edited: int = 0


def summarise(source: Path, cancel=None) -> Summary:
    s = Summary()
    for m in members(source):
        if cancelled(cancel):
            break
        if m.rel.lower().endswith(".json"):
            s.jsons += 1
            continue
        if classify(Path(m.rel)) is None:
            continue
        s.photos += 1
        s.size += m.size
        folder = m.rel.rpartition("/")[0]
        if folder and not _YEAR.match(folder.split("/")[0]):
            s.albums[folder] = s.albums.get(folder, 0) + 1
        if _edited_base(m.rel.rpartition("/")[2]):
            s.edited += 1
    return s


def find(roots: list[Path], progress: ProgressFn | None = None, cancel: threading.Event | None = None
         ) -> ScanResult:
    result = ScanResult("takeout")
    sources = find_sources(roots)
    for i, src in enumerate(sources):
        if cancelled(cancel):
            result.cancelled = True
            return result
        if progress:
            progress(f"Reading {src.name}", i, len(sources))
        try:
            s = summarise(src, cancel)
        except (OSError, zipfile.BadZipFile, tarfile.TarError) as e:
            result.notes.append(f"{src.name} can't be read ({e}).")
            continue
        if not s.photos:
            continue
        parts = [f"{s.photos:,} photo{'s' if s.photos != 1 else ''} or video{'s' if s.photos != 1 else ''}",
                 f"{s.jsons:,} date file{'s' if s.jsons != 1 else ''}"]
        if s.albums:
            parts.append(f"{len(s.albums)} album{'s' if len(s.albums) != 1 else ''}")
        if s.edited:
            parts.append(f"{s.edited:,} edited versions")
        result.items.append(Candidate(src, s.size, title=src.name, detail=" · ".join(parts), is_dir=src.is_dir(),
                                      group="Takeout", extra={"photos": s.photos, "location": str(src)}))
    if not result.items and not result.cancelled:
        result.notes.append("No Google Photos Takeout found. Add the folder with your takeout-….zip files.")
    return result


# ---------------------------------------------------------------- merge

@dataclass
class MergeResult:
    copied: int = 0          # photos and videos written (new this run)
    already: int = 0         # already in place (a resumed run)
    linked: int = 0          # album copies stored as hard links
    dated: int = 0           # given their taken date
    undated: int = 0         # no JSON found: date left as it was
    removed_edited: int = 0
    written: list[tuple[str, Path]] = field(default_factory=list)   # (source, output) for History


def _write(m: Member, dest: Path) -> int:
    """Copy one member to ``dest`` (through a temporary name) and return its CRC32."""
    from ..fileops import temp_name

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_name(dest.parent, dest.suffix, "-takeout")
    crc = 0
    try:
        with m.open() as src, open(tmp, "wb") as out:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                crc = zlib.crc32(chunk, crc)
                out.write(chunk)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return crc


def _file_crc(path: Path) -> int:
    crc = 0
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            crc = zlib.crc32(chunk, crc)
    return crc


def _set_times(path: Path, ts: float) -> None:
    from ..fileops import _set_windows_creation_time

    os.utime(path, (ts, ts))
    if os.name == "nt":
        _set_windows_creation_time(path, int(ts * 1_000_000_000))


def merge(sources: list[Path], output: Path, *, albums: str = "link", edited: str = "both",
          progress: Callable[[str, int, int], None] | None = None, cancel: threading.Event | None = None
          ) -> MergeResult:
    """Merge Takeout ``sources`` into ``output`` (see the module notes). Safe to run again: resumes."""
    res = MergeResult()
    output.mkdir(parents=True, exist_ok=True)
    out_root = output.resolve()
    index = JsonIndex()
    same: dict[tuple[int, int], list[tuple[Path, bool]]] = {}   # content -> [(output, in an album)]
    outputs: dict[str, Path] = {}                                # rel -> output path
    total = len(sources)
    for si, src in enumerate(sources):
        n = 0
        for m in members(src):
            if cancelled(cancel):
                return res
            if m.rel.lower().endswith(".json"):
                try:
                    with m.open() as f:
                        data = json.loads(f.read().decode("utf-8", "replace"))
                    if isinstance(data, dict):
                        index.add(m.rel, data)
                except (OSError, ValueError):
                    pass
                continue
            if classify(Path(m.rel)) is None:
                continue
            folder = m.rel.split("/")[0] if "/" in m.rel else ""
            in_album = bool(folder) and not _YEAR.match(folder)
            if in_album and albums == "skip":
                continue
            dest = output.joinpath(*m.rel.split("/"))
            if not dest.resolve().is_relative_to(out_root):       # belt and braces: never outside ``output``
                continue
            n += 1
            if progress and n % 20 == 0:
                progress(f"Copying from {src.name}… {n:,}", si, total)
            outputs[m.rel] = dest
            if dest.exists() and dest.stat().st_size == m.size:          # resuming: already in place
                crc = m.crc if m.crc is not None else _file_crc(dest)
                same.setdefault((m.size, crc), []).append((dest, in_album))
                res.already += 1
                continue
            key = (m.size, m.crc) if m.crc is not None else None
            known = same.get(key) if key is not None else None
            if key is not None and known and in_album and albums == "link" and _link(known[0][0], dest):
                same[key].append((dest, in_album))     # the zip's checksum says it's a repeat
                res.linked += 1
                res.written.append((f"{m.source}:{m.rel}", dest))
                continue
            crc = _write(m, dest)
            same.setdefault((m.size, crc), []).append((dest, in_album))
            res.copied += 1
            res.written.append((f"{m.source}:{m.rel}", dest))
    # Album copies that arrived before their year-folder twin: link them now.
    if albums == "link":
        for group in same.values():
            if len(group) < 2:
                continue
            primary = next((d for d, alb in group if not alb), group[0][0])
            for d, alb in group:
                if d == primary or not alb or _same_file(primary, d):
                    continue
                if _relink(primary, d):
                    res.linked += 1
                    res.copied -= 1
    # Dates, and edited versions.
    if progress:
        progress("Setting the dates the photos were taken…", total, total)
    rels = set(outputs)
    removed: set[Path] = set()
    for rel, dest in outputs.items():
        if cancelled(cancel):
            break
        base = _edited_base(rel.rpartition("/")[2])
        original_rel = (rel.rpartition("/")[0] + "/" + base).lstrip("/") if base else None
        if (edited == "edited" and not base and _has_edited(rel, rels)) or \
                (edited == "original" and base and original_rel in rels):
            try:
                os.remove(dest)
                removed.add(dest)
                res.removed_edited += 1
            except OSError:
                pass
            continue
        data = index.find(rel)
        ts = taken_time(data) if data else None
        if ts is None:
            res.undated += 1
            continue
        try:
            _set_times(dest, ts)
            res.dated += 1
        except OSError:
            res.undated += 1
    if removed:
        res.written = [(s_, p) for s_, p in res.written if p not in removed]
    return res


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _relink(primary: Path, dest: Path) -> bool:
    """Replace ``dest`` (identical content) with a hard link to ``primary``."""
    from ..fileops import temp_name

    tmp = temp_name(dest.parent, dest.suffix, "-link")
    try:
        os.link(primary, tmp)
        os.replace(tmp, dest)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _has_edited(rel: str, rels: set[str]) -> bool:
    folder, _, name = rel.rpartition("/")
    stem, ext = os.path.splitext(name)
    prefix = (folder + "/" if folder else "")
    return any(f"{prefix}{stem}{suffix}{ext}" in rels for suffix in _EDITED_SUFFIXES)


def _link(first: Path, dest: Path) -> bool:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.link(first, dest)
        return True
    except OSError:
        return False


__all__ = ["find", "merge", "find_sources", "JsonIndex", "taken_time", "ALBUMS", "EDITED", "MergeResult"]
