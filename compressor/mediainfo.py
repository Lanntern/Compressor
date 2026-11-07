"""Short, human-readable technical details of a file (shown in lists and hover cards)."""

from __future__ import annotations

from .formats import AUDIO, VIDEO

_VIDEO_CODECS = {"h264": "H.264", "hevc": "HEVC", "av1": "AV1", "vp9": "VP9", "vp8": "VP8", "mpeg4": "MPEG-4",
                 "mpeg2video": "MPEG-2", "mpeg1video": "MPEG-1", "prores": "ProRes", "mjpeg": "MJPEG",
                 "wmv3": "WMV", "wmv2": "WMV", "vc1": "VC-1", "theora": "Theora", "vvc": "VVC"}


def duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def bitrate(bps: float) -> str:
    return f"{bps / 1e6:.1f} Mb/s" if bps >= 1e6 else f"{bps / 1e3:.0f} kb/s"


def cadence_note(v) -> str:
    """Explains repeated frames, or "" if there are none."""
    if not getattr(v, "cadence", 0):
        return ""
    from .plan import fps_label

    return f"{fps_label(v.content_fps)} fps footage padded to {fps_label(v.fps)} fps (repeated frames)"


def describe(entry) -> str:
    """e.g. "1920×1080 · 29.97 fps · H.264 · 8.2 Mb/s · 3:21"."""
    from .plan import fps_label

    if entry.kind == VIDEO and entry.video is not None:
        v = entry.video
        parts = [f"{v.width}×{v.height}"]
        if v.fps:
            # "30→24 fps": stored at 30, but only 24 distinct frames per second (see cadence_note)
            fps = f"{fps_label(v.fps)}→{fps_label(v.content_fps)}" if v.cadence else fps_label(v.fps)
            parts.append(f"{fps} fps")
        if getattr(v, "vfr", False):
            parts[-1] = parts[-1].replace(" fps", " fps (variable)")
        if getattr(v, "interlaced", False):
            parts[-1] += " interlaced"
        parts.append(_VIDEO_CODECS.get(v.codec, v.codec.upper()) + (" 10-bit" if v.is_10bit else ""))
        if getattr(v, "hdr", ""):
            parts.append(v.hdr)
        if v.bit_rate:
            parts.append(bitrate(v.bit_rate))
        if v.duration:
            parts.append(duration(v.duration))
        return " · ".join(parts)
    if entry.kind == AUDIO and entry.audio is not None:
        from .audio import source_label

        a = entry.audio
        ch = {1: "mono", 2: "stereo"}.get(a.channels, f"{a.channels} ch")
        parts = [source_label(entry.path, a), f"{a.sample_rate / 1000:g} kHz"]
        if a.bits and a.codec.startswith(("pcm_", "flac", "alac", "wavpack", "ape")):
            parts.append(f"{a.bits}-bit")
        parts.append(ch)
        if a.bit_rate:
            parts.append(bitrate(a.bit_rate))
        if a.duration:
            parts.append(duration(a.duration))
        return " · ".join(parts)
    if entry.image is not None:
        i = entry.image
        parts = [f"{i.width}×{i.height}"]
        if i.bit_depth > 8:
            parts.append(f"{i.bit_depth}-bit")
        if i.animated:
            parts.append(f"{i.frames} frames")
        if getattr(i, "pages", 1) > 1:
            parts.append(f"{i.pages} pages")
        return " · ".join(parts)
    return ""


__all__ = ["describe", "duration", "bitrate", "cadence_note"]
