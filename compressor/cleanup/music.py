"""Same music: the same song saved more than once, even as different files (an MP3 and a FLAC, two
downloads at different bitrates, a copy with other tags).

"Same recording" listens to the songs: the first two minutes are decoded to a quiet 5.5 kHz mono signal
and turned into an audio fingerprint, 32 bits per 46 ms frame saying whether the energy in neighbouring
frequency bands rises or falls (the method in Haitsma & Kalker, "A Highly Robust Audio Fingerprinting
System", 2002). Encoding, bitrate, volume and sample rate barely change those bits; a different song
gets about half of them different. Songs are only compared when their lengths are within a few seconds,
first by their loudness over time (cheap), then bit by bit at the best alignment.

"Same title and artist" only reads the tags: fast, but a live version or a remix counts as the same song
unless its length differs.
"""

from __future__ import annotations

import os
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING



from .. import index, tools
from ..formats import AUDIO_EXTS
if TYPE_CHECKING:
    import numpy as np

from .common import Candidate, ProgressFn, ScanResult, cancelled, walk_files
from .similar import _Ticker

RATE = 5512
SECONDS = 120
WINDOW = 2048
HOP = 256
BANDS = 33
ENVELOPE_HOP = 8                 # frames per loudness step (~0.37 s)
MAX_SHIFT = 40                   # frames tried either side when aligning (~1.9 s)
SAME = 0.22                      # share of differing bits below which two songs are one recording
LENGTH_SLACK = 3.0               # seconds two copies' lengths may differ by
LOSSLESS = {"flac", "alac", "wavpack", "ape", "tta", "mlp", "truehd"}
_VERSION = 1                     # (bump when the fingerprint changes: cached ones are recomputed)


def decode(path: Path, seconds: int = SECONDS) -> np.ndarray | None:
    import numpy as np

    ffmpeg = tools.find_tool("ffmpeg")
    if not ffmpeg:
        return None
    try:
        p = tools.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0", "-t", str(seconds),
                       "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"], timeout=120, check=False, text=False)
    except Exception:  # noqa: BLE001 - timeouts, missing ffmpeg
        return None
    if p.returncode != 0 or len(p.stdout) < RATE * 2 * 5:
        return None
    return np.frombuffer(p.stdout, dtype="<i2").astype(np.float32) / 32768.0


@lru_cache(maxsize=1)
def _band_edges() -> np.ndarray:
    import numpy as np

    freqs = np.fft.rfftfreq(WINDOW, 1 / RATE)
    edges = np.geomspace(300, 2000, BANDS + 1)
    return np.searchsorted(freqs, edges)


@lru_cache(maxsize=1)
def _hann() -> np.ndarray:
    import numpy as np

    return np.hanning(WINDOW).astype(np.float32)


