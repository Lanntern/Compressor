"""File-type classification and image header inspection."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path

JPEG = "jpeg"
PNG = "png"
GIF = "gif"
WEBP = "webp"
TIFF = "tiff"
BMP = "bmp"
HEIC = "heic"
AVIF = "avif"
VIDEO = "video"
AUDIO = "audio"
DOCUMENT = "document"     # Office documents and PDFs (pictures inside shrunk; see documents.py)
JXL = "jxl"               # JPEG XL: only listed where it's an input (Convert); elsewhere it's already efficient
# Only inputs for Convert (nothing to compress in them, or no way to write them back):
RAW = "raw"               # camera RAW photos (decoded with LibRaw)
PSD = "psd"               # Photoshop (its flattened picture)
SVG = "svg"               # vector drawings (drawn at the size asked for)
ICO = "ico"               # Windows icons (the largest picture in it)
DOC = "doc"               # documents Pandoc reads: Markdown, HTML, EPUB, RTF, LaTeX...
PDF = "pdf"               # PDFs, on Convert (pages as images, split, turned, passwords); elsewhere a DOCUMENT
FONT = "font"             # TrueType, OpenType, WOFF, WOFF2 (fonts.py)
PICTURE = "picture"       # other pictures Pillow reads: TGA, DDS, QOI, cursors, PCX, PNM
COMIC = "comic"           # comic books: pictures in a .cbz (ZIP), .cb7 (7-Zip) or .cbt (TAR)
ARCHIVE = "archive"       # ZIP, 7-Zip, TAR: repacked only when asked (archives.py)
PICTURE_EXTS = (".tga", ".dds", ".qoi", ".cur", ".pcx", ".ppm", ".pgm", ".pbm", ".pnm")
COMIC_EXTS = (".cbz", ".cb7", ".cbt")

IMAGE_KINDS = (JPEG, PNG, GIF, WEBP, TIFF, BMP, HEIC, AVIF)
# Sources whose pixels are exact (lossless): can be converted losslessly.
LOSSLESS_SOURCE_KINDS = (PNG, GIF, TIFF, BMP)
# Already lossy-compressed photo formats.
LOSSY_PHOTO_KINDS = (HEIC, AVIF)

_EXT_KIND = {
    ".jpg": JPEG, ".jpeg": JPEG, ".jpe": JPEG, ".jfif": JPEG,
    ".png": PNG, ".apng": PNG,
    ".gif": GIF,
    ".webp": WEBP,
    ".tif": TIFF, ".tiff": TIFF,
    ".bmp": BMP, ".dib": BMP,
    ".heic": HEIC, ".heif": HEIC, ".hif": HEIC,
    ".avif": AVIF,
}
VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".3gp",
    ".mkv", ".webm",
    ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".vob",
}
AUDIO_EXTS = {
    ".mp3", ".wav", ".wave", ".flac", ".aif", ".aiff", ".aifc", ".m4a", ".aac", ".ogg", ".oga", ".opus",
    ".wma", ".ape", ".wv", ".mka", ".caf", ".w64",
}
DOCUMENT_EXTS = {".docx", ".docm", ".dotx", ".pptx", ".pptm", ".potx", ".ppsx", ".xlsx", ".xlsm", ".xltx",
                 ".odt", ".ods", ".odp", ".odg", ".pdf"}
RAW_EXTS = {".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf", ".rw2", ".raf", ".pef",
            ".srw", ".x3f", ".3fr", ".iiq", ".erf", ".kdc", ".dcr", ".mrw", ".mos", ".rwl", ".raw"}
# What Pandoc reads, by extension (its reader's name). .docx and .odt are also DOCUMENT (Compress shrinks them).
PANDOC_INPUTS = {".md": "markdown", ".markdown": "markdown", ".html": "html", ".htm": "html", ".rst": "rst",
                 ".tex": "latex", ".org": "org", ".rtf": "rtf", ".epub": "epub", ".docx": "docx", ".odt": "odt",
                 ".ipynb": "ipynb", ".textile": "textile", ".fb2": "fb2", ".opml": "opml"}
# Older Office formats: only an office app reads them (to PDF, see office.py).
OLD_OFFICE_EXTS = (".doc", ".xls", ".xlsb", ".ppt", ".pps", ".wpd")
CONVERT_INPUTS = {**dict.fromkeys(RAW_EXTS, RAW), ".psd": PSD, ".svg": SVG, ".ico": ICO, ".jxl": JXL,
                  **{e: DOC for e in PANDOC_INPUTS if e not in DOCUMENT_EXTS}, **dict.fromkeys(OLD_OFFICE_EXTS, DOC),
                  **dict.fromkeys((".ttf", ".otf", ".woff", ".woff2"), FONT),
                  **dict.fromkeys(PICTURE_EXTS, PICTURE), **dict.fromkeys(COMIC_EXTS, COMIC)}
CONVERT_ONLY_KINDS = (RAW, PSD, SVG, ICO, DOC, PDF, FONT, PICTURE, COMIC, ARCHIVE)
KIND_LABEL = {JPEG: "JPEG", PNG: "PNG", GIF: "GIF", WEBP: "WebP", TIFF: "TIFF", BMP: "BMP",
              HEIC: "HEIC", AVIF: "AVIF", VIDEO: "Video", AUDIO: "Audio", DOCUMENT: "Document", JXL: "JPEG XL",
              RAW: "RAW", PSD: "Photoshop", SVG: "SVG", ICO: "Icon", DOC: "Document", PDF: "PDF", FONT: "Font",
              PICTURE: "Picture", COMIC: "Comic book", ARCHIVE: "Archive"}
# Extension used when an image keeps its format.
KEEP_EXT = {JPEG: ".jpg", PNG: ".png", GIF: ".gif", WEBP: ".webp", TIFF: ".tif", BMP: ".png",
            HEIC: ".heic", AVIF: ".avif", JXL: ".jxl"}

_MP4_SOURCES = {".mp4", ".m4v", ".mov", ".3gp"}

# Prefix for every temporary file Wick creates next to the originals.
TEMP_PREFIX = ".cmp-"


def classify(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return VIDEO
    if ext in AUDIO_EXTS:
        return AUDIO
    if ext in DOCUMENT_EXTS:
        return DOCUMENT
    return _EXT_KIND.get(ext)


def type_label(path: Path, kind: str) -> str:
    """What a person calls the file's type: JPEG, PNG… for images; the extension (MP4, MOV, MP3, PDF) otherwise."""
    if kind in IMAGE_KINDS or kind == JXL:
        return KIND_LABEL.get(kind, kind)
    return path.suffix.lstrip(".").upper() or KIND_LABEL.get(kind, kind)


