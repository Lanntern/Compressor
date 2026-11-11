"""Carrying out clean-up actions, journaled so History can undo them."""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import cfa, journal
from ..fileops import BIN_NAME, copy_times, move_to_trash, remove_quietly, temp_name, trash_available
from . import appcompress
from .common import Candidate

TRASH, HARDLINK, COMPACT, TAKEOUT, RENAME = "trash", "hardlink", "compact", "takeout", "rename"


@dataclass
class Outcome:
    item: Candidate
    ok: bool
    message: str
    freed: int = 0
    trashed_at: float = 0.0      # when it went to the Recycle Bin (to find it there again)
    blocked: bool = False        # refused by Windows' Controlled folder access (explained once, not per file)


def _failed(item: Candidate, e: BaseException, what: str, path: Path | None = None) -> Outcome:
    """A failure, told as Controlled folder access when that's what refused it."""
    if cfa.blocked(e, path or item.path):
        return Outcome(item, False, cfa.SHORT, blocked=True)
    return Outcome(item, False, f"{what}: {e}")


def _record(writer: journal.RunWriter, item: Candidate, mode: str, method: str, freed: int,
            trashed_at: float, output: str = "") -> None:
    writer.add(journal.FileRecord(
        original=str(item.path), output=output, original_size=freed, output_size=0, kind="cleanup",
        method=method, reversible=False, trashed_at=trashed_at, original_mtime_ns=item.extra.get("mtime_ns", 0),
        mode=mode))


def trash_item(item: Candidate, writer: journal.RunWriter, label: str) -> Outcome:
    if not item.path.exists():
        return Outcome(item, False, "No longer exists")
    if not trash_available(item.path):
        return Outcome(item, False, f"No {BIN_NAME} on this drive")
    at = time.time()
    try:
        move_to_trash(item.path, item.size)
    except Exception as e:  # noqa: BLE001 - e.g. a file in use
        return _failed(item, e, f"Couldn't move to the {BIN_NAME}")
    main_size = item.size
    moved_with = []
    for extra_path in item.extra.get("companions", []):     # a Live Photo's video goes with its photo
        cp = Path(extra_path)
        try:
            csize = cp.stat().st_size
            move_to_trash(cp, csize)
        except Exception:  # noqa: BLE001 - the photo is gone already; the video simply stays
            continue
        main_size -= csize
        moved_with.append((cp, csize))
    _record(writer, item, "removed", label, max(0, main_size), at)
    for cp, csize in moved_with:
        _record(writer, Candidate(cp, csize), "removed", label + " (Live Photo video)", csize, at)
    return Outcome(item, True, f"Moved to the {BIN_NAME}", item.size, at)


def docker_item(item: Candidate, writer: journal.RunWriter) -> Outcome:
    """Docker data goes through Docker (it can't go to the Recycle Bin); recorded, but not undoable."""
    from . import docker

    ok, msg = docker.remove(item.extra["docker"])
    if not ok:
        return Outcome(item, False, msg)
    writer.add(journal.FileRecord(
        original=f"Docker: {item.title}", output="", original_size=item.size, output_size=0, kind="cleanup",
        method="Removed by Docker", reversible=False, trashed_at=time.time(), mode="docker"))
    return Outcome(item, True, msg, item.size)


def trash_paths(item: Candidate, writer: journal.RunWriter, label: str) -> Outcome:
    """An item made of many files or folders (old temporary files): each goes to the Recycle Bin; ones in use
    are skipped."""
    at = time.time()
    moved = freed = skipped = 0
    for raw in item.extra.get("paths", []):
        p = Path(raw)
        try:
            size = p.stat().st_size if p.is_file() else 0
            if p.is_dir():
                from .common import folder_size

                size = folder_size(p)[0]
            if not p.exists() or not trash_available(p):
                continue
            move_to_trash(p, size)
        except Exception:  # noqa: BLE001 - in use, or gone
            skipped += 1
            continue
        _record(writer, Candidate(p, size), "removed", label, size, at)
        moved += 1
        freed += size
    if not moved:
        return Outcome(item, False, "Nothing could be moved (all in use)")
    return Outcome(item, True, f"Moved {moved:,} to the {BIN_NAME}" + (f", {skipped:,} in use" if skipped else ""),
                   freed, at)


