"""Perceptual quality measurement for Smart quality.

Images: SSIMULACRA2 (libjxl's ``ssimulacra2`` tool) when available, else luma SSIM via ffmpeg.
Videos: VMAF (ffmpeg built with libvmaf) when available, else luma SSIM via ffmpeg.

Each metric has its own thresholds for the three targets. "identical" means
differences are not visible in normal viewing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import tools

# metric -> target -> minimum score
THRESHOLDS = {
    # libjxl guidance: 90 ≈ visually lossless at 1:1, 80 ≈ not noticeable in normal viewing, 70 ≈ medium.
    "SSIMULACRA2": {"identical": 85.0, "high": 80.0, "good": 70.0},
    "VMAF": {"identical": 95.0, "high": 93.0, "good": 90.0},
    # Fallback: SSIM on luma (brightness), which tracks visible differences far better than RGB SSIM.
    "SSIM": {"identical": 0.985, "high": 0.970, "good": 0.950},
    "SSIM-video": {"identical": 0.980, "high": 0.970, "good": 0.955},
}
TARGET_LABELS = {"identical": "Visually identical", "high": "High", "good": "Good"}


@dataclass
class Score:
    metric: str
    value: float

    def meets(self, target: str) -> bool:
        return self.value >= THRESHOLDS[self.metric][target]

    def verdict(self) -> str:
        """In words: how the result looks next to the original."""
        t = THRESHOLDS[self.metric]
        if self.value >= t["identical"]:
            return "looks identical"
        if self.value >= t["high"]:
            return "differences hard to see"
        if self.value >= t["good"]:
            return "small visible differences"
        return "visible differences"

    def __str__(self) -> str:
        name = "SSIM" if self.metric.startswith("SSIM") and self.metric != "SSIMULACRA2" else self.metric
        return f"{name} {self.value:.3f}" if name == "SSIM" else f"{name} {self.value:.1f}"


@lru_cache(maxsize=None)
def has_vmaf() -> bool:
    ffmpeg = tools.find_tool("ffmpeg")
    if not ffmpeg:
        return False
    try:
        out = tools.run([ffmpeg, "-hide_banner", "-filters"], timeout=30, check=False).stdout
        return " libvmaf " in out
    except Exception:  # noqa: BLE001
        return False


def _python_ssimulacra2_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("numpy") is not None


def image_metric() -> str:
    return "SSIMULACRA2" if tools.find_tool("ssimulacra2") or _python_ssimulacra2_available() else "SSIM"


def video_metric() -> str:
    return "VMAF" if has_vmaf() else "SSIM-video"


CROP = 640  # Python SSIMULACRA2 scores representative crops to keep the cost bounded


def _crops(img) -> list[tuple[int, int, int, int]]:
    """The most detailed CROP x CROP region plus the centre (whole image if small)."""
    from PIL import ImageFilter, ImageStat

    w, h = img.size
    if w <= CROP * 1.5 and h <= CROP * 1.5:
        return [(0, 0, w, h)]
    cw, ch = min(CROP, w), min(CROP, h)
    edges = img.convert("L").filter(ImageFilter.FIND_EDGES)
    best: tuple[int, int, int, int] = (0, 0, cw, ch)
    best_var = -1.0
    for gy in range(3):
        for gx in range(3):
            x, y = (w - cw) * gx // 2, (h - ch) * gy // 2
            v = ImageStat.Stat(edges.crop((x, y, x + cw, y + ch))).var[0]
            if v > best_var:
                best, best_var = (x, y, x + cw, y + ch), v
    centre = ((w - cw) // 2, (h - ch) // 2, (w - cw) // 2 + cw, (h - ch) // 2 + ch)
    return [best] if best == centre else [best, centre]


def _python_score(reference: Path, candidate: Path) -> Score:
    from PIL import Image

    from . import _ssimulacra2
    from .fileops import remove_quietly, temp_name

    with Image.open(reference) as a, Image.open(candidate) as b:
        ra, rb = a.convert("RGB"), b.convert("RGB")
    scores = []
    for box in _crops(ra):
        pa, pb = temp_name(reference.parent, ".png", "-ra"), temp_name(reference.parent, ".png", "-rb")
        try:
            ra.crop(box).save(pa)
            rb.crop(box).save(pb)
            scores.append(float(_ssimulacra2.compute_ssimulacra2(str(pa), str(pb))))
        finally:
            remove_quietly(pa, pb)
    return Score("SSIMULACRA2", min(scores))


def score_images(reference: Path, candidate: Path) -> Score:
    """Both files must be PNGs of the same size (in the same folder or with ASCII-safe names)."""
    tool = tools.find_tool("ssimulacra2")
    if tool:
        out = tools.run([tool, reference.name, candidate.name], cwd=reference.parent, timeout=600).stdout
        nums = re.findall(r"-?\d+(?:\.\d+)?", out)
        if nums:
            return Score("SSIMULACRA2", float(nums[-1]))
    if _python_ssimulacra2_available():
        return _python_score(reference, candidate)
    ffmpeg = tools.require("ffmpeg")
    proc = tools.run([ffmpeg, "-hide_banner", "-nostdin", "-i", candidate.name, "-i", reference.name,
                      "-lavfi", "[0:v]format=yuv444p[a];[1:v]format=yuv444p[b];[a][b]ssim", "-f", "null", "-"],
                     cwd=reference.parent, timeout=600, check=False)
    m = re.search(r"Y:([0-9.]+)", proc.stderr)
    if not m:
        raise RuntimeError("Couldn't measure image quality")
    return Score("SSIM", float(m.group(1)))


def score_video_segment(source: Path, start: float, length: float, sample: Path,
                        size: tuple[int, int], crop: str = "") -> Score:
    """Compare a sample encode with the same segment of the source, at the sample's resolution."""
    ffmpeg = tools.require("ffmpeg")
    w, h = size
    ref = f"[1:v]{crop + ',' if crop else ''}scale={w}:{h}:flags=bicubic,format=yuv420p,setpts=PTS-STARTPTS[r]"
    dist = "[0:v]format=yuv420p,setpts=PTS-STARTPTS[d]"
    if has_vmaf():
        graph = f"{dist};{ref};[d][r]libvmaf=n_threads=4"
        pattern = r"VMAF score:\s*([0-9.]+)"
        metric = "VMAF"
    else:
        graph = f"{dist};{ref};[d][r]ssim"
        pattern = r"Y:([0-9.]+)"
        metric = "SSIM-video"
    proc = tools.run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(sample), "-ss", f"{start:.3f}", "-t",
                      f"{length:.3f}", "-i", str(source), "-lavfi", graph, "-f", "null", "-"],
                     timeout=1800, check=False)
    m = re.search(pattern, proc.stderr)
    if not m:
        raise RuntimeError("Couldn't measure video quality")
    return Score(metric, float(m.group(1)))


