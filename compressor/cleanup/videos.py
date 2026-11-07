"""Similar videos: the same video saved more than once as different files (a smaller copy, another resolution
or format, a re-download).

Each video gets a difference hash (as for similar photos) of one frame at each tenth of its length, from
10% to 90%. Two videos of about the same length are the same video when those frames match: most of them
within a few bits, and on average closer still. Nearly uniform frames (black, a title card) don't count.
The copy that stays has the most pixels, then the highest bitrate, then is the largest.
"""

from __future__ import annotations

import os
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import index, tools
from ..formats import VIDEO_EXTS
from .common import Candidate, ProgressFn, ScanResult, cancelled, walk_files
from .similar import _Ticker

POINTS = tuple(i / 10 for i in range(1, 10))
STRICTNESS = {"very": (5, 3.0), "similar": (10, 6.0)}   # (most bits a frame may differ, average)
LENGTH_SLACK = 0.02                                      # share of the length two copies may differ by
_VERSION = 1


def _frame_hash(ffmpeg: str, path: Path, at: float) -> int | None:
    """dHash of the frame at ``at`` seconds; -1 for a nearly uniform frame; None if it can't be read."""
    try:
        p = tools.run([ffmpeg, "-nostdin", "-v", "error", "-ss", f"{at:.3f}", "-i", str(path), "-map", "0:v:0",
                       "-frames:v", "1", "-vf", "scale=9:8:flags=area,format=gray", "-f", "rawvideo", "-"],
                      timeout=60, check=False, text=False)
    except Exception:  # noqa: BLE001
        return None
    px = p.stdout
    if p.returncode != 0 or len(px) < 72:
        return None
    mean = sum(px[:72]) / 72
    if (sum((v - mean) ** 2 for v in px[:72]) / 72) ** 0.5 < 4:
        return -1
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def fingerprint(path: Path) -> dict | None:
    """{"duration", "width", "height", "bit_rate", "codec", "hashes"} or None for unreadable videos."""
    from ..probe import probe_video

    ffmpeg = tools.find_tool("ffmpeg")
    info = probe_video(path) if ffmpeg else None
    if info is None or info.duration < 2 or ffmpeg is None:
        return None
    hashes = []
    for point in POINTS:
        h = _frame_hash(ffmpeg, path, info.duration * point)
        if h is None:
            return None
        hashes.append(h)
    w, h = info.width, info.height
    return {"duration": info.duration, "width": max(w, h), "height": min(w, h), "bit_rate": info.bit_rate,
            "codec": info.codec, "hashes": hashes}


def matches(a: list[int], b: list[int], limit: tuple[int, float]) -> bool:
    most, average = limit
    pairs = [(x, y) for x, y in zip(a, b, strict=False) if x >= 0 and y >= 0]
    if len(pairs) < 5 and len(pairs) < len(a):
        return False                                     # (mostly black frames: nothing to compare)
    if not pairs:
        return False
    dists = sorted((x ^ y).bit_count() for x, y in pairs)
    allowed_bad = len(dists) // 5                        # a scene cut landing differently may miss
    good = dists[: len(dists) - allowed_bad]
    return good[-1] <= most and sum(dists) / len(dists) <= average * 1.5 and sum(good) / len(good) <= average


_CODECS = {"h264": "H.264", "hevc": "HEVC", "av1": "AV1", "vp9": "VP9", "vp8": "VP8", "mpeg4": "MPEG-4",
           "mpeg2video": "MPEG-2", "prores": "ProRes", "wmv3": "WMV", "mjpeg": "Motion JPEG"}


def codec_label(codec: str) -> str:
    return _CODECS.get(codec, codec.upper())


def label(d: dict, path: Path) -> str:
    res = f"{d['height']}p" if d.get("height") else ""
    rate = f"{d['bit_rate'] / 1e6:.1f} Mbps" if d.get("bit_rate") else ""
    return ", ".join(x for x in (res, codec_label(d.get("codec", "")), rate) if x)


