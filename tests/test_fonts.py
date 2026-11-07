"""Fonts: TrueType/OpenType ↔ WOFF/WOFF2, and Latin-only subsets."""

from fontTools.ttLib import TTFont

from compressor import convert, fonts
from compressor.engine import DONE, process_convert
from compressor.plan import Plan
from compressor.scanner import scan
from compressor.settings import Settings


def _font(path, chars="ABCabc", extra=(0x0416, 0x4E2D)):
    """A tiny TrueType font with a square for each character (some Latin, plus Cyrillic Zhe and a CJK one)."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    codes = [ord(c) for c in chars] + list(extra)
    names = [".notdef"] + [f"g{c:04X}" for c in codes]
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({c: f"g{c:04X}" for c in codes})
    glyphs = {}
    for n in names:
        pen = TTGlyphPen(None)
        pen.moveTo((100, 0))
        for point in ((100, 700), (600, 700), (600, 0)):
            pen.lineTo(point)
        pen.closePath()
        glyphs[n] = pen.glyph()
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({n: (700, 100) for n in names})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "Test", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    fb.save(str(path))


def test_fonts_for_the_web_and_back(tmp_path):
    _font(tmp_path / "Test.ttf")
    fonts.convert(tmp_path / "Test.ttf", tmp_path / "t.woff2", "woff2")
    with TTFont(tmp_path / "t.woff2") as f:
        assert f.flavor == "woff2" and len(f.getGlyphOrder()) == 9
    assert fonts.target_ext(tmp_path / "t.woff2", "desktop") == ".ttf"
    fonts.convert(tmp_path / "t.woff2", tmp_path / "back.ttf", "desktop")
    with TTFont(tmp_path / "back.ttf") as f:
        assert f.flavor is None and 0x4E2D in f.getBestCmap()
    fonts.convert(tmp_path / "Test.ttf", tmp_path / "latin.woff2", "woff2", latin_only=True)
    with TTFont(tmp_path / "latin.woff2") as f:
        cmap = f.getBestCmap()
        assert ord("A") in cmap and 0x0416 not in cmap and 0x4E2D not in cmap


def test_convert_lists_fonts(tmp_path):
    _font(tmp_path / "Test.ttf")
    s = Settings(output_mode="beside", convert_inputs=True)
    entry = scan(tmp_path, s).entries[0]
    assert entry.kind == "font"
    opts = convert.ConvertOptions(font_target="woff2", font_latin=True)
    assert convert.describe("font", opts) == "WOFF2, Latin only"
    r = process_convert(entry, Plan(True, "", ".woff2", route="woff2"), s, None, opts=opts)
    assert r.status == DONE and r.output == tmp_path / "Test.woff2"