def video_output_ext(src_ext: str, codec: str) -> str:
    src_ext = src_ext.lower()
    if src_ext in _MP4_SOURCES:
        return ".mp4"
    if src_ext == ".webm" and codec == "av1":
        return ".webm"
    return ".mkv"


def human(n: float) -> str:
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{sign}{n:.0f} {unit}" if unit == "B" else f"{sign}{n:.1f} {unit}"
        n /= 1024
    return f"{n}"


@dataclass(frozen=True)
class PngInfo:
    width: int
    height: int
    bit_depth: int
    color_type: int
    animated: bool


def read_png_info(path: Path) -> PngInfo | None:
    """Parse IHDR and look for an acTL chunk (APNG) before the first IDAT."""
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            width = height = depth = ctype = None
            animated = False
            for _ in range(64):  # acTL must appear before IDAT; don't read far
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                length, ctype_bytes = struct.unpack(">I4s", hdr)
                if ctype_bytes == b"IHDR":
                    data = f.read(length)
                    width, height, depth, ctype = struct.unpack(">IIBB", data[:10])
                    f.seek(4, 1)
                    continue
                if ctype_bytes == b"acTL":
                    animated = True
                    break
                if ctype_bytes == b"IDAT":
                    break
                f.seek(length + 4, 1)
            if width is None or height is None or depth is None or ctype is None:
                return None
            return PngInfo(width, height, depth, ctype, animated)
    except OSError:
        return None