def windows_items(items: list[Candidate], writer: journal.RunWriter, cancel=None) -> list[Outcome]:
    """Windows' own leftovers, removed by Windows in one batch (one administrator prompt)."""
    import shutil

    from . import winclean

    drive = str(items[0].path.anchor or "C:\\")
    try:
        before = shutil.disk_usage(drive).free
    except OSError:
        before = None
    ok, msg = winclean.run_system([c.extra["system"] for c in items], cancel)
    if not ok:
        return [Outcome(c, False, msg) for c in items]
    freed_total = 0
    if before is not None:
        try:
            freed_total = max(0, shutil.disk_usage(drive).free - before)
        except OSError:
            pass
    out = []
    measured = sum(c.size for c in items)
    for c in items:
        # Windows doesn't say what each step freed: share what the drive gained by each item's measured size
        # (items Windows sizes itself, like update leftovers, get the remainder).
        share = c.size if freed_total >= measured else int(freed_total * c.size / max(1, measured))
        if not c.size:
            share = max(0, freed_total - measured)
        compact_os = c.extra.get("system") == "compact_os"
        writer.add(journal.FileRecord(
            original=f"Windows: {c.title}", output="", original_size=share, output_size=0, kind="cleanup",
            method="Compressed by Windows (Compact OS)" if compact_os else "Removed by Windows",
            reversible=compact_os, trashed_at=time.time(), mode="compactos" if compact_os else "system"))
        out.append(Outcome(c, True, "Removed by Windows", share))
    return out


def takeout_items(items: list[Candidate], writer: journal.RunWriter, cancel=None, on_line=None) -> list[Outcome]:
    """Merge Google Takeout archives into one folder (all of them together: a photo's date can be in another
    archive). Every file written is recorded, so History can remove the merged copy again; the archives
    themselves are never changed."""
    from . import takeout

    first = items[0].extra
    out = Path(first["takeout_out"])
    try:
        res = takeout.merge([c.path for c in items], out, albums=first.get("albums", "link"),
                            edited=first.get("edited", "both"),
                            progress=(lambda msg, d, t: on_line(msg)) if on_line else None, cancel=cancel)
    except Exception as e:  # noqa: BLE001 - e.g. a damaged archive
        return [Outcome(c, False, f"Couldn't merge: {e}") for c in items]
    at = time.time()
    for source, dest in res.written:
        try:
            size = dest.stat().st_size
        except OSError:
            continue
        writer.add(journal.FileRecord(original=source, output=str(dest), original_size=0, output_size=size,
                                      kind="cleanup", method="Google Takeout → merged folder", reversible=True,
                                      trashed_at=at, mode="copy"))
    msg = (f"{res.copied + res.already:,} photos and videos, {res.dated:,} with the date they were taken"
           + (f", {res.linked:,} album copies linked" if res.linked else "")
           + (f", {res.undated:,} without a date file" if res.undated else ""))
    outs = [Outcome(c, True, msg, 0) for c in items]
    for o in outs:
        o.extra = res     # type: ignore[attr-defined]  # (the GUI shows the summary once)
    return outs


def empty_from_trash(outcomes: list[Outcome], progress: Callable[[str, int, int], None] | None = None,
                     cancel: threading.Event | None = None) -> tuple[int, int, list[str]]:
    """Permanently delete just the items these outcomes moved to the Recycle Bin, leaving everything else in
    it alone. Returns (items deleted, bytes freed, problems)."""
    from .. import trash_restore

    todo = [o for o in outcomes if o.ok and o.trashed_at]
    count = freed = 0
    problems: list[str] = []
    for i, o in enumerate(todo):
        if cancel is not None and cancel.is_set():
            break
        if progress:
            progress(f"Freeing {o.item.title}", i, len(todo))
        paths = [Path(p) for p in o.item.extra.get("paths", [])] or [o.item.path]
        paths += [Path(p) for p in o.item.extra.get("companions", [])]
        gone = 0
        for path in paths:
            found = trash_restore.find_trashed(path, o.trashed_at)
            if found is None:
                continue
            try:
                trash_restore.purge(found)
                gone += 1
            except OSError as e:
                problems.append(f"{path.name}: {e}")
        if not gone:
            problems.append(f"{o.item.title}: no longer in the {BIN_NAME}")
            continue
        count += 1
        freed += o.freed
    return count, freed, problems


