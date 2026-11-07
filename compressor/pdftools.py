"""PDFs and pictures: pages as images, pictures (and PDFs) combined into one PDF, splitting, turning pages,
and passwords.

Pictures go into a PDF without being re-encoded where the PDF format allows it: a JPEG is stored as the very
same bytes (PDF reads JPEG directly), turned with the page's rotation when its EXIF says the camera was held
sideways. Other pictures are stored losslessly (Flate, with their transparency as a soft mask).
Pages are drawn with Qt's PDF module (PDFium) on a white background.
Turning pages only changes each page's /Rotate entry: nothing on the page is redrawn.
"""

from __future__ import annotations

import threading
import zlib
from collections.abc import Callable, Iterable
from pathlib import Path

PAGE_SIZES = {"picture": None, "a4": (595.276, 841.89), "letter": (612.0, 792.0)}   # points
PDF_EXT = ".pdf"


class WrongPassword(ValueError):
    pass


def _open(path: Path, password: str = ""):
    import warnings

    import pikepdf

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)        # ("a password was given but not needed")
            return pikepdf.open(path, password=password)
    except pikepdf.PasswordError as e:
        raise WrongPassword("That password isn't right" if password else "This PDF needs its password") from e


# ---------------------------------------------------------------- pictures into a PDF

_JPEG_TURN = {1: 0, 3: 180, 6: 90, 8: 270}        # EXIF orientation -> page /Rotate (clockwise)


def _image_xobject(pdf, path: Path):
    """(image XObject, width, height, page rotation) for a picture; JPEGs as they are."""
    import pikepdf
    from PIL import Image, ImageOps

    from .formats import register_heif

    register_heif()
    with Image.open(path) as im:
        orientation = int(im.getexif().get(0x0112, 1) or 1)
        if im.format == "JPEG" and im.mode in ("L", "RGB", "CMYK") and orientation in _JPEG_TURN:
            space = {"L": pikepdf.Name.DeviceGray, "RGB": pikepdf.Name.DeviceRGB, "CMYK": pikepdf.Name.DeviceCMYK}
            xo = pikepdf.Stream(pdf, b"")
            xo.write(path.read_bytes(), filter=pikepdf.Name.DCTDecode)
            xo.Type, xo.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
            xo.Width, xo.Height, xo.BitsPerComponent = im.width, im.height, 8
            xo.ColorSpace = space[im.mode]
            if im.mode == "CMYK" and "adobe" in im.info:        # (Photoshop's CMYK JPEGs are stored inverted)
                xo.Decode = pikepdf.Array([1, 0] * 4)
            return xo, im.width, im.height, _JPEG_TURN[orientation], im.info.get("dpi")
        dpi = im.info.get("dpi")
        im.seek(0)
        pic = ImageOps.exif_transpose(im) or im
        if pic.mode in ("I;16", "I", "F"):
            pic = pic.convert("L")
        alpha = None
        if pic.mode in ("RGBA", "LA", "PA") or (pic.mode == "P" and "transparency" in pic.info):
            rgba = pic.convert("RGBA")
            alpha = rgba.getchannel("A")
            pic = rgba.convert("RGB")
        elif pic.mode not in ("L", "RGB"):
            pic = pic.convert("RGB")

    def flate(img, space):
        xo = pikepdf.Stream(pdf, b"")
        xo.write(zlib.compress(img.tobytes(), 9), filter=pikepdf.Name.FlateDecode)
        xo.Type, xo.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
        xo.Width, xo.Height, xo.BitsPerComponent, xo.ColorSpace = img.width, img.height, 8, space
        return xo

    xo = flate(pic, pikepdf.Name.DeviceGray if pic.mode == "L" else pikepdf.Name.DeviceRGB)
    if alpha is not None and alpha.getextrema() != (255, 255):
        xo.SMask = flate(alpha, pikepdf.Name.DeviceGray)
    return xo, pic.width, pic.height, 0, dpi


def _add_picture(pdf, path: Path, page_size: str) -> None:
    import pikepdf

    xo, w, h, turn, dpi = _image_xobject(pdf, path)
    try:
        dx, dy = (float(dpi[0]), float(dpi[1])) if dpi else (96.0, 96.0)
        if not (20 <= dx <= 2400 and 20 <= dy <= 2400):
            dx = dy = 96.0
    except (TypeError, ValueError, IndexError):
        dx = dy = 96.0
    pw, ph = w * 72 / dx, h * 72 / dy                         # the picture at its own size
    box = PAGE_SIZES.get(page_size)
    if box is None:
        page_w, page_h, x, y, dw, dh = pw, ph, 0.0, 0.0, pw, ph
    else:
        page_w, page_h = box
        if turn in (90, 270):                                 # (the page is turned: fit the turned picture)
            page_w, page_h = page_h, page_w
        if (pw > ph) != (page_w > page_h) and turn in (0, 180):
            page_w, page_h = page_h, page_w                   # a landscape picture on a landscape page
        margin = 36.0
        scale = min((page_w - 2 * margin) / pw, (page_h - 2 * margin) / ph, 1.0)
        dw, dh = pw * scale, ph * scale
        x, y = (page_w - dw) / 2, (page_h - dh) / 2
    page = pdf.add_blank_page(page_size=(page_w, page_h))
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=xo))
    page.Contents = pdf.make_stream(f"q {dw:.4f} 0 0 {dh:.4f} {x:.4f} {y:.4f} cm /Im0 Do Q".encode())
    if turn:
        page.Rotate = turn


