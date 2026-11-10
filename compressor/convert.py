"""Plain format conversion (not about saving space): images, videos and audio to a chosen format."""

from __future__ import annotations

import io
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import metadata as md
from . import steps, tools
from .fileops import link_or_copy, remove_quietly, temp_name
from .formats import (
    ARCHIVE, AUDIO, COMIC, DOC, DOCUMENT, FONT, HEIC, JXL, PANDOC_INPUTS, PDF, RAW, SVG, VIDEO, read_jxl_info,
    register_heif,
)
from .images import Ctx, VerificationFailed, _decoded_path, _filtered_meta, _load_frames, _load_static, _open
from .plan import Plan
from .probe import VideoInfo, probe_video
from .settings import Settings

# id: (label, extension, supports animation, supports lossless, lossy quality applies)
IMAGE_TARGETS = {
    "png": ("PNG", ".png", True, True, False),
    "jpeg": ("JPEG", ".jpg", False, False, True),
    "webp": ("WebP", ".webp", True, True, True),
    "avif": ("AVIF", ".avif", True, False, True),
    "jxl": ("JPEG XL", ".jxl", True, True, True),
    "heic": ("HEIC", ".heic", False, False, True),
    "tiff": ("TIFF", ".tif", False, True, False),
    "bmp": ("BMP", ".bmp", False, True, False),
    "gif": ("GIF", ".gif", True, False, False),
    "mp4": ("MP4 video (animations only)", ".mp4", True, False, False),   # GIF, WebP and APNG animations
    "ico": ("ICO (icon)", ".ico", False, True, False),                    # several sizes: icons.py
    "svg": ("SVG, optimized (SVGs only)", ".svg", False, True, False),    # smaller, looks the same: icons.py
    "pdf": ("PDF", ".pdf", False, True, False),                          # JPEGs kept as they are: pdftools.py
    "icns": ("ICNS (Mac icon)", ".icns", False, True, False),            # icons.make_icns
    "cur": ("CUR (Windows cursor)", ".cur", False, True, False),          # icons.make_cur
    "keep": ("Same format (to turn or flip)", "", True, True, False),    # see turn_label / _turn_jpeg
}
# The format a picture keeps when only turned or flipped.
KEEP_TARGET = {"jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp", "tiff": "tiff", "bmp": "bmp",
               "heic": "heic", "avif": "avif", "jxl": "jxl"}
TURN_LABELS = {90: "turned right", 180: "turned upside down", 270: "turned left"}
FLIP_LABELS = {"h": "mirrored", "v": "flipped upside down"}
MP4_FAMILY = {".mp4", ".m4v", ".mov", ".3gp"}       # containers that store a turn as a display matrix
# id: (label, extension, codec)
VIDEO_TARGETS = {
    "mp4_h264": ("MP4 (H.264, plays everywhere)", ".mp4", "h264"),
    "mp4_hevc": ("MP4 (HEVC)", ".mp4", "hevc"),
    "mkv_av1": ("MKV (AV1)", ".mkv", "av1"),
    "webm_vp9": ("WebM (VP9)", ".webm", "vp9"),
    "keep": ("Same format (to turn or flip)", "", "keep"),
    "gif": ("GIF (animation, no sound)", ".gif", "gif"),
    "webp_anim": ("Animated WebP (no sound)", ".webp", "webp"),
    # The sound only (copied as it is when it's already in that format: no quality lost).
    "audio_mp3": ("Sound only: MP3", ".mp3", "audio"),
    "audio_aac": ("Sound only: AAC (.m4a)", ".m4a", "audio"),
    "audio_opus": ("Sound only: Opus", ".opus", "audio"),
    "audio_flac": ("Sound only: FLAC", ".flac", "audio"),
    "audio_wav": ("Sound only: WAV", ".wav", "audio"),
}
SOUND_TARGETS = {k for k, v in VIDEO_TARGETS.items() if v[2] == "audio"}
_COPYABLE = {"mp3": {"mp3"}, "aac": {"aac"}, "opus": {"opus"}, "flac": {"flac"}}
# id: (label, extension, Pandoc's writer)
DOCUMENT_TARGETS = {
    "docx": ("Word (.docx)", ".docx", "docx"),
    "odt": ("OpenDocument Text (.odt)", ".odt", "odt"),
    "html": ("Web page (.html)", ".html", "html5"),
    "md": ("Markdown (text only)", ".md", "gfm"),
    "epub": ("EPUB e-book", ".epub", "epub3"),
    "rtf": ("Rich Text (.rtf)", ".rtf", "rtf"),
    "txt": ("Plain text", ".txt", "plain"),
    "pdf": ("PDF (with Office or LibreOffice)", ".pdf", "pdf"),          # office.py
}
# PDFs (see pdftools.py). id: (label, extension: "" for a folder of files)
PDF_TARGETS = {
    "png": ("Pages as PNG images", ""),
    "jpeg": ("Pages as JPEG images", ""),
    "split": ("Single-page PDFs", ""),
    "rotate90": ("Pages turned right", ".pdf"),
    "rotate270": ("Pages turned left", ".pdf"),
    "rotate180": ("Pages turned upside down", ".pdf"),
    "protect": ("With a password", ".pdf"),
    "unlock": ("Without its password", ".pdf"),
}
# Fonts (see fonts.py). id: (label, extension: "" = .ttf or .otf, whichever the font's outlines are)
FONT_TARGETS = {
    "woff2": ("WOFF2 (for websites)", ".woff2"),
    "woff": ("WOFF (older browsers)", ".woff"),
    "desktop": ("TTF or OTF (to install)", ""),
}
# Archives (only when chosen). id: (label, extension)
ARCHIVE_TARGETS = {
    "7z": ("7-Zip (smallest)", ".7z"),
    "zip": ("ZIP (opens everywhere)", ".zip"),
}
PDF_PAGE_TARGETS = ("png", "jpeg", "split")         # (what the page's "PDFs to" offers; the rest: context menu)
ANIMATION_TARGETS = {"gif", "webp_anim"}
ANIMATION_HEIGHT = 480          # an animation's short side unless chosen (GIFs of full-size video are huge)
ANIMATION_FPS = {"gif": 15, "webp_anim": 24}

# id: (label, extension)
AUDIO_TARGETS = {
    "mp3": ("MP3 (plays everywhere)", ".mp3"),
    "aac": ("AAC (.m4a)", ".m4a"),
    "opus": ("Opus", ".opus"),
    "flac": ("FLAC (lossless)", ".flac"),
    "wav": ("WAV (uncompressed)", ".wav"),
}


