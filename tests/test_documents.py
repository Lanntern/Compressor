"""Office documents and PDFs: smaller, with identical pictures; signed ones untouched."""

import io
import os
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from wick import documents
from wick.settings import Settings


def _photo(size=(1600, 1200)) -> Image.Image:
    return Image.effect_mandelbrot(size, (-2, -1.2, 1, 1.2), 60).convert("RGB")


def _docx(path: Path, signed: bool = False, odf: bool = False) -> dict[str, bytes]:
    png = io.BytesIO()
    _photo((900, 600)).save(png, "PNG", compress_level=0)          # a pasted screenshot, stored loosely
    jpg = io.BytesIO()
    _photo().save(jpg, "JPEG", quality=95, optimize=False)
    parts = {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document>" + b"text " * 2000 +
             b"</w:document>", "word/media/image1.png": png.getvalue(), "word/media/image2.jpeg": jpg.getvalue()}
    if signed:
        parts["_xmlsignatures/sig1.xml"] = b"<Signature/>"
    with zipfile.ZipFile(path, "w") as z:
        if odf:
            z.writestr(zipfile.ZipInfo("mimetype"), b"application/vnd.oasis.opendocument.text")   # stored, first
        for name, data in parts.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=1)
    return parts


def _same(a: bytes, b: bytes) -> bool:
    with Image.open(io.BytesIO(a)) as x, Image.open(io.BytesIO(b)) as y:
        return ImageChops.difference(x.convert("RGB"), y.convert("RGB")).getbbox() is None


def test_office_pictures_shrink_losslessly(tmp_path):
    doc = tmp_path / "report.docx"
    parts = _docx(doc)
    out = documents.optimise_office(doc)
    assert out.stat().st_size < doc.stat().st_size * 0.9
    with zipfile.ZipFile(out) as z:
        assert z.namelist() == list(parts)                                   # same parts, same order
        assert z.read("word/document.xml") == parts["word/document.xml"]    # text untouched
        for name in ("word/media/image1.png", "word/media/image2.jpeg"):
            assert _same(z.read(name), parts[name])                          # identical pixels
            assert len(z.read(name)) < len(parts[name])
    os.remove(out)


def test_opendocument_keeps_its_stored_mimetype_first(tmp_path):
    doc = tmp_path / "letter.odt"
    _docx(doc, odf=True)
    out = documents.optimise_office(doc)
    with zipfile.ZipFile(out) as z:
        first = z.infolist()[0]
        assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
    os.remove(out)


def test_reduce_scales_big_pictures(tmp_path):
    doc = tmp_path / "slides.pptx"
    _docx(doc)
    out = documents.optimise_office(doc, reduce=True, long_edge=800)
    with zipfile.ZipFile(out) as z, Image.open(io.BytesIO(z.read("word/media/image2.jpeg"))) as im:
        assert max(im.size) == 800
    os.remove(out)


def test_signed_documents_are_left_alone(tmp_path):
    doc = tmp_path / "contract.docx"
    _docx(doc, signed=True)
    with pytest.raises(documents.Unsupported):
        documents.optimise_office(doc)
    (tmp_path / "locked.docx").write_bytes(b"\xd0\xcf\x11\xe0 encrypted Office files are OLE containers")
    with pytest.raises(documents.Unsupported):
        documents.office_pictures(tmp_path / "locked.docx")


pikepdf = pytest.importorskip("pikepdf")


def _pdf(path: Path) -> None:
    pages = [_photo(), _photo((1200, 1600))]
    pages[0].save(path, "PDF", save_all=True, append_images=pages[1:], quality=95, resolution=150)


def _images(path: Path) -> list[bytes]:
    with pikepdf.open(path) as pdf:
        return [obj.read_raw_bytes() for obj in pdf.objects
                if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == "/Image"]


def test_pdf_lossless(tmp_path):
    src = tmp_path / "scan.pdf"
    _pdf(src)
    out = documents.optimise_pdf(src)
    assert out.stat().st_size < src.stat().st_size
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 2
    before, after = _images(src), _images(out)
    assert len(before) == len(after) and all(_same(a, b) for a, b in zip(before, after, strict=True))
    os.remove(out)


def test_signed_pdfs_are_left_alone(tmp_path):
    src = tmp_path / "signed.pdf"
    _pdf(src)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.Root.AcroForm = pikepdf.Dictionary(SigFlags=3, Fields=pikepdf.Array())
        pdf.save(src)
    assert "signed" in documents.pdf_problem(src).lower()
    with pytest.raises(documents.Unsupported):
        documents.optimise_pdf(src)


def test_documents_through_the_compress_job(tmp_path):
    """Planned, compressed, replaced (original in the Recycle Bin/Trash) and undoable like any file."""
    from wick.engine import DONE, run_job, undo_run
    from wick.plan import plan_entry
    from wick.scanner import scan

    _docx(tmp_path / "report.docx")
    _pdf(tmp_path / "scan.pdf")
    original = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    s = Settings(image_format="none", video_codec="none", min_savings_pct=0)
    entries = scan(tmp_path, s).entries
    plans = [(e, plan_entry(e, s)) for e in entries]
    assert all(p.convert and p.route == "document" for _, p in plans), [p.reason for _, p in plans]
    results, run_id = run_job(plans, s, tmp_path)
    assert all(r.status == DONE for r in results), [r.message for r in results]
    assert all((tmp_path / n).stat().st_size < len(b) for n, b in original.items())
    assert undo_run(run_id)[1] == 0
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == original
    assert not plan_entry(entries[0], Settings(documents="none")).convert