def hardlink_item(item: Candidate, writer: journal.RunWriter) -> Outcome:
    """Replace a duplicate with a hard link to its keeper (same contents, stored once)."""
    keeper, dup = item.keeper, item.path
    if keeper is None or not keeper.is_file() or not dup.is_file():
        return Outcome(item, False, "File no longer exists")
    try:
        ks, ds = keeper.stat(), dup.stat()
    except OSError as e:
        return Outcome(item, False, str(e))
    if ks.st_dev != ds.st_dev:
        return Outcome(item, False, "On a different drive: hard links only work within one drive")
    if ks.st_size != ds.st_size:
        return Outcome(item, False, "Files changed since the scan")
    from ..fileops import clone_file, supports_block_clone

    tmp = temp_name(dup.parent, dup.suffix, "-link")
    # ReFS (e.g. a Dev Drive): a block clone stores the data once too, but stays a separate file, so changing
    # one copy never changes the other (as it would with a hard link).
    cloned = False
    if supports_block_clone(dup):
        try:
            clone_file(keeper, tmp)
            os.utime(tmp, ns=(ds.st_atime_ns, ds.st_mtime_ns))
            os.replace(tmp, dup)
            cloned = True
        except OSError:
            remove_quietly(tmp)                 # (fall back to a hard link)
    if not cloned:
        try:
            os.link(keeper, tmp)
            os.replace(tmp, dup)
        except OSError as e:
            remove_quietly(tmp)
            return _failed(item, e, "Couldn't create a hard link")
    item.extra["mtime_ns"] = ds.st_mtime_ns
    how = "block clone of" if cloned else "hard link to"
    _record(writer, item, HARDLINK, f"Duplicate → {how} {keeper.name}", item.size, time.time(), str(keeper))
    return Outcome(item, True, "Now a block clone (stored once, still a separate file)" if cloned
                   else "Now a hard link (stored once)", item.size)


def compact_item(item: Candidate, writer: journal.RunWriter, on_line=None,
                 cancel: threading.Event | None = None) -> Outcome:
    before = item.extra.get("disk") or appcompress.measure(item.path)[1]
    code = appcompress.run_compact(item.path, False, on_line, cancel, elevate=item.extra.get("admin", False))
    after = appcompress.measure(item.path)[1]
    freed = max(0, before - after)
    if code != 0 and freed == 0:
        return Outcome(item, False, f"compact failed (exit {code})" + (" or was cancelled" if cancel and
                                                                       cancel.is_set() else ""))
    writer.add(journal.FileRecord(
        original=str(item.path), output=str(item.path), original_size=before, output_size=after, kind="cleanup",
        method="Compressed with Windows LZX (transparent)", reversible=True, trashed_at=time.time(), mode=COMPACT))
    return Outcome(item, True, "Compressed", freed)


def _still_empty(item: Candidate) -> bool:
    from .empties import still_empty

    return still_empty(item.path, item.is_dir)


