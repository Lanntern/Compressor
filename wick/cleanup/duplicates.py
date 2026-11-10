"""Exact duplicate files: size → partial fingerprint → full checksum."""

from __future__ import annotations

import hashlib
import os
import threading
from collections import defaultdict
from pathlib import Path

from .common import Candidate, ProgressFn, ScanResult, cancelled, walk_files

PARTIAL = 64 * 1024
KEEP_RULES = ("oldest", "newest", "shortest_path", "folder")


def _partial(path: Path, size: int) -> bytes | None:
    try:
        h = hashlib.blake2b(digest_size=16)
        with open(path, "rb") as f:
            h.update(f.read(PARTIAL))
            if size > PARTIAL * 2:
                f.seek(size - PARTIAL)
                h.update(f.read(PARTIAL))
        return h.digest()
    except OSError:
        return None


def _full(path: Path, cancel: threading.Event | None, on_bytes) -> bytes | None:
    try:
        h = hashlib.blake2b(digest_size=32)
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                if cancelled(cancel):
                    return None
                h.update(chunk)
                on_bytes(len(chunk))
        return h.digest()
    except OSError:
        return None


def choose_keeper(paths: list[tuple[Path, os.stat_result]], rule: str, prefer: Path | None = None) -> int:
    """Index of the copy to keep."""
    def shortest(i):
        return (len(str(paths[i][0])), str(paths[i][0]).lower())

    idx = range(len(paths))
    if rule == "folder" and prefer is not None:
        pref = os.path.normcase(str(prefer)).rstrip("\\/") + os.sep
        inside = [i for i in idx if os.path.normcase(str(paths[i][0])).startswith(pref)]
        if inside:
            return min(inside, key=shortest)
        rule = "oldest"
    if rule == "newest":
        return max(idx, key=lambda i: (paths[i][1].st_mtime, -len(str(paths[i][0]))))
    if rule == "shortest_path":
        return min(idx, key=shortest)
    return min(idx, key=lambda i: (paths[i][1].st_mtime, len(str(paths[i][0]))))


def find_duplicates(roots: list[Path], *, min_size: int = 100 * 1024, keep: str = "oldest",
                    prefer: Path | None = None, skip_program_folders: bool = True,
                    progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> ScanResult:
    result = ScanResult("duplicates")
    by_size: dict[int, list[tuple[Path, os.stat_result]]] = defaultdict(list)
    inodes: set[tuple[int, int]] = set()
    n = 0
    for path, st in walk_files(roots, skip_program_folders=skip_program_folders, cancel=cancel):
        n += 1
        if progress and n % 500 == 0:
            progress(f"Looking at files… {n:,}", n, 0)
        if st.st_size < max(1, min_size):
            continue
        by_size[st.st_size].append((path, st))
    if cancelled(cancel):
        result.cancelled = True
        return result

    # Only files sharing a size can be duplicates. For those, get the real file ID and
    # volume (Windows' directory listing reports 0 for both) so names that are already
    # hard links to the same data count once.
    for size in list(by_size):
        group = by_size[size]
        if len(group) < 2:
            continue
        unique = []
        for p, _st in group:
            try:
                real = os.stat(p)
            except OSError:
                continue
            key = (real.st_dev, real.st_ino)
            if real.st_ino and key in inodes:
                continue  # another name for data we already have: no extra space used
            inodes.add(key)
            unique.append((p, real))
        by_size[size] = unique

    # Stage 2: quick fingerprint of the start and end.
    stage2: list[list[tuple[Path, os.stat_result]]] = []
    for size, group in by_size.items():
        if len(group) < 2:
            continue
        sub: dict[bytes, list] = defaultdict(list)
        for p, st in group:
            d = _partial(p, size)
            if d is not None:
                sub[d].append((p, st))
        stage2.extend(g for g in sub.values() if len(g) > 1)

    # Stage 3: full checksum.
    total_bytes = sum(st.st_size for g in stage2 for _, st in g)
    done = 0

    def on_bytes(k: int) -> None:
        nonlocal done
        done += k
        if progress:
            progress("Comparing contents…", done, max(1, total_bytes))

    groups = []
    for g in stage2:
        by_hash: dict[bytes, list] = defaultdict(list)
        for p, st in g:
            d = _full(p, cancel, on_bytes)
            if cancelled(cancel):
                result.cancelled = True
                return result
            if d is not None:
                by_hash[d].append((p, st))
        groups.extend(v for v in by_hash.values() if len(v) > 1)

    groups.sort(key=lambda g: g[0][1].st_size * (len(g) - 1), reverse=True)
    for gi, g in enumerate(groups):
        k = choose_keeper(g, keep, prefer)
        keeper = g[k][0]
        for i, (p, st) in enumerate(g):
            if i == k:
                continue
            result.items.append(Candidate(
                path=p, size=st.st_size, keeper=keeper, group=f"dup{gi}",
                detail="Identical to the copy that stays",
                extra={"same_volume": st.st_dev == g[k][1].st_dev, "mtime_ns": st.st_mtime_ns},
            ))
    if groups:
        result.notes.append(f"{len(groups)} set{'s' if len(groups) != 1 else ''} of identical files")
    return result


def switch_keeper(items: list[Candidate], new_keeper: Path) -> list[Candidate]:
    """The same duplicate set with ``new_keeper`` as the copy that stays (the old keeper becomes removable)."""
    if not items or items[0].keeper is None:
        return items
    old = items[0].keeper
    try:
        kst = os.stat(new_keeper)
    except OSError:
        return items
    from .similar import format_pair, live_companion

    similar = str(items[0].group).startswith("sim")
    # Copies that differ (another encoding of the same song or video) carry a description of each copy.
    labels = {c.path: c.extra["label"] for c in items if "label" in c.extra}
    if labels:
        labels[old] = items[0].extra.get("keeper_label", "")
    out = []
    for c in items:
        path = old if c.path == new_keeper else c.path
        size = c.size
        try:
            st = os.stat(path)
            same, mtime = st.st_dev == kst.st_dev, st.st_mtime_ns
            if similar or labels:
                size = st.st_size          # similar photos and other encodings differ in size
        except OSError:
            same, mtime = c.extra.get("same_volume", True), c.extra.get("mtime_ns", 0)
        checked = True if path == old else c.extra.get("checked", c.selected)
        extra = {"same_volume": same, "mtime_ns": mtime, "checked": checked}
        detail = c.detail
        if labels:
            label, keeper_label = labels.get(path, ""), labels.get(new_keeper, "")
            extra.update(label=label, keeper_label=keeper_label)
            detail = (f"{label}, like the copy that stays" if label == keeper_label else
                      f"{label}; the copy that stays is {keeper_label}")
        elif similar:
            detail = "Similar to the copy that stays"
            if format_pair(path, new_keeper):
                detail = f"The same photo saved as {path.suffix[1:].upper()} and {new_keeper.suffix[1:].upper()}"
            mov = live_companion(path, new_keeper)
            if mov is not None:
                try:
                    size += mov.stat().st_size
                    extra["companions"] = [str(mov)]
                    detail += f". Live Photo: its video ({mov.name}) goes with it"
                except OSError:
                    pass
        out.append(Candidate(path=path, size=size, keeper=new_keeper, group=c.group, detail=detail,
                             selected=c.selected, extra=extra))
    return out