def _rank(d: dict, size: int) -> tuple:
    return (d["width"] * d["height"], d.get("bit_rate", 0), size)


def find_similar_videos(roots: list[Path], *, strictness: str = "very", min_size: int = 1024 * 1024,
                        progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> ScanResult:
    result = ScanResult("videos")
    limit = STRICTNESS.get(strictness, STRICTNESS["very"])
    files = []
    for path, st in walk_files(roots, cancel=cancel):
        if path.suffix.lower() in VIDEO_EXTS and st.st_size >= min_size:
            files.append((path, st))
            if progress and len(files) % 200 == 0:
                progress(f"Finding videos… {len(files):,}", len(files), 0)
    if cancelled(cancel):
        result.cancelled = True
        return result

    prints: dict[int, dict] = {}
    todo = []
    for i, (p, st) in enumerate(files):
        cached = index.lookup_value("vhash", p, st.st_size, st.st_mtime)
        if cached is not None and cached.get("v") == _VERSION:
            if cached.get("fp"):
                prints[i] = cached["fp"]
        else:
            todo.append(i)
    ticker = _Ticker(progress)
    done: list[int] = []

    def work(i: int) -> None:
        if cancelled(cancel):
            return
        fp = fingerprint(files[i][0])
        if fp is not None:
            prints[i] = fp
        done.append(i)
        ticker.step("Looking at videos", len(todo))

    with ThreadPoolExecutor(max_workers=min(6, os.cpu_count() or 2)) as pool:
        list(pool.map(work, todo))
    index.store_values("vhash", [(files[i][0], files[i][1].st_size, files[i][1].st_mtime,
                                  {"v": _VERSION, "fp": prints.get(i)}) for i in done])
    if cancelled(cancel):
        result.cancelled = True
        return result

    ids = sorted(prints, key=lambda i: prints[i]["duration"])
    parent = {i: i for i in ids}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for n, a in enumerate(ids):
        da = prints[a]["duration"]
        for b in ids[n + 1:]:
            if prints[b]["duration"] - da > max(1.0, da * LENGTH_SLACK):
                break
            if root(a) != root(b) and matches(prints[a]["hashes"], prints[b]["hashes"], limit):
                parent[root(a)] = root(b)
    linked: dict[int, list[int]] = defaultdict(list)
    for i in ids:
        linked[root(i)].append(i)
    sets = [g for g in linked.values() if len(g) > 1]
    sets.sort(key=lambda g: sum(files[i][1].st_size for i in g), reverse=True)
    for gi, members in enumerate(sets):
        best = max(members, key=lambda i: _rank(prints[i], files[i][1].st_size))
        kp, kst = files[best]
        kq = label(prints[best], kp)
        for i in members:
            if i == best:
                continue
            p, st = files[i]
            q = label(prints[i], p)
            close = matches(prints[i]["hashes"], prints[best]["hashes"], STRICTNESS["very"])
            detail = f"{q}, like the copy that stays" if q == kq else f"{q}; the copy that stays is {kq}"
            result.items.append(Candidate(
                path=p, size=st.st_size, keeper=kp, group=f"vid{gi}", detail=detail,
                selected=close and _rank(prints[i], st.st_size) <= _rank(prints[best], kst.st_size)
                and prints[i]["width"] * prints[i]["height"] < prints[best]["width"] * prints[best]["height"],
                extra={"same_volume": st.st_dev == kst.st_dev, "mtime_ns": st.st_mtime_ns, "label": q,
                       "keeper_label": kq}))
    if sets:
        result.notes.append(f"{len(sets)} video{'s' if len(sets) != 1 else ''} saved more than once. The copy "
                            "with the most pixels stays; right-click to keep a different one.")
    elif not files:
        result.notes.append("No videos here.")
    return result


def group_title(g: str, items: list[Candidate]) -> str:
    keeper = items[0].keeper
    return f"{len(items) + 1} copies of {keeper.name if keeper else 'a video'}"


__all__ = ["find_similar_videos", "fingerprint", "matches", "group_title", "STRICTNESS"]
