"""Office documents and PDFs: smaller without changing what they show.

Office files (.docx/.pptx/.xlsx and OpenDocument) are zip archives; nearly all of their size is pictures.
Each picture is shrunk losslessly (PNG optimised, JPEG optimised without re-encoding: identical pixels)
and the archive is recompressed. Everything else inside stays byte-for-byte the same, and that is checked.

PDFs: the same lossless JPEG optimisation for pictures stored as JPEG, then a lossless restructure with
qpdf (compressed object streams, streams recompressed at the highest level). qpdf's check must pass.

"Reduce large pictures" (off by default) also scales pictures above a size down and re-encodes them.

Never changed: digitally signed documents (any change breaks the signature) and password-protected ones.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from .images import NotWorthIt, VerificationFailed
from .fileops import remove_quietly, temp_name

OFFICE_EXTS = {".docx", ".docm", ".dotx", ".pptx", ".pptm", ".potx", ".ppsx", ".xlsx", ".xlsm", ".xltx",
               ".odt", ".ods", ".odp", ".odg"}
PDF_EXTS = {".pdf"}
_PICTURE_DIRS = ("media/", "pictures/", "embeddings/")    # word/media, ppt/media, xl/media; ODF Pictures/
_SIGNATURES = ("_xmlsignatures/", "meta-inf/documentsignatures.xml")
REDUCE_LONG_EDGE = 2560


class Unsupported(NotWorthIt):
    """A document this module leaves alone (signed, encrypted, not really an Office file)."""


def pdf_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("pikepdf") is not None


# ---------------------------------------------------------------- pictures

def _same_pixels(a: bytes, b: bytes) -> bool:
    from PIL import Image, ImageChops

    with Image.open(io.BytesIO(a)) as ia, Image.open(io.BytesIO(b)) as ib:
        if ia.size != ib.size:
            return False
        mode = "RGBA" if "A" in ia.getbands() or "A" in ib.getbands() or ia.mode == "P" else "RGB"
        if ia.mode in ("L", "1") and ib.mode in ("L", "1"):
            mode = "L"
        if ia.mode == "CMYK" or ib.mode == "CMYK":
            mode = "CMYK"
        return ImageChops.difference(ia.convert(mode), ib.convert(mode)).getbbox() is None


def shrink_jpeg(data: bytes) -> bytes:
    """Same pixels, fewer bytes (Huffman tables optimised, progressive): or the original."""
    try:
        import mozjpeg_lossless_optimization as mlo

        out = mlo.optimize(data)
    except Exception:  # noqa: BLE001 - odd JPEGs (CMYK, arithmetic...) stay as they are
        return data
    return out if len(out) < len(data) and _same_pixels(data, out) else data


def shrink_png(data: bytes) -> bytes:
    try:
        import oxipng

        out = oxipng.optimize_from_memory(data, level=3)
    except Exception:  # noqa: BLE001
        return data
    return out if len(out) < len(data) and _same_pixels(data, out) else data


def reduce_picture(data: bytes, ext: str, long_edge: int) -> bytes:
    """Scale a picture larger than ``long_edge`` down (and re-encode); else the original."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if max(w, h) <= long_edge:
                return data
            im.load()
            scale = long_edge / max(w, h)
            small = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            if ext in (".jpg", ".jpeg"):
                if small.mode not in ("RGB", "L", "CMYK"):
                    small = small.convert("RGB")
                small.save(buf, "JPEG", quality=88, optimize=True, progressive=True,
                           icc_profile=im.info.get("icc_profile"))
            else:
                small.save(buf, "PNG", optimize=True)
                return min(shrink_png(buf.getvalue()), data, key=len)
            out = buf.getvalue()
    except Exception:  # noqa: BLE001
        return data
    return out if len(out) < len(data) else data


# ---------------------------------------------------------------- Office documents

def _is_picture(name: str) -> bool:
    low = name.lower()
    return any(d in low for d in _PICTURE_DIRS) and low.rsplit(".", 1)[-1] in ("png", "jpg", "jpeg")


def office_pictures(path: Path) -> tuple[int, int, int]:
    """(JPEG bytes, PNG bytes, everything else) inside an Office file, from its directory only."""
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
    except (zipfile.BadZipFile, OSError) as e:
        raise Unsupported("Password-protected, or not a standard Office document") from e
    if any(i.filename.lower().startswith(_SIGNATURES) for i in infos):
        raise Unsupported("Digitally signed: any change would break the signature")
    jpeg = png = other = 0
    for i in infos:
        if _is_picture(i.filename):
            if i.filename.lower().endswith(".png"):
                png += i.compress_size
            else:
                jpeg += i.compress_size
        else:
            other += i.compress_size
    return jpeg, png, other