def score_video_output(source: Path, output: Path, start: float, length: float, size: tuple[int, int],
                       crop: str = "") -> Score:
    """Compare a stretch of the finished video with the same stretch of the original."""
    ffmpeg = tools.require("ffmpeg")
    w, h = size
    ref = f"[1:v]{crop + ',' if crop else ''}scale={w}:{h}:flags=bicubic,format=yuv420p,setpts=PTS-STARTPTS[r]"
    dist = "[0:v]format=yuv420p,setpts=PTS-STARTPTS[d]"
    if has_vmaf():
        graph, pattern, metric = f"{dist};{ref};[d][r]libvmaf=n_threads=4", r"VMAF score:\s*([0-9.]+)", "VMAF"
    else:
        graph, pattern, metric = f"{dist};{ref};[d][r]ssim", r"Y:([0-9.]+)", "SSIM-video"
    seek = ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"]
    proc = tools.run([ffmpeg, "-hide_banner", "-nostdin", *seek, "-i", str(output), *seek, "-i", str(source),
                      "-lavfi", graph, "-f", "null", "-"], timeout=1800, check=False)
    m = re.search(pattern, proc.stderr)
    if not m:
        raise RuntimeError("Couldn't measure video quality")
    return Score(metric, float(m.group(1)))


def search(values: list[int], try_value) -> tuple[int, object] | None:
    """Binary search for the most compressed setting that still meets the target.

    ``values`` are ordered from best quality to most compression. ``try_value(v)`` returns
    (meets_target: bool, payload). Returns (value, payload) of the chosen setting, or None
    if even the best quality misses the target.
    """
    lo, hi = 0, len(values) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        ok, payload = try_value(values[mid])
        if ok:
            best = (values[mid], payload)
            lo = mid + 1      # try more compression
        else:
            hi = mid - 1      # need more quality
    return best


__all__ = ["Score", "THRESHOLDS", "TARGET_LABELS", "score_images", "score_video_segment", "score_video_output",
           "search", "image_metric", "video_metric"]
