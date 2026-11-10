"""Music and other audio files.

* Lossless sources (WAV, AIFF, ALAC, APE, WavPack, FLAC) become FLAC: every sample is kept, and the
  result is verified by decoding both files and comparing the audio sample for sample (MD5 of the PCM).
* Or anything becomes Opus, AAC or MP3. These encoders already drop what people can't hear (their
  psychoacoustic models and band limits), so there is no separate "remove inaudible frequencies" step.
* Two lossless-in-practice reductions: stereo files whose channels are identical are stored as mono, and
  (optionally) hi-res audio is reduced to CD quality (44.1/48 kHz, 16-bit with dither) - ultrasonic
  content and dynamic range beyond human hearing.
* Tags and album art are kept (Opus: art stored as a METADATA_BLOCK_PICTURE comment).
"""

from __future__ import annotations

import base64
import re
import struct
import threading
from collections.abc import Callable
from pathlib import Path

from . import steps, tools
from .fileops import remove_quietly, temp_name
from .images import VerificationFailed
from .plan import Plan
from .probe import AudioInfo, probe_audio
from .settings import Settings

LOSSLESS_CODECS = {"flac", "alac", "ape", "wavpack", "tta", "mlp", "truehd", "wmalossless", "shorten"}

# format -> (extension, ffmpeg encoder, label)
FORMATS = {
    "flac": (".flac", "flac", "FLAC"),
    "opus": (".opus", "libopus", "Opus"),
    "aac": (".m4a", "aac", "AAC"),
    "mp3": (".mp3", "libmp3lame", "MP3"),
}
# Stereo bitrates (kbps). Opus 128 kbps is transparent for almost all listeners; AAC needs more.
BITRATE = {
    "opus": {"high": 160, "balanced": 128, "small": 96},
    "aac": {"high": 256, "balanced": 192, "small": 128},
    "mp3": {"high": 245, "balanced": 190, "small": 130},   # LAME VBR V0 / V2 / V5 averages
}
MP3_VBR = {"high": "0", "balanced": "2", "small": "5"}
_CODEC_LABEL = {"mp3": "MP3", "aac": "AAC", "vorbis": "Vorbis", "opus": "Opus", "wmav1": "WMA", "wmav2": "WMA",
                "wmapro": "WMA", "flac": "FLAC", "alac": "ALAC", "ape": "APE", "wavpack": "WavPack", "tta": "TTA",
                "ac3": "AC-3", "eac3": "E-AC-3", "dts": "DTS"}


def is_lossless(codec: str) -> bool:
    return codec in LOSSLESS_CODECS or codec.startswith("pcm_")


def source_label(path: Path, info: AudioInfo) -> str:
    if info.codec.startswith("pcm_"):
        ext = path.suffix.lower()
        return "AIFF" if ext.startswith(".aif") else "CAF" if ext == ".caf" else "WAV"
    return _CODEC_LABEL.get(info.codec, info.codec.upper())


def target_kbps(fmt: str, quality: str, channels: int) -> int:
    kb = BITRATE[fmt][quality]
    if channels == 1:
        kb = max(48, int(kb * 0.6))
    elif channels > 2:
        kb = int(kb * channels / 2 * 0.8)
    return kb


def cd_target(info: AudioInfo) -> tuple[int | None, int | None]:
    """(sample rate, bits) for "CD quality", or None where nothing changes."""
    rate = None
    if info.sample_rate > 48000:
        rate = 44100 if info.sample_rate % 44100 == 0 else 48000
    bits = 16 if info.bits > 16 else None
    return rate, bits


def flac_problem(info: AudioInfo, s: Settings) -> str:
    """Why this source can't be stored losslessly as FLAC ("" if it can)."""
    if s.audio_cd_quality and cd_target(info)[1]:
        return ""  # becomes 16-bit anyway
    if info.sample_fmt.startswith(("flt", "dbl")) or info.codec.startswith(("pcm_f", "pcm_lxf")):
        return "Floating-point audio can't be stored losslessly as FLAC"
    if info.bits > 24:
        return "32-bit audio can't be stored losslessly as FLAC"
    return ""


