"""PDFs and pictures: pages as images, pictures into a PDF (JPEGs as they are), split, turn, passwords."""

import pikepdf
import pytest
from PIL import Image

from compressor import convert, pdftools
from compressor.engine import DONE, process_convert, run_job, undo_run
from compressor.plan import Plan
from compressor.scanner import scan
from compressor.settings import Settings


@pytest.fixture(scope="module", autouse=True)
def qt():
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance() or QGuiApplication([])


def _pictures(d):
    ex = Image.Exif()
    ex[0x0112] = 6                                                     # held sideways
    Image.radial_gradient("L").convert("RGB").resize((400, 300)).save(d / "side.jpg", exif=ex.tobytes(),
                                                                         dpi=(300, 300))
    Image.new("RGBA", (200, 100), (255, 0, 0, 128)).save(d / "alpha.png")
    Image.new("CMYK", (100, 100), (0, 255, 0, 0)).save(d / "cmyk.jpg")


def test_pictures_into_one_pdf_without_re_encoding(tmp_path):
    _pictures(tmp_path)
    n = pdftools.combine([tmp_path / "side.jpg", tmp_path / "alpha.png", tmp_path / "cmyk.jpg"], tmp_path / "a.pdf")
    assert n == 3
    with pikepdf.open(tmp_path / "a.pdf") as pdf:
        first = pdf.pages[0]
        assert first.Resources.XObject.Im0.read_raw_bytes() == (tmp_path / "side.jpg").read_bytes()
        assert int(first.obj.Rotate) == 90 and list(map(float, first.MediaBox)) == [0, 0, 96, 72]   # 300 dpi
        assert "/SMask" in pdf.pages[1].Resources.XObject.Im0                 # transparency kept
    pages = pdftools.render_pages(tmp_path / "a.pdf", tmp_path / "pages", "png", 72)
    sizes = [Image.open(p).size for p in pages]
    assert sizes == [(72, 96), (150, 75), (75, 75)]                    # upright, at their own size
    magenta = Image.open(pages[2]).convert("RGB").getpixel((37, 37))
    assert magenta[0] > 200 and magenta[1] < 80 and magenta[2] > 120   # CMYK colours not inverted
    pdftools.combine([tmp_path / "a.pdf", tmp_path / "alpha.png"], tmp_path / "b.pdf", "a4")
    with pikepdf.open(tmp_path / "b.pdf") as pdf:
        assert len(pdf.pages) == 4 and [round(float(v)) for v in pdf.pages[3].MediaBox] == [0, 0, 842, 595]


def test_page_changes_and_passwords(tmp_path):
    _pictures(tmp_path)
    pdftools.combine([tmp_path / "alpha.png", tmp_path / "cmyk.jpg"], tmp_path / "a.pdf")
    pdftools.rotate(tmp_path / "a.pdf", tmp_path / "r.pdf", 270)
    with pikepdf.open(tmp_path / "r.pdf") as pdf:
        assert [int(p.obj.Rotate) for p in pdf.pages] == [270, 270]
    pdftools.protect(tmp_path / "a.pdf", tmp_path / "locked.pdf", "s3cret")
    assert pdftools.is_encrypted(tmp_path / "locked.pdf")
    with pytest.raises(pdftools.WrongPassword):
        pdftools.unlock(tmp_path / "locked.pdf", tmp_path / "x.pdf", "wrong")
    with pytest.raises(pdftools.WrongPassword):
        pdftools.render_pages(tmp_path / "locked.pdf", tmp_path / "x")
    assert len(pdftools.render_pages(tmp_path / "locked.pdf", tmp_path / "y", "jpeg", 50, "s3cret")) == 2
    pdftools.unlock(tmp_path / "locked.pdf", tmp_path / "open.pdf", "s3cret")
    assert not pdftools.is_encrypted(tmp_path / "open.pdf")
    assert [p.name for p in pdftools.split(tmp_path / "a.pdf", tmp_path / "split")] == ["page 001.pdf",
                                                                                      "page 002.pdf"]


def test_convert_pdfs_and_undo(tmp_path):
    _pictures(tmp_path)
    pdftools.combine([tmp_path / "alpha.png", tmp_path / "cmyk.jpg"], tmp_path / "report.pdf")
    for p in ("side.jpg", "alpha.png", "cmyk.jpg"):
        (tmp_path / p).unlink()
    s = Settings(output_mode="beside", convert_inputs=True)
    entry = next(e for e in scan(tmp_path, s).entries if e.path.name == "report.pdf")
    assert entry.kind == "pdf"
    opts = convert.ConvertOptions(pdf_target="png", pdf_dpi=72)
    results, run_id = run_job([(entry, Plan(True, convert.describe("pdf", opts), "", route="png"))], s, tmp_path,
                              processor=lambda *a: process_convert(*a, opts=opts), tool="convert")
    r = results[0]
    assert r.status == DONE and r.output == tmp_path / "report pages" and "2 files" in r.message
    assert sorted(p.name for p in r.output.iterdir()) == ["page 001.png", "page 002.png"]
    (r.output / "my notes.txt").write_text("mine")
    undo_run(run_id)
    assert sorted(p.name for p in (tmp_path / "report pages").iterdir()) == ["my notes.txt"]   # yours stays
    for target in ("rotate90", "protect"):
        opts = convert.ConvertOptions(pdf_target=target, pdf_password="pw")
        r = process_convert(entry, Plan(True, "", ".pdf", route=target), s, None, opts=opts)
        assert r.status == DONE and r.output.suffix == ".pdf" and r.output != entry.path, r.message
    Image.new("RGB", (64, 48), "teal").save(tmp_path / "pic.png")
    pic = next(e for e in scan(tmp_path, s).entries if e.path.name == "pic.png")
    r = process_convert(pic, Plan(True, "", ".pdf", route="pdf"), s, None,
                        opts=convert.ConvertOptions(image_target="pdf"))
    assert r.status == DONE and pdftools.page_count(r.output) == 1


def test_undo_of_pages_made_in_replace_mode_removes_the_folder(tmp_path):
    """PDF → page images with Replace: undoing brings the PDF back from the bin and removes the page folder."""
    import time

    from compressor import journal
    from compressor.engine import undo_record
    from compressor.fileops import move_to_trash

    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4 stand-in")
    pages = tmp_path / "report pages"
    pages.mkdir()
    (pages / "page 1.png").write_bytes(b"x")
    trashed_at = time.time()
    move_to_trash(pdf)
    writer = journal.RunWriter(tmp_path, {})
    rec = journal.FileRecord(original=str(pdf), output=str(pages), original_size=17, output_size=1, kind="pdf",
                             method="PDF → PNG", reversible=False, trashed_at=trashed_at)
    writer.add(rec)
    writer.close()
    ok, msg = undo_record(writer.run.run_id, rec)
    assert ok, msg
    assert pdf.exists() and not pages.exists()