@dataclass
class ConvertOptions:
    image_target: str = "png"      # IMAGE_TARGETS key or "none"
    video_target: str = "mp4_h264"  # VIDEO_TARGETS key or "none"
    quality: int = 90              # lossy image quality
    lossless: bool = False         # WebP / JPEG XL lossless
    only_if_smaller: bool = False
    audio_target: str = "mp3"      # AUDIO_TARGETS key or "none"
    document_target: str = "docx"  # DOCUMENT_TARGETS key or "none"
    max_side: int = 0              # images: longest side in pixels (0 = keep; never enlarged)
    video_height: int = 0          # videos: short side, e.g. 1080 (0 = keep; never enlarged)
    video_fps: int = 0             # videos: frame rate cap (0 = keep)
    audio_kbps: int = 0            # lossy audio bitrate (0 = automatic, high quality)
    sample_rate: int = 0           # audio: Hz (0 = keep)
    channels: int = 0              # audio: 1 mono, 2 stereo (0 = keep)
    ico_sizes: tuple[int, ...] = (16, 32, 48)   # icons: the sizes inside (see icons.ICO_PRESETS)
    turn: int = 0                  # pictures and videos: degrees clockwise (0, 90, 180, 270)
    flip: str = ""                 # "" | "h" (mirror left to right) | "v" (upside down mirror); before turning
    pdf_target: str = "none"       # PDF_TARGETS key or "none"
    pdf_dpi: int = 150             # PDF pages as images: dots per inch
    pdf_password: str = ""         # the password to add, or the one that opens it (never saved)
    pdf_page: str = "picture"      # pictures in a PDF: page size (pdftools.PAGE_SIZES)
    font_target: str = "none"      # FONT_TARGETS key or "none"
    font_latin: bool = False       # fonts: keep only the characters of Latin-script languages (smaller)
    comic_target: str = "none"     # comic books: "pdf" or "none"
    archive_target: str = "none"   # ARCHIVE_TARGETS key or "none" (never a default: see archives.py)

    def target_for(self, kind: str) -> tuple[str, str] | None:
        """(target id, extension) for a file kind, or None if it isn't converted."""
        if kind == COMIC:
            return ("pdf", ".pdf") if self.comic_target == "pdf" else None
        if kind == ARCHIVE:
            if self.archive_target not in ARCHIVE_TARGETS:
                return None
            return self.archive_target, ARCHIVE_TARGETS[self.archive_target][1]
        if kind == FONT:
            if self.font_target not in FONT_TARGETS:
                return None
            return self.font_target, FONT_TARGETS[self.font_target][1]
        if kind == PDF:
            if self.pdf_target not in PDF_TARGETS:
                return None
            return self.pdf_target, PDF_TARGETS[self.pdf_target][1]
        if kind in (DOCUMENT, DOC):       # (only documents Pandoc reads are listed: see PANDOC_INPUTS)
            if self.document_target not in DOCUMENT_TARGETS:
                return None
            return self.document_target, DOCUMENT_TARGETS[self.document_target][1]
        if kind == AUDIO:
            if self.audio_target not in AUDIO_TARGETS:
                return None
            return self.audio_target, AUDIO_TARGETS[self.audio_target][1]
        if kind == VIDEO:
            if self.video_target not in VIDEO_TARGETS:
                return None
            return self.video_target, VIDEO_TARGETS[self.video_target][1]
        if self.image_target not in IMAGE_TARGETS:
            return None
        return self.image_target, IMAGE_TARGETS[self.image_target][1]


LOSSLESS_AUDIO = {"flac", "wav"}


def turn_label(opts: ConvertOptions) -> str:
    parts = [FLIP_LABELS[opts.flip]] if opts.flip in FLIP_LABELS else []
    if opts.turn in TURN_LABELS:
        parts.append(TURN_LABELS[opts.turn])
    return " and ".join(parts)


def describe(kind: str, opts: ConvertOptions) -> str:
    text = _describe(kind, opts)
    t = opts.target_for(kind)
    if t is not None and kind not in (AUDIO, DOC, DOCUMENT, PDF, FONT, COMIC, ARCHIVE) \
            and t[0] not in SOUND_TARGETS \
            and turn_label(opts):
        text = (text.replace("Same format (to turn or flip)", "Same format") + ", " + turn_label(opts))
    return text


def _describe(kind: str, opts: ConvertOptions) -> str:
    t = opts.target_for(kind)
    if t is None:
        return "Not converted"
    if kind == VIDEO and t[0] in SOUND_TARGETS:
        return VIDEO_TARGETS[t[0]][0]
    if kind == VIDEO:
        parts = [re.sub(r" \(.*\)$", "", VIDEO_TARGETS[t[0]][0]) if opts.video_height or opts.video_fps
                 else VIDEO_TARGETS[t[0]][0]]
        if opts.video_height:
            parts.append(f"{opts.video_height}p")
        if opts.video_fps:
            parts.append(f"{opts.video_fps} fps")
        return ", ".join(parts)
    if kind in (DOCUMENT, DOC):
        return DOCUMENT_TARGETS[t[0]][0]
    if kind == FONT:
        return FONT_TARGETS[t[0]][0].split(" (")[0] + (", Latin only" if opts.font_latin else "")
    if kind == COMIC:
        return "PDF, a page per picture"
    if kind == ARCHIVE:
        return ARCHIVE_TARGETS[t[0]][0].split(" (")[0] + ", repacked"
    if kind == PDF:
        label = PDF_TARGETS[t[0]][0]
        return f"{label}, {opts.pdf_dpi} dpi" if t[0] in ("png", "jpeg") else label
    if kind == AUDIO:
        lossy = t[0] not in LOSSLESS_AUDIO
        parts = [re.sub(r" \(.*\)$", "", AUDIO_TARGETS[t[0]][0])
                 if (opts.audio_kbps and lossy) or opts.sample_rate or opts.channels else AUDIO_TARGETS[t[0]][0]]
        if opts.audio_kbps and lossy:
            parts.append(f"{opts.audio_kbps} kbps")
        if opts.sample_rate and t[0] != "opus":
            parts.append(f"{opts.sample_rate / 1000:g} kHz")
        if opts.channels:
            parts.append("mono" if opts.channels == 1 else "stereo")
        return ", ".join(parts)
    label, _ext, _anim, lossless_ok, lossy = IMAGE_TARGETS[t[0]]
    if t[0] == "ico":
        from .icons import ICO_PRESETS, preset_of

        preset = preset_of(opts.ico_sizes)
        return "ICO, " + (ICO_PRESETS[preset][0].split(" (")[0] + " sizes" if preset != "custom" else
                          ", ".join(map(str, sorted(opts.ico_sizes))) + " px")
    if t[0] == "svg":
        return "SVG, optimized"
    if opts.lossless and lossless_ok and lossy:
        out = f"{label}, lossless"
    else:
        out = f"{label}, quality {opts.quality}" if lossy else label
    return out + (f", up to {opts.max_side:,} px" if opts.max_side else "")


# ================================================================ images

