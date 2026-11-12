"""Icons with several sizes, website icon sets and smaller SVGs."""


import pytest
from PIL import Image

from wick import convert, icons
from wick.engine import DONE, process_convert
from wick.plan import Plan
from wick.scanner import scan
from wick.settings import Settings

INKSCAPE = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!-- Created with Inkscape -->
<svg width="64" height="64" viewBox="0 0 64.000000 64.000000" id="svg5"
   inkscape:version="1.3" sodipodi:docname="logo.svg"
   xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
   xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
   xmlns:xlink="http://www.w3.org/1999/xlink" xmlns="http://www.w3.org/2000/svg"
   xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <sodipodi:namedview id="namedview7" pagecolor="#ffffff" inkscape:zoom="2.2" />
  <defs id="defs2">
    <linearGradient id="grad1"><stop style="stop-color:#0a84ff" offset="0.000000" id="stop1" />
      <stop style="stop-color:#5e5ce6" offset="1.0000000" id="stop2" /></linearGradient>
    <linearGradient xlink:href="#grad1" id="grad2" x1="2.3456789" y1="3.1234567" x2="60.987654" y2="61.111111"
       gradientUnits="userSpaceOnUse" />
  </defs>
  <metadata id="metadata5"><rdf:RDF><dc:title>Logo</dc:title></rdf:RDF></metadata>
  <g inkscape:label="Layer 1" id="layer1"
     transform="matrix(0.70710678,0.70710678,-0.70710678,0.70710678,32.000000,-13.254834)">
    <rect style="fill:url(#grad2)" id="rect123" width="40.123456789" height="40.123456789" x="11.938271"
       y="11.938271" rx="6.0000001" />
    <path d="M 20.000001,32.000001 L 32.333333,20.666667 L 44.000000,32.000000 Z" style="fill:#fff" id="p1"
       sodipodi:nodetypes="cccc" />
    <text x="20" y="50" xml:space="preserve" style="font-size:6px">  Hi  there </text>
  </g>
