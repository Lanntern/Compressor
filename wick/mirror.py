"""A smaller copy of a music library (for a phone or a car), kept in sync.

Lossless files (FLAC, WAV, ALAC...) become Opus, AAC or MP3; lossy files (MP3, AAC...) are copied as they
are, because encoding them again only loses quality. Tags and album art come along, and the folder layout
stays the same; cover.jpg/folder.jpg images are copied too. The library itself is never changed.

Running it again only does what changed: new or edited songs, a different format or quality. Copies whose
song was removed from the library are removed from the copy, but only files this app made (they're listed
in ``.wick-sync.json`` in the copy) are ever touched there.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import tools
from .formats import AUDIO, classify
from .settings import Settings

MANIFEST = ".wick-sync.json"
FORMATS = ("opus", "aac", "mp3")
QUALITIES = ("high", "balanced", "small")
_COVERS = {"cover.jpg", "cover.png", "folder.jpg", "folder.png", "front.jpg", "front.png", "albumart.jpg"}


@dataclass
class SyncResult:
    encoded: int = 0
    copied: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)   # (song, why)
    library_bytes: int = 0
    copy_bytes: int = 0


def _ext(fmt: str) -> str:
    from .audio import FORMATS as AF

    return AF[fmt][0]


def _load(dest: Path) -> dict:
    try:
        data = json.loads((dest / MANIFEST).read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(dest: Path, manifest: dict) -> None:
    tmp = dest / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=0, sort_keys=True), "utf-8")
    os.replace(tmp, dest / MANIFEST)


def _inside(a: Path, b: Path) -> bool:
    try:
        Path(os.path.abspath(a)).relative_to(os.path.abspath(b))
        return True
    except ValueError:
        return False


def sync(library: Path, dest: Path, fmt: str = "opus", quality: str = "balanced",
         progress: Callable[[str, int, int], None] | None = None, cancel: threading.Event | None = None,
         workers: int = 0) -> SyncResult:
    """Bring the copy in ``dest`` up to date with ``library`` (see the module notes)."""
    from .audio import _cover_metadata, build_args, is_lossless, verify_audio
    from .fileops import remove_quietly, temp_name
    from .probe import probe_audio

    library, dest = Path(os.path.abspath(library)), Path(os.path.abspath(dest))
    if fmt not in FORMATS or quality not in QUALITIES:
        raise ValueError("Unknown format or quality")
    if _inside(dest, library) or _inside(library, dest):
        raise ValueError("The copy must be in a different folder than the library, not inside it")
    dest.mkdir(parents=True, exist_ok=True)
    old = _load(dest)
    entries: dict[str, dict] = old.get("files", {}) if old.get("format") == fmt and old.get("quality") == quality \
        else {}
    previous: dict[str, dict] = old.get("files", {})          # everything we made before, to tidy up
    s = Settings(audio_quality=quality)
    res = SyncResult()

    # What the library holds.
    songs: list[tuple[str, Path, os.stat_result]] = []
    covers: list[tuple[str, Path, os.stat_result]] = []
    for dirpath, dirs, files in os.walk(library):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            p = Path(dirpath) / name
            rel = p.relative_to(library).as_posix()
            try:
                st = p.stat()
            except OSError:
                continue
            if name.lower() in _COVERS:
                covers.append((rel, p, st))
            elif classify(p) == AUDIO:
                songs.append((rel, p, st))
                res.library_bytes += st.st_size

    lock = threading.Lock()
    done = [0]
    total = len(songs) + len(covers)
    new_entries: dict[str, dict] = {}

    def up_to_date(rel: str, st: os.stat_result) -> bool:
        e = entries.get(rel)
        return e is not None and e.get("size") == st.st_size and e.get("mtime_ns") == st.st_mtime_ns \
            and (dest / e["out"]).is_file()

    def tick(what: str) -> None:
        with lock:
            done[0] += 1
            if progress:
                progress(what, done[0], total)

    def one(item: tuple[str, Path, os.stat_result], is_cover: bool) -> None:
        rel, src, st = item
        if cancel is not None and cancel.is_set():
            return
        if up_to_date(rel, st):
            with lock:
                new_entries[rel] = entries[rel]
                res.unchanged += 1
            tick(f"Up to date: {src.name}")
            return
        info = None if is_cover else probe_audio(src)
        if not is_cover and info is None:
            with lock:
                res.failed.append((rel, "can't be read"))
            tick(src.name)
            return
        encode = not is_cover and is_lossless(info.codec)
        out_rel = str(Path(rel).with_suffix(_ext(fmt)).as_posix()) if encode else rel
        out = dest / out_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = temp_name(out.parent, out.suffix, "-sync")
        meta = None
        try:
            if encode:
                assert info is not None           # (only covers have none, and they're copied)
                meta = _cover_metadata(src, info, out.parent) if fmt == "opus" else None
                tools.run(build_args(src, tmp, info, s, fmt, False, meta), timeout=3600)
                verify_audio(src, info, tmp, fmt, False, exact=False)
            else:
                shutil.copyfile(src, tmp)
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(tmp, out)
        except Exception as e:  # noqa: BLE001 - one bad song doesn't stop the rest
            remove_quietly(tmp)
            with lock:
                res.failed.append((rel, str(e)[:200]))
            tick(src.name)
            return
        finally:
            remove_quietly(meta)
        with lock:
            new_entries[rel] = {"out": out_rel, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
            if encode:
                res.encoded += 1
            else:
                res.copied += 1
        tick(f"{'Encoded' if encode else 'Copied'} {src.name}")

    n = workers or max(1, min(8, (os.cpu_count() or 2)))
    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(lambda it: one(it, False), songs))
        list(pool.map(lambda it: one(it, True), covers))

    # Tidy up: copies we made whose song is gone (or that a new format replaced). Never anything else.
    keep = {e["out"] for e in new_entries.values()}
    if not (cancel is not None and cancel.is_set()):
        emptied: set[Path] = set()
        for e in previous.values():
            out_rel = e.get("out", "")
            target = dest / out_rel
            if out_rel and out_rel not in keep and _inside(target, dest) and target.is_file():
                try:
                    target.unlink()
                    res.removed += 1
                    emptied.add(target.parent)
                except OSError:
                    pass
        for folder in emptied:   # only folders a removed copy was in, up to the copy's top folder
            while folder != dest and _inside(folder, dest):
                try:
                    folder.rmdir()
                except OSError:
                    break
                folder = folder.parent
    else:   # stopped: keep what's known so the next run continues
        for rel, e in entries.items():
            new_entries.setdefault(rel, e)
    _save(dest, {"format": fmt, "quality": quality, "library": str(library), "files": new_entries})
    for e in new_entries.values():
        try:
            res.copy_bytes += (dest / e["out"]).stat().st_size
        except OSError:
            pass
    return res


__all__ = ["sync", "SyncResult", "FORMATS", "QUALITIES", "MANIFEST"]