def identical_channels(src: Path, info: AudioInfo, exact: bool) -> bool:
    """True when a stereo file's left and right channels carry the same signal."""
    if info.channels != 2:
        return False
    ffmpeg = tools.require("ffmpeg")
    proc = tools.run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(src), "-map", f"0:{info.audio_index}",
                      "-af", "pan=mono|c0=c0-c1,astats=metadata=0:measure_perchannel=none:measure_overall=Peak_level",
                      "-f", "null", "-"], timeout=1800, check=False)
    m = re.search(r"Peak level dB:\s*(-?inf|-?[0-9.]+)", proc.stderr)
    if proc.returncode != 0 or not m:
        return False
    level = m.group(1)
    if level.endswith("inf"):
        return True
    return not exact and float(level) < -80.0  # lossy decoders may differ by rounding noise


def _cover_metadata(src: Path, info: AudioInfo, folder: Path | None = None) -> Path | None:
    """An ffmetadata file carrying the album art as METADATA_BLOCK_PICTURE (for Ogg/Opus), written in
    ``folder`` (default: next to ``src``)."""
    mime = {"mjpeg": "image/jpeg", "png": "image/png"}.get(info.cover_codec)
    if info.cover_index < 0 or not mime:
        return None
    ffmpeg = tools.require("ffmpeg")
    proc = tools.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(src), "-map",
                      f"0:{info.cover_index}", "-c", "copy", "-f", "image2pipe", "-"], timeout=120, text=False,
                     check=False)
    data = proc.stdout or b""
    if proc.returncode != 0 or not data:
        return None
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001
        w = h = 0
    m = mime.encode()
    block = (struct.pack(">II", 3, len(m)) + m + struct.pack(">I", 0) + struct.pack(">IIII", w, h, 24, 0)
             + struct.pack(">I", len(data)) + data)
    b64 = base64.b64encode(block).decode()
    escaped = b64.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#")
    meta = temp_name(folder or src.parent, ".txt", "-meta")
    meta.write_text(";FFMETADATA1\nMETADATA_BLOCK_PICTURE=" + escaped + "\n", encoding="ascii")
    return meta