def _flatten(img, background=(255, 255, 255)):
    """Composite transparency onto a solid background (for formats without alpha)."""
    from PIL import Image

    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, background + (255,))
        bg.alpha_composite(rgba)
        return bg.convert("RGB")
    if img.mode not in ("RGB", "L"):
        return img.convert("RGB")
    return img


def convert_image(src: Path, kind: str, info, target: str, opts: ConvertOptions, s: Settings) -> Path:
    """Convert to a temp file next to ``src`` and verify it decodes. Caller owns the result."""
    if target == "keep":                                  # only turned or flipped: in its own format
        if kind == "jpeg" and getattr(info, "frames", 1) <= 1:
            return _turn_jpeg(src, opts)
        if kind not in KEEP_TARGET:
            raise ValueError(f"A {kind.upper()} file can't be saved in its own format here: choose a format")
        import dataclasses

        target = KEEP_TARGET[kind]
        exact = bool(getattr(info, "lossless_source", False))
        opts = dataclasses.replace(opts, lossless=exact or opts.lossless, quality=max(opts.quality, 95))
    label, ext, anim_ok, lossless_ok, lossy = IMAGE_TARGETS[target]
    if kind == HEIC or target == "heic":
        register_heif()
    plan = Plan(True, target_ext=ext, route="convert", lossless=bool(opts.lossless and lossless_ok))
    ctx = Ctx(src, kind, info, plan, s, temp_name(src.parent, ext))
    try:
        ctx.staged = ctx.tmp(src.suffix.lower(), "-in")
        link_or_copy(src, ctx.staged)
        if kind == JXL:
            if target == "jpeg" and not opts.max_side and _rebuild_jpeg(ctx, s):
                return ctx.out
            _decode_jxl(ctx)
        elif kind == RAW:
            _decode_raw(ctx)
        if target in ("ico", "svg", "icns", "cur"):
            _make_icon_or_svg(ctx, target, opts)
            return ctx.out
        if target == "pdf":
            from . import pdftools

            if kind == SVG:
                _draw_svg(ctx, opts.max_side)
            steps.report("Making the PDF")
            pdftools.combine([ctx.staged], ctx.out, opts.pdf_page)
            return ctx.out
        if opts.max_side and ctx.info is not None and kind != SVG:
            plan.resize = Settings(resize_mode="long_edge", resize_long_edge=opts.max_side).resize_target(
                ctx.info.width, ctx.info.height)
        if kind == SVG:
            _draw_svg(ctx, opts.max_side)
        steps.report(f"Encoding {label}")
        animated = bool(info is not None and getattr(info, "frames", 1) > 1 and anim_ok)
        if animated:
            frames, durations, loop = _load_frames(ctx.staged)
            if plan.resize:
                from PIL import Image

                frames = [f.resize(plan.resize, Image.LANCZOS) for f in frames]
            frames = [_turn_picture(f, opts) for f in frames]
            with _open(ctx.staged) as im:
                meta = _filtered_meta(ctx, im)
            _save_animated(frames, durations, loop, target, ctx, opts, meta)
        else:
            img, meta = _load_static(ctx)
            img = _turn_picture(img, opts)
            if img.mode in ("I;16", "I", "F") and target not in ("png", "tiff", "jxl"):
                img = img.convert("RGB")
            _save_static(img, target, ctx, opts, meta)
        if not ctx.out.exists() or ctx.out.stat().st_size == 0:
            raise RuntimeError("The encoder produced no output")
        steps.report("Checking the result")
        _verify(ctx, frames=len(frames) if animated else 1)
        return ctx.out
    except BaseException:
        remove_quietly(ctx.out)
        raise
    finally:
        remove_quietly(*ctx.temps)


def _rebuild_jpeg(ctx: Ctx, s: Settings) -> bool:
    """A JPEG XL made losslessly from a JPEG holds what's needed to give back that exact JPEG, byte for byte:
    no re-encoding, so no quality is lost. Only when the metadata is kept as it was (it's part of the bytes)."""
    info = read_jxl_info(ctx.staged)
    if info is None or not info.reconstructs_jpeg or s.metadata != "keep":
        return False
    steps.report("Rebuilding the original JPEG")
    try:
        tools.run([tools.require("djxl"), ctx.staged.name, ctx.out.name], cwd=ctx.dir, timeout=1800)
    except tools.ToolError:
        remove_quietly(ctx.out)
        return False            # (then it's decoded and encoded like any other image)
    if not ctx.out.exists() or ctx.out.read_bytes()[:2] != b"\xff\xd8":
        remove_quietly(ctx.out)
        return False
    steps.report("Checking the result")
    _verify(ctx, frames=1)
    return True


def _raw_profile(text) -> bytes | None:
    """ImageMagick's "Raw profile type …" text (how djxl writes EXIF/XMP into a PNG): name, length, then hex."""
    if not isinstance(text, str):
        return None
    lines = [ln.strip() for ln in text.strip().splitlines()]
    try:
        return bytes.fromhex("".join(lines[2:])) or None
    except ValueError:
        return None


def _decode_jxl(ctx: Ctx) -> None:
    """Decode a JPEG XL source with djxl to PNG (keeping its colour profile and metadata) for the encoders.
    djxl turns the pixels upright, so the EXIF orientation is reset; its EXIF also carries the JPEG XL box's
    4-byte offset, which isn't part of EXIF."""
    from PIL import Image

    steps.report("Decoding JPEG XL")
    animated = ctx.info is not None and ctx.info.animated
    dec = ctx.tmp(".png", "-dec")
    tools.run([tools.require("djxl"), ctx.staged.name, dec.name], cwd=ctx.dir, timeout=1800)
    with Image.open(dec) as im:
        im.load()                                # (text chunks may follow the image data)
        exif = _raw_profile(im.info.get("Raw profile type exif")) or im.info.get("exif")
        xmp = _raw_profile(im.info.get("Raw profile type xmp")) or im.info.get("xmp") \
            or im.info.get("XML:com.adobe.xmp")
        size = im.size
    if isinstance(exif, bytes):
        exif = md._strip_header(exif)
        if exif and exif[:4] == b"\0\0\0\0" and exif[4:6] in (b"MM", b"II"):
            exif = exif[4:]
        exif = md.set_orientation(exif, 1)
    if isinstance(xmp, str):
        xmp = xmp.encode("utf-8")
    if not animated:      # (rewriting the chunks of an animated PNG isn't needed: animations keep no EXIF here)
        # (drop_other: djxl's text chunks hold the same metadata in a form Pillow misreads)
        dec.write_bytes(md.png_write_meta(dec.read_bytes(), md.Meta(exif or None, xmp or None, None), True))
    ctx.staged = dec
    if ctx.info is not None and not (ctx.info.width and ctx.info.height):
        ctx.info.width, ctx.info.height = size


_PIL_TURN = {90: "ROTATE_270", 180: "ROTATE_180", 270: "ROTATE_90"}      # (Pillow turns counter-clockwise)
_PIL_FLIP = {"h": "FLIP_LEFT_RIGHT", "v": "FLIP_TOP_BOTTOM"}


