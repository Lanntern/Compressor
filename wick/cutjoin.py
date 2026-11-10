"""Trimming and joining videos and music, without re-encoding wherever that's possible.

Trim: the streams are copied from the chosen start to the chosen end. A video can only start on a keyframe
when nothing is re-encoded, so the copy starts at the keyframe at or before the chosen start (usually less
than a few seconds earlier); "cut exactly" re-encodes the video instead (H.264 or HEVC at a quality that
looks the same). Music is cut to the frame (a few hundredths of a second) either way.

Join: files with the same kind of streams (codec, picture size, sample rate, channels) are joined by copying
(FFmpeg's concat demuxer). Others can be joined by re-encoding, each fitted into the first one's picture.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from . import tools
from .images import VerificationFailed
from .probe import probe_audio, probe_video

_BASE = ["-hide_banner", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats"]
_MP4 = {".mp4", ".m4v", ".mov", ".m4a", ".3gp"}


def duration_of(path: Path) -> float:
    v = probe_video(path)
    if v is not None:
        return v.duration
    a = probe_audio(path)
    return a.duration if a is not None else 0.0


def _faststart(dest: Path) -> list[str]:
    return ["-movflags", "+faststart"] if dest.suffix.lower() in _MP4 else []


def trim(src: Path, start: float, end: float, dest: Path, exact: bool = False,
         on_progress: Callable[[float], None] | None = None, cancel: threading.Event | None = None) -> float:
    """``src`` from ``start`` to ``end`` seconds into ``dest`` (same format). Returns the result's length."""
    ffmpeg = tools.require("ffmpeg")
    video = probe_video(src)
    length = end - start
    if length <= 0:
        raise ValueError("The end has to be after the start")
    if video is not None and exact:
        codec = ["-c:v", "libx265", "-crf", "20", "-tag:v", "hvc1"] if video.codec == "hevc" else \
            ["-c:v", "libx264", "-crf", "18", "-preset", "medium"]
        if dest.suffix.lower() in (".webm", ".mkv") and video.codec in ("vp9", "av1"):
            codec = ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-row-mt", "1"]
        audio = ["-c:a", "libopus", "-b:a", "160k"] if dest.suffix.lower() == ".webm" else \
            ["-c:a", "aac", "-b:a", "192k"]
        args = [ffmpeg, *_BASE, "-i", str(src), "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-map", "0:v:0",
                "-map", "0:a?", "-map_metadata", "0", *codec, "-pix_fmt", "yuv420p", *audio, *_faststart(dest),
                str(dest)]
    else:
        args = [ffmpeg, *_BASE, "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}", "-map", "0",
                "-map_metadata", "0", "-c", "copy", "-avoid_negative_ts", "make_zero", *_faststart(dest),
                str(dest)]
    tools.run_ffmpeg_progress(args, length, on_progress, cancel)
    got = duration_of(dest)
    # Copying starts at a keyframe (up to several seconds early); never shorter than asked, beyond rounding.
    if got < length - 0.25 or got > length + (0.25 if exact or video is None else 15):
        raise VerificationFailed(f"The trimmed copy lasts {got:.1f} s instead of {length:.1f} s")
    return got


def _signature(path: Path) -> tuple | None:
    """What has to match for files to be joined by copying."""
    v = probe_video(path)
    if v is not None:
        a = probe_audio(path, allow_video=True)
        return ("video", v.codec, v.width, v.height, v.pix_fmt, round(v.fps, 2),
                (a.codec, a.sample_rate, a.channels) if a else None)
    a = probe_audio(path)
    return ("audio", a.codec, a.sample_rate, a.channels) if a is not None else None


def join_problem(paths: list[Path]) -> str:
    """"" when the files can be joined by copying; otherwise why not (they can still be re-encoded)."""
    sigs = [_signature(p) for p in paths]
    if any(s is None for s in sigs):
        bad = next(p for p, s in zip(paths, sigs, strict=True) if s is None)
        return f"{bad.name} can't be read"
    if len({s[0] for s in sigs if s}) > 1:
        return "Videos and music can't be joined together"
    if len(set(sigs)) > 1:
        first = sigs[0]
        assert first is not None
        what = "picture size, frame rate, format or sound" if first[0] == "video" else "format, sample rate or channels"
        return f"They differ in {what}"
    return ""


def join(paths: list[Path], dest: Path, reencode: bool = False,
         on_progress: Callable[[float], None] | None = None, cancel: threading.Event | None = None) -> float:
    """``paths`` one after another into ``dest``. Returns the result's length."""
    import tempfile

    ffmpeg = tools.require("ffmpeg")
    total = sum(duration_of(p) for p in paths)
    if not reencode:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            for p in paths:
                f.write("file '" + str(p.resolve()).replace("'", "'\\''") + "'\n")
            listing = Path(f.name)
        try:
            tools.run_ffmpeg_progress([ffmpeg, *_BASE, "-f", "concat", "-safe", "0", "-i", str(listing), "-map", "0",
                                       "-c", "copy", *_faststart(dest), str(dest)], total, on_progress, cancel)
        finally:
            listing.unlink(missing_ok=True)
    else:
        first = probe_video(paths[0])
        audio_only = first is None
        has_audio = [probe_audio(p, allow_video=True) is not None for p in paths]
        if not audio_only and any(has_audio) and not all(has_audio):
            raise ValueError("Some of these videos have no sound, so they can't be joined")
        sound = all(has_audio)
        inputs, chains, labels = [], [], []
        for i, p in enumerate(paths):
            inputs += ["-i", str(p)]
            if not audio_only:
                assert first is not None
                w, h = first.width - first.width % 2, first.height - first.height % 2
                chains.append(f"[{i}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={first.fps or 30:g},format=yuv420p[v{i}]")
                labels.append(f"[v{i}]")
            if sound:
                chains.append(f"[{i}:a:0]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
                labels.append(f"[a{i}]")
        n = len(paths)
        graph = ";".join(chains) + ";" + "".join(labels) + \
            f"concat=n={n}:v={0 if audio_only else 1}:a={1 if sound else 0}" + \
            ("[v]" if not audio_only else "") + ("[a]" if sound else "")
        maps = (["-map", "[v]"] if not audio_only else []) + (["-map", "[a]"] if sound else [])
        ext = dest.suffix.lower()
        if audio_only:
            codec = {".mp3": ["-c:a", "libmp3lame", "-q:a", "2"], ".flac": ["-c:a", "flac"],
                     ".wav": ["-c:a", "pcm_s16le"], ".opus": ["-c:a", "libopus", "-b:a", "160k"],
                     ".ogg": ["-c:a", "libvorbis", "-q:a", "6"]}.get(ext, ["-c:a", "aac", "-b:a", "256k"])
        else:
            codec = ["-c:v", "libx264", "-crf", "18", "-preset", "medium"] + \
                (["-c:a", "aac", "-b:a", "192k"] if sound else [])
        tools.run_ffmpeg_progress([ffmpeg, *_BASE, *inputs, "-filter_complex", graph, *maps, *codec,
                                   *_faststart(dest), str(dest)], total, on_progress, cancel)
    got = duration_of(dest)
    if abs(got - total) > max(1.0, total * 0.02):
        raise VerificationFailed(f"The joined file lasts {got:.1f} s instead of {total:.1f} s")
    return got


__all__ = ["trim", "join", "join_problem", "duration_of"]
