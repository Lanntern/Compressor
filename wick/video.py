"""Video conversion with ffmpeg (AV1 / HEVC, GPU or CPU) and verification."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from collections.abc import Callable
import re
from functools import lru_cache
from pathlib import Path

from . import steps, tools
from .fileops import remove_quietly, temp_name
from .images import NotWorthIt, VerificationFailed
from .plan import Plan
from .probe import VideoInfo, probe_video
from .settings import Settings

# GPU encoders, tried in this order: NVIDIA (NVENC), Intel (Quick Sync), AMD (AMF). Each is used only if a short
# test encode with its real settings works on this computer (encoder_works); otherwise the next, then the CPU.
GPU_ENCODERS = {"av1": ("av1_nvenc", "av1_qsv", "av1_amf"), "hevc": ("hevc_nvenc", "hevc_qsv", "hevc_amf")}
CPU_ENCODERS = {"av1": "libsvtav1", "hevc": "libx265"}

# Constant-quality values per encoder for high / balanced / small. NVENC: CQ; Quick Sync: ICQ (1-51); AMF: a
# constant QP (HEVC 0-51; AV1 0-255, so about four times the others' numbers); CPU: CRF.
_QUALITY = {
    "av1_nvenc": {"high": 26, "balanced": 32, "small": 38},
    "hevc_nvenc": {"high": 22, "balanced": 27, "small": 32},
    "av1_qsv": {"high": 24, "balanced": 30, "small": 36},
    "hevc_qsv": {"high": 22, "balanced": 26, "small": 30},
    "av1_amf": {"high": 100, "balanced": 124, "small": 148},
    "hevc_amf": {"high": 22, "balanced": 26, "small": 30},
    "libsvtav1": {"high": 26, "balanced": 32, "small": 38},
    "libx265": {"high": 20, "balanced": 24, "small": 28},
}


def is_gpu(encoder: str) -> bool:
    return encoder.endswith(("_nvenc", "_qsv", "_amf"))


def quality_label(encoder: str) -> str:
    """How each encoder names its quality number ("CRF 24", "CQ 27"...)."""
    return {"_nvenc": "CQ", "_qsv": "ICQ", "_amf": "QP"}.get(encoder[encoder.rfind("_"):], "CRF")


NVENC_BASIC = ("-preset", "p6", "-tune", "hq")
# Best first; the first one this GPU and ffmpeg accept is used (ultra high quality: RTX 40 and later, ffmpeg 7.1+
# for HEVC and newer for AV1; a deeper preset and look-ahead everywhere else they work).
NVENC_TUNINGS = (("-preset", "p7", "-tune", "uhq", "-rc-lookahead", "32"),
                 ("-preset", "p7", "-tune", "hq", "-rc-lookahead", "32"))
SPLIT_FROM_PIXELS = 3840 * 2160 * 0.9   # 4K and up: both encoders of a two-encoder GPU work on each frame


@lru_cache(maxsize=None)
def nvenc_tuning(encoder: str) -> tuple[str, ...]:
    """The best NVENC settings this computer takes (each tried once with a tiny encode)."""
    ffmpeg = tools.find_tool("ffmpeg")
    for tuning in NVENC_TUNINGS:
        if ffmpeg and _test_encode(ffmpeg, encoder, tuning):
            return tuning
    return NVENC_BASIC


@lru_cache(maxsize=None)
def nvenc_split(encoder: str) -> bool:
    """Whether split-frame encoding works here (GPUs with two or more encoders, recent drivers): tried once
    with a 4K frame, the size it's used for."""
    ffmpeg = tools.find_tool("ffmpeg")
    if not ffmpeg:
        return False
    return _test_encode(ffmpeg, encoder, (*nvenc_tuning(encoder), "-split_encode_mode", "forced"), size="3840x2160")


def _nvenc_args(encoder: str, pixels: int = 0) -> list[str]:
    split = ["-split_encode_mode", "forced"] if pixels >= SPLIT_FROM_PIXELS and nvenc_split(encoder) else []
    return [*nvenc_tuning(encoder), *split]


@lru_cache(maxsize=None)
def svt_tuning() -> str:
    """SVT-AV1's visual-quality tuning (instead of its default, which aims at the PSNR number) and variance boost
    (protects dark and flat areas), where the bundled build has them; tried once."""
    ffmpeg = tools.find_tool("ffmpeg")
    for params in ("tune=0:enable-variance-boost=1", "tune=0"):
        if ffmpeg and _test_encode(ffmpeg, "libsvtav1", ("-svtav1-params", params)):
            return params
    return ""