def fingerprint(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(32-bit code per frame, loudness envelope)."""
    import numpy as np

    n = 1 + max(0, (len(samples) - WINDOW) // HOP)
    frames = np.lib.stride_tricks.sliding_window_view(samples, WINDOW)[::HOP][:n] * _hann()
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    energy = np.add.reduceat(power, _band_edges()[:-1], axis=1)[:, :BANDS]
    d = energy[:, :-1] - energy[:, 1:]                  # band m vs band m+1
    bits = (d[1:] - d[:-1]) > 0                          # ... rising or falling since the last frame
    codes = np.packbits(bits, axis=1, bitorder="little").view("<u4").ravel()
    loud = np.log1p(power.sum(axis=1) * 1e3)
    m = len(loud) // ENVELOPE_HOP
    env = loud[: m * ENVELOPE_HOP].reshape(m, ENVELOPE_HOP).mean(axis=1)
    return codes.astype(np.uint32), env.astype(np.float32)


@lru_cache(maxsize=1)
def _pop() -> np.ndarray:
    """Set bits in each byte value."""
    import numpy as np

    return np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def bit_error(a: np.ndarray, b: np.ndarray, max_shift: int = MAX_SHIFT) -> float:
    """Share of differing fingerprint bits at the best alignment (0 = identical, ~0.5 = unrelated)."""
    import numpy as np

    best = 1.0
    for s in range(-max_shift, max_shift + 1):
        x, y = (a[s:], b) if s >= 0 else (a, b[-s:])
        n = min(len(x), len(y))
        if n < 100:
            continue
        diff = np.bitwise_xor(x[:n], y[:n]).view(np.uint8)
        best = min(best, float(_pop()[diff].sum()) / (n * 32))
    return best


def _envelopes(envs: list[np.ndarray]) -> np.ndarray:
    """Loudness envelopes as rows of equal length, each scaled to mean 0 and spread 1 (short ones padded)."""
    import numpy as np

    width = max((len(e) for e in envs), default=1)
    out = np.zeros((len(envs), width), dtype=np.float32)
    for r, e in enumerate(envs):
        if len(e) > 1 and e.std() > 0:
            out[r, :len(e)] = (e - e.mean()) / e.std()
    return out


def _envelope_close(a: np.ndarray, others: np.ndarray, shift: int = 5) -> np.ndarray:
    """For each row of ``others``: does its loudness over time follow ``a``'s at some small shift?"""
    import numpy as np

    width = len(a)
    best = np.full(len(others), -1.0, dtype=np.float32)
    for s in range(-shift, shift + 1):
        n = width - abs(s)
        x = a[max(0, s): max(0, s) + n]
        y = others[:, max(0, -s): max(0, -s) + n]
        best = np.maximum(best, (y @ x) / n)
    return best > 0.6


# ---------------------------------------------------------------- what a file is

def _norm(s: str) -> str:
    s = re.sub(r"\s*[\(\[](feat\.?|ft\.?|remaster(ed)?|explicit)[^\)\]]*[\)\]]", "", s.lower())
    return re.sub(r"[^\w]+", " ", s).strip()


def describe(path: Path) -> dict | None:
    from ..probe import probe_audio

    info = probe_audio(path)
    if info is None or info.duration <= 0:
        return None
    t = info.tags
    return {"duration": info.duration, "codec": info.codec, "bit_rate": info.bit_rate,
            "rate": info.sample_rate, "bits": info.bits, "cover": info.cover_index >= 0,
            "title": t.get("title", ""), "artist": t.get("artist", "") or t.get("album_artist", ""),
            "album": t.get("album", ""), "tags": len(t)}


def quality_label(d: dict, path: Path) -> str:
    from .extensions import _NAMES

    ext = path.suffix.lower()
    fmt = _NAMES.get(ext, ext.lstrip(".").upper())
    if d["codec"] in LOSSLESS or d["codec"].startswith("pcm_"):
        depth = f"{d['bits']}-bit " if d.get("bits") else ""
        rate = f"{d['rate'] / 1000:g} kHz" if d.get("rate") else ""
        return f"{fmt}, lossless{', ' + depth + rate if rate else ''}"
    return f"{fmt}, {round(d['bit_rate'] / 1000)} kbps" if d.get("bit_rate") else fmt


def _rank(d: dict, size: int) -> tuple:
    """Which copy is best: lossless, then higher bitrate, then better tagged, then larger."""
    lossless = d["codec"] in LOSSLESS or d["codec"].startswith("pcm_")
    return (lossless, d.get("rate", 0) * d.get("bits", 0) if lossless else d.get("bit_rate", 0),
            d.get("cover", False), d.get("tags", 0), size)


# ---------------------------------------------------------------- the scan

def find_same_music(roots: list[Path], *, by: str = "sound", min_size: int = 500 * 1024,
                    exclude: list[Path] | None = None, progress: ProgressFn | None = None,
                    cancel: threading.Event | None = None) -> ScanResult:
    import numpy as np

    result = ScanResult("music")
    skip = [os.path.normcase(str(p)) for p in exclude or []]
    files: list[tuple[Path, os.stat_result]] = []
    for path, st in walk_files(roots, cancel=cancel):
        if path.suffix.lower() in AUDIO_EXTS and st.st_size >= min_size \
                and not any(os.path.normcase(str(path)).startswith(s + os.sep) for s in skip):
            files.append((path, st))
            if progress and len(files) % 500 == 0:
                progress(f"Finding music… {len(files):,}", len(files), 0)
    if cancelled(cancel):
        result.cancelled = True
        return result

    workers = min(8, os.cpu_count() or 2)
    infos: dict[int, dict] = {}
    prints: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    todo = []
    for i, (p, st) in enumerate(files):
        cached = index.lookup_value("music", p, st.st_size, st.st_mtime)
        if cached and cached.get("v") == _VERSION and cached.get("info"):
            infos[i] = cached["info"]
            if cached.get("codes"):
                prints[i] = (np.frombuffer(bytes.fromhex(cached["codes"]), dtype="<u4"),
                             np.frombuffer(bytes.fromhex(cached["env"]), dtype="<f4"))
            if by != "sound" or i in prints:
                continue
        elif cached is not None and cached.get("v") == _VERSION and not cached.get("info"):
            continue                                   # (unreadable last time, unchanged since)
        todo.append(i)

    ticker = _Ticker(progress)
    done: list[int] = []

    def work(i: int) -> None:
        if cancelled(cancel):
            return
        p = files[i][0]
        if i not in infos:
            d = describe(p)
            if d is not None:
                infos[i] = d
        if by == "sound" and i in infos:
            samples = decode(p)
            if samples is not None:
                prints[i] = fingerprint(samples)
        done.append(i)
        ticker.step("Listening" if by == "sound" else "Reading tags", len(todo))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, todo))
    index.store_values("music", [(files[i][0], files[i][1].st_size, files[i][1].st_mtime, {
        "v": _VERSION, "info": infos.get(i),
        "codes": prints[i][0].tobytes().hex() if i in prints else "",
        "env": prints[i][1].tobytes().hex() if i in prints else ""}) for i in done])
    if cancelled(cancel):
        result.cancelled = True
        return result

    if progress:
        progress("Comparing songs…", 0, 0)
    ids = sorted(infos, key=lambda i: infos[i]["duration"])
    parent = {i: i for i in ids}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if by == "sound":
        heard = [i for i in ids if i in prints]
        env = _envelopes([prints[i][1] for i in heard])
        for n, a in enumerate(heard):
            if cancelled(cancel):
                result.cancelled = True
                return result
            end = n + 1
            while end < len(heard) and infos[heard[end]]["duration"] - infos[a]["duration"] <= LENGTH_SLACK:
                end += 1
            if end == n + 1:
                continue
            close = _envelope_close(env[n], env[n + 1:end])
            for k in np.flatnonzero(close):
                b = heard[n + 1 + int(k)]
                if root(a) != root(b) and bit_error(prints[a][0], prints[b][0]) <= SAME:
                    parent[root(a)] = root(b)
    else:
        by_tag: dict[tuple[str, str], list[int]] = defaultdict(list)
        for i in ids:
            title, artist = _norm(infos[i]["title"]), _norm(infos[i]["artist"])
            if title and artist:
                by_tag[(title, artist)].append(i)
        for group in by_tag.values():
            for x, y in zip(group, group[1:], strict=False):
                if infos[y]["duration"] - infos[x]["duration"] <= LENGTH_SLACK:
                    parent[root(x)] = root(y)

    linked: dict[int, list[int]] = defaultdict(list)
    for i in ids:
        linked[root(i)].append(i)
    sets = [g for g in linked.values() if len(g) > 1]
    sets.sort(key=lambda g: sum(files[i][1].st_size for i in g), reverse=True)
    for gi, members in enumerate(sets):
        best = max(members, key=lambda i: _rank(infos[i], files[i][1].st_size))
        kp, kst = files[best]
        kq = quality_label(infos[best], kp)
        for i in members:
            if i == best:
                continue
            p, st = files[i]
            q = quality_label(infos[i], p)
            worse = _rank(infos[i], st.st_size)[:2] <= _rank(infos[best], kst.st_size)[:2]
            same_kind = infos[i]["codec"] == infos[best]["codec"]
            detail = f"{q}; the copy that stays is {kq}" if q != kq else f"{q}, like the copy that stays"
            result.items.append(Candidate(
                path=p, size=st.st_size, keeper=kp, group=f"music{gi}", detail=detail,
                # A lossy copy of a lossless song is often kept on purpose (for a phone): not pre-selected.
                selected=worse and same_kind and by == "sound",
                extra={"same_volume": st.st_dev == kst.st_dev, "mtime_ns": st.st_mtime_ns, "label": q,
                       "keeper_label": kq}))
    if sets:
        result.notes.append(f"{len(sets)} song{'s' if len(sets) != 1 else ''} saved more than once. The best "
                            "copy of each stays; right-click to keep a different one.")
    elif not files:
        result.notes.append("No music here.")
    return result


def group_title(g: str, items: list[Candidate]) -> str:
    keeper = items[0].keeper
    return f"{len(items) + 1} copies of {keeper.stem if keeper else 'a song'}"


__all__ = ["find_same_music", "fingerprint", "bit_error", "decode", "group_title"]
