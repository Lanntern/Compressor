"""Perceptual quality measurement for Smart quality.

Images: SSIMULACRA2 (libjxl's ``ssimulacra2`` tool) when available, else luma SSIM via ffmpeg.
Videos: VMAF (ffmpeg built with libvmaf) when available, else luma SSIM via ffmpeg. Other measures can be
chosen (Settings.video_metric): XPSNR (ffmpeg 7.1+), and SSIMULACRA2 or Butteraugli on frames taken at the same
moments of both videos. One that isn't available falls back to the default.

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
    # Frames seen in motion hide a little more than a still photo does.
    "SSIMULACRA2-video": {"identical": 80.0, "high": 74.0, "good": 65.0},
    # XPSNR (perceptually weighted PSNR, dB, brightness): about 40+ is transparent for typical material.
    "XPSNR": {"identical": 44.0, "high": 41.0, "good": 38.0},
    # Butteraugli 3-norm distance: lower is better; 1.0 is about one "just noticeable difference".
    "Butteraugli": {"identical": 1.0, "high": 1.5, "good": 2.2},
}
LOWER_IS_BETTER = {"Butteraugli"}
VIDEO_METRICS = ("auto", "vmaf", "ssimulacra2", "xpsnr", "butteraugli")
VIDEO_METRIC_LABELS = {"auto": "Automatic (VMAF)", "vmaf": "VMAF", "ssimulacra2": "SSIMULACRA2",
                       "xpsnr": "XPSNR", "butteraugli": "Butteraugli"}
TARGET_LABELS = {"identical": "Visually identical", "high": "High", "good": "Good"}


@dataclass
class Score:
    metric: str
    value: float

    def meets(self, target: str) -> bool:
        limit = THRESHOLDS[self.metric][target]
        return self.value <= limit if self.metric in LOWER_IS_BETTER else self.value >= limit

    def verdict(self) -> str:
        """In words: how the result looks next to the original."""
        for target, words in (("identical", "looks identical"), ("high", "differences hard to see"),
                              ("good", "small visible differences")):
            if self.meets(target):
                return words
        return "visible differences"

    def __str__(self) -> str:
        name = {"SSIM-video": "SSIM", "SSIMULACRA2-video": "SSIMULACRA2"}.get(self.metric, self.metric)
        if name == "SSIM":
            return f"SSIM {self.value:.3f}"
        if name == "Butteraugli":
            return f"Butteraugli {self.value:.2f}"
        return f"{name} {self.value:.1f}{' dB' if name == 'XPSNR' else ''}"


def worst(scores: list[Score]) -> Score:
    """The score that looks furthest from the original."""
    pick = max if scores[0].metric in LOWER_IS_BETTER else min
    return pick(scores, key=lambda x: x.value)


def mean(scores: list[Score]) -> Score:
    return Score(scores[0].metric, sum(x.value for x in scores) / len(scores))


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


def _default_video_metric() -> str:
    return "VMAF" if has_vmaf() else "SSIM-video"


@lru_cache(maxsize=None)
def _metric_works(metric: str) -> bool:
    """Whether a chosen measure can run here: its tool or filter exists and a tiny comparison succeeds."""
    try:
        if metric == "VMAF":
            return has_vmaf()
        if metric == "XPSNR":
            return _xpsnr_selftest()
        if metric == "Butteraugli":
            return tools.find_tool("butteraugli_main") is not None
        if metric == "SSIMULACRA2-video":
            return image_metric() == "SSIMULACRA2"
    except Exception:  # noqa: BLE001
        return False
    return metric == "SSIM-video"


def _xpsnr_selftest() -> bool:
    ffmpeg = tools.find_tool("ffmpeg")
    if not ffmpeg:
        return False
    proc = tools.run([ffmpeg, "-hide_banner", "-nostdin", "-f", "lavfi", "-i", "testsrc2=s=64x64:d=0.2",
                      "-f", "lavfi", "-i", "testsrc2=s=64x64:d=0.2", "-lavfi", "[0:v][1:v]xpsnr", "-f", "null", "-"],
                     timeout=60, check=False)
    return _parse(proc.stderr, "XPSNR") is not None


def video_metric(choice: str = "auto") -> str:
    """The measure used for videos: the chosen one when it works here, else VMAF (or SSIM without VMAF)."""
    wanted = {"vmaf": "VMAF", "ssimulacra2": "SSIMULACRA2-video", "xpsnr": "XPSNR",
              "butteraugli": "Butteraugli"}.get(choice)
    return wanted if wanted and _metric_works(wanted) else _default_video_metric()


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


_PATTERNS = {
    "VMAF": r"VMAF score:\s*([0-9.]+)",
    "SSIM-video": r"SSIM .*?Y:([0-9.]+)",
    # ffmpeg's xpsnr filter ends with e.g. "XPSNR average, 50 frames  y: 41.2345  u: ..." (y: brightness)
    "XPSNR": r"XPSNR[^\n]*?\by:\s*([0-9.]+)",
}


def _parse(text: str, metric: str) -> float | None:
    found = re.findall(_PATTERNS[metric], text)
    return float(found[-1]) if found else None


FRAMES_PER_STRETCH = 4      # frames compared per stretch by the frame-based measures (SSIMULACRA2, Butteraugli)


def _compare(dist: list[str], ref: list[str], length: float, size: tuple[int, int], crop: str, metric: str,
             grain: int = 0, subsample: int = 1) -> Score:
    """Compare a stretch of a candidate (``dist``: ffmpeg input arguments) with the same stretch of the original
    (``ref``), at the candidate's size. ``grain``: the candidate was encoded with film-grain synthesis, so it's
    decoded without the grain and compared with a lightly denoised original (comparing grain with different
    grain would measure only the grain)."""
    ffmpeg = tools.require("ffmpeg")
    w, h = size
    clean = _denoise_filter(grain)
    ref_chain = ",".join(x for x in (crop, f"scale={w}:{h}:flags=bicubic", clean) if x)   # (denoise after any
    # downscale: much quicker, and the measured stretches are short)
    no_grain = ["-export_side_data", "film_grain"] if grain else []   # (the decoder hands the grain over, unapplied)
    if metric in ("SSIMULACRA2-video", "Butteraugli"):
        return _compare_frames(ffmpeg, no_grain + dist, ref, length, ref_chain, metric)
    ref_f = f"[1:v]{ref_chain},format=yuv420p,setpts=PTS-STARTPTS[r]"
    dist_f = "[0:v]format=yuv420p,setpts=PTS-STARTPTS[d]"
    compare = {"VMAF": f"libvmaf=n_threads=4:n_subsample={max(1, subsample)}", "SSIM-video": "ssim",
               "XPSNR": "xpsnr"}[metric]
    proc = tools.run([ffmpeg, "-hide_banner", "-nostdin", *no_grain, *dist, *ref, "-lavfi",
                      f"{dist_f};{ref_f};[d][r]{compare}", "-f", "null", "-"], timeout=3600, check=False)
    value = _parse(proc.stderr, metric)
    if value is None:
        raise RuntimeError("Couldn't measure video quality")
    return Score(metric, value)


def _denoise_filter(grain: int) -> str:
    """A denoise roughly matching what the encoder removes before it re-creates grain (0: none): non-local
    means at about the measured noise level (grain level 6 ≈ noise 2, 18 ≈ 6). Weaker denoisers leave enough
    noise behind that every encode would look "different"."""
    if not grain:
        return ""
    return f"nlmeans=s={max(1.5, grain / 3):.1f}:p=5:r=9"


def _compare_frames(ffmpeg: str, dist: list[str], ref: list[str], length: float, ref_chain: str,
                    metric: str) -> Score:
    """SSIMULACRA2 / Butteraugli: frames at the same moments of both, scored as pictures and averaged."""
    import tempfile

    def seek_args(inputs: list[str], at: float) -> list[str]:
        # The stretch's own -ss (if any) moves on by ``at``, counted from where the file's picture starts (a
        # result can start a frame's fraction later than its original, e.g. to make room for an audio
        # encoder's lead-in: seeking to the same clock time would then compare neighbouring frames).
        at += _video_start(inputs[inputs.index("-i") + 1])
        out, i = [], 0
        while i < len(inputs):
            if inputs[i] == "-ss":
                out += ["-ss", f"{float(inputs[i + 1]) + at:.3f}"]
                i += 2
            elif inputs[i] == "-t":
                i += 2
            else:
                out.append(inputs[i])
                i += 1
        if "-ss" not in out:
            out = ["-ss", f"{at:.3f}", *out]
        return out

    values = []
    with tempfile.TemporaryDirectory(prefix="wick-q-") as tmp:
        folder = Path(tmp)
        count = FRAMES_PER_STRETCH if length > 1 else 1
        for i in range(count):
            at = length * (i + 0.5) / count
            a, b = folder / f"r{i}.png", folder / f"d{i}.png"
            tools.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", *seek_args(ref, at), "-frames:v", "1",
                       "-vf", ref_chain + ",format=rgb24", str(a)], timeout=600)
            tools.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", *seek_args(dist, at), "-frames:v", "1",
                       "-vf", "format=rgb24", str(b)], timeout=600)
            if not (a.exists() and b.exists()):
                continue
            if metric == "Butteraugli":
                out = tools.run([tools.require("butteraugli_main"), a.name, b.name], cwd=folder, timeout=900).stdout
                m = re.search(r"3-norm:\s*([0-9.]+)", out) or re.search(r"^\s*([0-9.]+)", out)
                if m:
                    values.append(float(m.group(1)))
            else:
                values.append(score_images(a, b).value)
    if not values:
        raise RuntimeError("Couldn't measure video quality")
    return Score(metric, sum(values) / len(values))


@lru_cache(maxsize=256)
def _video_start(path: str) -> float:
    """When the first video stream's picture starts in the file's timeline (seconds; usually 0)."""
    ffprobe = tools.find_tool("ffprobe")
    if not ffprobe:
        return 0.0
    out = tools.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=start_time",
                     "-of", "csv=p=0", path], timeout=60, check=False).stdout.strip()
    try:
        return float(out.splitlines()[0])
    except (ValueError, IndexError):
        return 0.0


def _input(path: Path, start: float | None, length: float) -> list[str]:
    seek = ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"] if start is not None else ["-t", f"{length:.3f}"]
    return [*seek, "-i", str(path)]


def score_video_segment(source: Path, start: float, length: float, sample: Path,
                        size: tuple[int, int], crop: str = "", metric: str | None = None, grain: int = 0,
                        subsample: int = 1) -> Score:
    """Compare a sample encode (of ``length`` seconds from ``start``) with the same stretch of the source, at the
    sample's resolution."""
    return _compare(["-t", f"{length:.3f}", "-i", str(sample)], _input(source, start, length), length, size, crop,
                    metric or _default_video_metric(), grain, subsample)


def score_video_output(source: Path, output: Path, start: float, length: float, size: tuple[int, int],
                       crop: str = "", metric: str | None = None, grain: int = 0) -> Score:
    """Compare a stretch of the finished video with the same stretch of the original."""
    return _compare(_input(output, start, length), _input(source, start, length), length, size, crop,
                    metric or _default_video_metric(), grain)


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


__all__ = ["Score", "THRESHOLDS", "TARGET_LABELS", "VIDEO_METRICS", "VIDEO_METRIC_LABELS", "score_images",
           "score_video_segment", "score_video_output", "search", "image_metric", "video_metric", "worst", "mean"]
