"""Reading video metadata with ffprobe."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import tools


@dataclass
class StreamInfo:
    index: int
    kind: str          # video | audio | subtitle | data | attachment
    codec: str
    attached_pic: bool = False
    language: str = ""     # ISO 639-2 as tagged ("eng", "swe"...); "" when untagged
    title: str = ""
    default: bool = False
    forced: bool = False   # forced subtitles: shown for foreign-language lines only


@dataclass
class VideoInfo:
    codec: str
    width: int
    height: int
    pix_fmt: str
    fps: float
    duration: float
    bit_rate: int              # whole file, bits/s
    main_video_index: int      # input stream index of the main video stream
    streams: list[StreamInfo] = field(default_factory=list)
    tags: dict = field(default_factory=dict)   # container metadata
    creation_time: str = ""
    cadence: int = 0           # N if one frame in every N is a repeat (24 fps stored as 30: 5); 0 = none found
    vfr: bool = False          # variable frame rate (phones)
    interlaced: bool = False   # fields, not frames (camcorders, TV recordings)
    hdr: str = ""              # "" (SDR), "HDR10", "HLG" or "Dolby Vision"
    color: dict = field(default_factory=dict)       # primaries / transfer / matrix / range, as ffmpeg names them
    mastering: dict = field(default_factory=dict)   # HDR10 mastering display + content light levels
    crop: tuple[int, int, int, int] | None = None   # (w, h, x, y) of the picture without black bars, when removed
    video_bit_rate: int = 0    # main picture only; never use the whole-file rate as a picture budget

    @property
    def content_fps(self) -> float:
        """The frame rate of the actual motion (repeated frames don't count)."""
        return self.fps * (self.cadence - 1) / self.cadence if self.cadence else self.fps

    @property
    def is_10bit(self) -> bool:
        return any(t in self.pix_fmt for t in ("10le", "10be", "12le", "p010", "p016"))

    def audio_streams(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.kind == "audio"]

    def subtitle_streams(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.kind == "subtitle"]

    def to_dict(self) -> dict:
        return {
            "codec": self.codec, "width": self.width, "height": self.height,
            "pix_fmt": self.pix_fmt, "fps": self.fps, "duration": self.duration,
            "bit_rate": self.bit_rate,
            "video_bit_rate": self.video_bit_rate,
        }


def _fps(rate: str | None) -> float:
    if not rate or rate in ("0/0", "0"):
        return 0.0
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        return float(rate)
    except ValueError:
        return 0.0


def probe_raw(path: Path) -> dict:
    ffprobe = tools.require("ffprobe")
    proc = tools.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=60,
    )
    return json.loads(proc.stdout or "{}")


def _ratio(v) -> float:
    try:
        if isinstance(v, str) and "/" in v:
            a, b = v.split("/", 1)
            return float(a) / float(b) if float(b) else 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _mastering(path: Path, index: int) -> dict:
    """HDR10 mastering-display and content-light metadata from the first frame (kept when re-encoding)."""
    try:
        ffprobe = tools.require("ffprobe")
        proc = tools.run([ffprobe, "-v", "error", "-select_streams", str(index), "-read_intervals", "%+#1",
                          "-show_frames", "-print_format", "json", str(path)], timeout=60, check=False)
        frames = json.loads(proc.stdout or "{}").get("frames", [])
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for sd in (frames[0].get("side_data_list", []) if frames else []):
        t = str(sd.get("side_data_type", ""))
        if "Mastering display" in t:
            for k in ("red_x", "red_y", "green_x", "green_y", "blue_x", "blue_y", "white_point_x", "white_point_y",
                      "min_luminance", "max_luminance"):
                if k in sd:
                    out[k] = _ratio(sd[k])
        elif "Content light" in t:
            out["max_content"] = int(sd.get("max_content", 0) or 0)
            out["max_average"] = int(sd.get("max_average", 0) or 0)
    return out


