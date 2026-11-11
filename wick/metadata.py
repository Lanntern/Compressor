"""Reading, filtering and (losslessly) rewriting image metadata.

Policies:
* ``keep``      - everything is preserved.
* ``strip_gps`` - GPS location removed from EXIF and XMP; everything else kept.
* ``strip_private`` - GPS plus identifying details (camera/lens make, model and
                  serial numbers, owner, author, copyright, comments, XMP).
                  Dates and exposure settings are kept.
* ``strip_all_keep_date`` - everything except the date the photo was taken.
* ``strip_all`` - EXIF/XMP/IPTC/comments removed.
Orientation and the color profile are always kept: dropping them would show
the image rotated or with wrong colors.

JPEG, PNG and WebP are edited at the container level, so no pixels change.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

EXIF_HEADER = b"Exif\x00\x00"
XMP_JPEG_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
ICC_JPEG_HEADER = b"ICC_PROFILE\x00"
GPS_IFD = 0x8825
ORIENTATION = 0x0112


@dataclass
class Meta:
    exif: bytes | None = None   # raw TIFF structure, *without* the "Exif\0\0" header
    xmp: bytes | None = None
    icc: bytes | None = None

    def pil_kwargs(self) -> dict:
        """Keyword arguments for Pillow's ``save()``."""
        kw: dict = {}
        if self.exif:
            kw["exif"] = EXIF_HEADER + self.exif
        if self.icc:
            kw["icc_profile"] = self.icc
        if self.xmp:
            kw["xmp"] = self.xmp
        return kw


def _strip_header(exif: bytes | None) -> bytes | None:
    if not exif:
        return None
    return exif[6:] if exif.startswith(EXIF_HEADER) else exif


def read_meta(img) -> Meta:
    """Metadata from an open Pillow image."""
    info = img.info
    exif = info.get("exif")
    if isinstance(exif, str):
        exif = exif.encode("latin-1", "replace")
    xmp = info.get("xmp") or info.get("XML:com.adobe.xmp")
    if isinstance(xmp, str):
        xmp = xmp.encode("utf-8")
    return Meta(_strip_header(exif), xmp or None, info.get("icc_profile") or None)


EXIF_IFD = 0x8769
# Identifying tags (device, owner, author, free text). Technical and date tags stay.
PRIVATE_IFD0 = (0x010F, 0x0110, 0x0131, 0x013C, 0x013B, 0x8298, 0x010E,  # make, model, software, host, artist,
                0x9C9B, 0x9C9C, 0x9C9D, 0x9C9E, 0x9C9F)                    # copyright, description, XP* fields
PRIVATE_EXIF = (0x927C, 0x9286, 0xA420, 0xA430, 0xA431, 0xA432, 0xA433, 0xA434, 0xA435)  # maker note, comment,
#                                                    unique id, owner, body serial, lens info/make/model/serial
DATE_IFD0 = (0x0132,)                                 # DateTime
DATE_EXIF = (0x9003, 0x9004, 0x9010, 0x9011, 0x9012, 0x9290, 0x9291, 0x9292)  # date taken/digitized, offsets,
#                                                                                 sub-second times


def filter_exif(exif: bytes | None, policy: str) -> bytes | None:
    """Policies: keep | strip_gps | strip_private | strip_all_keep_date | strip_all.

    Orientation is always kept (the image would display rotated otherwise).
    """
    if not exif or policy == "keep":
        return exif
    from PIL import Image

    try:
        ex = Image.Exif()
        ex.load(EXIF_HEADER + exif)
    except Exception:  # noqa: BLE001 - corrupt EXIF: drop it rather than keep GPS
        return None
    if policy in ("strip_gps", "strip_private"):
        if GPS_IFD in ex:
            ex.get_ifd(GPS_IFD)  # make sure Pillow forgets the cached IFD too
            del ex[GPS_IFD]
        if policy == "strip_private":
            for tag in PRIVATE_IFD0:
                ex.pop(tag, None)
            if EXIF_IFD in ex:
                sub = ex.get_ifd(EXIF_IFD)
                for tag in PRIVATE_EXIF:
                    sub.pop(tag, None)
        return _strip_header(ex.tobytes())
    # strip_all / strip_all_keep_date: a fresh EXIF with orientation (and dates)
    new = Image.Exif()
    orientation = ex.get(ORIENTATION)
    if orientation and orientation != 1:
        new[ORIENTATION] = orientation
    if policy == "strip_all_keep_date":
        for tag in DATE_IFD0:
            if tag in ex:
                new[tag] = ex[tag]
        if EXIF_IFD in ex:
            sub = ex.get_ifd(EXIF_IFD)
            dates = {tag: sub[tag] for tag in DATE_EXIF if tag in sub}
            if dates:
                new.get_ifd(EXIF_IFD).update(dates)
    if not len(new) and not (policy == "strip_all_keep_date" and new.get_ifd(EXIF_IFD)):
        return None
    return _strip_header(new.tobytes())