def _turn_picture(img, opts: ConvertOptions):
    from PIL import Image

    if opts.flip in _PIL_FLIP:
        img = img.transpose(getattr(Image.Transpose, _PIL_FLIP[opts.flip]))
    if opts.turn in _PIL_TURN:
        img = img.transpose(getattr(Image.Transpose, _PIL_TURN[opts.turn]))
    return img


_EXIF_TURN = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE", 6: "ROTATE_270",
              7: "TRANSVERSE", 8: "ROTATE_90"}           # what each EXIF orientation does (as Pillow does it)


def orientation_after(orientation: int, opts: ConvertOptions) -> int:
    """The EXIF orientation that shows the picture flipped and turned as asked, on top of ``orientation``.
    Worked out on a tiny test picture, so it matches what viewers do exactly."""
    from PIL import Image

    probe = Image.frombytes("L", (3, 2), bytes([1, 2, 3, 4, 5, 6]))

    def shown(o: int):
        return probe.transpose(getattr(Image.Transpose, _EXIF_TURN[o])) if o in _EXIF_TURN else probe

    want = _turn_picture(shown(orientation if orientation in range(1, 9) else 1), opts)
    for o in range(1, 9):
        if shown(o).size == want.size and shown(o).tobytes() == want.tobytes():
            return o
    return 1


def _turn_jpeg(src: Path, opts: ConvertOptions) -> Path:
    """A JPEG turned or flipped losslessly: only its EXIF orientation changes, the picture data is the very
    same bytes (every current viewer and browser follows the orientation)."""
    from PIL import Image

    steps.report("Turning (no quality lost)")
    data = src.read_bytes()
    with Image.open(src) as im:
        meta = md.read_meta(im)
        before = int(im.getexif().get(md.ORIENTATION, 1) or 1)
    after = orientation_after(before, opts)
    if after == 1:
        exif = md.set_orientation(meta.exif, 1) if meta.exif else None
    else:
        ex = Image.Exif()
        if meta.exif:
            ex.load(md.EXIF_HEADER + meta.exif)
        ex[md.ORIENTATION] = after
        exif = md._strip_header(ex.tobytes())
    out = temp_name(src.parent, src.suffix.lower() or ".jpg")
    try:
        new = md.jpeg_write_meta(data, md.Meta(exif, meta.xmp, meta.icc), drop_other=False)
        out.write_bytes(new)
        steps.report("Checking the result")
        with Image.open(out) as im:
            im.load()
            got = int(im.getexif().get(md.ORIENTATION, 1) or 1)
        if got != after:
            raise VerificationFailed("The new orientation didn't take")
        if new[md._jpeg_segments(new)[1]:] != data[md._jpeg_segments(data)[1]:]:
            raise VerificationFailed("The picture data changed while turning")
        return out
    except BaseException:
        remove_quietly(out)
        raise


def _orientation_of(exif: bytes) -> int:
    from PIL import Image

    ex = Image.Exif()
    try:
        ex.load(md.EXIF_HEADER + exif)
    except Exception:  # noqa: BLE001
        return 1
    return int(ex.get(md.ORIENTATION, 1) or 1)


def video_rotation(path: Path) -> int:
    """The video's display rotation in degrees counter-clockwise (what ffmpeg's -display_rotation takes)."""
    import json as _json

    ffprobe = tools.require("ffprobe")
    p = tools.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream_side_data=rotation",
                   "-of", "json", str(path)], timeout=60, check=False)
    try:
        for st in _json.loads(p.stdout or "{}").get("streams", []):
            for sd in st.get("side_data_list", []) or []:
                if "rotation" in sd:
                    return int(round(float(sd["rotation"])))
    except (ValueError, TypeError):
        pass
    return 0


def turn_video(src: Path, opts: ConvertOptions, cancel: threading.Event | None = None) -> Path:
    """An MP4 or MOV turned or flipped without re-encoding: only its display matrix changes (players and
    phones turn the picture as they show it)."""
    from .probe import probe_video

    if src.suffix.lower() not in MP4_FAMILY:
        raise ValueError("Only MP4 and MOV videos can be turned without re-encoding: convert it to MP4")
    before = video_rotation(src)
    ccw = (before - opts.turn) % 360
    ccw = ccw - 360 if ccw > 180 else ccw
    out = temp_name(src.parent, src.suffix.lower())
    ffmpeg = tools.require("ffmpeg")
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-display_rotation:v:0", str(ccw)]
    if opts.flip == "h":
        args += ["-display_hflip:v:0"]
    elif opts.flip == "v":
        args += ["-display_vflip:v:0"]
    args += ["-i", str(src), "-map", "0", "-map_metadata", "0", "-c", "copy", "-movflags", "+faststart", str(out)]
    try:
        steps.report("Turning (no quality lost)")
        tools.run(args, timeout=3600)
        steps.report("Checking the result")
        a, b = probe_video(src), probe_video(out)
        if b is None or a is None or abs(a.duration - b.duration) > max(0.5, a.duration * 0.01) \
                or len(a.streams) != len(b.streams):
            raise VerificationFailed("The turned video doesn't match the original")
        if not opts.flip and video_rotation(out) % 360 != ccw % 360:
            raise VerificationFailed("The video didn't take the new rotation")
        return out
    except BaseException:
        remove_quietly(out)
        raise


def _make_icon_or_svg(ctx: Ctx, target: str, opts: ConvertOptions) -> None:
    """An .ico with the chosen sizes (an SVG drawn at each size), or an optimized copy of an SVG; both checked."""
    from PIL import Image

    from . import icons

    if target == "svg":
        if ctx.kind != SVG:
            raise ValueError("Only SVG drawings can be optimized")
        steps.report("Optimizing the SVG")
        original = ctx.src.read_bytes()
        smaller = icons.optimize_svg(original)
        steps.report("Checking the result")
        if not icons.looks_same(original, smaller):
            raise VerificationFailed("The optimized drawing doesn't look the same")
        ctx.out.write_bytes(smaller)
        return
    steps.report("Making the icon" if target != "cur" else "Making the cursor")
    if target == "icns":
        icons.make_icns(ctx.staged, ctx.out)
        steps.report("Checking the result")
        try:
            with Image.open(ctx.out) as im:
                im.load()
                if im.size != (1024, 1024) and im.size != (512, 512):
                    raise VerificationFailed(f"The icon's largest picture is {im.size}")
        except VerificationFailed:
            raise
        except Exception as e:  # noqa: BLE001
            raise VerificationFailed(f"The icon can't be opened ({e})") from e
        return
    made = (icons.make_cur if target == "cur" else
            lambda src, dst: icons.make_ico(src, opts.ico_sizes, dst))(ctx.staged, ctx.out)   # (SVGs: redrawn)
    steps.report("Checking the result")
    got = icons.ico_sizes(ctx.out)
    if target == "ico":
        try:
            with Image.open(ctx.out) as im:
                im.load()
        except Exception as e:  # noqa: BLE001
            raise VerificationFailed(f"The icon can't be opened ({e})") from e
    if got != sorted(made):
        raise VerificationFailed(f"The icon holds {got}, expected {sorted(made)}")