def probe_video(path: Path) -> VideoInfo | None:
    try:
        data = probe_raw(path)
    except (tools.ToolError, OSError, ValueError, TimeoutError):
        return None
    except Exception:  # subprocess.TimeoutExpired and friends
        return None

    streams: list[StreamInfo] = []
    main = None
    for s in data.get("streams", []):
        kind = s.get("codec_type", "data")
        disp = s.get("disposition", {}) or {}
        tags = {k.lower(): v for k, v in (s.get("tags", {}) or {}).items()}
        attached = bool(disp.get("attached_pic"))
        lang = str(tags.get("language", "")).strip().lower()
        info = StreamInfo(int(s.get("index", len(streams))), kind, s.get("codec_name", "?"), attached,
                          "" if lang in ("und", "zxx", "mis") else lang, str(tags.get("title", "")),
                          bool(disp.get("default")), bool(disp.get("forced")))
        streams.append(info)
        if kind == "video" and not attached and main is None:
            main = s
    if main is None:
        return None

    fmt = data.get("format", {})
    try:
        duration = float(fmt.get("duration") or main.get("duration") or 0)
    except ValueError:
        duration = 0.0
    try:
        bit_rate = int(fmt.get("bit_rate") or 0)
    except ValueError:
        bit_rate = 0
    if not bit_rate and duration > 0:
        try:
            bit_rate = int(path.stat().st_size * 8 / duration)
        except OSError:
            pass

    r_fps, avg_fps = _fps(main.get("r_frame_rate")), _fps(main.get("avg_frame_rate"))
    color = {k: str(main.get(f"color_{k}")) for k in ("primaries", "transfer", "space", "range")
             if main.get(f"color_{k}") not in (None, "unknown", "reserved")}
    side = [str(sd.get("side_data_type", "")) for sd in main.get("side_data_list", []) or []]
    hdr = ""
    if any("DOVI" in t or "Dolby Vision" in t for t in side):
        hdr = "Dolby Vision"
    elif color.get("transfer") == "smpte2084":
        hdr = "HDR10"
    elif color.get("transfer") == "arib-std-b67":
        hdr = "HLG"
    return VideoInfo(
        vfr=bool(r_fps and avg_fps and abs(r_fps - avg_fps) / r_fps > 0.02),
        interlaced=main.get("field_order") in ("tt", "bb", "tb", "bt"),
        hdr=hdr,
        color=color,
        mastering=_mastering(path, int(main.get("index", 0))) if hdr == "HDR10" else {},
        codec=main.get("codec_name", "?"),
        width=int(main.get("width") or 0),
        height=int(main.get("height") or 0),
        pix_fmt=main.get("pix_fmt", ""),
        fps=_fps(main.get("avg_frame_rate")) or _fps(main.get("r_frame_rate")),
        duration=duration,
        bit_rate=bit_rate,
        video_bit_rate=_positive_int(main.get("bit_rate")),
        main_video_index=int(main.get("index", 0)),
        streams=streams,
        tags={str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()},
        creation_time=str((fmt.get("tags") or {}).get("creation_time", "")),
    )


def _positive_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError):
        return 0


# One repeated frame per cycle: 60 from 30 (2), 30 from 24 (5), 30 from 25 (6). Minimum stored fps for each.
_CYCLES = ((5, 29.0), (2, 47.0), (6, 29.0))


# Metadata that only camera originals carry. Phones and cameras record real frames; padded frame rates come
# from downloads and re-uploads, so originals don't need checking.
_CAMERA_TAGS = ("com.apple.quicktime.make", "com.apple.quicktime.model", "com.android.version",
                "com.android.manufacturer", "com.android.model", "make", "model")


def is_camera_original(info: VideoInfo) -> bool:
    return any(info.tags.get(k) for k in _CAMERA_TAGS)


