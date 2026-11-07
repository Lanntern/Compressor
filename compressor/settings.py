"""User-facing job settings."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

IMAGE_FORMATS = ("keep", "jxl", "webp", "avif", "none")
IMAGE_MODES = ("lossless", "lossy")
JPEG_SUBSAMPLING = ("auto", "444", "422", "420")
RESIZE_MODES = ("none", "long_edge", "fit", "percent")
METADATA_MODES = ("keep", "strip_gps", "strip_private", "strip_all_keep_date", "strip_all")
OUTPUT_MODES = ("replace", "beside", "suffix", "folder")
VIDEO_CODECS = ("auto", "av1", "hevc", "none")
VIDEO_ENCODERS = ("auto", "gpu", "cpu")
VIDEO_QUALITIES = ("identical", "high", "balanced", "small", "custom")
VIDEO_TRACKS = ("all", "mine")
DOCUMENT_MODES = ("lossless", "reduce", "none")
SMART_TARGETS = ("identical", "high", "good")
AUDIO_LOSSLESS_MODES = ("flac", "lossy", "keep")   # WAV/AIFF/FLAC/ALAC/APE/WavPack
AUDIO_LOSSY_MODES = ("keep", "reencode")           # MP3/AAC/Vorbis/WMA/Opus
AUDIO_FORMATS = ("opus", "aac", "mp3")
AUDIO_QUALITIES = ("high", "balanced", "small")


@dataclass
class Settings:
    # Images
    image_format: str = "jxl"        # keep | jxl | webp | avif | none
    jpeg_mode: str = "lossless"      # JPEG: lossless (JXL: reversible; keep: mozjpeg optimize) | lossy
    png_mode: str = "lossless"       # PNG/GIF/BMP/TIFF/lossless WebP: lossless | lossy
    image_quality: int = 90          # lossy images, 1-100
    image_effort: int = 7            # cjxl effort 1-9
    jpeg_progressive: bool = True    # JPEG output: progressive (smaller) or baseline
    jpeg_subsampling: str = "auto"   # JPEG output chroma subsampling
    resize_mode: str = "none"        # none | long_edge | fit | percent
    resize_long_edge: int = 2560
    resize_width: int = 1920
    resize_height: int = 1080
    resize_percent: int = 50
    metadata: str = "keep"           # see METADATA_MODES / metadata.py (orientation + colour profile always kept)
    preserve_dates: bool = True      # give outputs the original's modified/created dates
    # Videos
    video_codec: str = "auto"        # auto (HEVC for camera/phone videos, else AV1) | av1 | hevc | none
    video_encoder: str = "auto"      # auto | gpu | cpu
    video_quality: str = "balanced"  # identical (measured: smallest that's visually identical) | high | balanced | small
    #                                  | custom (video_crf)
    video_crf: int = 24              # "custom": the encoder's constant-quality value (CRF/CQ; lower = better)
    video_max_height: int = 0        # 0 = keep; else e.g. 2160 / 1440 / 1080 / 720 (never upscales)
    video_max_fps: int = 0           # 0 = keep; else cap (e.g. 60 / 30)
    video_fix_cadence: bool = True   # drop frames that were only repeated to pad the frame rate (24 -> 30 fps)
    video_crop_bars: bool = False    # remove black bars (letterboxing) found in the picture
    audio_bitrate: int = 0           # 0 = copy audio as-is; else kbps (AAC for MP4, Opus for MKV/WebM)
    video_tracks: str = "all"        # all | mine (audio and subtitle tracks only in my languages + the main soundtrack)
    languages: str = ""              # my languages, ISO 639-1 comma list ("sv,en"); "" = from this computer
    # Music and other audio files
    audio_lossless: str = "flac"     # lossless sources: flac (lossless, verified) | lossy (audio_format) | keep
    audio_lossy: str = "keep"        # lossy sources: keep | reencode (only when well above the target bitrate)
    audio_format: str = "opus"       # lossy output: opus | aac | mp3
    audio_quality: str = "balanced"  # high | balanced | small
    audio_mono: bool = True          # store as mono when both channels are identical
    audio_cd_quality: bool = False   # reduce hi-res (88.2-192 kHz, 24-bit) to 44.1/48 kHz, 16-bit
    # Office documents and PDFs
    documents: str = "lossless"      # lossless (pictures inside shrunk, identical pixels) | reduce (+ large ones
    #                                  scaled down) | none
    # Smart quality: search for the smallest result that still meets a measured quality target
    fit_under_kb: int = 0            # 0 = off; else make each image/video at most this size (KB)
    smart_quality: bool = False
    smart_target: str = "identical"  # identical | high | good
    # Output
    output_mode: str = "replace"     # replace (original -> Recycle Bin) | suffix | folder
    output_suffix: str = "_compressed"
    output_dir: str = ""
    keep_structure: bool = True
    originals_to: str = ""            # Convert: after converting, move each original into this folder ("" = leave it)
    allow_permanent_delete: bool = False  # replace mode on drives without a Recycle Bin
    # Rules
    require_savings: bool = True     # only keep a result if it saved enough (below)
    min_savings_pct: float = 5.0     # ...at least this many percent
    min_savings_kb: int = 0          # ...and at least this many KB
    min_image_kb: int = 16
    min_video_mb: int = 10
    min_audio_kb: int = 100
    skip_program_folders: bool = True
    include_hidden: bool = False
    scan_jxl: bool = False           # list JPEG XL files too (Convert: they're an input there, not a result)
    convert_inputs: bool = False     # also list what only Convert reads: RAW, PSD, SVG, icons, Markdown, HTML...
    # Performance
    workers: int = 0                 # parallel image jobs; 0 = auto
    encoder_threads: int = 0         # threads per cjxl; 0 = share the CPU between parallel jobs, -1 = cjxl decides
    video_jobs: int = 0              # videos encoded at once; 0 = auto (2 on NVIDIA GPUs, which have 2 encoders)
    low_priority: bool = True        # run encoders at below-normal priority

    def image_workers(self) -> int:
        """Images (and audio) processed at once, unless set: one per CPU thread (measured ~25% faster than half
        with JPEG XL, whose own threading scales less well), but no more than memory allows (~1 GB per job
        for large photos), and at most 16."""
        import os

        if self.workers:
            return self.workers
        from .tools import total_memory_gb

        return max(1, min(os.cpu_count() or 2, int(total_memory_gb()) or 4, 16))

    def threads_per_job(self) -> int:
        """CPU threads each multi-threaded encoder (cjxl) gets, so parallel jobs share the CPU fairly
        (-1 lets the encoder decide; used by the speed test to reproduce the old behaviour)."""
        import os

        if self.encoder_threads:
            return self.encoder_threads
        return max(1, (os.cpu_count() or 2) // self.image_workers())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "Settings":
        s = cls()
        if not data:
            return s
        for f in fields(cls):
            if f.name in data:
                default = getattr(s, f.name)
                raw = data[f.name]
                if isinstance(default, bool):
                    setattr(s, f.name, raw in (True, 1, "1", "true", "True"))
                    continue
                try:
                    value = type(default)(raw)
                except (TypeError, ValueError):
                    continue
                setattr(s, f.name, value)
        s.validate()
        return s

    def validate(self) -> None:
        def pick(value, allowed, default):
            return value if value in allowed else default

        self.image_format = pick(self.image_format, IMAGE_FORMATS, "jxl")
        self.jpeg_mode = pick(self.jpeg_mode, IMAGE_MODES, "lossless")
        self.png_mode = pick(self.png_mode, IMAGE_MODES, "lossless")
        self.jpeg_subsampling = pick(self.jpeg_subsampling, JPEG_SUBSAMPLING, "auto")
        self.resize_mode = pick(self.resize_mode, RESIZE_MODES, "none")
        self.metadata = pick(self.metadata, METADATA_MODES, "keep")
        self.output_mode = pick(self.output_mode, OUTPUT_MODES, "replace")
        self.video_codec = pick(self.video_codec, VIDEO_CODECS, "auto")
        self.video_encoder = pick(self.video_encoder, VIDEO_ENCODERS, "auto")
        self.video_quality = pick(self.video_quality, VIDEO_QUALITIES, "balanced")
        self.video_tracks = pick(self.video_tracks, VIDEO_TRACKS, "all")
        self.documents = pick(self.documents, DOCUMENT_MODES, "lossless")
        self.languages = ",".join(x for x in (p.strip() for p in str(self.languages or "").split(",")) if x)
        self.image_quality = max(1, min(100, int(self.image_quality)))
        self.video_crf = max(1, min(63, int(self.video_crf)))
        self.image_effort = max(1, min(9, int(self.image_effort)))
        self.resize_long_edge = max(16, int(self.resize_long_edge))
        self.resize_width = max(16, int(self.resize_width))
        self.resize_height = max(16, int(self.resize_height))
        self.resize_percent = max(1, min(99, int(self.resize_percent)))
        self.min_savings_pct = max(0.0, min(90.0, float(self.min_savings_pct)))
        self.min_savings_kb = max(0, int(self.min_savings_kb))
        self.min_image_kb = max(0, int(self.min_image_kb))
        self.min_video_mb = max(0, int(self.min_video_mb))
        self.workers = max(0, min(64, int(self.workers)))
        self.video_jobs = max(0, min(4, int(self.video_jobs)))
        self.video_max_height = max(0, int(self.video_max_height))
        self.video_max_fps = max(0, int(self.video_max_fps))
        self.audio_bitrate = max(0, min(512, int(self.audio_bitrate)))
        self.smart_target = pick(self.smart_target, SMART_TARGETS, "identical")
        self.audio_lossless = pick(self.audio_lossless, AUDIO_LOSSLESS_MODES, "flac")
        self.audio_lossy = pick(self.audio_lossy, AUDIO_LOSSY_MODES, "keep")
        self.audio_format = pick(self.audio_format, AUDIO_FORMATS, "opus")
        self.audio_quality = pick(self.audio_quality, AUDIO_QUALITIES, "balanced")
        self.min_audio_kb = max(0, int(self.min_audio_kb))
        if not self.output_suffix.strip():
            self.output_suffix = "_compressed"

    def video_target_size(self, w: int, h: int) -> tuple[int, int]:
        """Output size for a w x h video under the resolution limit (even numbers, never upscaled).

        The limit applies to the short side, so portrait phone videos are treated like landscape ones.
        """
        short = min(w, h)
        if not self.video_max_height or short <= self.video_max_height or w <= 0 or h <= 0:
            return w - w % 2, h - h % 2
        scale = self.video_max_height / short
        nw, nh = round(w * scale), round(h * scale)
        return nw - nw % 2, nh - nh % 2

    def resize_target(self, w: int, h: int) -> tuple[int, int] | None:
        """New size for a w x h image, or None if it shouldn't be resized (never upscales)."""
        if self.resize_mode == "none" or w <= 0 or h <= 0:
            return None
        if self.resize_mode == "long_edge":
            scale = self.resize_long_edge / max(w, h)
        elif self.resize_mode == "fit":
            scale = min(self.resize_width / w, self.resize_height / h)
        else:
            scale = self.resize_percent / 100
        if scale >= 1:
            return None
        return max(1, round(w * scale)), max(1, round(h * scale))


def my_languages(s: Settings) -> list[str]:
    """The languages whose tracks are kept: the saved choice, else this computer's languages."""
    from . import languages

    return languages.parse(s.languages) or languages.detect()