def webp_is_lossless(path: Path) -> bool | None:
    """True for VP8L (lossless) WebP, False for VP8 (lossy), None if unknown/animated."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return None
    if head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return None
    first = head[12:16]
    if first == b"VP8L":
        return True
    if first == b"VP8 ":
        return False
    if first == b"VP8X":
        # Look for the image chunk after VP8X (and optional ICCP etc.).
        try:
            with open(path, "rb") as f:
                f.seek(12)
                for _ in range(16):
                    hdr = f.read(8)
                    if len(hdr) < 8:
                        return None
                    tag, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
                    if tag == b"VP8L":
                        return True
                    if tag == b"VP8 ":
                        return False
                    if tag == b"ANIM":
                        return None
                    f.seek(size + (size & 1), 1)
        except OSError:
            return None
    return None


@dataclass
class ImageInfo:
    width: int
    height: int
    frames: int = 1
    bit_depth: int = 8
    pages: int = 1                 # TIFF pages
    lossless_source: bool = True   # pixels are exact (PNG/GIF/BMP/TIFF/lossless WebP)
    mode: str = ""
    motion: bool = False           # Motion Photo: a short video is stored inside the image file

    @property
    def animated(self) -> bool:
        return self.frames > 1


def read_raw_info(path: Path) -> ImageInfo | None:
    """A camera RAW photo's size as it will look (turned the way the camera was held)."""
    try:
        import rawpy

        with rawpy.imread(str(path)) as raw:
            sz = raw.sizes
            w, h = sz.width, sz.height
            if sz.flip in (5, 6):                   # (LibRaw: 5 = 90° left, 6 = 90° right)
                w, h = h, w
            return ImageInfo(w, h, 1, 16, 1, True, "RGB")
    except Exception:  # noqa: BLE001 - not a RAW LibRaw knows, or damaged
        return None


def svg_size(path: Path) -> tuple[int, int] | None:
    """An SVG's own size in pixels (from width/height, else its viewBox)."""
    try:
        from PySide6.QtSvg import QSvgRenderer

        r = QSvgRenderer(str(path))
        if not r.isValid():
            return None
        size = r.defaultSize()
        if size.width() <= 0 or size.height() <= 0:
            box = r.viewBoxF()
            return (max(1, round(box.width())), max(1, round(box.height()))) if box.width() > 0 else None
        return size.width(), size.height()
    except Exception:  # noqa: BLE001
        return None


_heif_registered = False


def register_heif() -> bool:
    global _heif_registered
    if not _heif_registered:
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
            _heif_registered = True
        except Exception:  # noqa: BLE001
            return False
    return True


_MOTION_HEAD = (b"MotionPhoto", b"MicroVideo")   # Google/Samsung XMP (GCamera:MotionPhoto, Container items)
_MOTION_TAIL = (b"MotionPhoto_Data", b"SEFT")    # Samsung trailer


def has_embedded_video(path: Path) -> bool:
    """Motion Photos (Google, Samsung...) append a short MP4 to the JPEG/HEIC and point to it from XMP.
    Reads only the first 256 KB and the last 64 KB."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(256 * 1024)
            if any(m in head for m in _MOTION_HEAD):
                return True
            f.seek(max(0, size - 64 * 1024))
            tail = f.read()
        return any(m in tail for m in _MOTION_TAIL)
    except OSError:
        return False


def read_image_info(path: Path, kind: str) -> ImageInfo | None:
    """Dimensions, frame count, bit depth. Reads headers only (GIF: scans frames)."""
    if kind == RAW:
        return read_raw_info(path)
    if kind == SVG:
        size = svg_size(path)
        return None if size is None else ImageInfo(size[0], size[1], 1, 8, 1, True, "RGBA")
    if kind == JXL:
        j = read_jxl_info(path)
        return None if j is None else ImageInfo(j.width, j.height, 2 if j.animated else 1, j.bit_depth, 1, False)
    if kind == PNG:
        p = read_png_info(path)
        if p is None:
            return None
        frames = 1
        if p.animated:
            frames = _pil_frames(path) or 2
        return ImageInfo(p.width, p.height, frames, p.bit_depth, 1, True)
    if kind == HEIC:
        register_heif()
    try:
        from PIL import Image

        with Image.open(path) as im:
            frames = getattr(im, "n_frames", 1) or 1
            info = ImageInfo(im.width, im.height, frames, 8, 1, kind in LOSSLESS_SOURCE_KINDS, im.mode)
            if kind == TIFF:
                info.pages, info.frames = frames, 1
                bits = im.tag_v2.get(258) if hasattr(im, "tag_v2") else None
                if bits:
                    info.bit_depth = max(bits) if isinstance(bits, tuple) else int(bits)
                if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
                    info.bit_depth = max(info.bit_depth, 16)
            elif kind == WEBP:
                ll = webp_is_lossless(path)
                info.lossless_source = bool(ll)
            elif kind in (JPEG, HEIC, AVIF):
                info.lossless_source = False
            elif kind == PSD:
                info.frames = 1                    # (its other "frames" are the layers: the picture is the first)
            elif kind == ICO:
                info.frames = 1
            if kind in (JPEG, HEIC):
                info.motion = has_embedded_video(path)
            return info
    except Exception:  # noqa: BLE001 - unreadable/corrupt
        return None


def _pil_frames(path: Path) -> int | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return getattr(im, "n_frames", 1)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- JPEG XL headers

JXL_SIGNATURE = b"\x00\x00\x00\x0cJXL \r\n\x87\n"     # the container's first box


@dataclass
class JxlInfo:
    width: int                 # as displayed (orientation applied, like djxl decodes it)
    height: int
    animated: bool = False
    bit_depth: int = 8
    boxes: tuple[str, ...] = ()   # container box types, e.g. "jbrd" (the exact JPEG can be rebuilt), "Exif"

    @property
    def reconstructs_jpeg(self) -> bool:
        return "jbrd" in self.boxes


class _Bits:
    """JPEG XL's bit reader: least significant bit first."""

    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def u(self, n: int) -> int:
        v = 0
        for i in range(n):
            byte = self.data[self.pos >> 3]
            v |= ((byte >> (self.pos & 7)) & 1) << i
            self.pos += 1
        return v

    def u32(self, *dists: tuple[int, int]) -> int:
        """U32(): a 2-bit selector, then (bits, offset) for the chosen distribution (bits 0: a fixed value)."""
        bits, offset = dists[self.u(2)]
        return offset + (self.u(bits) if bits else 0)