def _gpu_quality_args(encoder: str, q: int, tuned: bool = True, pixels: int = 0) -> list[str]:
    """Constant-quality settings for a GPU encoder (``tuned=False``: the basic ones its availability test uses)."""
    if encoder.endswith("_nvenc"):
        tuning = _nvenc_args(encoder, pixels) if tuned else list(NVENC_BASIC)
        return [*tuning, "-rc", "vbr", "-cq", str(q), "-b:v", "0", "-spatial-aq", "1"]
    if encoder.endswith("_qsv"):
        return ["-preset", "slower", "-global_quality", str(q)]
    return ["-quality", "quality", "-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q)]       # AMF


def _gpu_rate_args(encoder: str, kbps: int, pixels: int = 0) -> list[str]:
    """Average-bitrate settings for a GPU encoder (a size limit)."""
    rate = ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k"]
    if encoder.endswith("_nvenc"):
        return [*_nvenc_args(encoder, pixels), "-rc", "vbr", "-multipass", "fullres", *rate, "-spatial-aq", "1"]
    if encoder.endswith("_qsv"):
        return ["-preset", "slower", *rate]
    return ["-quality", "quality", "-rc", "vbr_peak", *rate]

# Audio codecs that can be copied into MP4 as-is; anything else becomes AAC.
_MP4_AUDIO_OK = {"aac", "mp3", "ac3", "eac3", "alac", "opus", "flac"}
_TEXT_SUBS = {"mov_text", "subrip", "srt", "ass", "ssa", "webvtt", "text"}


_ENCODER_CACHE_DAYS = 7
HW_DECODE = True    # decode sources on the GPU where possible (falls back to the CPU per file)
SVT_PROBE_PRESET = 10   # quick encodes for choosing the quality scene by scene (the final encode uses 6)

# Film grain: the noise level (standard deviation, 8-bit scale) in flat areas -> SVT-AV1 film-grain level. Below
# the first step the video counts as clean (digital cameras, animation, screen recordings).
GRAIN_STEPS = ((1.6, 0), (2.5, 6), (3.5, 10), (5.0, 14), (float("inf"), 18))
GRAIN_SAMPLES = 6
CRF_MAX = {"av1_nvenc": 63, "libsvtav1": 63, "hevc_nvenc": 51, "libx265": 51, "av1_qsv": 51, "hevc_qsv": 51,
           "av1_amf": 255, "hevc_amf": 51}


def encoder_quality(s: Settings, encoder: str) -> int:
    """The constant-quality value for ``encoder`` (Custom: the user's own, within the encoder's range)."""
    if s.video_quality == "custom":
        value = s.video_crf * 4 if encoder == "av1_amf" else s.video_crf    # (AMF's AV1 scale is 0-255)
        return max(1, min(CRF_MAX.get(encoder, 51), value))
    return _QUALITY[encoder]["balanced" if s.video_quality == "identical" else s.video_quality]


def measures_quality(s: Settings) -> bool:
    """Whether the quality is chosen by measuring samples (Visually identical, or Smart quality's target).
    A Custom value is used as it is."""
    return s.video_quality == "identical" or (s.smart_quality and s.video_quality != "custom")


def source_rate_limit(info: VideoInfo, s: Settings, encoder: str) -> int:
    """Picture bitrate ceiling in kbps when savings are required. Leave room for muxing and savings.

    Audio is excluded: a lossless soundtrack must not inflate the picture's budget. Missing stream rates
    and encoders without capped constant-quality mode rely on the final file-size check instead.
    This bounds spending on an already compressed picture; it does not denoise or restore lost detail.
    """
    if not s.require_savings or info.video_bit_rate <= 0:
        return 0
    if encoder not in ("av1_nvenc", "hevc_nvenc", "libsvtav1", "libx265"):
        return 0
    fraction = max(0.01, 1 - s.min_savings_pct / 100)
    # Rate-control buffers can overshoot on short clips. Keep 10% headroom; the final byte-size check is
    # still authoritative (a bitrate ceiling is not an exact file-size guarantee).
    return max(1, int(info.video_bit_rate * fraction * 0.90 / 1000))


def check_quality_budget(score, info: VideoInfo, s: Settings, encoder: str) -> None:
    """A bitrate ceiling must not silently override a measured quality target."""
    target = "identical" if s.video_quality == "identical" else s.smart_target
    if score is not None and source_rate_limit(info, s, encoder) and measures_quality(s) and not score.meets(target):
        raise NotWorthIt("Result misses the requested video quality within the bitrate budget; original kept")


def _encoder_cache(ffmpeg: str) -> tuple[Path, str, dict]:
    """Saved test results for this exact ffmpeg build (so each launch doesn't re-test the GPU)."""
    import json
    import os

    from .journal import data_dir

    path = data_dir() / "encoders.json"
    try:
        st = os.stat(ffmpeg)
        stamp = f"{ffmpeg}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        stamp = ffmpeg
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    return path, stamp, data


@lru_cache(maxsize=None)
def encoder_works(encoder: str) -> bool:
    """Try a tiny test encode; NVENC is only usable with a supported NVIDIA GPU + driver.
    Results are remembered for a week per ffmpeg build (a GPU that stops working still falls back to the
    CPU encoder on Auto)."""
    import json
    import time

    ffmpeg = tools.find_tool("ffmpeg")
    if not ffmpeg:
        return False
    path, stamp, data = _encoder_cache(ffmpeg)
    hit = data.get(stamp, {}).get(encoder)
    if hit is not None and time.time() - hit[1] < _ENCODER_CACHE_DAYS * 86400:
        return bool(hit[0])
    ok = _test_encode(ffmpeg, encoder)
    data.setdefault(stamp, {})[encoder] = [ok, time.time()]
    try:
        path.write_text(json.dumps({stamp: data[stamp]}), "utf-8")   # keep only the current build's results
    except OSError:
        pass
    return ok


def _test_encode(ffmpeg: str, encoder: str, options: tuple[str, ...] | None = None, size: str = "320x240") -> bool:
    """A short test encode with the settings jobs use (so a driver that rejects one of them counts as
    unavailable, rather than failing every video later). ``options``: try these encoder settings instead (an
    optional extra, e.g. a newer tuning: the basic ones decide whether the encoder is usable at all)."""
    extra: list[str] = []
    if is_gpu(encoder):
        if options is not None and encoder.endswith("_nvenc"):
            extra = [*options, "-rc", "vbr", "-cq", "30", "-b:v", "0"]
        elif encoder in _QUALITY:
            extra = _gpu_quality_args(encoder, _QUALITY[encoder]["balanced"], tuned=False)
        extra += ["-pix_fmt", "yuv420p" if encoder.endswith("_nvenc") else "nv12"]
    elif options is not None:
        extra = [*options, "-crf", "40", "-preset", "12"]
    try:
        proc = tools.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-v", "warning" if options is not None else "error", "-f", "lavfi",
             "-i", f"color=black:s={size}:r=30:d=0.2", "-c:v", encoder, *extra, "-f", "null", "-"],
            timeout=120, check=False,
        )
        # (SVT-AV1 only warns about settings it doesn't know, and carries on without them.)
        rejected = re.search(r"Error parsing option|Unrecognized option|Unknown option|not supported",
                             proc.stderr or "", re.I)
        return proc.returncode == 0 and not (options is not None and rejected)
    except Exception:
        return False


def available_encoders() -> dict[str, bool]:
    gpus = [e for family in GPU_ENCODERS.values() for e in family]
    return {e: encoder_works(e) for e in (*gpus, *CPU_ENCODERS.values())}


def choose_encoder(codec: str, preference: str) -> str:
    cpu = CPU_ENCODERS[codec]
    if preference == "cpu":
        return cpu
    for gpu in GPU_ENCODERS[codec]:
        if encoder_works(gpu):
            return gpu
    if preference == "gpu":
        newer = codec == "av1"
        raise RuntimeError(
            f"No GPU can encode {codec.upper()} here (NVIDIA{' RTX 40 series or newer' if newer else ''}, "
            f"Intel{' Arc or Core Ultra' if newer else ''} or AMD{' RX 7000 or newer' if newer else ''}, "
            "with a recent driver). Pick Auto or CPU."
        )
    return cpu


LOCATION_KEYS = ("location", "location-eng", "com.apple.quicktime.location.ISO6709")
DEVICE_KEYS = ("make", "model", "com.apple.quicktime.make", "com.apple.quicktime.model",
               "com.apple.quicktime.software", "com.android.manufacturer", "com.android.model", "encoder",
               "artist", "author", "comment", "description", "copyright", "title")


