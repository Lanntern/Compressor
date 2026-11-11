"""Similar photos: burst shots, re-saved and resized copies, near-duplicates.

Each photo gets a 64-bit difference hash ("dHash": does brightness rise or fall between neighbouring
pixels of a 9x8 thumbnail). Photos whose hashes differ in only a few bits look alike. Candidates are found
through 4 bands of 16 bits (two hashes within 3 bits must share a band exactly), then confirmed by the full
distance and the aspect ratio. Each connected set is split around its best copy (most pixels, then
sharpest, then largest), so every photo listed is within the limit of the one that stays.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import index
from ..formats import IMAGE_KINDS, classify
from .common import Candidate, ProgressFn, ScanResult, cancelled, walk_files

STRICTNESS = {"very": 3, "similar": 6}   # max differing bits of 64
_BANDS = 4
_MAX_BUCKET = 5000                        # more photos sharing one band than this is noise (flat images)


class _Ticker:
    """Thread-safe progress, at most ten updates a second and always the last one."""

    def __init__(self, progress: ProgressFn | None):
        self.progress, self.done, self.last = progress, 0, 0.0
        self.lock = threading.Lock()

    def step(self, what: str, total: int) -> None:
        with self.lock:
            self.done += 1
            now = time.monotonic()
            if self.progress and (now - self.last >= 0.1 or self.done == total):
                self.last = now
                self.progress(f"{what}… {self.done:,} of {total:,}", self.done, total)


def fingerprint(path: Path) -> tuple[int, int, int] | None:
    """(dHash, width, height), or None for unreadable or nearly uniform images (blank, all black...)."""
    from PIL import Image, ImageOps, ImageStat

    from ..formats import HEIC, register_heif

    if classify(path) == HEIC:
        register_heif()
    try:
        with Image.open(path) as im:
            w, h = im.size
            im.draft("L", (160, 160))          # JPEG: decode at reduced size (fast)
            pic = ImageOps.exif_transpose(im) or im
            if pic.size[0] < pic.size[1]:
                w, h = min(w, h), max(w, h)
            else:
                w, h = max(w, h), min(w, h)
            small = pic.convert("L").resize((9, 8), Image.LANCZOS)
    except Exception:  # noqa: BLE001 - unreadable or unsupported
        return None
    if ImageStat.Stat(small).stddev[0] < 4:
        return None
    px = list(small.tobytes())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits, w, h


def inspect(path: Path, px: int = 256):
    """(sharpness, px×px grayscale thumbnail) from one decode; (0.0, None) if unreadable.

    Sharpness is the edge energy of a copy reduced to fit 512 px (higher = sharper), used to pick which
    burst shot to keep; the thumbnail (orientation applied) tells re-saves apart from different shots."""
    from PIL import Image, ImageFilter, ImageOps, ImageStat

    try:
        with Image.open(path) as im:
            im.draft("L", (512, 512))          # JPEG: decode at reduced size (fast)
            gray = (ImageOps.exif_transpose(im) or im).convert("L")
            gray.thumbnail((512, 512))
            sharp = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0]
            return sharp, gray.resize((px, px), Image.LANCZOS)
    except Exception:  # noqa: BLE001 - unreadable or unsupported
        return 0.0, None


def sharpness(path: Path) -> float:
    """Edge energy of a reduced copy: higher = sharper."""
    return inspect(path)[0]


def thumb(path: Path, px: int = 256):
    """A px×px grayscale thumbnail (orientation applied), or None."""
    return inspect(path, px)[1]


def looks_identical(a, b, tolerance: float = 2.5) -> bool:
    """Mean pixel difference of two thumbnails below ``tolerance`` (of 255): re-saves and resizes, not
    burst shots or edits."""
    from PIL import ImageChops, ImageStat

    if a is None or b is None:
        return False
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0] < tolerance


_LIVE_STILL = {".heic", ".heif", ".jpg", ".jpeg"}


def format_pair(path: Path, keeper: Path) -> bool:
    """The same shot saved twice: same folder and name, different image format (an iPhone import that wrote
    both IMG_1234.HEIC and IMG_1234.JPG)."""
    return (path.parent == keeper.parent and path.stem.lower() == keeper.stem.lower()
            and path.suffix.lower() != keeper.suffix.lower())


def live_companion(path: Path, keeper: Path) -> Path | None:
    """The Live Photo video (IMG_1234.MOV) that goes with ``path`` when it's removed, so it isn't left
    behind on its own. None when there isn't one, or when the copy that stays sits beside it with the same
    name (then the pair is still whole)."""
    if path.suffix.lower() not in _LIVE_STILL:
        return None
    for ext in (".MOV", ".mov"):
        mov = path.with_suffix(ext)
        if mov.is_file():
            if keeper.parent == path.parent and keeper.stem.lower() == path.stem.lower():
                return None
            return mov
    return None


def _distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _popcount(x):
    import numpy as np

    if hasattr(np, "bitwise_count"):     # numpy 2
        return np.bitwise_count(x)
    table = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    return table[x[..., None].view(np.uint8)].sum(axis=-1)


def _close_pairs(hashes: list[int], ratios: list[float], limit: int, cancel) -> list[tuple[int, int]]:
    """Index pairs within ``limit`` bits and the same shape. Candidates must share one 16-bit band (any
    two hashes within 3 bits do; within 6, most do), compared in blocks with numpy."""
    import numpy as np

    h = np.array(hashes, dtype=np.uint64)
    r = np.array(ratios, dtype=np.float64)
    pairs: list[tuple[int, int]] = []
    for b in range(_BANDS):
        key = (h >> np.uint64(16 * b)) & np.uint64(0xFFFF)
        order = np.argsort(key, kind="stable")
        bounds = np.flatnonzero(np.diff(key[order])) + 1
        for run in np.split(order, bounds):
            if len(run) < 2 or len(run) > _MAX_BUCKET:   # huge buckets are uniform-ish noise, skip
                continue
            if cancelled(cancel):
                return pairs
            for s in range(0, len(run) - 1, 256):
                rows = run[s:s + 256]
                cols = run[s + 1:]
                close = _popcount(h[rows, None] ^ h[None, cols]) <= limit
                close &= np.abs(r[rows, None] - r[None, cols]) <= 0.03 * r[rows, None]
                close &= (np.arange(len(rows))[:, None] + s) < (np.arange(len(cols))[None, :] + s + 1)
                for i, j in zip(*np.nonzero(close), strict=True):
                    pairs.append((int(rows[i]), int(cols[j])))
    return pairs


def _star_groups(members: list[int], prints, sharp: dict[int, float], files, limit: int
                 ) -> list[tuple[int, list[int]]]:
    """Split a connected set into groups around the copy that stays, so every photo listed is within
    ``limit`` of its keeper (look-alikes can chain: A~B and B~C without A~C).

    The keeper is the best copy: most pixels, then sharpest (within 10% of the sharpest counts as equal,
    so noise doesn't decide), then the largest file (higher quality), then the oldest."""
    top = max(sharp[i] for i in members) or 1.0
    left = sorted(members, key=lambda i: (prints[i][1] * prints[i][2], sharp[i] >= 0.9 * top,
                                          files[i][1].st_size, -files[i][1].st_mtime), reverse=True)
    out = []
    while len(left) > 1:
        best, rest = left[0], left[1:]
        hb, wb, hb_ = prints[best]
        ratio = wb / hb_
        near = [i for i in rest if _distance(prints[i][0], hb) <= limit
                and abs(prints[i][1] / prints[i][2] - ratio) <= 0.03 * ratio]
        if near:
            out.append((best, near))
            taken = set(near)
            left = [i for i in rest if i not in taken]
        else:
            left = rest
    return out


def find_similar(roots: list[Path], *, strictness: str = "very", min_size: int = 50 * 1024,
                 progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> ScanResult:
    result = ScanResult("similar")
    limit = STRICTNESS.get(strictness, 3)
    files = []
    for path, st in walk_files(roots, cancel=cancel):
        if st.st_size >= min_size and classify(path) in IMAGE_KINDS:
            files.append((path, st))
            if progress and len(files) % 500 == 0:
                progress(f"Finding photos… {len(files):,}", len(files), 0)
    if cancelled(cancel):
        result.cancelled = True
        return result

    prints: dict[int, tuple[int, int, int]] = {}
    todo = []
    for i, (p, st) in enumerate(files):
        cached = index.lookup_value("dhash", p, st.st_size, st.st_mtime)
        if cached is not None:
            if cached:
                prints[i] = tuple(cached)
        else:
            todo.append(i)
    from ..formats import HEIC

    if any(classify(files[i][0]) == HEIC for i in todo):
        from ..scanner import preload_codecs

        preload_codecs(heif=True)   # import on this thread before the workers use it
    workers = min(8, os.cpu_count() or 2)
    ticker = _Ticker(progress)

    finished: list[int] = []

    def fp_work(i: int) -> None:
        if cancelled(cancel):
            return
        fp = fingerprint(files[i][0])
        if fp is not None:
            prints[i] = fp
        finished.append(i)
        ticker.step("Reading photos", len(todo))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fp_work, todo))
    # Only what was read (a stopped scan must not record skipped files as unreadable).
    index.store_values("dhash", [(files[i][0], files[i][1].st_size, files[i][1].st_mtime, list(prints.get(i, ())))
                                 for i in finished])
    if cancelled(cancel):
        result.cancelled = True
        return result

    # Look-alikes, joined into connected sets.
    if progress:
        progress("Grouping similar photos…", 0, 0)
    ids = list(prints)
    pairs = _close_pairs([prints[i][0] for i in ids], [prints[i][1] / prints[i][2] for i in ids], limit, cancel)
    if cancelled(cancel):
        result.cancelled = True
        return result
    parent = {i: i for i in ids}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in pairs:
        parent[root(ids[a])] = root(ids[b])
    linked: dict[int, list[int]] = defaultdict(list)
    for i in ids:
        linked[root(i)].append(i)
    components = [g for g in linked.values() if len(g) > 1]

    # One decode per photo in a set (in parallel): sharpness picks the burst shot to keep, the thumbnail
    # tells a re-save from a different shot. Sets are done in batches so thumbnails don't pile up.
    components.sort(key=len)
    total = sum(len(g) for g in components)
    ticker = _Ticker(progress)
    sets: list[tuple[int, list[int]]] = []   # (keeper, others)
    looks: dict[int, bool] = {}
    sharp: dict[int, float] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        batch: list[list[int]] = []
        for n, g in enumerate(components):
            batch.append(g)
            if sum(len(x) for x in batch) < 512 and n != len(components) - 1:
                continue
            if cancelled(cancel):
                result.cancelled = True
                return result
            members = [i for x in batch for i in x]
            px = 256 if len(members) <= 4096 else 128

            def look(i: int, px: int = px):
                out = inspect(files[i][0], px) if not cancelled(cancel) else (0.0, None)
                ticker.step("Choosing the best copies", total)
                return out

            seen = dict(zip(members, pool.map(look, members), strict=True))
            sharp.update((i, seen[i][0]) for i in members)
            for x in batch:
                for keeper, others in _star_groups(x, prints, sharp, files, limit):
                    sets.append((keeper, others))
                    for i in others:
                        looks[i] = looks_identical(seen[i][1], seen[keeper][1])
            batch = []
    if cancelled(cancel):
        result.cancelled = True
        return result

    sets.sort(key=lambda kg: sum(files[i][1].st_size for i in (kg[0], *kg[1])), reverse=True)
    for gi, (best, others) in enumerate(sets):
        kp, kst = files[best]
        for i in others:
            p, st = files[i]
            d = _distance(prints[i][0], prints[best][0])
            same = looks[i]
            smaller = prints[i][1] * prints[i][2] < prints[best][1] * prints[best][2]
            why = ["lower resolution"] if smaller else []
            if sharp[i] < sharp[best] * 0.85:
                why.append("less sharp")
            detail = "Looks the same as the copy that stays" if same else "Similar to the copy that stays"
            if why:
                detail += f" ({', '.join(why)})"
            if format_pair(p, kp):
                detail = (f"The same photo saved as {p.suffix[1:].upper()} and {kp.suffix[1:].upper()}; the "
                          f"{kp.suffix[1:].upper()} stays" + (" (opens everywhere)" if kp.suffix.lower() in
                                                             (".jpg", ".jpeg") else ""))
            extra: dict[str, object] = {"same_volume": st.st_dev == kst.st_dev, "mtime_ns": st.st_mtime_ns,
                                        "distance": d}
            size = st.st_size
            mov = live_companion(p, kp)
            if mov is not None:
                try:
                    size += mov.stat().st_size
                    extra["companions"] = [str(mov)]
                    detail += f". Live Photo: its video ({mov.name}) goes with it"
                except OSError:
                    pass
            result.items.append(Candidate(
                path=p, size=size, keeper=kp, group=f"sim{gi}", detail=detail,
                selected=same and (smaller or st.st_size <= kst.st_size),   # only obvious copies pre-selected
                extra=extra,
            ))
    if sets:
        result.notes.append(f"{len(sets)} group{'s' if len(sets) != 1 else ''} of similar photos. Hover a "
                            "row to see it; right-click to keep a different one.")
    return result


__all__ = ["find_similar", "fingerprint", "inspect", "sharpness", "STRICTNESS"]