_RATIOS = {1: (1, 1), 2: (12, 10), 3: (4, 3), 4: (3, 2), 5: (16, 9), 6: (5, 4), 7: (2, 1)}


def _jxl_size(b: _Bits, preview: bool = False) -> tuple[int, int]:
    if preview:
        div8 = b.u(1)
        dist = ((0, 16), (0, 32), (5, 1), (9, 33)) if div8 else ((6, 1), (8, 65), (10, 321), (12, 1345))
        h = b.u32(*dist) * (8 if div8 else 1)
        ratio = b.u(3)
        w = b.u32(*dist) * (8 if div8 else 1) if ratio == 0 else 0
    else:
        small = b.u(1)
        dist = ((9, 1), (13, 1), (18, 1), (30, 1))
        h = (b.u(5) + 1) * 8 if small else b.u32(*dist)
        ratio = b.u(3)
        w = ((b.u(5) + 1) * 8 if small else b.u32(*dist)) if ratio == 0 else 0
    if ratio:
        num, den = _RATIOS[ratio]
        w = h * num // den
    return w, h


def _jxl_codestream_info(cs: bytes) -> tuple[int, int, bool, int]:
    """(width, height, animated, bit depth) from the start of a codestream (SizeHeader, ImageMetadata)."""
    if cs[:2] != b"\xff\x0a":
        raise ValueError("not a JPEG XL codestream")
    b = _Bits(cs[2:])
    w, h = _jxl_size(b)
    orientation, animated, depth = 1, False, 8
    if not b.u(1):                                   # all_default
        if b.u(1):                                   # extra_fields
            orientation = 1 + b.u(3)
            if b.u(1):                               # have_intrinsic_size
                _jxl_size(b)
            if b.u(1):                               # have_preview
                _jxl_size(b, preview=True)
            animated = bool(b.u(1))
        if not animated and not b.u(1):              # bit depth (integer samples)
            depth = b.u32((0, 8), (0, 10), (0, 12), (6, 1))
    if orientation > 4:                              # transposed: displayed the other way round
        w, h = h, w
    return w, h, animated, depth


def read_jxl_info(path: Path) -> JxlInfo | None:
    """Size, animation and container boxes of a JPEG XL file, from its headers (nothing is decoded)."""
    import struct

    try:
        with open(path, "rb") as f:
            head = f.read(16)
            if head[:2] == b"\xff\x0a":
                f.seek(0)
                w, h, anim, depth = _jxl_codestream_info(f.read(512))
                return JxlInfo(w, h, anim, depth)
            if head[:12] != JXL_SIGNATURE:
                return None
            pos, boxes, cs = 12, [], b""
            end = os.fstat(f.fileno()).st_size
            while pos + 8 <= end and len(boxes) < 10_000:
                f.seek(pos)
                hdr = f.read(16)
                size, btype = struct.unpack(">I", hdr[:4])[0], hdr[4:8]
                header = 8
                if size == 1:
                    size, header = struct.unpack(">Q", hdr[8:16])[0], 16
                elif size == 0:
                    size = end - pos
                if size < header:
                    break
                name = btype.decode("latin-1")
                boxes.append(name)
                if not cs and name in ("jxlc", "jxlp"):
                    f.seek(pos + header + (4 if name == "jxlp" else 0))   # (jxlp: a 4-byte part index first)
                    cs = f.read(512)
                pos += size
            if not cs:
                return None
            w, h, anim, depth = _jxl_codestream_info(cs)
            return JxlInfo(w, h, anim, depth, tuple(boxes))
    except (OSError, ValueError, IndexError, KeyError):
        return None