def _decode_raw(ctx: Ctx) -> None:
    """A camera RAW photo developed by LibRaw with the camera's white balance (as the camera's own JPEG would
    be), turned upright, to an 8-bit PNG. Its EXIF comes along when the RAW keeps it TIFF-style (most do)."""
    import dataclasses

    import rawpy
    from PIL import Image

    steps.report("Developing the RAW photo")
    with rawpy.imread(str(ctx.staged)) as raw:
        rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
    img = Image.fromarray(rgb)
    exif = None
    try:
        with Image.open(ctx.staged) as im:          # (DNG, NEF, CR2, ARW... are TIFF files inside)
            ex = im.getexif()
            if len(ex):
                exif = md.set_orientation(ex.tobytes(), 1)
    except Exception:  # noqa: BLE001 - CR3, RAF and others: no EXIF that Pillow reads
        pass
    dec = ctx.tmp(".png", "-raw")
    img.save(dec, "PNG", compress_level=1)
    if exif:
        dec.write_bytes(md.png_write_meta(dec.read_bytes(), md.Meta(md._strip_header(exif), None, None), True))
    ctx.staged = dec
    ctx.info = dataclasses.replace(ctx.info, width=img.width, height=img.height, bit_depth=8)  # type: ignore[type-var]


def _draw_svg(ctx: Ctx, size: int) -> None:
    """Draw an SVG to a PNG: at its own size, or with its longest side ``size`` pixels (a drawing scales up
    as sharply as it scales down)."""
    import dataclasses

    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    steps.report("Drawing the SVG")
    r = QSvgRenderer(str(ctx.staged))
    if not r.isValid():
        raise RuntimeError("The SVG can't be read")
    w, h = ctx.info.width, ctx.info.height
    if size:
        scale = size / max(w, h)
        w, h = max(1, round(w * scale)), max(1, round(h * scale))
        ctx.plan.resize = None                       # (drawn at that size already)
    image = QImage(w, h, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    r.render(painter, QRectF(0, 0, w, h))
    painter.end()
    dec = ctx.tmp(".png", "-svg")
    if not image.save(str(dec)):
        raise RuntimeError("The SVG couldn't be drawn")
    ctx.staged = dec
    ctx.info = dataclasses.replace(ctx.info, width=w, height=h)  # type: ignore[type-var]


def _save_static(img, target: str, ctx: Ctx, opts: ConvertOptions, meta: md.Meta) -> None:
    out, q = ctx.out, opts.quality
    icc = {"icc_profile": meta.icc} if meta.icc else {}
    if target == "png":
        if img.mode == "CMYK":
            img = img.convert("RGB")
        img.save(out, "PNG", compress_level=6, **icc)
        if meta.exif or meta.xmp:
            out.write_bytes(md.png_write_meta(out.read_bytes(), md.Meta(meta.exif, meta.xmp, None), False))
    elif target == "jpeg":
        flat = img if img.mode == "CMYK" else _flatten(img)
        buf = io.BytesIO()
        flat.save(buf, "JPEG", quality=q, subsampling="4:4:4" if q >= 90 else "4:2:0", progressive=True,
                  optimize=True)
        out.write_bytes(md.jpeg_write_meta(buf.getvalue(), meta, drop_other=False))
    elif target in ("webp", "avif", "heic"):
        if img.mode in ("CMYK", "1", "I;16"):
            img = img.convert("RGB")
        kw = meta.pil_kwargs()
        if target == "webp":
            from .images import webp_method

            method = webp_method(opts.lossless, img.width * img.height)
            opt = {"lossless": True, "quality": 100, "method": method, "exact": True} if opts.lossless \
                else {"quality": q, "method": method}
            img.save(out, "WEBP", **opt, **kw)
        elif target == "avif":
            img.save(out, "AVIF", quality=q, speed=4, subsampling="4:4:4" if q >= 90 else "4:2:0", **kw)
        else:
            img.save(out, "HEIF", quality=q, **kw)
    elif target == "jxl":
        mid = ctx.tmp(".png", "-mid")
        if img.mode == "CMYK":
            img = img.convert("RGB")
        img.save(mid, "PNG", compress_level=0, **icc)
        args = [tools.require("cjxl"), mid.name, out.name, "-e", str(ctx.s.image_effort), "--quiet"]
        args += ["-d", "0"] if opts.lossless else ["-q", str(q)]
        tools.run(args, cwd=ctx.dir, timeout=3600)
        if meta.exif or meta.xmp:
            out.write_bytes(md.jxl_write_meta(out.read_bytes(), md.Meta(meta.exif, meta.xmp, None)))
    elif target == "tiff":
        kw = {"icc_profile": meta.icc} if meta.icc else {}
        try:
            img.save(out, "TIFF", compression="tiff_adobe_deflate",
                     **({**kw, "exif": md.EXIF_HEADER + meta.exif} if meta.exif else kw))
        except Exception:  # noqa: BLE001 - libtiff rejects some EXIF layouts: keep the image, drop EXIF
            remove_quietly(out)
            img.save(out, "TIFF", compression="tiff_adobe_deflate", **kw)
    elif target == "bmp":
        bmp = img if img.mode in ("RGB", "RGBA", "L", "P", "1") else img.convert("RGBA" if "A" in img.mode else "RGB")
        bmp.save(out, "BMP")
    elif target == "gif":
        img.convert("RGBA").save(out, "GIF", optimize=True)
    else:
        raise ValueError(f"Unknown target {target}")


def _save_animated(frames, durations, loop, target: str, ctx: Ctx, opts: ConvertOptions, meta: md.Meta) -> None:
    out, q = ctx.out, opts.quality
    common = {"save_all": True, "append_images": frames[1:], "duration": durations, "loop": loop}
    if target == "png":
        frames[0].save(out, "PNG", disposal=1, blend=0, **common)
    elif target == "gif":
        frames[0].save(out, "GIF", disposal=2, optimize=True, **common)
    elif target == "webp":
        opt = {"lossless": True, "quality": 100, "method": 4, "exact": True} if opts.lossless else {"quality": q,
                                                                                                    "method": 4}
        frames[0].save(out, "WEBP", **opt, **common, **meta.pil_kwargs())
    elif target == "avif":
        frames[0].save(out, "AVIF", quality=q, speed=6, **common, **meta.pil_kwargs())
    elif target == "jxl":
        mid = ctx.tmp(".png", "-apng")
        frames[0].save(mid, "PNG", disposal=1, blend=0, compress_level=0, **common)
        args = [tools.require("cjxl"), mid.name, out.name, "-e", str(ctx.s.image_effort), "--quiet"]
        args += ["-d", "0"] if opts.lossless else ["-q", str(q)]
        tools.run(args, cwd=ctx.dir, timeout=3600)
    else:
        raise ValueError(f"{target} can't hold animations")


def _verify(ctx: Ctx, frames: int) -> None:
    path = _decoded_path(ctx, ctx.out)
    w, h = ctx.plan.resize or (ctx.info.width, ctx.info.height)
    try:
        with _open(path) as im:
            size = im.size
            n = getattr(im, "n_frames", 1)
            im.seek(n - 1)
            im.load()
    except Exception as e:  # noqa: BLE001
        raise VerificationFailed(f"The result can't be opened ({e})") from e
    if tuple(size) not in ((w, h), (h, w)):
        raise VerificationFailed(f"The result is {size[0]}×{size[1]}, expected {w}×{h}")
    if frames > 1 and n != frames:
        raise VerificationFailed(f"The result has {n} frames, expected {frames}")


# ================================================================ videos

def video_args(src: Path, dst: Path, info: VideoInfo, target: str, s: Settings) -> list[str]:
    """``s.video_max_height`` and ``s.video_max_fps`` limit the size and frame rate (Convert's per-file
    settings); otherwise the picture is kept as it is."""
    from .video import build_args, choose_encoder, encoder_works, metadata_args

    _label, _ext, codec = VIDEO_TARGETS[target]
    if codec in ("av1", "hevc"):
        s2 = Settings.from_dict({**s.to_dict(), "video_codec": codec})
        return build_args(src, dst, info, s2, choose_encoder(codec, s.video_encoder))
    ffmpeg = tools.require("ffmpeg")
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
            "-i", str(src), "-map", f"0:{info.main_video_index}", "-map", "0:a?"]
    args += metadata_args(s.metadata, info)
    if codec == "h264":
        # On the GPU when one can (NVIDIA, Intel or AMD, in that order), else x264 on the CPU.
        gpu_args = {"h264_nvenc": ["-preset", "p6", "-rc", "vbr", "-cq", "21", "-b:v", "0", "-pix_fmt", "yuv420p"],
                    "h264_qsv": ["-preset", "slower", "-global_quality", "21", "-pix_fmt", "nv12"],
                    "h264_amf": ["-quality", "quality", "-rc", "cqp", "-qp_i", "21", "-qp_p", "21", "-qp_b", "23",
                                 "-pix_fmt", "nv12"]}
        gpu = next((e for e in gpu_args if s.video_encoder != "cpu" and encoder_works(e)), None)
        if gpu:
            args += ["-c:v", gpu, *gpu_args[gpu]]
        else:
            args += ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p"]
        args += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    elif codec == "vp9":
        args += ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-pix_fmt", "yuv420p",
                 "-c:a", "libopus", "-b:a", "128k"]
    filters = []
    w, h = s.video_target_size(info.width, info.height)
    if (w, h) != (info.width, info.height):
        filters.append(f"scale={w}:{h}:flags=lanczos" if (w, h) != (info.width - info.width % 2,
                                                                  info.height - info.height % 2)
                       else "scale=trunc(iw/2)*2:trunc(ih/2)*2")
    if s.video_max_fps and info.fps > s.video_max_fps + 0.5:
        filters.append(f"fps={s.video_max_fps}")
    if filters:
        args += ["-vf", ",".join(filters)]
    return [*args, str(dst)]