_XMP_GPS_ATTR = re.compile(rb'\s(?:exif|exifEX):GPS\w+="[^"]*"')
_XMP_GPS_ELEM = re.compile(rb"<(exif|exifEX):(GPS\w+)\b[^>]*?(?:/>|>.*?</\1:\2>)", re.S)


def filter_xmp(xmp: bytes | None, policy: str) -> bytes | None:
    if not xmp or policy == "keep":
        return xmp
    if policy != "strip_gps":
        return None  # XMP carries author, keywords, editing history: private/all policies drop it
    xmp = _XMP_GPS_ATTR.sub(b"", xmp)
    return _XMP_GPS_ELEM.sub(b"", xmp)


def filter_meta(meta: Meta, policy: str) -> Meta:
    return Meta(filter_exif(meta.exif, policy), filter_xmp(meta.xmp, policy), meta.icc)


# ---------------------------------------------------------------- JPEG

def _jpeg_segments(data: bytes) -> tuple[list[tuple[int, bytes]], int]:
    """(marker, full segment bytes) up to SOS; returns (segments, offset of SOS)."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    i, segs = 2, []
    while i < len(data) - 3:
        if data[i] != 0xFF:
            raise ValueError("corrupt JPEG marker stream")
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xDA:  # SOS: entropy-coded data follows
            return segs, i
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        segs.append((marker, data[i:i + 2 + length]))
        i += 2 + length
    raise ValueError("JPEG without image data")


def jpeg_read_meta(data: bytes) -> Meta:
    segs, _ = _jpeg_segments(data)
    meta = Meta()
    icc_parts: list[tuple[int, bytes]] = []
    for marker, seg in segs:
        body = seg[4:]
        if marker == 0xE1 and body.startswith(EXIF_HEADER) and meta.exif is None:
            meta.exif = body[6:]
        elif marker == 0xE1 and body.startswith(XMP_JPEG_HEADER) and meta.xmp is None:
            meta.xmp = body[len(XMP_JPEG_HEADER):]
        elif marker == 0xE2 and body.startswith(ICC_JPEG_HEADER):
            icc_parts.append((body[12], body[14:]))
    if icc_parts:
        meta.icc = b"".join(p for _, p in sorted(icc_parts))
    return meta


def _seg(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


def jpeg_write_meta(data: bytes, meta: Meta, drop_other: bool) -> bytes:
    """Replace the metadata segments of a JPEG without touching the image data.

    ``drop_other`` also removes comments, IPTC and other application segments.
    """
    segs, sos = _jpeg_segments(data)
    head, kept = [], []
    for marker, seg in segs:
        body = seg[4:]
        is_meta = (marker == 0xE1 and (body.startswith(EXIF_HEADER) or body.startswith(XMP_JPEG_HEADER))) \
            or (marker == 0xE2 and body.startswith(ICC_JPEG_HEADER))
        if is_meta:
            continue
        if marker == 0xE0 and body.startswith(b"JFIF") and not head and not kept:
            head.append(seg)
            continue
        if drop_other and (marker == 0xFE or marker == 0xED or (0xE1 <= marker <= 0xEF and marker != 0xEE)):
            continue  # COM, APP13 (IPTC/Photoshop), other APPn; keep APP14 (Adobe color transform)
        kept.append(seg)

    new = []
    if meta.exif and len(meta.exif) + 8 <= 65533:
        new.append(_seg(0xE1, EXIF_HEADER + meta.exif))
    if meta.xmp and len(meta.xmp) + len(XMP_JPEG_HEADER) + 2 <= 65535:
        new.append(_seg(0xE1, XMP_JPEG_HEADER + meta.xmp))
    if meta.icc:
        chunk = 65519
        parts = [meta.icc[i:i + chunk] for i in range(0, len(meta.icc), chunk)]
        for n, part in enumerate(parts, 1):
            new.append(_seg(0xE2, ICC_JPEG_HEADER + bytes([n, len(parts)]) + part))
    return b"\xff\xd8" + b"".join(head + new + kept) + data[sos:]


# ---------------------------------------------------------------- PNG

def _png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    i, out = 8, []
    while i < len(data):
        (length,) = struct.unpack(">I", data[i:i + 4])
        ctype = data[i + 4:i + 8]
        out.append((ctype, data[i + 8:i + 8 + length]))
        i += 12 + length
        if ctype == b"IEND":
            break
    return out


def _png_chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body))


def png_write_meta(data: bytes, meta: Meta, drop_other: bool) -> bytes:
    """Replace eXIf / XMP chunks (and with ``drop_other`` all text chunks)."""
    chunks = _png_chunks(data)
    out = [b"\x89PNG\r\n\x1a\n"]
    for ctype, body in chunks:
        if ctype == b"eXIf":
            continue
        if ctype == b"iTXt" and body.startswith(b"XML:com.adobe.xmp\x00"):
            continue
        if drop_other and ctype in (b"tEXt", b"zTXt", b"iTXt", b"tIME"):
            continue
        if ctype == b"IDAT" and out is not None:
            # metadata must precede image data
            if meta.exif:
                out.append(_png_chunk(b"eXIf", meta.exif))
            if meta.xmp:
                out.append(_png_chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + meta.xmp))
            meta = Meta(None, None, meta.icc)
        out.append(_png_chunk(ctype, body))
    return b"".join(out)


# ---------------------------------------------------------------- WebP

def webp_write_meta(data: bytes, meta: Meta) -> bytes:
    """Replace EXIF/XMP chunks of an extended (VP8X) WebP; simple WebPs have none."""
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("not a WebP")
    chunks = []
    i = 12
    while i + 8 <= len(data):
        tag = data[i:i + 4]
        (size,) = struct.unpack("<I", data[i + 4:i + 8])
        chunks.append((tag, data[i + 8:i + 8 + size]))
        i += 8 + size + (size & 1)
    if not chunks or chunks[0][0] != b"VP8X":
        return data  # no extended header: there was no EXIF/XMP to begin with
    vp8x = bytearray(chunks[0][1])
    chunks = [c for c in chunks if c[0] not in (b"EXIF", b"XMP ")]
    if meta.exif:
        chunks.append((b"EXIF", meta.exif))
    if meta.xmp:
        chunks.append((b"XMP ", meta.xmp))
    flags = vp8x[0] & ~0x0C
    if meta.exif:
        flags |= 0x08
    if meta.xmp:
        flags |= 0x04
    vp8x[0] = flags
    chunks[0] = (b"VP8X", bytes(vp8x))
    body = b"".join(t + struct.pack("<I", len(c)) + c + (b"\x00" if len(c) & 1 else b"") for t, c in chunks)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


# ---------------------------------------------------------------- files

def rewrite_file_meta(src: Path, dst: Path, kind: str, policy: str) -> bool:
    """Write ``src`` to ``dst`` with filtered metadata (lossless). Returns False if unsupported."""
    from .formats import JPEG, PNG, WEBP

    data = src.read_bytes()
    drop_other = policy in ("strip_all", "strip_all_keep_date", "strip_private")
    if kind == JPEG:
        meta = filter_meta(jpeg_read_meta(data), policy)
        dst.write_bytes(jpeg_write_meta(data, meta, drop_other))
        return True
    if kind in (PNG, WEBP):
        from PIL import Image

        with Image.open(src) as im:
            meta = filter_meta(read_meta(im), policy)
        if kind == PNG:
            dst.write_bytes(png_write_meta(data, meta, drop_other))
        else:
            dst.write_bytes(webp_write_meta(data, meta))
        return True
    return False


# ---------------------------------------------------------------- JPEG XL

_JXL_SIG = b"\x00\x00\x00\x0cJXL \r\n\x87\n"


def _box(btype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + btype + payload


def jxl_write_meta(data: bytes, meta: Meta) -> bytes:
    """Put EXIF/XMP boxes into a JPEG XL file (wrapping a bare codestream in a container)."""
    if data[:2] == b"\xff\x0a":  # bare codestream
        if not (meta.exif or meta.xmp):
            return data
        boxes = [(b"ftyp", b"jxl \x00\x00\x00\x00jxl "), (b"jxlc", data)]
    elif data[:12] == _JXL_SIG:
        boxes, i = [], 12
        while i + 8 <= len(data):
            (size,) = struct.unpack(">I", data[i:i + 4])
            btype = data[i + 4:i + 8]
            header = 8
            if size == 1:
                (size,) = struct.unpack(">Q", data[i + 8:i + 16])
                header = 16
            elif size == 0:
                size = len(data) - i
            boxes.append((btype, data[i + header:i + size]))
            i += size
    else:
        raise ValueError("not a JPEG XL file")

    boxes = [b for b in boxes if b[0] not in (b"Exif", b"xml ")]
    meta_boxes = []
    if meta.exif:
        meta_boxes.append((b"Exif", b"\x00\x00\x00\x00" + meta.exif))
    if meta.xmp:
        meta_boxes.append((b"xml ", meta.xmp))
    # Metadata goes before the codestream so readers find it without scanning everything.
    first_cs = next((n for n, b in enumerate(boxes) if b[0] in (b"jxlc", b"jxlp")), len(boxes))
    boxes = boxes[:first_cs] + meta_boxes + boxes[first_cs:]
    out = [_JXL_SIG]
    for btype, payload in boxes:
        if 8 + len(payload) > 0xFFFFFFFF:
            out.append(struct.pack(">I", 1) + btype + struct.pack(">Q", 16 + len(payload)) + payload)
        else:
            out.append(_box(btype, payload))
    return b"".join(out)


def set_orientation(exif: bytes | None, value: int = 1) -> bytes | None:
    """EXIF with the orientation tag reset (after the pixels were rotated)."""
    if not exif:
        return exif
    from PIL import Image

    try:
        ex = Image.Exif()
        ex.load(EXIF_HEADER + exif)
    except Exception:  # noqa: BLE001
        return None
    if ex.get(ORIENTATION, 1) == value:
        return exif
    if value == 1:
        ex.pop(ORIENTATION, None)
    else:
        ex[ORIENTATION] = value
    return _strip_header(ex.tobytes())
