"""Estimates that learn: compare each estimated size with the real result and correct future estimates.

Kept per kind of conversion (e.g. "jpeg:jxl_transcode:lossless", "video:video:h264>hevc") as a running
average of log(actual / estimated), so a route that keeps coming out smaller (or bigger) than predicted
gets its estimates scaled accordingly. Stored in calibration.json in the app data folder.
"""

from __future__ import annotations

import json
import math
import threading

MIN_SAMPLES = 3          # don't trust a correction until a few files agree
MAX_SAMPLES = 200        # older results fade out (running average over roughly this many)
_CLAMP = (0.4, 2.5)      # never scale an estimate by more than this

_lock = threading.Lock()
_data: dict[str, list[float]] | None = None   # key -> [mean log ratio, count]
_dirty = False
_enabled = True


def set_enabled(on: bool) -> None:
    global _enabled
    _enabled = on


def _file():
    from .journal import data_dir

    return data_dir() / "calibration.json"


def _load() -> dict[str, list[float]]:
    global _data
    if _data is None:
        try:
            raw = json.loads(_file().read_text("utf-8"))
            _data = {str(k): [float(v[0]), float(v[1])] for k, v in raw.items()}
        except (OSError, ValueError, TypeError, IndexError):
            _data = {}
    return _data


def key(entry, plan) -> str:
    if entry.kind == "video" and entry.video is not None:
        return f"video:{entry.video.codec}>{plan.target_ext}"
    return f"{entry.kind}:{plan.route}:{'lossless' if plan.lossless else 'lossy'}"


def factor(entry, plan) -> float:
    """How much to scale this plan's raw estimate (1.0 until enough results are known)."""
    if not _enabled:
        return 1.0
    with _lock:
        mean, n = _load().get(key(entry, plan), (0.0, 0))
    if n < MIN_SAMPLES:
        return 1.0
    return max(_CLAMP[0], min(_CLAMP[1], math.exp(mean)))


def record(entry, plan, actual_size: int, raw_estimate: int) -> None:
    """Remember how a real result compared with the uncorrected estimate."""
    global _dirty
    if not _enabled or actual_size <= 0 or raw_estimate <= 0:
        return
    r = math.log(actual_size / raw_estimate)
    with _lock:
        d = _load()
        mean, n = d.get(key(entry, plan), (0.0, 0))
        n = min(n + 1, MAX_SAMPLES)
        d[key(entry, plan)] = [mean + (r - mean) / n, n]
        _dirty = True


def save() -> None:
    global _dirty
    with _lock:
        if not _dirty or _data is None:
            return
        try:
            from .fileops import atomic_write_text

            atomic_write_text(_file(), json.dumps(_data, indent=1))
            _dirty = False
        except OSError:
            pass


def reset() -> None:
    global _data, _dirty
    with _lock:
        _data, _dirty = {}, False
        try:
            _file().unlink()
        except OSError:
            pass


__all__ = ["factor", "record", "save", "reset", "key", "set_enabled"]