def detect_cadence(path: Path, info: VideoInfo, seconds: float = 3.0) -> int:  # noqa: C901
    """Find frames that were repeated to pad the frame rate (e.g. a 24 fps film uploaded as 30 fps).

    Measures how much each frame differs from the previous one in a short sample. A repeat shows up as a
    near-zero difference at the same position in every cycle of N frames; static scenes don't count.
    """
    if info.fps < 29 or info.duration < 3 or info.vfr or info.interlaced or is_camera_original(info):
        return 0
    import re
    import statistics

    start = max(0.0, info.duration * 0.4 - seconds / 2)
    d: list[float] = []
    # GPU decoding when present; then on the processor, if that failed (some ffmpeg builds crash outright when
    # a GPU library is missing, and a crashed measurement must not read as "no repeated frames").
    for hw in (["-hwaccel", "auto"], []):
        try:
            ffmpeg = tools.require("ffmpeg")
            proc = tools.run([ffmpeg, "-hide_banner", "-nostdin", *hw,
                              "-ss", f"{start:.2f}", "-t", f"{seconds:.2f}", "-i", str(path),
                              "-map", f"0:{info.main_video_index}", "-an", "-sn", "-vf",
                              "scale=192:-2,format=gray,tblend=all_mode=difference,signalstats,"
                              "metadata=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"],
                             timeout=120, check=False)
        except Exception:  # noqa: BLE001 - detection is optional
            continue
        d = [float(x) for x in re.findall(r"YAVG=([0-9.]+)", proc.stdout or "")]
        if proc.returncode == 0 and len(d) >= 40:
            break
    if len(d) < 40:
        return 0
    high = statistics.quantiles(d, n=4)[2]   # typical difference between moving frames
    if high < 0.5:
        return 0  # mostly static: can't tell
    dup = [x < max(0.08, 0.1 * high) for x in d]
    for k, min_fps in _CYCLES:
        if info.fps < min_fps:
            continue
        frac = [sum(dup[p::k]) / max(1, len(dup[p::k])) for p in range(k)]
        best = max(frac)
        rest = (sum(frac) - best) / (k - 1)
        if best >= 0.75 and rest <= 0.15:
            return k
    return 0


@dataclass
class AudioInfo:
    codec: str
    sample_rate: int
    channels: int
    bits: int                  # bits per sample of lossless/PCM sources (0 if not applicable)
    sample_fmt: str
    duration: float
    bit_rate: int              # audio stream, bits/s (cover art excluded where possible)
    audio_index: int           # input stream index of the audio stream
    cover_index: int = -1      # attached picture (album art) stream, -1 if none
    cover_codec: str = ""
    tags: dict = field(default_factory=dict)


def probe_audio(path: Path, allow_video: bool = False) -> AudioInfo | None:
    """The first audio stream and any album art. None if unreadable or if it actually holds video (unless
    ``allow_video``: a video's soundtrack, for taking the sound out of it)."""
    try:
        data = probe_raw(path)
    except Exception:  # noqa: BLE001 - ToolError, timeouts, bad JSON
        return None
    audio, cover = None, None
    for st in data.get("streams", []):
        kind = st.get("codec_type")
        if kind == "audio" and (audio is None or (st.get("disposition", {}).get("default")
                                                  and not audio.get("disposition", {}).get("default"))):
            audio = st
        elif kind == "video":
            if st.get("disposition", {}).get("attached_pic"):
                cover = cover or st
            elif not allow_video:
                return None  # a real video stream: this is a video file
    if audio is None:
        return None
    fmt = data.get("format", {})

    def num(*values, cast=float):
        for v in values:
            try:
                if v not in (None, "", "N/A"):
                    return cast(v)
            except (TypeError, ValueError):
                continue
        return cast(0)

    duration = num(audio.get("duration"), fmt.get("duration"))
    bits = num(audio.get("bits_per_raw_sample"), audio.get("bits_per_sample"), cast=int)
    bit_rate = num(audio.get("bit_rate"), cast=int)
    if not bit_rate and duration > 0:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if cover is not None:
            size -= num(cover.get("extradata_size"), cast=int)  # rough; cover bytes aren't reported
        bit_rate = num(fmt.get("bit_rate"), cast=int) or int(max(0, size) * 8 / duration)
    tags = {str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()}
    tags.update({str(k).lower(): str(v) for k, v in (audio.get("tags") or {}).items() if str(k).lower() not in tags})
    return AudioInfo(
        codec=audio.get("codec_name", "?"),
        sample_rate=num(audio.get("sample_rate"), cast=int),
        channels=num(audio.get("channels"), cast=int),
        bits=bits,
        sample_fmt=audio.get("sample_fmt", ""),
        duration=duration,
        bit_rate=bit_rate,
        audio_index=int(audio.get("index", 0)),
        cover_index=int(cover.get("index", -1)) if cover is not None else -1,
        cover_codec=cover.get("codec_name", "") if cover is not None else "",
        tags=tags,
    )


def image_dimensions(path: Path) -> tuple[int, int] | None:
    """Width/height of an image ffmpeg can decode (PNG, JPEG, WebP)."""
    try:
        data = probe_raw(path)
    except Exception:
        return None
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            return int(s.get("width") or 0), int(s.get("height") or 0)
    return None