def combine(inputs: Iterable[Path], dest: Path, page_size: str = "picture",
            progress: Callable[[int, int], None] | None = None, cancel: threading.Event | None = None,
            passwords: dict[str, str] | None = None) -> int:
    """Pictures and PDFs, in this order, into one PDF at ``dest``. Returns its page count."""
    import pikepdf

    items = list(inputs)
    out = pikepdf.new()
    opened = []
    try:
        for i, p in enumerate(items):
            if cancel is not None and cancel.is_set():
                raise InterruptedError()
            if p.suffix.lower() == PDF_EXT:
                src = _open(p, (passwords or {}).get(str(p), ""))
                opened.append(src)
                out.pages.extend(src.pages)
            else:
                _add_picture(out, p, page_size)
            if progress:
                progress(i + 1, len(items))
        out.save(dest, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        n = len(out.pages)
    finally:
        out.close()
        for src in opened:
            src.close()
    check(dest, n)
    return n


# ---------------------------------------------------------------- pages as images

def render_pages(src: Path, folder: Path, fmt: str = "png", dpi: int = 150, password: str = "",
                 progress: Callable[[int, int], None] | None = None,
                 cancel: threading.Event | None = None) -> list[Path]:
    """Every page as an image ("page 001.png"...) in ``folder`` (made if needed), on white."""
    from PIL import Image
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImage
    from PySide6.QtPdf import QPdfDocument

    doc = QPdfDocument()
    if password:
        doc.setPassword(password)
    status = doc.load(str(src))
    if status != QPdfDocument.Error.None_:
        if status == QPdfDocument.Error.IncorrectPassword:
            raise WrongPassword("That password isn't right" if password else "This PDF needs its password")
        raise ValueError(f"The PDF can't be read ({status.name})")
    n = doc.pageCount()
    folder.mkdir(parents=True, exist_ok=True)
    width = len(str(n)) if n >= 1000 else 3
    made = []
    try:
        for i in range(n):
            if cancel is not None and cancel.is_set():
                raise InterruptedError()
            pts = doc.pagePointSize(i)
            size = QSize(max(1, round(pts.width() * dpi / 72)), max(1, round(pts.height() * dpi / 72)))
            img = doc.render(i, size).convertToFormat(QImage.Format_RGBA8888)
            pic = Image.frombuffer("RGBA", (img.width(), img.height()), bytes(img.constBits()), "raw", "RGBA",
                                   img.bytesPerLine(), 1)
            page = Image.new("RGB", pic.size, "white")
            page.paste(pic, mask=pic.getchannel("A"))
            out = folder / f"page {i + 1:0{width}d}.{'jpg' if fmt == 'jpeg' else 'png'}"
            if fmt == "jpeg":
                page.save(out, "JPEG", quality=90, optimize=True, progressive=True, dpi=(dpi, dpi))
            else:
                page.save(out, "PNG", optimize=False, compress_level=6, dpi=(dpi, dpi))
            made.append(out)
            if progress:
                progress(i + 1, n)
    finally:
        doc.close()
    if len(made) != n or n == 0:
        raise ValueError("The PDF has no pages")
    return made


# ---------------------------------------------------------------- page and password changes

def split(src: Path, folder: Path, password: str = "") -> list[Path]:
    """One PDF per page ("page 001.pdf"...) in ``folder``."""
    import pikepdf

    folder.mkdir(parents=True, exist_ok=True)
    made = []
    with _open(src, password) as pdf:
        n = len(pdf.pages)
        width = len(str(n)) if n >= 1000 else 3
        for i, page in enumerate(pdf.pages):
            one = pikepdf.new()
            one.pages.append(page)
            out = folder / f"page {i + 1:0{width}d}.pdf"
            one.save(out)
            one.close()
            made.append(out)
    for p in made:
        check(p, 1)
    return made


def rotate(src: Path, dest: Path, degrees: int, password: str = "") -> None:
    """Every page turned clockwise by ``degrees`` (90, 180, 270): only the page's /Rotate entry changes."""
    with _open(src, password) as pdf:
        for page in pdf.pages:
            page.Rotate = (int(page.obj.get("/Rotate", 0)) + degrees) % 360
        n = len(pdf.pages)
        pdf.save(dest)
    check(dest, n)


def protect(src: Path, dest: Path, password: str) -> None:
    """A copy that opens only with ``password`` (AES-256)."""
    import pikepdf

    if not password:
        raise ValueError("Choose a password")
    with _open(src) as pdf:
        n = len(pdf.pages)
        pdf.save(dest, encryption=pikepdf.Encryption(user=password, owner=password, R=6))
    check(dest, n, password)
    try:
        _open(dest).close()
    except WrongPassword:
        return
    raise ValueError("The copy didn't get its password")


def unlock(src: Path, dest: Path, password: str) -> None:
    """A copy without the password (it has to be known)."""
    with _open(src, password) as pdf:
        n = len(pdf.pages)
        pdf.save(dest, encryption=False)
    check(dest, n)


def is_encrypted(path: Path) -> bool:
    import pikepdf

    try:
        with pikepdf.open(path) as pdf:
            return bool(pdf.is_encrypted)
    except pikepdf.PasswordError:
        return True
    except Exception:  # noqa: BLE001
        return False


def check(path: Path, pages: int, password: str = "") -> None:
    """The result opens and has the pages it should."""
    from .images import VerificationFailed

    try:
        with _open(path, password) as pdf:
            got = len(pdf.pages)
    except Exception as e:  # noqa: BLE001
        raise VerificationFailed(f"The PDF can't be opened ({e})") from e
    if got != pages:
        raise VerificationFailed(f"The PDF has {got} pages, expected {pages}")


def page_count(path: Path, password: str = "") -> int:
    with _open(path, password) as pdf:
        return len(pdf.pages)


__all__ = ["combine", "render_pages", "split", "rotate", "protect", "unlock", "is_encrypted", "check",
           "page_count", "WrongPassword", "PAGE_SIZES"]