def metadata_args(policy: str, info: VideoInfo | None = None) -> list[str]:
    """ffmpeg arguments that apply a metadata policy to the container (streams untouched)."""
    if policy == "keep":
        return ["-map_metadata", "0"]
    if policy in ("strip_gps", "strip_private"):
        args = ["-map_metadata", "0"]
        keys = LOCATION_KEYS + (DEVICE_KEYS if policy == "strip_private" else ())
        for key in keys:
            args += ["-metadata", f"{key}="]
        return args
    args = ["-map_metadata", "-1", "-fflags", "+bitexact"]
    if policy == "strip_all_keep_date" and info is not None and info.creation_time:
        args += ["-metadata", f"creation_time={info.creation_time}"]
    return args


def parallel_videos(s: Settings) -> int:
    """Videos encoded at once. NVIDIA GPUs from the RTX 40 series up have two NVENC encoders, so two GPU
    encodes run side by side at nearly full speed each; CPU encoders already use every core."""
    if s.video_jobs:
        return s.video_jobs
    try:
        return 2 if choose_encoder(codec_for(None, s), s.video_encoder).endswith("_nvenc") else 1
    except Exception:  # noqa: BLE001
        return 1


def codec_for(info: VideoInfo | None, s: Settings) -> str:
    """Automatic: HEVC for videos straight from a camera or phone (grain and fine detail, where AV1 encoders
    smooth too much; and more devices play it), AV1 for everything else."""
    if s.video_codec != "auto":
        return s.video_codec
    from .probe import is_camera_original

    return "hevc" if info is not None and is_camera_original(info) else "av1"


def select_tracks(info: VideoInfo, s: Settings) -> tuple[list, list, list]:
    """(audio kept, subtitles kept, dropped) under the "only my languages" option.

    Kept: tracks in my languages, untagged tracks (their language is unknown), and the main soundtrack (the
    default track, else the first), so a film always keeps its original audio. Nothing is dropped when
    the option is off."""
    from .languages import track_codes
    from .settings import my_languages

    audio, subs = info.audio_streams(), info.subtitle_streams()
    if s.video_tracks != "mine":
        return audio, subs, []
    codes = track_codes(my_languages(s))
    main = next((a for a in audio if a.default), audio[0] if audio else None)

    def mine(st) -> bool:
        return not st.language or st.language in codes

    keep_a = [a for a in audio if mine(a) or a is main]
    keep_s = [t for t in subs if mine(t)]
    dropped = [x for x in audio + subs if x not in keep_a and x not in keep_s]
    return keep_a, keep_s, dropped


def _hdr10_params(info: VideoInfo, encoder: str) -> str:
    """Encoder parameters that carry HDR10 mastering-display and content-light metadata (CPU encoders)."""
    if info.hdr != "HDR10" or encoder not in ("libx265", "libsvtav1"):
        return ""
    m = info.mastering
    keys = ("green_x", "green_y", "blue_x", "blue_y", "red_x", "red_y", "white_point_x", "white_point_y",
            "max_luminance", "min_luminance")
    parts = []
    if all(k in m for k in keys):
        gx, gy, bx, by, rx, ry, wx, wy, lmax, lmin = (m[k] for k in keys)
        if encoder == "libx265":
            c = [round(v / 0.00002) for v in (gx, gy, bx, by, rx, ry, wx, wy)]
            parts.append(f"master-display=G({c[0]},{c[1]})B({c[2]},{c[3]})R({c[4]},{c[5]})WP({c[6]},{c[7]})"
                         f"L({round(lmax / 0.0001)},{round(lmin / 0.0001)})")
        else:
            parts.append(f"mastering-display=G({gx:.4f},{gy:.4f})B({bx:.4f},{by:.4f})R({rx:.4f},{ry:.4f})"
                         f"WP({wx:.4f},{wy:.4f})L({lmax:.4f},{lmin:.4f})")
    if m.get("max_content") or m.get("max_average"):
        cll = f"{m.get('max_content', 0)},{m.get('max_average', 0)}"
        parts.append(f"max-cll={cll}" if encoder == "libx265" else f"content-light={cll}")
    if encoder == "libx265":
        parts = ["hdr10=1", "repeat-headers=1"] + parts
    return ":".join(parts)


@lru_cache(maxsize=None)
def _has_filter(name: str) -> bool:
    try:
        out = tools.run([tools.require("ffmpeg"), "-hide_banner", "-filters"], timeout=30, check=False).stdout
        return f" {name} " in out
    except Exception:  # noqa: BLE001
        return False


def display_filter(info: VideoInfo) -> str:
    """Filters that make frames look right on a normal screen (HDR tone-mapped to SDR, fields joined)."""
    parts = []
    if info.interlaced:
        parts.append("bwdif=mode=send_frame")
    if info.hdr and _has_filter("zscale"):
        parts.append("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
                     "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    return ",".join(parts)


def drops_repeats(info: VideoInfo, s: Settings) -> bool:
    """Whether repeated (padding) frames are removed, restoring the original frame rate."""
    return bool(s.video_fix_cadence and info.cadence)


def output_fps(info: VideoInfo, s: Settings) -> float | None:
    """The capped frame rate, or None if the frame rate isn't capped."""
    fps = info.content_fps if drops_repeats(info, s) else info.fps
    if s.video_max_fps and fps and fps > s.video_max_fps + 0.5:
        return float(s.video_max_fps)
    return None


def picture_size(info: VideoInfo) -> tuple[int, int]:
    """The picture's size before scaling: without black bars when they're removed."""
    return (info.crop[0], info.crop[1]) if info.crop else (info.width, info.height)


def frame_size(info: VideoInfo, s: Settings) -> tuple[int, int]:
    """The output frame size (bars removed, then the size limit from the options)."""
    return s.video_target_size(*picture_size(info))


def crop_filter(info: VideoInfo) -> str:
    return "crop={}:{}:{}:{}".format(*info.crop) if info.crop else ""


def detect_bars(src: Path, info: VideoInfo, cancel: threading.Event | None = None) -> tuple[int, int, int, int] | None:
    """Black bars around the picture, found at several points of the video (the area kept is the union of what
    every point shows, so nothing that's ever on screen is cut). None if there are none worth removing."""
    import re

    ffmpeg = tools.require("ffmpeg")
    seconds = max(1.0, info.duration)
    points = [seconds * f for f in (0.1, 0.25, 0.4, 0.55, 0.7, 0.85)] if seconds > 20 else [0.0]
    x0, y0, x1, y1 = info.width, info.height, 0, 0
    found = False
    for at in points:
        if cancel is not None and cancel.is_set():
            raise tools.Cancelled()
        proc = tools.run([ffmpeg, "-hide_banner", "-nostdin", "-ss", f"{at:.2f}", "-i", str(src), "-t", "2",
                          "-map", f"0:{info.main_video_index}", "-vf", "cropdetect=limit=0.094:round=2:reset=0",
                          "-f", "null", "-"], timeout=300, check=False)
        crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", proc.stderr or "")
        if not crops:
            continue
        w, h, x, y = map(int, crops[-1])
        if w <= 0 or h <= 0:
            continue
        found = True
        x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x + w), max(y1, y + h)
    if not found:
        return None
    w, h = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
    # Only when there's really something to remove (2% or more), and never a sliver of a picture.
    if w < info.width * 0.3 or h < info.height * 0.3 or (w >= info.width * 0.98 and h >= info.height * 0.98):
        return None
    return w, h, x0, y0


