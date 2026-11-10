"""Home overview: where space can come from, ranked by expected saving x confidence."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from .formats import AUDIO, AVIF, BMP, DOCUMENT, GIF, HEIC, JPEG, PNG, TIFF, VIDEO, WEBP
from .plan import EFFICIENT_VIDEO_CODECS, Plan, plan_entry
from .scanner import FileEntry, ProgressFn, scan
from .settings import Settings

HIGH, MEDIUM, LOW = "High", "Medium", "Low"
_WEIGHT = {HIGH: 0.95, MEDIUM: 0.6, LOW: 0.3}
LEGACY_VIDEO = {"mpeg1video", "mpeg2video", "mpeg4", "msmpeg4v2", "msmpeg4v3", "wmv1", "wmv2", "wmv3", "vc1",
                "mjpeg", "prores", "dvvideo", "rawvideo", "huffyuv", "ffv1", "h263", "flv1", "theora", "cinepak"}


@dataclass
class Opportunity:
    entry: FileEntry
    plan: Plan
    saving: int
    confidence: str
    why: str

    @property
    def score(self) -> float:
        return self.saving * _WEIGHT[self.confidence]


@dataclass
class EfficientGroup:
    label: str
    explanation: str
    count: int = 0
    size: int = 0


@dataclass
class Overview:
    opportunities: list[Opportunity] = field(default_factory=list)
    efficient: dict[str, EfficientGroup] = field(default_factory=dict)
    files: int = 0
    total_size: int = 0
    skipped_dirs: int = 0
    cancelled: bool = False
    by_kind: dict = field(default_factory=dict)        # space.PHOTOS... -> (files, bytes), for "where space goes"
    space: object = None                               # space.Breakdown, filled in by Home

    @property
    def potential(self) -> int:
        return sum(o.saving for o in self.opportunities)

    @property
    def likely(self) -> int:
        """Savings weighted by confidence (a conservative headline number)."""
        return int(sum(o.score for o in self.opportunities))

    @property
    def efficient_size(self) -> int:
        return sum(g.size for g in self.efficient.values())

    @property
    def efficient_count(self) -> int:
        return sum(g.count for g in self.efficient.values())


_RECORDING_FOLDERS = ("captures", "nvidia", "obs", "shadowplay", "recordings", "game recordings", "geforce")
_RECORDING_TAGS = ("obs", "nvidia", "shadowplay", "geforce", "xbox game bar", "game dvr", "amd relive")


def is_recording(e: FileEntry) -> bool:
    """A screen or game recording (OBS, NVIDIA ShadowPlay, Xbox Game Bar, AMD ReLive): recorded fast at a high
    bitrate, so it shrinks a lot with little visible change."""
    v = e.video
    if v is None:
        return False
    parts = {p.lower() for p in e.path.parent.parts[-3:]}
    tags = " ".join(str(x) for x in v.tags.values()).lower()
    hinted = bool(parts & set(_RECORDING_FOLDERS)) or any(t in tags for t in _RECORDING_TAGS)
    bpp = v.bit_rate / max(1.0, v.width * v.height * (v.fps or 30))
    return hinted and bpp > 0.08     # recorders spend far more bits per pixel than a final encode


def confidence(e: FileEntry, p: Plan) -> tuple[str, str]:
    """How sure the saving estimate is, and why."""
    if e.kind == VIDEO and e.video is not None:
        v = e.video
        if is_recording(e):
            return HIGH, ("Screen or game recording: recorders save at a very high bitrate to keep up, so it "
                            "shrinks a lot")
        if v.codec in LEGACY_VIDEO:
            return HIGH, f"Old {v.codec.upper()} video: modern codecs are far more efficient"
        pixels = max(1.0, v.width * v.height * (v.fps or 30))
        bpp = v.bit_rate / pixels
        if bpp >= 0.12:
            return HIGH, "High-bitrate H.264 video"
        if bpp >= 0.05:
            return MEDIUM, "H.264 video at a typical bitrate"
        return LOW, "Already a low bitrate: limited gains"
    if e.kind == DOCUMENT:
        return MEDIUM, ("Pictures inside the document can be stored smaller with identical pixels"
                        if p.lossless else "Large pictures inside the document scaled down")
    if e.kind == AUDIO and e.audio is not None:
        if p.route == "audio_flac":
            if e.audio.codec.startswith("pcm_"):
                return HIGH, "Uncompressed audio: FLAC keeps every sample at about half the size"
            return (HIGH, "Hi-res audio reduced to CD quality") if not p.lossless else \
                (LOW, "Already lossless-compressed: small gains")
        if e.audio.codec.startswith("pcm_") or e.audio.codec in ("flac", "alac", "ape", "wavpack"):
            return HIGH, "Lossless audio to an efficient lossy format"
        return MEDIUM, "High-bitrate audio re-encoded more efficiently"
    if p.route == "jxl_transcode":
        return HIGH, "Lossless JPEG → JPEG XL reliably saves about 20%"
    if e.kind in (BMP,) or (e.kind == TIFF and p.lossless):
        return HIGH, "Uncompressed image: shrinks dramatically"
    if e.kind == PNG and p.lossless:
        return HIGH if p.route in ("jxl", "png_opt") else MEDIUM, "Lossless PNG recompression"
    if e.kind in (HEIC, AVIF) or (e.kind == WEBP and e.image is not None and not e.image.lossless_source):
        return LOW, "Already efficiently compressed"
    if e.kind == GIF:
        return MEDIUM, "GIF animation: modern formats are usually much smaller"
    if e.kind == JPEG:
        return MEDIUM, "Lossy re-encode: depends on the original's quality"
    return MEDIUM, "Estimate"


def _efficient_bucket(e: FileEntry, p: Plan) -> tuple[str, str] | None:
    if e.kind == VIDEO and e.video is not None and e.video.codec in EFFICIENT_VIDEO_CODECS:
        return ("Modern videos (HEVC, AV1, VP9)",
                "Already use an efficient codec; re-encoding would cost quality for little gain.")
    if e.kind == AUDIO and e.audio is not None and e.audio.codec in ("opus", "aac", "mp3", "vorbis") and not p.convert:
        return ("Compressed music (MP3, AAC, Opus)",
                "Already lossy-compressed; re-encoding would cost quality for little gain.")
    if e.kind in (HEIC, AVIF):
        return ("HEIC and AVIF photos", "Phone-style formats that are already compressed efficiently.")
    if e.kind == WEBP and e.image is not None and e.image.animated:
        return ("Animated WebP", "Already compact.")
    return None


def analyze(roots: list[Path], s: Settings, progress: ProgressFn | None = None,
            cancel: threading.Event | None = None) -> Overview:
    result = scan(roots, s, progress, cancel)
    ov = Overview(files=len(result.entries), total_size=sum(e.size for e in result.entries),
                  skipped_dirs=len(result.skipped_program_dirs), cancelled=result.cancelled)
    from . import space

    for e in result.entries:
        cat = (space.VIDEOS if e.kind == VIDEO else space.MUSIC if e.kind == AUDIO
               else space.DOCUMENTS if e.kind == DOCUMENT else space.PHOTOS)
        count, size = ov.by_kind.get(cat, (0, 0))
        ov.by_kind[cat] = (count + 1, size + e.size)
        p = plan_entry(e, s)
        bucket = _efficient_bucket(e, p)
        if bucket is not None:
            g = ov.efficient.setdefault(bucket[0], EfficientGroup(*bucket))
            g.count += 1
            g.size += e.size
        if not p.convert:
            continue
        saving = max(0, e.size - p.est_size)
        if saving < 32 * 1024:
            continue
        level, why = confidence(e, p)
        ov.opportunities.append(Opportunity(e, p, saving, level, why))
    if result.jxl_files:
        count, size = ov.by_kind.get(space.PHOTOS, (0, 0))
        ov.by_kind[space.PHOTOS] = (count + result.jxl_files, size + result.jxl_bytes)
        ov.efficient["JPEG XL images"] = EfficientGroup("JPEG XL images", "Already the most efficient image format.",
                                                        result.jxl_files, result.jxl_bytes)
    ov.opportunities.sort(key=lambda o: o.score, reverse=True)
    return ov