def convert_video(src: Path, info: VideoInfo, target: str, s: Settings,
                  on_progress: Callable[[float], None] | None = None,
                  cancel: threading.Event | None = None) -> Path:
    from .video import verify_video

    out = temp_name(src.parent, VIDEO_TARGETS[target][1])
    try:
        steps.report("Encoding " + re.sub(r",[^)]*\)", ")", VIDEO_TARGETS[target][0]))     # "MP4 (H.264)"
        tools.run_ffmpeg_progress(video_args(src, out, info, target, s), info.duration, on_progress, cancel)
        steps.report("Checking the result")
        verify_video(info, out, VIDEO_TARGETS[target][2], dst_is_mp4=out.suffix == ".mp4",
                     expected_size=s.video_target_size(info.width, info.height)
                     if VIDEO_TARGETS[target][2] in ("av1", "hevc") or s.video_max_height else None)
        return out
    except BaseException:
        remove_quietly(out)
        raise


def video_to_animation(src: Path, info: VideoInfo, target: str, opts: ConvertOptions, s: Settings,
                       on_progress: Callable[[float], None] | None = None,
                       cancel: threading.Event | None = None) -> Path:
    """A video as a GIF (its own 256-colour palette, made for this video) or an animated WebP. Unless chosen
    otherwise: at most 480 pixels on the short side and 15 (GIF) or 24 (WebP) frames a second."""
    import dataclasses

    from PIL import Image

    sized = dataclasses.replace(s, video_max_height=opts.video_height or ANIMATION_HEIGHT)
    w, h = sized.video_target_size(info.width, info.height)
    fps = min(opts.video_fps or ANIMATION_FPS[target], info.fps or 30)
    out = temp_name(src.parent, VIDEO_TARGETS[target][1])
    ffmpeg = tools.require("ffmpeg")
    base = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
            "-i", str(src), "-an", "-sn", "-map_metadata", "-1"]
    scale = f"fps={fps:g},scale={w}:{h}:flags=lanczos"
    turn = {"h": ",hflip", "v": ",vflip"}.get(opts.flip, "") + \
        {90: ",transpose=clock", 180: ",hflip,vflip", 270: ",transpose=cclock"}.get(opts.turn, "")
    scale += turn
    if opts.turn in (90, 270):
        w, h = h, w
    if target == "gif":
        args = base + ["-filter_complex", f"[0:{info.main_video_index}]{scale},split[a][b];"
                       "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:"
                       "diff_mode=rectangle[out]", "-map", "[out]", "-loop", "0", str(out)]
    else:
        args = base + ["-map", f"0:{info.main_video_index}", "-vf", scale, "-c:v", "libwebp_anim", "-lossless", "0",
                       "-quality", "80", "-compression_level", "4", "-loop", "0", str(out)]
    try:
        steps.report(f"Making the {'GIF' if target == 'gif' else 'animated WebP'}")
        tools.run_ffmpeg_progress(args, info.duration, on_progress, cancel)
        steps.report("Checking the result")
        try:
            with Image.open(out) as im:
                frames, size = getattr(im, "n_frames", 1), im.size
                im.seek(frames - 1)
                im.load()
        except Exception as e:  # noqa: BLE001
            raise VerificationFailed(f"The result can't be opened ({e})") from e
        if size != (w, h):
            raise VerificationFailed(f"The result is {size[0]}×{size[1]}, expected {w}×{h}")
        if info.duration * fps >= 2 and frames < 2:
            raise VerificationFailed("The result has only one frame")
        return out
    except BaseException:
        remove_quietly(out)
        raise