FIT_HEIGHTS = (2160, 1440, 1080, 720, 540, 480, 360, 240)
FIT_MIN_BPP = 0.035          # bits per pixel per frame below which HEVC/AV1 pictures fall apart


def fit_bitrates(info: VideoInfo, s: Settings, audio_tracks: int) -> tuple[int, int, tuple[int, int]]:
    """(video kbps, audio kbps per track, frame size) for a file of at most ``s.fit_under_kb``. The picture is
    made smaller when the bitrate would be too low for its size. Raises ValueError if it can't fit."""
    seconds = max(1.0, info.duration)
    total_kbps = s.fit_under_kb * 8 * 1024 / 1000 * 0.96 / seconds       # (4% for the container)
    audio_kbps = 0
    if audio_tracks:
        audio_kbps = 128 if total_kbps > 2000 else 96 if total_kbps > 600 else 64 if total_kbps > 200 else 32
    video_kbps = int(total_kbps - audio_kbps * audio_tracks)
    if video_kbps < 40:
        raise ValueError(f"It's too long to fit in {s.fit_under_kb / 1024:.1f} MB at any watchable quality")
    w, h = frame_size(info, s)
    fps = output_fps(info, s) or info.content_fps or info.fps or 30
    for height in (h, *(x for x in FIT_HEIGHTS if x < h)):
        width = max(2, round(w * height / h / 2) * 2)
        if video_kbps * 1000 / (width * height * fps) >= FIT_MIN_BPP or height == FIT_HEIGHTS[-1]:
            return video_kbps, audio_kbps, (width, height)
    return video_kbps, audio_kbps, (w, h)