def optimise_office(src: Path, reduce: bool = False, long_edge: int = REDUCE_LONG_EDGE) -> Path:
    """A smaller copy of ``src`` next to it (verified); the caller decides whether to keep it."""
    office_pictures(src)        # raises Unsupported for signed / encrypted files
    out = temp_name(src.parent, src.suffix)
    changed: set[str] = set()
    try:
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info)
                new = data
                if _is_picture(info.filename):
                    ext = "." + info.filename.lower().rsplit(".", 1)[-1]
                    if reduce:
                        new = reduce_picture(new, ext, long_edge)
                    new = shrink_png(new) if ext == ".png" else shrink_jpeg(new)
                    if new is not data and new != data:
                        changed.add(info.filename)
                zi = zipfile.ZipInfo(info.filename, info.date_time)
                zi.external_attr, zi.create_system, zi.comment = info.external_attr, info.create_system, info.comment
                zi.compress_type = info.compress_type     # the ODF "mimetype" entry must stay stored
                if zi.compress_type == zipfile.ZIP_DEFLATED:
                    zout.writestr(zi, new, compresslevel=9)
                else:
                    zout.writestr(zi, new)
        _verify_office(src, out, changed, lossless=not reduce)
        return out
    except BaseException:
        remove_quietly(out)
        raise


def _verify_office(src: Path, out: Path, changed: set[str], lossless: bool) -> None:
    from PIL import Image

    with zipfile.ZipFile(src) as a, zipfile.ZipFile(out) as b:
        if b.testzip() is not None:
            raise VerificationFailed("The rebuilt document doesn't read back correctly")
        if [i.filename for i in a.infolist()] != [i.filename for i in b.infolist()]:
            raise VerificationFailed("Parts of the document went missing")
        for ia, ib in zip(a.infolist(), b.infolist(), strict=True):
            if ia.filename not in changed:
                if ia.CRC != ib.CRC or ia.file_size != ib.file_size:
                    raise VerificationFailed(f"{ia.filename} changed")
                continue
            old, new = a.read(ia), b.read(ib)
            try:
                with Image.open(io.BytesIO(new)) as im:
                    im.load()
            except Exception as e:  # noqa: BLE001
                raise VerificationFailed(f"Picture {ia.filename} doesn't open") from e
            if lossless and not _same_pixels(old, new):
                raise VerificationFailed(f"Picture {ia.filename} looks different")


# ---------------------------------------------------------------- PDFs

def pdf_problem(path: Path) -> str:
    """Why a PDF is left alone, or ""."""
    import pikepdf

    try:
        with pikepdf.open(path) as pdf:
            acro = pdf.Root.get("/AcroForm")
            if acro is not None and int(acro.get("/SigFlags", 0)) & 1:
                return "Digitally signed: any change would break the signature"
            for field in (acro.get("/Fields", []) if acro is not None else []):
                if field.get("/FT") == "/Sig":
                    return "Digitally signed: any change would break the signature"
    except pikepdf.PasswordError:
        return "Password-protected"
    except Exception as e:  # noqa: BLE001
        return f"Can't be read as a PDF ({type(e).__name__})"
    return ""


def _pdfa_part(pdf) -> int:
    try:
        with pdf.open_metadata() as meta:
            return int(meta.get("pdfaid:part", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def optimise_pdf(src: Path, reduce: bool = False, long_edge: int = REDUCE_LONG_EDGE) -> Path:
    import pikepdf

    problem = pdf_problem(src)
    if problem:
        raise Unsupported(problem)
    out = temp_name(src.parent, ".pdf")
    try:
        with pikepdf.open(src) as pdf:
            pages = len(pdf.pages)
            boxes = [tuple(float(x) for x in p.mediabox) for p in pdf.pages]
            for obj in pdf.objects:
                if not isinstance(obj, pikepdf.Stream) or obj.get("/Subtype") != "/Image":
                    continue
                filt = obj.get("/Filter")
                if filt != "/DCTDecode" or "/DecodeParms" in obj or "/SMask" in obj and reduce:
                    continue
                raw = obj.read_raw_bytes()
                new = raw
                if reduce and "/SMask" not in obj and "/Mask" not in obj:
                    new = reduce_picture(new, ".jpg", long_edge)
                new = shrink_jpeg(new)
                if new == raw:
                    continue
                if new is not raw and reduce:
                    from PIL import Image

                    with Image.open(io.BytesIO(new)) as im:
                        w, h = im.size
                    obj.Width, obj.Height = w, h
                obj.write(new, filter=pikepdf.Name.DCTDecode)
            pikepdf.settings.set_flate_compression_level(9)
            mode = pikepdf.ObjectStreamMode.preserve if _pdfa_part(pdf) == 1 else pikepdf.ObjectStreamMode.generate
            pdf.save(out, object_stream_mode=mode, compress_streams=True, recompress_flate=True)
        with pikepdf.open(out) as check:
            if len(check.pages) != pages:
                raise VerificationFailed("Pages went missing")
            if [tuple(float(x) for x in p.mediabox) for p in check.pages] != boxes:
                raise VerificationFailed("Page sizes changed")
            checker = getattr(check, "check_pdf_syntax", None) or check.check   # renamed in pikepdf 9
            problems = [p for p in checker() if "error" in str(p).lower()]
            if problems:
                raise VerificationFailed(f"The rebuilt PDF has problems: {problems[0]}")
        return out
    except BaseException:
        remove_quietly(out)
        raise


def optimise(src: Path, reduce: bool = False) -> Path:
    if src.suffix.lower() in PDF_EXTS:
        return optimise_pdf(src, reduce)
    return optimise_office(src, reduce)


__all__ = ["optimise", "optimise_office", "optimise_pdf", "office_pictures", "pdf_problem", "pdf_available",
           "Unsupported", "OFFICE_EXTS", "PDF_EXTS"]
