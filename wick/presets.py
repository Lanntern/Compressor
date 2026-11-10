"""Named bundles of compression options (built-in and user-defined).

A preset only stores the options it cares about; everything else (output folder,
performance, scan rules) comes from the current settings. User presets live in
presets.json in the app data folder, shared by the app and the command line.
"""

from __future__ import annotations

import json
from pathlib import Path

from .fileops import atomic_write_text
from .settings import Settings

# Options a preset may set (output location, performance and scan rules stay personal).
PRESET_KEYS = (
    "image_format", "jpeg_mode", "png_mode", "image_quality", "image_effort", "jpeg_progressive",
    "jpeg_subsampling", "resize_mode", "resize_long_edge", "resize_width", "resize_height", "resize_percent",
    "metadata", "preserve_dates", "video_codec", "video_quality", "video_max_height", "video_max_fps",
    "audio_bitrate", "smart_quality", "smart_target", "require_savings", "min_savings_pct", "min_savings_kb",
    "video_fix_cadence", "audio_lossless", "audio_lossy", "audio_format", "audio_quality", "audio_mono", "audio_cd_quality",
    "fit_under_kb", "video_crop_bars",
)

BUILTIN: dict[str, tuple[str, dict]] = {
    "Photo archive": ("Lossless and reversible: JPEG XL, every bit kept, metadata kept; FLAC for music.", {
        "image_format": "jxl", "jpeg_mode": "lossless", "png_mode": "lossless", "metadata": "keep",
        "resize_mode": "none", "video_codec": "av1", "video_quality": "high", "video_max_height": 0,
        "video_max_fps": 0, "audio_bitrate": 0, "smart_quality": False,
        "audio_lossless": "flac", "audio_lossy": "keep", "audio_mono": True, "audio_cd_quality": False}),
    "Screenshots": ("Lossless JPEG XL for screenshots and graphics; videos untouched.", {
        "image_format": "jxl", "jpeg_mode": "lossless", "png_mode": "lossless", "metadata": "keep",
        "resize_mode": "none", "video_codec": "none", "smart_quality": False,
        "audio_lossless": "keep", "audio_lossy": "keep"}),
    "For sharing": ("Small WebP files that open everywhere, max 2560 px, no location or personal info; "
                    "1080p HEVC videos; AAC music.", {
        "image_format": "webp", "jpeg_mode": "lossy", "png_mode": "lossy", "image_quality": 85,
        "resize_mode": "long_edge", "resize_long_edge": 2560, "metadata": "strip_private",
        "video_codec": "hevc", "video_quality": "balanced", "video_max_height": 1080, "video_max_fps": 60,
        "audio_bitrate": 128, "smart_quality": False,
        "audio_lossless": "lossy", "audio_lossy": "reencode", "audio_format": "aac", "audio_quality": "balanced"}),
    "Maximum savings": ("Smart quality aiming for 'good': lossy JPEG XL and AV1, videos up to 1080p, "
                        "Opus music.", {
        "image_format": "jxl", "jpeg_mode": "lossy", "png_mode": "lossy", "image_quality": 80,
        "resize_mode": "none", "metadata": "keep", "video_codec": "av1", "video_quality": "small",
        "video_max_height": 1080, "video_max_fps": 0, "audio_bitrate": 96, "smart_quality": True,
        "smart_target": "good", "audio_lossless": "lossy", "audio_lossy": "reencode", "audio_format": "opus",
        "audio_quality": "balanced", "audio_mono": True}),
    "Keep compatible": ("Same file types (JPEG stays JPEG), visually identical; HEVC video.", {
        "image_format": "keep", "jpeg_mode": "lossy", "png_mode": "lossless", "image_quality": 88,
        "resize_mode": "none", "metadata": "keep", "video_codec": "hevc", "video_quality": "high",
        "video_max_height": 0, "video_max_fps": 0, "audio_bitrate": 0, "smart_quality": True,
        "smart_target": "identical", "audio_lossless": "keep", "audio_lossy": "keep"}),
}


def _file() -> Path:
    from .journal import data_dir

    return data_dir() / "presets.json"


def user_presets() -> dict[str, dict]:
    try:
        text = _file().read_text("utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
        return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}
    except (ValueError, AttributeError, TypeError):
        # Damaged (a disk error, an edit by hand): kept aside, never written over with an empty list.
        from .fileops import set_aside
        from .log import log

        log.warning("Presets file was damaged; kept as %s", set_aside(_file()))
        return {}


def names() -> list[str]:
    return list(BUILTIN) + [n for n in user_presets() if n not in BUILTIN]


def get(name: str) -> dict | None:
    if name in BUILTIN:
        return dict(BUILTIN[name][1])
    return user_presets().get(name)


def description(name: str) -> str:
    if name in BUILTIN:
        return BUILTIN[name][0]
    return "Your preset"


def is_builtin(name: str) -> bool:
    return name in BUILTIN


def save(name: str, s: Settings) -> None:
    name = name.strip()
    if not name or name in BUILTIN:
        raise ValueError("Choose a name that isn't one of the built-in presets")
    data = user_presets()
    data[name] = {k: getattr(s, k) for k in PRESET_KEYS}
    atomic_write_text(_file(), json.dumps(data, indent=2, ensure_ascii=False))


def delete(name: str) -> None:
    data = user_presets()
    if data.pop(name, None) is not None:
        atomic_write_text(_file(), json.dumps(data, indent=2, ensure_ascii=False))


def apply(name: str, base: Settings) -> Settings:
    values = get(name)
    if values is None:
        raise KeyError(f"No preset called {name!r}")
    merged = {**base.to_dict(), **{k: v for k, v in values.items() if k in PRESET_KEYS}}
    return Settings.from_dict(merged)


def matching(s: Settings) -> str | None:
    """The preset whose options all equal the current settings, if any."""
    for name in names():
        values = get(name) or {}
        if values and all(getattr(s, k, None) == v for k, v in values.items() if k in PRESET_KEYS):
            return name
    return None
