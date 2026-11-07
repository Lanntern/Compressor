"""Video conversion with ffmpeg (AV1 / HEVC, GPU or CPU) and verification."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from . import steps, tools
from .fileops import remove_quietly, temp_name
from .images import VerificationFailed
from .plan import Plan
from .probe import VideoInfo, probe_video
from .settings import Settings

GPU_ENCODERS = {"av1": "av1_nvenc", "hevc": "hevc_nvenc"}
CPU_ENCODERS = {"av1": "libsvtav1", "hevc": "libx265"}

# Constant-quality values per encoder for high / balanced / small.
_QUALITY = {
    "av1_nvenc": {"high": 26, "balanced": 32, "small": 38},
    "hevc_nvenc": {"high": 22, "balanced": 27, "small": 32},
    "libsvtav1": {"high": 26, "balanced": 32, "small": 38},
    "libx265": {"high": 20, "balanced": 24, "small": 28},
}

# Audio codecs that can be copied into MP4 as-is; anything else becomes AAC.
_MP4_AUDIO_OK = {"aac", "mp3", "ac3", "eac3", "alac", "opus", "flac"}
_TEXT_SUBS = {"mov_text", "subrip", "srt", "ass", "ssa", "webvtt", "text"}


_ENCODER_CACHE_DAYS = 7
CRF_MAX = {"av1_nvenc": 63, "libsvtav1": 63, "hevc_nvenc": 51, "libx265": 51}


def encoder_quality(s: Settings, encoder: str) -> int:
    """The constant-quality value for ``encoder`` (Custom: the user's own, within the encoder's range)."""
    if s.video_quality == "custom":
        return max(1, min(CRF_MAX.get(encoder, 51), s.video_crf))
    return _QUALITY[encoder]["balanced" if s.video_quality == "identical" else s.video_quality]


def measures_quality(s: Settings) -> bool:
    """Whether the quality is chosen by measuring samples (Visually identical, or Smart quality's target).
    A Custom value is used as it is."""
    return s.video_quality == "identical" or (s.smart_quality and s.video_quality != "custom")


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


def _test_encode(ffmpeg: str, encoder: str) -> bool:
    try:
        proc = tools.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi",
             "-i", "color=black:s=320x240:r=30:d=0.2", "-c:v", encoder, "-f", "null", "-"],
            timeout=60, check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def available_encoders() -> dict[str, bool]:
    return {e: encoder_works(e) for e in (*GPU_ENCODERS.values(), *CPU_ENCODERS.values())}


def choose_encoder(codec: str, preference: str) -> str:
    gpu, cpu = GPU_ENCODERS[codec], CPU_ENCODERS[codec]
    if preference == "cpu":
        return cpu
    if encoder_works(gpu):
        return gpu
    if preference == "gpu":
        raise RuntimeError(
            f"GPU encoder {gpu} isn't available (needs an NVIDIA GPU"
            f"{' RTX 40 series or newer' if codec == 'av1' else ''} and a recent driver). "
            f"Pick Auto or CPU."
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


def build_args(src: Path, dst: Path, info: VideoInfo, s: Settings, encoder: str, q: int | None = None,
               segment: tuple[float, float] | None = None, progress: bool = True, remux: bool = False,
               fit: tuple[int, int, tuple[int, int]] | None = None) -> list[str]:
    """ffmpeg command for the full encode, or (with ``segment=(start, length)``) a sample. ``remux``: copy the
    video as it is and only leave out tracks (audio/subtitles in other languages)."""
    ffmpeg = tools.require("ffmpeg")
    mp4 = dst.suffix.lower() == ".mp4"
    if q is None and not remux:
        q = encoder_quality(s, encoder)
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error"]
    if progress:
        args += ["-progress", "pipe:1", "-nostats"]
    if segment:
        args += ["-ss", f"{segment[0]:.3f}", "-t", f"{segment[1]:.3f}"]
    args += ["-i", str(src)]

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
        args += ["-map", f"0:{st.index}"]
    args += metadata_args(s.metadata, info)
    args += ["-map_chapters", "-1" if segment else "0", "-c", "copy"]

    # Output index of the main video stream among output video streams.
    v_out = [st for st in mapped if st.kind == "video"].index(
        next(st for st in mapped if st.index == info.main_video_index))
    if remux:
        if mp4:
            if any(st.kind == "subtitle" for st in mapped):
                args += ["-c:s", "mov_text"]
            args += ["-movflags", "+faststart"]
        args.append(str(dst))
        return args
    args += [f"-c:v:{v_out}", encoder]

    ten_bit = info.is_10bit or bool(info.hdr) or encoder in ("av1_nvenc", "libsvtav1")
    hdr10 = _hdr10_params(info, encoder)
    if fit is not None:                  # a size limit: an average bitrate instead of a constant quality
        kbps = fit[0]
        rate = ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k"]
        if encoder.endswith("_nvenc"):
            args += ["-preset", "p6", "-tune", "hq", "-rc", "vbr", "-multipass", "fullres", *rate, "-spatial-aq", "1"]
            pix = "p010le" if ten_bit else "yuv420p"
        elif encoder == "libsvtav1":
            args += ["-preset", "6", "-b:v", f"{kbps}k"]
            if hdr10:
                args += ["-svtav1-params", hdr10]
            pix = "yuv420p10le"
        else:
            args += ["-preset", "medium", *rate, "-x265-params", "log-level=error" + (f":{hdr10}" if hdr10 else "")]
            pix = "yuv420p10le" if ten_bit else "yuv420p"
    elif encoder.endswith("_nvenc"):
        args += ["-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0",
                 "-spatial-aq", "1"]
        pix = "p010le" if ten_bit else "yuv420p"
    elif encoder == "libsvtav1":
        args += ["-crf", str(q), "-preset", "6"]
        if hdr10:
            args += ["-svtav1-params", hdr10]
        pix = "yuv420p10le"
    else:  # libx265
        args += ["-crf", str(q), "-preset", "medium", "-x265-params", "log-level=error" + (f":{hdr10}" if hdr10 else "")]
        pix = "yuv420p10le" if ten_bit else "yuv420p"
    args += [f"-pix_fmt:v:{v_out}", pix]
    # Keep the colour description (essential for HDR: without it the picture looks washed out).
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
    if filters:
        args += [f"-filter:v:{v_out}", ",".join(filters)]
    if mp4 and encoder in ("hevc_nvenc", "libx265"):
        args += [f"-tag:v:{v_out}", "hvc1"]  # plays in Windows/Apple players

    if not segment:
        for i, st in enumerate(keep_audio):
            if fit is not None and fit[1]:
                codec = "aac" if mp4 else "libopus"
                args += [f"-c:a:{i}", codec, f"-b:a:{i}", f"{fit[1]}k"]
            elif s.audio_bitrate:
                codec = "aac" if mp4 else "libopus"
                args += [f"-c:a:{i}", codec, f"-b:a:{i}", f"{s.audio_bitrate}k"]
            elif mp4 and st.codec not in _MP4_AUDIO_OK:
                args += [f"-c:a:{i}", "aac", f"-b:a:{i}", "192k"]
        if mp4:
            if any(st.kind == "subtitle" for st in mapped):
                args += ["-c:s", "mov_text"]
            args += ["-movflags", "+faststart"]
    args.append(str(dst))
    return args


SMART_STEPS = {  # best quality first
    "av1_nvenc": [20, 24, 28, 30, 32, 34, 36, 38, 40, 42, 44],
    "libsvtav1": [20, 24, 28, 30, 32, 34, 36, 38, 40, 42, 44],
    "hevc_nvenc": [18, 21, 24, 26, 28, 30, 32, 34, 36],
    "libx265": [16, 19, 22, 24, 26, 28, 30, 32, 34],
}
SAMPLE_SECONDS = 4.0


def sample_segments(duration: float) -> list[tuple[float, float]]:
    """Up to three short, spread-out segments (the whole clip if it's short)."""
    if duration <= 3 * SAMPLE_SECONDS * 1.5:
        return [(0.0, max(0.5, duration))]
    return [(max(0.0, duration * f - SAMPLE_SECONDS / 2), SAMPLE_SECONDS) for f in (0.2, 0.5, 0.8)]


def smart_quality(src: Path, info: VideoInfo, s: Settings, encoder: str,
                  cancel: threading.Event | None = None) -> tuple[int, str]:
    """Pick the most compressed quality setting whose samples meet the perceptual target."""
    from . import quality

    target = "identical" if s.video_quality == "identical" else s.smart_target
    size = frame_size(info, s)
    segments = sample_segments(info.duration)
    samples: list[Path] = []

    def attempt(q: int):
        scores = []
        for start, length in segments:
            if cancel is not None and cancel.is_set():
                raise tools.Cancelled()
            sample = temp_name(src.parent, ".mkv", "-sample")
            samples.append(sample)
            tools.run(build_args(src, sample, info, s, encoder, q, segment=(start, length), progress=False),
                      timeout=1800)
            scores.append(quality.score_video_segment(src, start, length, sample, size, crop_filter(info)))
            remove_quietly(sample)
        mean = sum(x.value for x in scores) / len(scores)
        score = quality.Score(scores[0].metric, mean)
        return score.meets(target), score

    try:
        ladder = SMART_STEPS[encoder]
        chosen = quality.search(ladder, attempt)
    finally:
        remove_quietly(*samples)
    name = "CQ" if encoder.endswith("nvenc") else "CRF"
    if chosen is None:
        return ladder[0], f"{name} {ladder[0]} (target not reached)"
    q, score = chosen
    return q, f"{name} {q}"


def measure(src: Path, info: VideoInfo, out: Path, s: Settings, cancel: threading.Event | None = None):
    """How close the finished video is to the original: VMAF (or SSIM) over up to three short stretches,
    the lowest one counting. None if it can't be measured."""
    from . import quality

    size = frame_size(info, s)
    scores = []
    try:
        for start, length in sample_segments(info.duration):
            if cancel is not None and cancel.is_set():
                return None
            scores.append(quality.score_video_output(src, out, start, length, size, crop_filter(info)))
    except Exception:  # noqa: BLE001 - a score is information, never a reason to fail
        return None
    if not scores:
        return None
    worst = min(scores, key=lambda x: x.value)
    return worst


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
        note = f"{'CQ' if encoder.endswith('nvenc') else 'CRF'} {encoder_quality(s, encoder)}"
    length = min(PREVIEW_SECONDS, info.duration)
    if start is None:
        start = info.duration / 2 - length / 2
    start = max(0.0, min(start, info.duration - length))
    sample = temp_name(src.parent, ".mkv", "-preview")
    try:
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
        try:
            size = frame_size(info, s)
            sc = quality.score_video_segment(src, start, length, sample, size, crop_filter(info))
            note = f"{note}, {sc} ({sc.verdict()})" if note else f"{sc} ({sc.verdict()})"
        except Exception:  # noqa: BLE001 - the score is a bonus
            pass
        mid = length / 2
        show = display_filter(info)
        tone = show.replace("bwdif=mode=send_frame,", "").replace("bwdif=mode=send_frame", "")  # sample: progressive
        before = _frame_png(src, start + mid, f"0:{info.main_video_index}", show)
        after = _frame_png(sample, mid, "0:v:0", tone)
        folder = Path(tempfile.mkdtemp(prefix="compressor-preview-"))
        frames, times = [], []
        try:
            a = _frame_sequence(src, f"0:{info.main_video_index}", folder, "a", frame_count, length, start, show)
            b = _frame_sequence(sample, "0:v:0", folder, "b", frame_count, length, vf=tone)
            frames = list(zip(a, b, strict=False))     # (the shorter sequence decides)
            times = [start + i * length / frame_count for i in range(len(frames))]
        except Exception:  # noqa: BLE001 - the single middle frame still works
            pass
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
    q = None
    notes = []
    if measures_quality(s):
        steps.report("Encoding short samples to choose the quality")
        q, choice = smart_quality(src, info, s, encoder, cancel)
        notes.append(choice)
    try:
        try:
            steps.report(f"Encoding {codec.upper()} video ({encoder})")
            tools.run_ffmpeg_progress(build_args(src, out, info, s, encoder, q), info.duration, on_progress, cancel)
        except tools.ToolError:
            # Some sources exceed GPU limits (e.g. > 8K, unusual formats). On Auto,
            # retry on the CPU; on an explicit GPU choice, report the failure.
            if s.video_encoder != "auto" or encoder == CPU_ENCODERS[codec]:
                raise
            remove_quietly(out)
            encoder, notes = CPU_ENCODERS[codec], []   # the measured choice was for the GPU encoder
            q = None
            if s.video_quality == "identical":
                q, choice = smart_quality(src, info, s, encoder, cancel)
                notes.append(choice)
            steps.report(f"The graphics card couldn't encode this video: encoding {codec.upper()} on the processor "
                         f"({encoder})")
            tools.run_ffmpeg_progress(build_args(src, out, info, s, encoder, q), info.duration, on_progress, cancel)
        steps.report("Checking the result")
        verify_video(info, out, codec, dst_is_mp4=out.suffix == ".mp4",
                     expected_size=frame_size(info, s), audio_tracks=len(keep_audio))
        steps.report("Measuring how close it looks to the original")
        score = measure(src, info, out, s, cancel)
        if score is not None:
            notes.append(f"{score} ({score.verdict()})")
        if dropped:
            notes.append(f"{len(dropped)} track{'s' if len(dropped) != 1 else ''} in other languages left out")
        if info.crop:
            notes.append(f"black bars removed ({info.crop[0]}×{info.crop[1]})")
        plan.outcome = ", ".join(notes)
        return out, encoder
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