def build_args(src: Path, dst: Path, info: AudioInfo, s: Settings, fmt: str, mono: bool,
               meta: Path | None = None, kbps: int = 0, rate: int = 0, channels: int = 0) -> list[str]:
    """``kbps``, ``rate`` and ``channels`` (Convert's per-file settings) override the automatic choices."""
    ffmpeg = tools.require("ffmpeg")
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
            "-i", str(src)]
    if meta is not None:
        args += ["-i", str(meta)]
    args += ["-map", f"0:{info.audio_index}"]
    if info.cover_index >= 0 and info.cover_codec in ("mjpeg", "png") and fmt in ("flac", "aac", "mp3"):
        args += ["-map", f"0:{info.cover_index}", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    args += ["-map_metadata", "0"]
    if meta is not None:
        args += ["-map_metadata:s:a:0", "1:g"]

    filters = []
    if mono:
        filters.append("pan=mono|c0=c0")
    elif channels:
        args += ["-ac", str(channels)]
    cd_rate, bits = cd_target(info) if s.audio_cd_quality else (None, None)
    rate = rate or cd_rate or 0
    if rate and fmt != "opus":  # Opus always works at 48 kHz internally
        filters.append(f"aresample={rate}:resampler=swr:filter_size=64:phase_shift=10")
    out_channels = 1 if mono else channels or info.channels
    if fmt == "flac":
        if bits == 16:
            filters.append("aresample=osf=s16:dither_method=triangular")
        args += ["-c:a", "flac", "-compression_level", "8"]
    elif fmt == "opus":
        args += ["-c:a", "libopus", "-b:a", f"{kbps or target_kbps('opus', s.audio_quality, out_channels)}k",
                 "-vbr", "on", "-compression_level", "10"]
        if out_channels > 2:
            args += ["-mapping_family", "1"]
    elif fmt == "aac":
        args += ["-c:a", "aac", "-b:a", f"{kbps or target_kbps('aac', s.audio_quality, out_channels)}k",
                 "-movflags", "+faststart"]
    else:  # mp3
        args += ["-c:a", "libmp3lame", *(["-b:a", f"{kbps}k"] if kbps else ["-q:a", MP3_VBR[s.audio_quality]]),
                 "-id3v2_version", "3"]
    if filters:
        args += ["-af", ",".join(filters)]
    args.append(str(dst))
    return args


def pcm_md5(path: Path, stream: str, mono: bool = False) -> str:
    """MD5 of the decoded audio as 32-bit samples (comparable across WAV, FLAC, ALAC...)."""
    ffmpeg = tools.require("ffmpeg")
    args = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-map", stream]
    if mono:
        args += ["-af", "pan=mono|c0=c0"]
    out = tools.run(args + ["-c:a", "pcm_s32le", "-f", "md5", "-"], timeout=3600).stdout
    m = re.search(r"MD5=([0-9a-f]+)", out)
    if not m:
        raise VerificationFailed("couldn't decode the audio to compare")
    return m.group(1)


def verify_audio(src: Path, info: AudioInfo, out: Path, fmt: str, mono: bool, exact: bool,
                 channels: int = 0) -> None:
    got = probe_audio(out)
    expected = {"flac": "flac", "opus": "opus", "aac": "aac", "mp3": "mp3", "wav": "pcm_"}[fmt]
    if got is None or not got.codec.startswith(expected):
        raise VerificationFailed(f"result isn't readable {FORMATS[fmt][2] if fmt in FORMATS else fmt.upper()}")
    channels = 1 if mono else channels or info.channels
    if got.channels != channels:
        raise VerificationFailed(f"{got.channels} channels instead of {channels}")
    if info.duration > 0 and abs(got.duration - info.duration) > max(0.5, info.duration * 0.01):
        raise VerificationFailed(f"duration {got.duration:.1f}s instead of {info.duration:.1f}s")
    if exact:
        if pcm_md5(src, f"0:{info.audio_index}", mono) != pcm_md5(out, f"0:{got.audio_index}"):
            raise VerificationFailed("decoded audio differs from the original")
        return
    ffmpeg = tools.require("ffmpeg")
    proc = tools.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(out), "-map",
                      f"0:{got.audio_index}", "-f", "null", "-"], timeout=3600, check=False)
    if proc.returncode != 0 or proc.stderr.strip():
        raise VerificationFailed("result doesn't decode cleanly")


def convert_audio(src: Path, info: AudioInfo, plan: Plan, s: Settings,
                  on_progress: Callable[[float], None] | None = None,
                  cancel: threading.Event | None = None, kbps: int = 0, rate: int = 0, channels: int = 0) -> Path:
    """Encode to a temp file next to ``src``, verify it, and return its path."""
    fmt = plan.route.split("_", 1)[1]
    exact = (fmt == "flac" and is_lossless(info.codec) and not flac_problem(info, s)
             and not (s.audio_cd_quality and any(cd_target(info))) and not rate and not channels)
    mono = s.audio_mono and not channels and identical_channels(src, info, exact=exact)
    if cancel is not None and cancel.is_set():
        raise tools.Cancelled()
    out = temp_name(src.parent, FORMATS[fmt][0])
    meta = _cover_metadata(src, info) if fmt == "opus" else None
    try:
        steps.report(f"Encoding {steps.format_name(FORMATS[fmt][0])}")
        tools.run_ffmpeg_progress(build_args(src, out, info, s, fmt, mono, meta, kbps, rate, channels),
                                  info.duration, on_progress, cancel)
        steps.report("Checking the result")
        verify_audio(src, info, out, fmt, mono, exact, channels)
        if mono:
            plan.outcome = "stored as mono (both channels were identical)"
        return out
    except BaseException:
        remove_quietly(out)
        raise
    finally:
        remove_quietly(meta)


__all__ = ["convert_audio", "verify_audio", "is_lossless", "source_label", "target_kbps", "cd_target",
           "flac_problem", "identical_channels", "FORMATS", "BITRATE"]