def animation_to_video(src: Path, info, opts: ConvertOptions, s: Settings,
                       on_progress: Callable[[float], None] | None = None,
                       cancel: threading.Event | None = None) -> Path:
    """An animated GIF, WebP or PNG as an MP4 (H.264, plays everywhere), each frame shown as long as in the
    animation. See-through parts become white (MP4 has no transparency)."""
    from PIL import Image

    frames, durations, _loop = _load_frames(src)
    if len(frames) < 2:
        raise RuntimeError("Only animations can become a video")
    w, h = frames[0].size
    if opts.max_side and max(w, h) > opts.max_side:
        scale = opts.max_side / max(w, h)
        w, h = max(2, round(w * scale)), max(2, round(h * scale))
    w, h = w - w % 2, h - h % 2
    flat = []
    for f in frames:
        bg = Image.new("RGBA", f.size, (255, 255, 255, 255))
        bg.alpha_composite(f)
        img = bg.convert("RGB")
        flat.append(img.resize((w, h), Image.LANCZOS) if img.size != (w, h) else img)
    apng = temp_name(src.parent, ".png", "-anim")
    out = temp_name(src.parent, ".mp4")
    try:
        steps.report("Making the video")
        flat[0].save(apng, "PNG", save_all=True, append_images=flat[1:], duration=durations, loop=0,
                     compress_level=1)
        ffmpeg = tools.require("ffmpeg")
        total = sum(durations) / 1000
        tools.run_ffmpeg_progress([ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1",
                                   "-nostats", "-i", str(apng), "-fps_mode", "vfr", "-c:v", "libx264", "-crf", "20",
                                   "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
                                  total, on_progress, cancel)
        steps.report("Checking the result")
        got = probe_video(out)
        if got is None:
            raise VerificationFailed("The video can't be read")
        if (got.width, got.height) != (w, h):
            raise VerificationFailed(f"The video is {got.width}×{got.height}, expected {w}×{h}")
        if abs(got.duration - total) > max(0.5, total * 0.1):
            raise VerificationFailed(f"The video lasts {got.duration:.1f} s instead of {total:.1f} s")
        return out
    except BaseException:
        remove_quietly(out)
        raise
    finally:
        remove_quietly(apng)


def extract_audio(src: Path, target: str, opts: ConvertOptions, s: Settings,
                  on_progress: Callable[[float], None] | None = None,
                  cancel: threading.Event | None = None) -> tuple[Path, str]:
    """A video's sound as an audio file: (temp file, what was done). Copied untouched when it's already in
    the chosen format and nothing about it is to change; otherwise encoded like Convert's audio."""
    from .probe import probe_audio

    info = probe_audio(src, allow_video=True)
    if info is None:
        raise ValueError("This video has no sound")
    fmt = target.split("_", 1)[1]
    changes = (opts.audio_kbps and fmt not in LOSSLESS_AUDIO) or opts.channels or opts.sample_rate
    if not changes and info.codec in _COPYABLE.get(fmt, ()):
        out = temp_name(src.parent, VIDEO_TARGETS[target][1])
        ffmpeg = tools.require("ffmpeg")
        try:
            steps.report("Copying the sound out")
            tools.run_ffmpeg_progress([ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-progress",
                                       "pipe:1", "-nostats", "-i", str(src), "-map", f"0:{info.audio_index}",
                                       "-map_metadata", "0", "-vn", "-sn", "-dn", "-c:a", "copy",
                                       *(["-movflags", "+faststart"] if fmt == "aac" else []), str(out)],
                                      info.duration, on_progress, cancel)
            steps.report("Checking the result")
            got = probe_audio(out)
            if got is None or got.codec != info.codec:
                raise VerificationFailed("The sound couldn't be read back")
            if info.duration > 0 and abs(got.duration - info.duration) > max(0.5, info.duration * 0.01):
                raise VerificationFailed(f"The sound lasts {got.duration:.1f} s instead of {info.duration:.1f} s")
            return out, "sound copied as it was (no quality lost)"
        except BaseException:
            remove_quietly(out)
            raise
    return convert_audio(src, info, fmt, s, on_progress, cancel, opts), ""


def convert_pdf(src: Path, target: str, opts: ConvertOptions,
                on_progress: Callable[[float], None] | None = None,
                cancel: threading.Event | None = None) -> Path:
    """A PDF's pages as images or single-page PDFs (a temp folder), or a copy with its pages turned or its
    password added or removed (a temp file). Every result is checked."""
    import shutil

    from . import pdftools

    def step(done: int, total: int) -> None:
        if on_progress and total:
            on_progress(done / total)

    ext = PDF_TARGETS[target][1]
    out = temp_name(src.parent, ext, "-pages" if not ext else "")
    try:
        if target in ("png", "jpeg"):
            steps.report("Drawing the pages")
            pdftools.render_pages(src, out, target, opts.pdf_dpi, opts.pdf_password, step, cancel)
        elif target == "split":
            steps.report("Splitting the pages")
            pdftools.split(src, out, opts.pdf_password)
        elif target.startswith("rotate"):
            steps.report("Turning the pages")
            pdftools.rotate(src, out, int(target[6:]), opts.pdf_password)
        elif target == "protect":
            steps.report("Adding the password")
            pdftools.protect(src, out, opts.pdf_password)
        else:
            steps.report("Removing the password")
            pdftools.unlock(src, out, opts.pdf_password)
        return out
    except BaseException:
        if out.is_dir():
            shutil.rmtree(out, ignore_errors=True)
        else:
            remove_quietly(out)
        raise


def convert_archive(src: Path, target: str, on_progress: Callable[[float], None] | None = None,
                    cancel: threading.Event | None = None) -> Path:
    from . import archives

    out = temp_name(src.parent, ARCHIVE_TARGETS[target][1])
    try:
        steps.report("Repacking the archive")
        archives.repack(src, out, target, on_progress, cancel)
        return out
    except BaseException:
        remove_quietly(out)
        raise


def convert_font(src: Path, target: str, opts: ConvertOptions) -> Path:
    from . import fonts

    out = temp_name(src.parent, fonts.target_ext(src, target))
    try:
        steps.report("Converting the font")
        fonts.convert(src, out, target, opts.font_latin)
        return out
    except BaseException:
        remove_quietly(out)
        raise


def comic_pages(src: Path, folder: Path) -> list[Path]:
    """A comic book's pictures, in reading order (natural: 2 before 10), written to ``folder`` under plain
    names (the archive's own names are never used as paths)."""
    import re as _re
    import tarfile
    import zipfile

    from PIL import Image

    def natural(name: str):
        return [int(x) if x.isdigit() else x.lower() for x in _re.split(r"(\d+)", name)]

    pictures = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".jxl")
    items: list[tuple[str, bytes]] = []
    ext = src.suffix.lower()
    if ext == ".cbz":
        with zipfile.ZipFile(src) as z:
            items = [(i.filename, z.read(i)) for i in z.infolist()
                     if not i.is_dir() and i.filename.lower().endswith(pictures) and "__macosx" not in i.filename.lower()]
    elif ext == ".cbt":
        with tarfile.open(src) as t:
            for m in t.getmembers():
                f = t.extractfile(m) if m.isfile() and m.name.lower().endswith(pictures) else None
                if f is not None:
                    items.append((m.name, f.read()))
    else:
        import py7zr

        with py7zr.SevenZipFile(src) as z:
            names = [n for n in z.getnames() if n.lower().endswith(pictures)]
            items = [(n, b.read()) for n, b in (z.read(names) or {}).items()]
    if not items:
        raise ValueError("This comic book has no pictures")
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i, (name, data) in enumerate(sorted(items, key=lambda kv: natural(kv[0])), 1):
        p = folder / f"{i:05d}{Path(name).suffix.lower()}"
        p.write_bytes(data)
        if p.suffix not in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"):
            with Image.open(p) as im:                          # (formats a PDF can't hold as they are)
                png = p.with_suffix(".png")
                im.convert("RGBA").save(png)
            p.unlink()
            p = png
        out.append(p)
    return out


