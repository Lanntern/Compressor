"""Metadata cleanup without re-encoding.

What each file reveals (location, camera, people, dates) is read for display, and
removal rewrites only the metadata:

* JPEG / PNG / WebP / JPEG XL: container-level edits (metadata.py); the compressed
  image data is compared byte for byte afterwards.
* GIF: gifsicle drops comments/extensions; pixels compared frame by frame.
* Video: ffmpeg remux with ``-c copy`` (streams untouched); verified by stream layout
  and duration.
HEIC/AVIF/TIFF can't be edited without re-encoding and are reported, not changed.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import metadata as md
from . import tools
from .fileops import link_or_copy, remove_quietly, temp_name
from .formats import GIF, JPEG, PNG, VIDEO, WEBP, classify
from .images import VerificationFailed

JXL = "jxl"
EDITABLE = (JPEG, PNG, WEBP, GIF, VIDEO, JXL)
POLICY_LABELS = {
    "strip_gps": "Location",
    "strip_private": "Location and personal info",
    "strip_all_keep_date": "Everything except the date taken",
    "strip_all": "Everything",
}


def kind_of(path: Path) -> str | None:
    if path.suffix.lower() == ".jxl":
        return JXL
    return classify(path)


@dataclass
class MetaSummary:
    gps: str = ""            # "59.3293, 18.0686" or "yes"
    camera: str = ""         # make + model
    taken: str = ""          # date taken
    people: list[str] = field(default_factory=list)   # artist/copyright/comment/owner...
    software: str = ""
    other: int = 0           # count of other tags
    editable: bool = True
    reason: str = ""         # why not editable

    @property
    def anything(self) -> bool:
        return bool(self.gps or self.camera or self.taken or self.people or self.software or self.other)

    def describe(self) -> str:
        parts = []
        if self.gps:
            parts.append(f"Location {self.gps}")
        if self.camera:
            parts.append(self.camera)
        if self.people:
            parts.append(", ".join(self.people))
        if self.software:
            parts.append(self.software)
        return " · ".join(parts) or ("Other metadata" if self.other else "No metadata")


def pretty_date(raw: str) -> str:
    """'2024:05:01 10:00:00' or '2025-07-14T12:00:00.000000Z' -> '2024-05-01 10:00'."""
    import re

    m = re.match(r"(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2})", raw.strip())
    return f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}" if m else raw.strip()


def _dms(v) -> float | None:
    try:
        d, m, s = (float(x) for x in v)
        return d + m / 60 + s / 3600
    except Exception:  # noqa: BLE001
        return None


def _summarize_exif(exif: bytes | None, s: MetaSummary) -> None:
    if not exif:
        return
    from PIL import Image

    ex = Image.Exif()
    try:
        ex.load(md.EXIF_HEADER + exif)
    except Exception:  # noqa: BLE001
        return
    gps = ex.get_ifd(md.GPS_IFD)
    if gps:
        lat, lon = _dms(gps.get(2)), _dms(gps.get(4))
        if lat is not None and lon is not None:
            lat *= -1 if gps.get(1) == "S" else 1
            lon *= -1 if gps.get(3) == "W" else 1
            s.gps = f"{lat:.4f}, {lon:.4f}"
        else:
            s.gps = "yes"
    make, model = str(ex.get(0x010F, "")).strip(), str(ex.get(0x0110, "")).strip()
    s.camera = model if make and model.lower().startswith(make.lower()) else " ".join(x for x in (make, model) if x)
    sub = ex.get_ifd(md.EXIF_IFD)
    s.taken = pretty_date(str(sub.get(0x9003) or ex.get(0x0132) or ""))
    for tag, label in ((0x013B, "Artist"), (0x8298, "Copyright"), (0x010E, "Description")):
        if ex.get(tag):
            s.people.append(f"{label}: {str(ex[tag]).strip()[:40]}")
    if sub.get(0xA430):
        s.people.append(f"Owner: {str(sub[0xA430])[:40]}")
    if sub.get(0xA431):
        s.people.append("Camera serial number")
    s.software = str(ex.get(0x0131, "")).strip()[:40]
    s.other = len(ex) + len(sub)


def summarize(path: Path) -> MetaSummary:
    kind = kind_of(path)
    s = MetaSummary()
    if kind not in EDITABLE:
        s.editable, s.reason = False, "Can't be edited without re-encoding"
    try:
        if kind == VIDEO:
            from .probe import probe_video

            info = probe_video(path)
            tags = info.tags if info else {}
            loc = next((tags[k] for k in ("location", "location-eng", "com.apple.quicktime.location.iso6709")
                        if tags.get(k)), "")
            s.gps = loc.strip("/")
            s.camera = " ".join(tags.get(k, "") for k in ("com.apple.quicktime.make", "com.apple.quicktime.model")
                                ).strip() or tags.get("com.android.model", "")
            s.taken = pretty_date(tags.get("creation_time", ""))
            s.software = tags.get("com.apple.quicktime.software", "") or tags.get("encoder", "")
            s.people = [f"{k.title()}: {tags[k][:40]}" for k in ("artist", "author", "comment", "copyright")
                        if tags.get(k)]
            s.other = len(tags)
        elif kind == JXL:
            exif, xmp, _ = _jxl_boxes(path.read_bytes())
            _summarize_exif(exif, s)
            if xmp:
                s.other += 1
        elif kind is not None:
            from PIL import Image

            from .formats import register_heif

            register_heif()
            with Image.open(path) as im:
                meta = md.read_meta(im)
                comment = im.info.get("comment")
            _summarize_exif(meta.exif, s)
            if meta.xmp:
                s.other += 1
                if b"GPS" in meta.xmp and not s.gps:
                    s.gps = "yes (XMP)"
            if comment:
                c = comment.decode("utf-8", "replace") if isinstance(comment, bytes) else str(comment)
                s.people.append(f"Comment: {c.strip()[:40]}")
    except Exception:  # noqa: BLE001 - unreadable: show as unknown
        pass
    return s


# ---------------------------------------------------------------- JPEG XL boxes

def _jxl_box_list(data: bytes) -> list[tuple[bytes, bytes]]:
    if data[:12] != b"\x00\x00\x00\x0cJXL \r\n\x87\n":
        return []
    boxes, i = [], 12
    while i + 8 <= len(data):
        (size,) = struct.unpack(">I", data[i:i + 4])
        btype, header = data[i + 4:i + 8], 8
        if size == 1:
            (size,) = struct.unpack(">Q", data[i + 8:i + 16])
            header = 16
        elif size == 0:
            size = len(data) - i
        boxes.append((btype, data[i + header:i + size]))
        i += size
    return boxes


def _jxl_boxes(data: bytes) -> tuple[bytes | None, bytes | None, bool]:
    """(exif, xmp, has_jpeg_reconstruction)."""
    exif = xmp = None
    jbrd = False
    for btype, payload in _jxl_box_list(data):
        if btype == b"Exif" and len(payload) > 4:
            exif = payload[4:]
        elif btype == b"xml ":
            xmp = payload
        elif btype == b"jbrd":
            jbrd = True
        elif btype == b"brob" and payload[:4] in (b"Exif", b"xml "):
            try:
                import brotli

                inner = brotli.decompress(payload[4:])
                if payload[:4] == b"Exif":
                    exif = inner[4:]
                else:
                    xmp = inner
            except Exception:  # noqa: BLE001
                pass
    return exif, xmp, jbrd


def _payload(data: bytes, kind: str) -> bytes:
    """The compressed image data that must stay identical."""
    if kind == JPEG:
        return data[data.index(b"\xff\xda"):]
    if kind == PNG:
        return b"".join(body for ctype, body in md._png_chunks(data) if ctype in (b"IDAT", b"fdAT", b"fcTL", b"acTL"))
    if kind == WEBP:
        out, i = [], 12
        while i + 8 <= len(data):
            tag = data[i:i + 4]
            (size,) = struct.unpack("<I", data[i + 4:i + 8])
            if tag in (b"VP8 ", b"VP8L", b"ANMF", b"ALPH", b"ANIM"):
                out.append(data[i + 8:i + 8 + size])
            i += 8 + size + (size & 1)
        return b"".join(out)
    if kind == JXL:
        return b"".join(p for t, p in _jxl_box_list(data) if t in (b"jxlc", b"jxlp", b"jbrd")) or data
    return b""


# ---------------------------------------------------------------- cleaning

def clean(path: Path, policy: str, info=None) -> Path:
    """Write a copy of ``path`` with metadata removed per ``policy``; verify; return the temp path."""
    kind = kind_of(path)
    if kind not in EDITABLE:
        raise ValueError("This format can't be edited without re-encoding")
    out = temp_name(path.parent, path.suffix.lower())
    staged = temp_name(path.parent, path.suffix.lower(), "-in")
    try:
        link_or_copy(path, staged)
        if kind in (JPEG, PNG, WEBP):
            md.rewrite_file_meta(staged, out, kind, policy)
        elif kind == JXL:
            data = staged.read_bytes()
            exif, xmp, jbrd = _jxl_boxes(data)
            if jbrd:
                raise ValueError("This JPEG XL can be turned back into the original JPEG; editing its metadata "
                                 "would break that")
            meta = md.filter_meta(md.Meta(exif, xmp, None), policy)
            out.write_bytes(md.jxl_write_meta(data, meta))
        elif kind == GIF:
            args = [tools.require("gifsicle"), "--no-comments", "--no-names"]
            if policy != "strip_gps":
                args.append("--no-extensions")
            tools.run([*args, staged.name, "-o", out.name], cwd=path.parent, timeout=600)
        elif kind == VIDEO:
            from .probe import probe_video
            from .video import metadata_args

            info = info or probe_video(staged)
            args = [tools.require("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(staged),
                    "-map", "0", "-c", "copy", "-map_chapters", "0", *metadata_args(policy, info)]
            if out.suffix in (".mp4", ".m4v", ".mov"):
                args += ["-movflags", "+faststart"]
            tools.run([*args, str(out)], timeout=3600)
        _verify(staged, out, kind)
        return out
    except BaseException:
        remove_quietly(out)
        raise
    finally:
        remove_quietly(staged)


def _verify(src: Path, out: Path, kind: str) -> None:
    if not out.exists() or out.stat().st_size == 0:
        raise VerificationFailed("No output was written")
    if kind in (JPEG, PNG, WEBP, JXL):
        if _payload(src.read_bytes(), kind) != _payload(out.read_bytes(), kind):
            raise VerificationFailed("The image data changed")
        return
    if kind == GIF:
        from PIL import Image, ImageSequence

        from .images import digest_images

        with Image.open(src) as a, Image.open(out) as b:
            if digest_images(f.copy() for f in ImageSequence.Iterator(a)) != \
                    digest_images(f.copy() for f in ImageSequence.Iterator(b)):
                raise VerificationFailed("The animation changed")
        return
    from .probe import probe_video

    va, vb = probe_video(src), probe_video(out)
    if va is None or vb is None:
        raise VerificationFailed("The video can't be read")
    if [(s.kind, s.codec) for s in va.streams if s.kind in ("video", "audio", "subtitle")] != \
            [(s.kind, s.codec) for s in vb.streams if s.kind in ("video", "audio", "subtitle")]:
        raise VerificationFailed("Streams changed")
    if abs(va.duration - vb.duration) > 0.5:
        raise VerificationFailed("Duration changed")