</svg>
"""


@pytest.fixture(scope="module", autouse=True)
def qt():
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance() or QGuiApplication([])


def test_svgs_get_smaller_and_look_the_same():
    data = INKSCAPE.encode()
    small = icons.optimize_svg(data)
    text = small.decode()
    assert len(small) < len(data) * 0.6
    for gone in ("inkscape", "sodipodi", "metadata", "<!--", "<?xml", 'id="rect123"', "rdf"):
        assert gone not in text
    assert 'id="grad1"' in text and 'id="grad2"' in text                # (referred to: kept)
    assert "  Hi  there " in text                                       # text keeps its spaces
    assert 'x1="2.346"' in text and "matrix(.707107," in text           # digits the size can show
    assert icons.looks_same(data, small)


def test_svg_optimizer_reads_nothing_outside_the_file(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    evil = (f'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x SYSTEM "{secret.as_uri()}">]>'
            '<svg xmlns="http://www.w3.org/2000/svg"><text>&x;</text></svg>').encode()
    assert b"TOP SECRET" not in icons.optimize_svg(evil)
    with pytest.raises(ValueError):
        icons.optimize_svg(b"<html><body/></html>")


def test_icons_hold_each_size(tmp_path):
    (tmp_path / "logo.svg").write_text(INKSCAPE)
    Image.radial_gradient("L").convert("RGB").resize((300, 200)).save(tmp_path / "photo.jpg")
    Image.new("RGB", (40, 40), "red").save(tmp_path / "small.png")
    assert icons.make_ico(tmp_path / "logo.svg", (16, 32, 48, 256), tmp_path / "a.ico") == [16, 32, 48, 256]
    assert icons.ico_sizes(tmp_path / "a.ico") == [16, 32, 48, 256]
    with Image.open(tmp_path / "a.ico") as im:
        assert im.size == (256, 256)
    assert icons.make_ico(tmp_path / "photo.jpg", (16, 256), tmp_path / "b.ico") == [16, 256]
    with Image.open(tmp_path / "b.ico") as im:
        im.size = (16, 16)
        small = im.convert("RGBA")                                         # (oxipng may store a palette)
        assert small.getpixel((8, 0))[3] == 0 and small.getpixel((8, 8))[3] == 255   # not square: centered
    assert icons.make_ico(tmp_path / "small.png", (16, 32, 64, 256), tmp_path / "c.ico") == [16, 32]  # not enlarged
    assert icons.preset_of((48, 16, 32)) == "web" and icons.preset_of((16, 64)) == "custom"


def test_convert_makes_icons_and_optimized_svgs(tmp_path):
    (tmp_path / "logo.svg").write_text(INKSCAPE)
    Image.new("RGB", (64, 64), "teal").save(tmp_path / "pic.png")
    s = Settings(output_mode="beside", min_image_kb=0, convert_inputs=True)
    entries = {e.path.name: e for e in scan(tmp_path, s).entries}
    opts = convert.ConvertOptions(only_if_smaller=False, image_target="ico", ico_sizes=(16, 32, 48, 64))
    assert convert.describe("png", opts) == "ICO, MediaWiki sizes"
    for name in ("logo.svg", "pic.png"):
        r = process_convert(entries[name], Plan(True, "", ".ico", route="ico"), s, None, opts=opts)
        assert r.status == DONE, r.message
        assert icons.ico_sizes(r.output) == [16, 32, 48, 64]
    opts = convert.ConvertOptions(only_if_smaller=False, image_target="svg")
    r = process_convert(entries["logo.svg"], Plan(True, "", ".svg", route="svg"), s, None, opts=opts)
    assert r.status == DONE and r.output != entries["logo.svg"].path and r.new_size < entries["logo.svg"].size


def test_website_icon_set(tmp_path):
    (tmp_path / "logo.svg").write_text(INKSCAPE)
    made = icons.web_icons(tmp_path / "logo.svg", tmp_path / "out")
    assert sorted(p.name for p in made) == sorted(icons.WEB_FILES)
    assert icons.ico_sizes(tmp_path / "out" / "favicon.ico") == [16, 32, 48]
    with Image.open(tmp_path / "out" / "apple-touch-icon.png") as im:
        assert im.size == (180, 180) and im.mode == "RGB"                 # iOS: no transparency
    with Image.open(tmp_path / "out" / "icon-512.png") as im:
        assert im.size == (512, 512)
    assert '"sizes": "192x192"' in (tmp_path / "out" / "site.webmanifest").read_text()
    assert 'href="/icon.svg"' in icons.web_snippet(True)
    Image.new("RGB", (64, 64), "teal").save(tmp_path / "pic.png")
    made = icons.web_icons(tmp_path / "pic.png", tmp_path / "out2")
    assert "icon.svg" not in {p.name for p in made}


def test_mac_icons_and_cursors(tmp_path):
    (tmp_path / "logo.svg").write_text(INKSCAPE)
    icons.make_icns(tmp_path / "logo.svg", tmp_path / "logo.icns")
    with Image.open(tmp_path / "logo.icns") as im:
        sizes = {s[0] * s[2] for s in im.info["sizes"]}
        assert {32, 64, 128, 256, 512, 1024} <= sizes          # (16 is stored as 16@2x, as macOS uses it)
    Image.new("RGBA", (40, 40), (255, 0, 0, 255)).save(tmp_path / "small.png")
    assert icons.make_cur(tmp_path / "small.png", tmp_path / "arrow.cur") == [32]      # (not enlarged)
    data = (tmp_path / "arrow.cur").read_bytes()
    assert data[2:4] == b"\x02\x00" and icons.ico_sizes(tmp_path / "arrow.cur") == [32]
    with Image.open(tmp_path / "arrow.cur") as im:                                 # Pillow reads it as a cursor
        assert im.format == "CUR" and im.size == (32, 32)


def test_other_pictures_and_comic_books(tmp_path):
    import zipfile

    import pikepdf

    Image.new("RGB", (30, 20), "red").save(tmp_path / "game.tga")
    Image.new("RGB", (30, 20), "blue").save(tmp_path / "pixels.qoi")
    def page(width: int) -> bytes:
        buf = __import__("io").BytesIO()
        Image.new("RGB", (width, 60), "white").save(buf, "JPEG")
        return buf.getvalue()

    with zipfile.ZipFile(tmp_path / "story.cbz", "w") as z:
        for n in (10, 2, 1):                               # (stored out of order: read naturally)
            z.writestr(f"pages/{n}.jpg", page(40 + n))
        z.writestr("../../evil.jpg", page(45))             # (a name pointing outside: never used as a path)
    s = Settings(output_mode="beside", min_image_kb=0, convert_inputs=True)
    entries = {e.path.name: e for e in scan(tmp_path, s).entries}
    assert entries["game.tga"].kind == "picture" and entries["story.cbz"].kind == "comic"
    for name in ("game.tga", "pixels.qoi"):
        r = process_convert(entries[name], Plan(True, "", ".png", route="png"), s, None,
                            opts=convert.ConvertOptions(only_if_smaller=False, image_target="png"))
        assert r.status == DONE, r.message
    r = process_convert(entries["story.cbz"], Plan(True, "", ".pdf", route="pdf"), s, None,
                        opts=convert.ConvertOptions(only_if_smaller=False, comic_target="pdf"))
    assert r.status == DONE, r.message
    with pikepdf.open(r.output) as pdf:
        assert [int(p.Resources.XObject.Im0.Width) for p in pdf.pages] == [45, 41, 42, 50]   # natural order
    assert not (tmp_path.parent / "evil.jpg").exists()