def noise_sigma(src: Path, info: VideoInfo, cancel: threading.Event | None = None) -> float:
    """How grainy the picture is: the noise's standard deviation (8-bit scale) in flat areas of a few frames, at
    full resolution (scaling down would hide grain). Uses Immerkær's estimator on the flattest blocks, so edges and
    texture don't count, and leaves out clipped (black or white) areas, which hold no grain."""
    import numpy as np

    ffmpeg = tools.require("ffmpeg")
    w = min(info.width, 1280) // 16 * 16
    h = min(info.height, 720) // 16 * 16
    if w < 64 or h < 64:
        return 0.0
    kernel = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float32)
    sigmas = []
    for i in range(GRAIN_SAMPLES):
        if cancel is not None and cancel.is_set():
            raise tools.Cancelled()
        at = info.duration * (0.15 + 0.7 * i / max(1, GRAIN_SAMPLES - 1))
        proc = tools.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{at:.3f}", "-i", str(src),
                          "-map", f"0:{info.main_video_index}", "-frames:v", "1",
                          "-vf", f"crop={w}:{h},format=gray", "-f", "rawvideo", "-"], timeout=300, check=False,
                         text=False)
        data = proc.stdout or b""
        if len(data) < w * h:
            continue
        y = np.frombuffer(data[: w * h], dtype=np.uint8).reshape(h, w).astype(np.float32)
        # |I * M| (the Laplacian-difference kernel) and the local detail, per 16x16 block
        conv = np.zeros_like(y)
        for dy in range(3):
            for dx in range(3):
                conv[1:-1, 1:-1] += kernel[dy, dx] * y[dy:h - 2 + dy, dx:w - 2 + dx]
        gx = np.abs(np.diff(y, axis=1, prepend=y[:, :1]))
        gy = np.abs(np.diff(y, axis=0, prepend=y[:1, :]))
        blocks = lambda a: a.reshape(h // 16, 16, w // 16, 16).mean(axis=(1, 3))  # noqa: E731
        noise, detail, level = blocks(np.abs(conv)), blocks(gx + gy), blocks(y)
        usable = (level > 20) & (level < 235)
        if usable.sum() < 8:
            continue
        flat = detail[usable] <= np.percentile(detail[usable], 25)
        sigmas.append(float(np.sqrt(np.pi / 2) / 6 * noise[usable][flat].mean()))
    return float(np.median(sigmas)) if sigmas else 0.0


def grain_level(src: Path, info: VideoInfo, s: Settings, encoder: str,
                cancel: threading.Event | None = None) -> int:
    """The film-grain synthesis level for this video (0: none). Only for SVT-AV1, and not for interlaced video
    (its combing looks like noise)."""
    if s.video_grain != "auto" or encoder != "libsvtav1" or info.interlaced:
        return 0
    try:
        sigma = noise_sigma(src, info, cancel)
    except tools.Cancelled:
        raise
    except Exception:  # noqa: BLE001 - no estimate: no grain synthesis
        return 0
    return next(level for limit, level in GRAIN_STEPS if sigma < limit)


def build_args(src: Path, dst: Path, info: VideoInfo, s: Settings, encoder: str, q: int | None = None,
               segment: tuple[float, float] | None = None, progress: bool = True, remux: bool = False,
               fit: tuple[int, int, tuple[int, int]] | None = None, hw_decode: bool = False, grain: int = 0,
               threads: int = 0, video_from: Path | None = None, probe: bool = False) -> list[str]:
    """ffmpeg command for the full encode, or (with ``segment=(start, length)``) a sample or one part of a
    scene-by-scene encode (video only). ``remux``: copy the video as it is and only leave out tracks (audio/subtitles
    in other languages). ``probe``: a quick, faster-preset encode for choosing the quality (scene by scene).
    ``grain``: AV1 film-grain synthesis level (0: none). ``threads``: limit the encoder's
    threads (parts encoded side by side). ``video_from``: the video is already encoded (the joined parts): copy it
    from there and take everything else from ``src``."""
    ffmpeg = tools.require("ffmpeg")
    mp4 = dst.suffix.lower() == ".mp4"
    if q is None and not remux:
        q = encoder_quality(s, encoder)
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error"]
    if progress:
        args += ["-progress", "pipe:1", "-nostats"]
    if hw_decode:
        # Decode the source on the graphics card where it can (D3D11VA, CUDA, Quick Sync...): the frames come back
        # to normal memory, so filters and every encoder work as before; ffmpeg decodes on the CPU otherwise.
        args += ["-hwaccel", "auto"]
    if segment:
        if segment[0] > 0:
            args += ["-ss", f"{segment[0]:.3f}"]
        if segment[1] > 0:                                  # (0: to the end)
            args += ["-t", f"{segment[1]:.3f}"]
    args += ["-i", str(src)]
    if video_from is not None:
        args += ["-i", str(video_from)]

    # Which input streams go into the output, in order.
    keep_audio, keep_subs, dropped = select_tracks(info, s)
    if segment:
        mapped = [st for st in info.streams if st.index == info.main_video_index]
    elif mp4:
        mapped = [st for st in info.streams if st.index == info.main_video_index]
        mapped += keep_audio
        mapped += [st for st in keep_subs if st.codec in _TEXT_SUBS]
    else:  # mkv / webm keep everything except data streams (and tracks in languages left out)
        gone = {st.index for st in dropped}
        mapped = [st for st in info.streams if st.kind != "data" and st.index not in gone]
    for st in mapped:
        joined = video_from is not None and st.index == info.main_video_index
        args += ["-map", "1:v:0" if joined else f"0:{st.index}"]
    args += metadata_args(s.metadata, info)
    args += ["-map_chapters", "-1" if segment else "0", "-c", "copy"]

    # Output index of the main video stream among output video streams.
    v_out = [st for st in mapped if st.kind == "video"].index(
        next(st for st in mapped if st.index == info.main_video_index))
    if remux or video_from is not None:
        if video_from is not None:
            for key, opt in (("primaries", "color_primaries"), ("transfer", "color_trc"), ("space", "colorspace"),
                             ("range", "color_range")):
                if info.color.get(key):
                    args += [f"-{opt}:v:{v_out}", info.color[key]]
            if mp4 and codec_for(info, s) == "hevc":
                args += [f"-tag:v:{v_out}", "hvc1"]
            args += _audio_args(keep_audio, s, mp4, fit)
        if mp4:
            if any(st.kind == "subtitle" for st in mapped):
                args += ["-c:s", "mov_text"]
            args += ["-movflags", "+faststart"]
        args.append(str(dst))
        return args
    args += [f"-c:v:{v_out}", encoder]

    ten_bit = info.is_10bit or bool(info.hdr) or encoder in ("av1_nvenc", "libsvtav1")
    gpu_pix = "p010le" if ten_bit else ("yuv420p" if encoder.endswith("_nvenc") else "nv12")   # (QSV/AMF: NV12)
    hdr10 = _hdr10_params(info, encoder)
    out_w, out_h = fit[2] if fit is not None else frame_size(info, s)
    pixels = out_w * out_h
    cap = source_rate_limit(info, s, encoder) if fit is None else 0
    svt = ":".join(p for p in (svt_tuning() if encoder == "libsvtav1" else "",
                               hdr10 if encoder == "libsvtav1" else "",
                               f"film-grain={grain}:film-grain-denoise=1" if grain and encoder == "libsvtav1" else "",
                               f"lp={threads}" if threads and encoder == "libsvtav1" else "") if p)
    if cap and encoder == "libsvtav1":
        svt = ":".join(p for p in (svt, f"mbr={min(cap, 100000)}") if p)
    x265 = ":".join(p for p in ("log-level=error", hdr10 if encoder == "libx265" else "",
                                f"pools={threads}" if threads and encoder == "libx265" else "") if p)
    if fit is not None:                  # a size limit: an average bitrate instead of a constant quality
        kbps = fit[0]
        rate = ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k"]
        if is_gpu(encoder):
            args += _gpu_rate_args(encoder, kbps, pixels)
            pix = gpu_pix
        elif encoder == "libsvtav1":
            args += ["-preset", "6", "-b:v", f"{kbps}k"]
            if svt:
                args += ["-svtav1-params", svt]
            pix = "yuv420p10le"
        else:
            args += ["-preset", "medium", *rate, "-x265-params", x265]
            pix = "yuv420p10le" if ten_bit else "yuv420p"
    elif is_gpu(encoder):
        args += _gpu_quality_args(encoder, q if q is not None else encoder_quality(s, encoder), pixels=pixels)
        pix = gpu_pix
    elif encoder == "libsvtav1":
        args += ["-crf", str(q), "-preset", str(SVT_PROBE_PRESET if probe else 6)]
        if svt:
            args += ["-svtav1-params", svt]
        pix = "yuv420p10le"
    else:  # libx265
        args += ["-crf", str(q), "-preset", "veryfast" if probe else "medium", "-x265-params", x265]
        pix = "yuv420p10le" if ten_bit else "yuv420p"
    if cap and encoder != "libsvtav1":
        args += ["-maxrate", f"{cap}k", "-bufsize", f"{cap * 2}k"]
    args += [f"-pix_fmt:v:{v_out}", pix]
    # Keep the color description (essential for HDR: without it the picture looks washed out).
    for key, opt in (("primaries", "color_primaries"), ("transfer", "color_trc"), ("space", "colorspace"),
                     ("range", "color_range")):
        if info.color.get(key):
            args += [f"-{opt}:v:{v_out}", info.color[key]]

    filters = []
    if info.interlaced:
        filters.append("bwdif=mode=send_frame:deint=all")  # one progressive frame per frame, no combing
    if info.crop:
        filters.append(crop_filter(info))                  # black bars removed
    if drops_repeats(info, s):
        filters.append(f"decimate=cycle={info.cadence}")  # drops the most similar frame in each cycle
    tw, th = fit[2] if fit is not None else frame_size(info, s)
    if (tw, th) != picture_size(info):
        filters.append(f"scale={tw}:{th}:flags=lanczos")
    fps = output_fps(info, s)
    if fps:
        filters.append(f"fps={fps:g}")
    if segment:
        filters.append("setpts=PTS-STARTPTS")      # (a part starts at 0: parts are joined by their exact lengths)
    if filters:
        args += [f"-filter:v:{v_out}", ",".join(filters)]
    if mp4 and (encoder.startswith("hevc_") or encoder == "libx265"):
        args += [f"-tag:v:{v_out}", "hvc1"]  # plays in Windows/Apple players

    if not segment:
        args += _audio_args(keep_audio, s, mp4, fit)
        if mp4:
            if any(st.kind == "subtitle" for st in mapped):
                args += ["-c:s", "mov_text"]
            args += ["-movflags", "+faststart"]
    args.append(str(dst))
    return args


def _audio_args(keep_audio: list, s: Settings, mp4: bool, fit) -> list[str]:
    args: list[str] = []
    for i, st in enumerate(keep_audio):
        if fit is not None and fit[1]:
            codec = "aac" if mp4 else "libopus"
            args += [f"-c:a:{i}", codec, f"-b:a:{i}", f"{fit[1]}k"]
        elif s.audio_bitrate:
            codec = "aac" if mp4 else "libopus"
            args += [f"-c:a:{i}", codec, f"-b:a:{i}", f"{s.audio_bitrate}k"]
        elif mp4 and st.codec not in _MP4_AUDIO_OK:
            args += [f"-c:a:{i}", "aac", f"-b:a:{i}", "192k"]
    return args


SMART_STEPS = {  # best quality first
    "av1_nvenc": [20, 24, 28, 30, 32, 34, 36, 38, 40, 42, 44],
    "libsvtav1": [20, 24, 28, 30, 32, 34, 36, 38, 40, 42, 44],
    "hevc_nvenc": [18, 21, 24, 26, 28, 30, 32, 34, 36],
    "libx265": [16, 19, 22, 24, 26, 28, 30, 32, 34],
    "av1_qsv": [18, 21, 24, 26, 28, 30, 32, 34, 36, 38],
    "hevc_qsv": [18, 21, 24, 26, 28, 30, 32, 34],
    "av1_amf": [80, 96, 108, 116, 124, 132, 140, 148, 156, 164],
    "hevc_amf": [18, 21, 24, 26, 28, 30, 32, 34],
}
SAMPLE_SECONDS = 4.0


def sample_segments(duration: float) -> list[tuple[float, float]]:
    """Up to three short, spread-out segments (the whole clip if it's short)."""
    if duration <= 3 * SAMPLE_SECONDS * 1.5:
        return [(0.0, max(0.5, duration))]
    return [(max(0.0, duration * f - SAMPLE_SECONDS / 2), SAMPLE_SECONDS) for f in (0.2, 0.5, 0.8)]


def smart_quality(src: Path, info: VideoInfo, s: Settings, encoder: str,
                  cancel: threading.Event | None = None, grain: int = 0) -> tuple[int, str]:
    """Pick the most compressed quality setting whose samples meet the perceptual target."""
    from . import quality

    metric = quality.video_metric(s.video_metric)

    target = "identical" if s.video_quality == "identical" else s.smart_target
    size = frame_size(info, s)
    segments = sample_segments(info.duration)
    samples: list[Path] = []

    def attempt(q: int):
        scores = []
        for number, (start, length) in enumerate(segments, 1):
            steps.report(f"Testing {quality_label(encoder)} {q}: sample {number} of {len(segments)}")
            if cancel is not None and cancel.is_set():
                raise tools.Cancelled()
            sample = temp_name(src.parent, ".mkv", "-sample")
            samples.append(sample)
            tools.run(build_args(src, sample, info, s, encoder, q, segment=(start, length), progress=False,
                                 grain=grain), timeout=1800)
            scores.append(quality.score_video_segment(src, start, length, sample, size, crop_filter(info), metric,
                                                      grain))
            remove_quietly(sample)
        score = quality.mean(scores)
        return score.meets(target), score

    try:
        ladder = SMART_STEPS[encoder]
        chosen = quality.search(ladder, attempt)
        if chosen is None and grain:
            # Some textures don't survive the denoising: if the target can't be reached with grain re-created,
            # keep the grain instead (the caller sees grain 0 in the choice).
            grain = 0
            chosen = quality.search(ladder, attempt)
            if chosen is not None:
                return chosen[0], f"{quality_label(encoder)} {chosen[0]} (grain kept)"
    finally:
        remove_quietly(*samples)
    name = quality_label(encoder)
    if chosen is None:
        if source_rate_limit(info, s, encoder):
            raise NotWorthIt("Requested video quality cannot be reached within the source bitrate budget; original kept")
        return ladder[0], f"{name} {ladder[0]} (target not reached)"
    q, score = chosen
    return q, f"{name} {q}"


def measure(src: Path, info: VideoInfo, out: Path, s: Settings, cancel: threading.Event | None = None,
            grain: int = 0):
    """How close the finished video is to the original (VMAF, or the measure chosen in the settings) over up to
    three short stretches, the worst one counting. None if it can't be measured."""
    from . import quality

    size = frame_size(info, s)
    metric = quality.video_metric(s.video_metric)
    scores = []
    try:
        for start, length in sample_segments(info.duration):
            if cancel is not None and cancel.is_set():
                return None
            scores.append(quality.score_video_output(src, out, start, length, size, crop_filter(info), metric,
                                                     grain))
    except tools.Cancelled:
        raise
    except Exception:  # noqa: BLE001 - a score is information, never a reason to fail
        from .log import log

        log.warning("Video quality measurement unavailable for %s", src, exc_info=True)
        return None
    return quality.worst(scores) if scores else None


@dataclass
class VideoPreview:
    before: bytes          # PNG of a source frame (source resolution)
    after: bytes           # PNG of the same frame from the sample encode
    est_size: int          # whole-file size estimated from the sample's bitrate
    start: float
    length: float
    encoder: str
    note: str              # Smart quality choice and/or measured score
    frames: list = field(default_factory=list)   # [(original frame PNG path, compressed frame PNG path)]
    times: list = field(default_factory=list)    # position of each frame in the video (seconds)
    folder: Path | None = None                   # temp folder holding the frames (caller removes it)


PREVIEW_SECONDS = 6.0
_DEFAULT_AUDIO_BPS = 160_000  # when the source audio bitrate isn't known


def _frame_png(path: Path, at: float, stream: str, vf: str = "") -> bytes:
    ffmpeg = tools.require("ffmpeg")
    proc = tools.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{max(0.0, at):.3f}", "-i",
                      str(path), "-map", stream, *(["-vf", vf] if vf else []), "-frames:v", "1", "-f", "image2pipe",
                      "-c:v", "png", "-"], timeout=300, text=False)
    if not proc.stdout:
        raise RuntimeError("Couldn't read a frame for the preview")
    return proc.stdout