def convert_comic(src: Path, opts: ConvertOptions, on_progress: Callable[[float], None] | None = None,
                  cancel: threading.Event | None = None) -> Path:
    """A comic book as one PDF, a page per picture (JPEGs as they are)."""
    import shutil
    import tempfile

    from . import pdftools

    work = Path(tempfile.mkdtemp(prefix="compressor-comic-"))
    out = temp_name(src.parent, ".pdf")
    try:
        steps.report("Reading the pages")
        pages = comic_pages(src, work)
        steps.report("Making the PDF")
        pdftools.combine(pages, out, opts.pdf_page, (lambda d, t: on_progress(d / t)) if on_progress else None,
                         cancel)
        return out
    except BaseException:
        remove_quietly(out)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ================================================================ documents

def needs_pandoc(src: Path, target: str) -> bool:
    """Pandoc does this conversion (or its first step: Markdown → Word → PDF)."""
    from .office import OFFICE_EXTS

    return target != "pdf" or src.suffix.lower() not in OFFICE_EXTS


def convert_document(src: Path, target: str) -> Path:
    """A document to another format: with Pandoc, or to PDF with Microsoft Office or LibreOffice (office.py);
    a Markdown or web page goes through Word's format first. Pandoc runs with ``--sandbox``: it reads only this
    file (never the network, never other files); pictures inside come along where the format can hold them."""
    if target == "pdf":
        from . import office

        out = temp_name(src.parent, ".pdf")
        middle = None
        try:
            source = src
            if src.suffix.lower() not in office.OFFICE_EXTS:
                middle = convert_document(src, "docx")
                source = middle
            steps.report(f"Making the PDF with {office.converter_for(source.suffix)}")
            office.to_pdf(source, out)
            return out
        except BaseException:
            remove_quietly(out)
            raise
        finally:
            remove_quietly(middle)
    label, ext, writer = DOCUMENT_TARGETS[target]
    pandoc = tools.find_tool("pandoc")
    if pandoc is None:
        raise tools.ToolMissing("pandoc")
    reader = PANDOC_INPUTS[src.suffix.lower()]
    out = temp_name(src.parent, ext)
    args = [pandoc, src.name, "-f", reader, "-t", writer, "-o", out.name, "--sandbox"]
    if writer in ("html5", "rtf", "plain", "gfm"):
        args.append("--standalone")
    if writer in ("html5", "epub3"):
        args += ["-M", f"pagetitle={src.stem}"]          # (a page title; the document's own title stays)
    try:
        steps.report("Converting to " + label.split(" (")[0])
        try:
            tools.run(args, cwd=src.parent, timeout=1800)
        except tools.ToolError as e:
            if "Could not find data file" in str(e):     # (distribution builds keep these outside the program)
                raise RuntimeError("This Pandoc can't convert without reaching outside the document. Pandoc's "
                                   "own builds (pandoc.org) can.") from e
            raise
        steps.report("Checking the result")
        if not out.exists() or out.stat().st_size == 0:
            raise VerificationFailed("Pandoc made no document")
        if ext in (".docx", ".odt", ".epub"):
            import zipfile

            try:
                with zipfile.ZipFile(out) as z:
                    bad = z.testzip()
            except zipfile.BadZipFile as e:
                raise VerificationFailed("The result can't be opened") from e
            if bad is not None:
                raise VerificationFailed(f"The result is damaged ({bad})")
        return out
    except BaseException:
        remove_quietly(out)
        raise


# ================================================================ audio

def convert_audio(src: Path, info, target: str, s: Settings,
                  on_progress: Callable[[float], None] | None = None,
                  cancel: threading.Event | None = None, opts: ConvertOptions | None = None) -> Path:
    """Audio to MP3/AAC/Opus/FLAC (high quality, channels unchanged unless ``opts`` say otherwise) or WAV."""
    import dataclasses

    from . import audio

    o = opts or ConvertOptions()
    channels = o.channels if o.channels and o.channels != info.channels else 0
    rate = o.sample_rate if o.sample_rate and o.sample_rate != info.sample_rate and target != "opus" else 0
    plain = dataclasses.replace(s, audio_quality="high", audio_mono=False, audio_cd_quality=False)
    if target != "wav":
        return audio.convert_audio(src, info, Plan(True, "", AUDIO_TARGETS[target][1], route=f"audio_{target}"),
                                   plain, on_progress, cancel, kbps=o.audio_kbps, rate=rate, channels=channels)
    ffmpeg = tools.require("ffmpeg")
    codec = "pcm_s24le" if info.bits > 16 else "pcm_s16le"
    out = temp_name(src.parent, ".wav")
    try:
        change = (["-ac", str(channels)] if channels else []) + (["-ar", str(rate)] if rate else [])
        tools.run_ffmpeg_progress([ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1",
                                   "-nostats", "-i", str(src), "-map", f"0:{info.audio_index}", "-map_metadata", "0",
                                   *change, "-c:a", codec, str(out)], info.duration, on_progress, cancel)
        exact = audio.is_lossless(info.codec) and info.bits <= 24 and not audio.flac_problem(info, plain) \
            and not change
        audio.verify_audio(src, info, out, "wav", False, exact, channels=channels)
        return out
    except BaseException:
        remove_quietly(out)
        raise


__all__ = ["IMAGE_TARGETS", "VIDEO_TARGETS", "AUDIO_TARGETS", "convert_audio", "ConvertOptions", "convert_image",
           "convert_video", "describe", "probe_video"]