# ---------------------------------------------------------------- what a file really is

# ISO media brands (bytes 8-12 of an "ftyp" box) -> (kind, extension)
_BRANDS = {
    b"heic": (HEIC, ".heic"), b"heix": (HEIC, ".heic"), b"hevc": (HEIC, ".heic"), b"heim": (HEIC, ".heic"),
    b"heis": (HEIC, ".heic"), b"mif1": (HEIC, ".heic"), b"msf1": (HEIC, ".heic"),
    b"avif": (AVIF, ".avif"), b"avis": (AVIF, ".avif"),
    b"qt  ": (VIDEO, ".mov"), b"M4V ": (VIDEO, ".m4v"), b"M4VH": (VIDEO, ".m4v"),
    b"M4A ": (AUDIO, ".m4a"), b"M4B ": (AUDIO, ".m4a"), b"M4P ": (AUDIO, ".m4a"),
    b"3gp4": (VIDEO, ".3gp"), b"3gp5": (VIDEO, ".3gp"), b"3gp6": (VIDEO, ".3gp"), b"3g2a": (VIDEO, ".3gp"),
}
# Extensions that are the same thing (not worth renaming one to another).
SAME_TYPE = [{".jpg", ".jpeg", ".jpe", ".jfif"}, {".tif", ".tiff"}, {".heic", ".heif", ".hif"},
             {".mp4", ".m4v", ".mov", ".3gp"}, {".ogg", ".oga", ".opus"}, {".png", ".apng"},
             {".mkv", ".webm", ".mka"}, {".aif", ".aiff", ".aifc"}, {".wav", ".wave"}, {".bmp", ".dib"},
             {".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".vob"}]


def sniff(path: Path, head: bytes | None = None) -> tuple[str, str] | None:
    """(kind, usual extension) from the file's first bytes, whatever it's called; None if not recognized."""
    if head is None:
        try:
            with open(path, "rb") as f:
                head = f.read(64)
        except OSError:
            return None
    h = head
    if h[:3] == b"\xff\xd8\xff":
        return JPEG, ".jpg"
    if h[:8] == b"\x89PNG\r\n\x1a\n":
        return PNG, ".png"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return GIF, ".gif"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return WEBP, ".webp"
    if h[:4] == b"RIFF" and h[8:12] == b"WAVE":
        return AUDIO, ".wav"
    if h[:4] == b"RIFF" and h[8:12] == b"AVI ":
        return VIDEO, ".avi"
    if h[:4] in (b"II*\x00", b"MM\x00*"):
        return TIFF, ".tif"
    if h[:2] == b"BM" and len(h) >= 14 and int.from_bytes(h[2:6], "little") > 0:
        return BMP, ".bmp"
    if h[:2] == b"\xff\x0a" or h[:12] == JXL_SIGNATURE:
        return JXL, ".jxl"
    if h[4:8] == b"ftyp":
        brand = h[8:12]
        if brand in _BRANDS:
            return _BRANDS[brand]
        return VIDEO, ".mp4"                      # isom, mp41, mp42, avc1, dash…: an MP4 video
    if h[:4] == b"\x1a\x45\xdf\xa3":
        return VIDEO, (".webm" if b"webm" in h[:64] else ".mkv")
    if h[:4] == b"fLaC":
        return AUDIO, ".flac"
    if h[:4] == b"OggS":
        return AUDIO, (".opus" if b"OpusHead" in h else ".ogg")
    if h[:3] == b"ID3" or (len(h) > 1 and h[0] == 0xFF and h[1] & 0xE6 == 0xE2):
        return AUDIO, ".mp3"
    if h[:4] == b"%PDF":
        return DOCUMENT, ".pdf"
    if h[:4] == b"\x30\x26\xb2\x75":
        return VIDEO, ".wmv"
    return None


def wrong_extension(path: Path, head: bytes | None = None) -> str | None:
    """The extension this file should have, if its name says something else (None: it's right, or unknown)."""
    found = sniff(path, head)
    if found is None:
        return None
    ext = path.suffix.lower()
    real = found[1]
    if ext == real or any(ext in group and real in group for group in SAME_TYPE):
        return None
    if real == ".mp4" and ext in AUDIO_EXTS:         # (an .m4a/.aac that's plain MP4: still audio, fine)
        return None
    return real