def _frame_sequence(path: Path, stream: str, folder: Path, prefix: str, count: int, length: float,
                    start: float | None = None, vf: str = "") -> list[Path]:
    """``count`` evenly spaced frames (PNG files) from the first ``length`` seconds (after ``start``)."""
    ffmpeg = tools.require("ffmpeg")
    args = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error"]
    if start is not None:
        args += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"]
    chain = ",".join(x for x in (vf, f"fps={count / length:.6f}") if x)
    args += ["-i", str(path), "-map", stream, "-vf", chain, "-frames:v", str(count),
             str(folder / f"{prefix}_%03d.png")]
    tools.run(args, timeout=600)
    return sorted(folder.glob(f"{prefix}_*.png"))


def preview_video(src: Path, info: VideoInfo, s: Settings, cancel: threading.Event | None = None,
                  start: float | None = None, frame_count: int = 12) -> VideoPreview:
    """Encode a short sample (from the middle, or from ``start``), measure it, estimate the full result,
    and extract matching frames from the original and the sample for a side-by-side comparison."""
    import tempfile

    from . import quality

    codec = codec_for(info, s)
    encoder = choose_encoder(codec, s.video_encoder)
    if s.video_crop_bars:
        import dataclasses

        bars = detect_bars(src, info, cancel)
        if bars:
            info = dataclasses.replace(info, crop=bars)
    q, note = None, ""
    if measures_quality(s):
        q, note = smart_quality(src, info, s, encoder, cancel)
    elif s.video_quality != "custom":           # (Custom's value is in the description already)
        note = f"{quality_label(encoder)} {encoder_quality(s, encoder)}"
    length = min(PREVIEW_SECONDS, info.duration)
    if start is None:
        start = info.duration / 2 - length / 2
    start = max(0.0, min(start, info.duration - length))
    sample = temp_name(src.parent, ".mkv", "-preview")
    try:
        steps.report(f"Encoding {codec.upper()} preview sample ({encoder})")
        try:
            tools.run(build_args(src, sample, info, s, encoder, q, segment=(start, length), progress=False),
                      timeout=1800)
        except tools.ToolError:
            if s.video_encoder != "auto" or encoder == CPU_ENCODERS[codec]:
                raise
            encoder, q, note = CPU_ENCODERS[codec], None, ""
            tools.run(build_args(src, sample, info, s, encoder, segment=(start, length), progress=False),
                      timeout=1800)
        if cancel is not None and cancel.is_set():
            raise tools.Cancelled()
        video_bps = sample.stat().st_size * 8 / max(0.1, length)
        audio_bps = len(info.audio_streams()) * (s.audio_bitrate * 1000 if s.audio_bitrate else _DEFAULT_AUDIO_BPS)
        est = int((video_bps + audio_bps) * info.duration / 8)
        # Always show how close the sample is (a measured choice puts its setting in front).
        steps.report("Measuring preview quality")
        try:
            size = frame_size(info, s)
            sc = quality.score_video_segment(src, start, length, sample, size, crop_filter(info))
            note = f"{note}, {sc} ({sc.verdict()})" if note else f"{sc} ({sc.verdict()})"
        except tools.Cancelled:
            raise
        except Exception:  # noqa: BLE001 - the score is a bonus
            from .log import log

            log.warning("Preview quality measurement unavailable for %s", src, exc_info=True)
        mid = length / 2
        steps.report("Extracting comparison frames")
        show = display_filter(info)
        tone = show.replace("bwdif=mode=send_frame,", "").replace("bwdif=mode=send_frame", "")  # sample: progressive
        before = _frame_png(src, start + mid, f"0:{info.main_video_index}", show)
        after = _frame_png(sample, mid, "0:v:0", tone)
        folder = Path(tempfile.mkdtemp(prefix="wick-preview-"))
        frames, times = [], []
        try:
            a = _frame_sequence(src, f"0:{info.main_video_index}", folder, "a", frame_count, length, start, show)
            b = _frame_sequence(sample, "0:v:0", folder, "b", frame_count, length, vf=tone)
            frames = list(zip(a, b, strict=False))     # (the shorter sequence decides)
            times = [start + i * length / frame_count for i in range(len(frames))]
        except tools.Cancelled:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)
            raise
        except Exception:  # noqa: BLE001 - the single middle frame still works
            from .log import log

            log.warning("Preview frame sequence unavailable for %s; using a single frame", src, exc_info=True)
        return VideoPreview(before, after, est, start, length, encoder, note, frames, times, folder)
    finally:
        remove_quietly(sample)