def apply(items: list[Candidate], action: str, tool: str, root: Path,
          progress: Callable[[Outcome, int, int], None] | None = None,
          cancel: threading.Event | None = None, on_line=None) -> tuple[list[Outcome], str | None]:
    writer = journal.RunWriter(root, {"tool": tool, "action": action})
    outcomes: list[Outcome] = []
    labels = {"duplicates": f"Duplicate → {BIN_NAME}", "archives": f"Extracted archive → {BIN_NAME}",
              "caches": f"Cache → {BIN_NAME}", "devclean": f"Rebuildable folder → {BIN_NAME}",
              "installers": f"Old installer → {BIN_NAME}", "empty": f"Empty item → {BIN_NAME}",
              "similar": f"Similar photo → {BIN_NAME}", "winclean": f"Windows leftover → {BIN_NAME}",
              "large": f"Large file → {BIN_NAME}"}
    if action == TAKEOUT:
        outcomes = takeout_items(items, writer, cancel, on_line)
        if progress:
            for i, o in enumerate(outcomes):
                progress(o, i + 1, len(outcomes))
        writer.close()
        if not writer.run.files:
            remove_quietly(writer.path)
            return outcomes, None
        return outcomes, writer.run.run_id
    disks = [c for c in items if c.extra.get("vdisk")]
    if disks and action == TRASH and not (cancel is not None and cancel.is_set()):
        from . import vdisk

        for c, ok, msg, freed in vdisk.compact(disks, on_line, cancel):
            if ok:
                writer.add(journal.FileRecord(
                    original=f"Virtual disk: {c.title}", output=str(c.path), original_size=freed, output_size=0,
                    kind="cleanup", method="Compacted (nothing inside changed)", reversible=False,
                    trashed_at=time.time(), mode="vdisk"))
            o = Outcome(c, ok, msg, freed)
            outcomes.append(o)
            if progress:
                progress(o, len(outcomes), len(items))
        items = [c for c in items if not c.extra.get("vdisk")]
    system = [c for c in items if c.extra.get("system")]
    if system and action == TRASH and not (cancel is not None and cancel.is_set()):
        for o in windows_items(system, writer, cancel):
            outcomes.append(o)
            if progress:
                progress(o, len(outcomes), len(items))
    items = [c for c in items if not c.extra.get("system")]
    done_before = len(outcomes)
    for i, item in enumerate(items):
        if cancel is not None and cancel.is_set():
            break
        if item.extra.get("paths") is not None and action == TRASH:
            o = trash_paths(item, writer, labels.get(tool, f"Moved to the {BIN_NAME}"))
            outcomes.append(o)
            if progress:
                progress(o, done_before + i + 1, done_before + len(items))
            continue
        if action == RENAME:
            o = rename_item(item, writer)
        elif action == HARDLINK:
            o = hardlink_item(item, writer)
        elif action == COMPACT:
            o = compact_item(item, writer, on_line, cancel)
        elif item.extra.get("docker"):
            o = docker_item(item, writer)
        elif tool == "empty" and not _still_empty(item):
            o = Outcome(item, False, "Not empty any more; left alone")
        else:
            o = trash_item(item, writer, labels.get(tool, f"Moved to the {BIN_NAME}"))
        outcomes.append(o)
        if progress:
            progress(o, done_before + i + 1, done_before + len(items))
    writer.close()
    if not writer.run.files:
        remove_quietly(writer.path)
        return outcomes, None
    return outcomes, writer.run.run_id


def rename_item(item: Candidate, writer: journal.RunWriter) -> Outcome:
    """Give a file the extension that matches its contents (recorded, so it can be renamed back)."""
    from .extensions import corrected

    real = item.extra.get("real_ext")
    if not real or not item.path.is_file():
        return Outcome(item, False, "No longer exists")
    target = corrected(item.path, real)
    try:
        os.rename(item.path, target)
    except OSError as e:
        return _failed(item, e, "Couldn't rename")
    writer.add(journal.FileRecord(
        original=str(item.path), output=str(target), original_size=0, output_size=0, kind="cleanup",
        method=f"Renamed to {target.name}", reversible=True, trashed_at=0.0, mode="renamed"))
    return Outcome(item, True, f"Renamed to {target.name}")


def undo_rename(rec: journal.FileRecord) -> str:
    original, now = Path(rec.original), Path(rec.output)
    if not now.exists():
        raise FileNotFoundError(f"{now.name} isn't there any more")
    if original.exists():
        raise FileExistsError(f"Something else is called {original.name} now")
    os.rename(now, original)
    return f"Named {original.name} again"


# ------------------------------------------------------------------ undo (called from engine.undo_record)

def undo_hardlink(rec: journal.FileRecord) -> str:
    """Give the path its own copy again (contents are identical, so this is lossless)."""
    path, keeper = Path(rec.original), Path(rec.output)
    source = keeper if keeper.is_file() else path
    if not source.is_file():
        raise FileNotFoundError("Neither copy exists any more")
    tmp = temp_name(path.parent, path.suffix, "-unlink")
    try:
        shutil.copy2(source, tmp)
        if rec.original_mtime_ns:
            os.utime(tmp, ns=(rec.original_mtime_ns, rec.original_mtime_ns))
        os.replace(tmp, path)
    finally:
        remove_quietly(tmp)
    return "Separate copy restored"


def undo_compact(rec: journal.FileRecord) -> str:
    folder = Path(rec.original)
    if not folder.is_dir():
        raise FileNotFoundError("Folder no longer exists")
    admin = not os.access(folder, os.W_OK)
    code = appcompress.run_compact(folder, undo=True, elevate=admin)
    if code != 0:
        raise RuntimeError(f"compact /u failed (exit {code})")
    return "Decompressed"


__all__ = ["apply", "empty_from_trash", "takeout_items", "Outcome", "TRASH", "HARDLINK", "COMPACT", "undo_hardlink",
           "undo_compact", "copy_times"]