def convert_video(
    src: Path,
    info: VideoInfo,
    plan: Plan,
    s: Settings,
    on_progress: Callable[[float], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[Path, str]:
    """Encode to a temp file next to ``src``; verify; return (temp path, encoder used)."""
    out = temp_name(src.parent, plan.target_ext)
    keep_audio, _subs, dropped = select_tracks(info, s)
    if plan.route == "video_remux":   # efficient already: only leave out tracks in other languages
        try:
            steps.report("Copying the video without the tracks in other languages")
            tools.run_ffmpeg_progress(build_args(src, out, info, s, "copy", remux=True), info.duration,
                                      on_progress, cancel)
            steps.report("Checking the result")
            verify_video(info, out, info.codec, dst_is_mp4=out.suffix == ".mp4", audio_tracks=len(keep_audio))
            plan.outcome = (f"picture untouched, {len(dropped)} track{'s' if len(dropped) != 1 else ''} in other "
                            "languages left out")
            return out, "copy"
        except BaseException:
            remove_quietly(out)
            raise
    codec = codec_for(info, s)
    encoder = choose_encoder(codec, s.video_encoder)
    if s.video_crop_bars:
        import dataclasses

        steps.report("Looking for black bars")
        bars = detect_bars(src, info, cancel)
        if bars:
            info = dataclasses.replace(info, crop=bars)
    limit = s.fit_under_kb * 1024
    if limit and src.stat().st_size > limit:
        return _encode_to_fit(src, out, info, plan, s, codec, encoder, len(keep_audio), on_progress, cancel)
    from . import chunked

    q = None
    notes: list[str] = []
    grain = 0
    if encoder == "libsvtav1" and s.video_grain == "auto":
        steps.report("Checking for film grain")
        grain = grain_level(src, info, s, encoder, cancel)
        if grain:
            notes.append(f"film grain re-created on playback (level {grain})")

    def finish(used: str) -> tuple[Path, str]:
        steps.report("Checking the result")
        verify_video(info, out, codec, dst_is_mp4=out.suffix == ".mp4",
                     expected_size=frame_size(info, s), audio_tracks=len(keep_audio))
        steps.report("Measuring how close it looks to the original")
        score = measure(src, info, out, s, cancel, grain if used == "libsvtav1" else 0)
        if score is not None:
            check_quality_budget(score, info, s, used)
            notes.append(f"{score} ({score.verdict()})")
        if dropped:
            notes.append(f"{len(dropped)} track{'s' if len(dropped) != 1 else ''} in other languages left out")
        if info.crop:
            notes.append(f"black bars removed ({info.crop[0]}×{info.crop[1]})")
        plan.outcome = ", ".join(notes)
        return out, used

    if chunked.applies(info, s, encoder):
        try:
            _, part_notes = chunked.encode(src, out, info, s, encoder, grain, on_progress, cancel)
            notes += part_notes
            result = finish(encoder)
            chunked.finished(src, s, encoder, grain)
            return result
        except (tools.Cancelled, NotWorthIt):
            remove_quietly(out)            # (the finished parts stay: the next run carries on from them)
            raise
        except Exception as e:  # noqa: BLE001 - encode it as a whole instead
            from .log import log

            log.warning("Scene-by-scene encoding failed for %s, encoding it as a whole: %s", src.name, e)
            remove_quietly(out)
            chunked.finished(src, s, encoder, grain)
            notes = [n for n in notes if n.startswith("film grain")] + ["encoded as a whole"]
    if measures_quality(s):
        steps.report("Encoding short samples to choose the quality")
        q, choice = smart_quality(src, info, s, encoder, cancel, grain)
        if choice.endswith("(grain kept)"):
            grain = 0
            notes = [n for n in notes if not n.startswith("film grain")]
        notes.append(choice)
    hw = s.video_encoder != "cpu" and HW_DECODE    # ("CPU only" keeps the graphics card out of it entirely)
    try:
        try:
            steps.report(f"Encoding {codec.upper()} video ({encoder})")
            try:
                tools.run_ffmpeg_progress(build_args(src, out, info, s, encoder, q, hw_decode=hw, grain=grain),
                                          info.duration, on_progress, cancel)
            except tools.ToolError:
                if not hw:
                    raise
                remove_quietly(out)        # hardware decoding failed on this file: decode it on the CPU
                steps.report(f"Encoding {codec.upper()} video ({encoder}, decoding on the processor)")
                tools.run_ffmpeg_progress(build_args(src, out, info, s, encoder, q, grain=grain), info.duration,
                                          on_progress, cancel)
        except tools.ToolError:
            # Some sources exceed GPU limits (e.g. > 8K, unusual formats). On Auto,
            # retry on the CPU; on an explicit GPU choice, report the failure.
            if s.video_encoder != "auto" or encoder == CPU_ENCODERS[codec]:
                raise
            remove_quietly(out)
            encoder, notes = CPU_ENCODERS[codec], []   # the measured choice was for the GPU encoder
            q, grain = None, 0
            if measures_quality(s):
                q, choice = smart_quality(src, info, s, encoder, cancel)
                notes.append(choice)
            steps.report(f"The graphics card couldn't encode this video: encoding {codec.upper()} on the processor "
                         f"({encoder})")
            tools.run_ffmpeg_progress(build_args(src, out, info, s, encoder, q), info.duration, on_progress, cancel)
        return finish(encoder)
    except BaseException:
        remove_quietly(out)
        raise


def _encode_to_fit(src: Path, out: Path, info: VideoInfo, plan: Plan, s: Settings, codec: str, encoder: str,
                   audio_tracks: int, on_progress, cancel) -> tuple[Path, str]:
    """Encode at the bitrate that fits ``s.fit_under_kb``; if the encoder overshoots, again a little lower."""
    from .formats import human

    limit = s.fit_under_kb * 1024
    try:
        video_kbps, audio_kbps, size = fit_bitrates(info, s, audio_tracks)
    except ValueError as e:
        raise tools.ToolError(["fit"], 1, str(e)) from None
    try:
        for attempt in range(3):
            steps.report(f"Encoding {codec.upper()} at {video_kbps:,} kbps to fit under {human(limit)} ({encoder})")
            fit = (video_kbps, audio_kbps, size)
            try:
                tools.run_ffmpeg_progress(build_args(src, out, info, s, encoder, fit=fit), info.duration,
                                          on_progress, cancel)
            except tools.ToolError:
                if s.video_encoder != "auto" or encoder == CPU_ENCODERS[codec]:
                    raise
                remove_quietly(out)
                encoder = CPU_ENCODERS[codec]
                tools.run_ffmpeg_progress(build_args(src, out, info, s, encoder, fit=fit), info.duration,
                                          on_progress, cancel)
            got = out.stat().st_size
            if got <= limit:
                break
            if attempt == 2:
                raise tools.ToolError(["fit"], 1, f"The encoder couldn't get it under {human(limit)} "
                                                  f"(it made {human(got)})")
            video_kbps = max(30, int(video_kbps * limit / got * 0.93))     # overshot: aim lower
        steps.report("Checking the result")
        verify_video(info, out, codec, dst_is_mp4=out.suffix == ".mp4", expected_size=size,
                     audio_tracks=audio_tracks)
        plan.outcome = f"{video_kbps:,} kbps, {size[0]}×{size[1]} to fit under {human(limit)}"
        return out, encoder
    except BaseException:
        remove_quietly(out)
        raise


def verify_video(src: VideoInfo, out: Path, codec: str, dst_is_mp4: bool,
                 expected_size: tuple[int, int] | None = None, audio_tracks: int | None = None) -> None:
    got = probe_video(out)
    if got is None:
        raise VerificationFailed("Output video can't be read")
    want_codec = {"av1": "av1", "hevc": "hevc", "h264": "h264", "vp9": "vp9"}.get(codec, codec)
    if got.codec != want_codec:
        raise VerificationFailed(f"Output codec is {got.codec}, expected {want_codec}")
    tolerance = max(1.0, src.duration * 0.01)
    if abs(got.duration - src.duration) > tolerance:
        raise VerificationFailed(
            f"Output is {got.duration:.1f}s long, original {src.duration:.1f}s")
    want_audio = len(src.audio_streams()) if audio_tracks is None else audio_tracks
    if len(got.audio_streams()) != want_audio:
        raise VerificationFailed("Audio tracks are missing from the output")
    if expected_size is None:
        expected_size = (src.width - src.width % 2, src.height - src.height % 2)
    if (got.width, got.height) != tuple(expected_size):
        raise VerificationFailed(f"Output is {got.width}×{got.height}, expected {expected_size[0]}×{expected_size[1]}")
    # Decode the start and the end to catch truncated or corrupt output.
    ffmpeg = tools.require("ffmpeg")
    for seek in (["-t", "3"], ["-sseof", "-3"]):
        pre, post = (seek, []) if seek[0] == "-sseof" else ([], seek)
        proc = tools.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", *pre, "-i", str(out),
                          *post, "-map", "0:v:0", "-f", "null", "-"], timeout=300, check=False)
        if proc.returncode != 0 or proc.stderr.strip():
            raise VerificationFailed("Output video has decoding errors")
