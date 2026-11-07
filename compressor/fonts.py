"""Fonts: TrueType and OpenType to the web's WOFF2 (or WOFF) and back, optionally only the characters most
websites use.

WOFF2 is the same font, compressed with Brotli and a transform made for fonts (usually 30–50% of a TTF). Going
back gives a .ttf or .otf, whichever the font's outlines are (TrueType or PostScript). "Latin only" keeps the
characters of English and most European languages (and the punctuation, currency signs and symbols around
them) and drops the rest, which can make a font with many scripts a fraction of its size. Every result is
read back and checked.
"""

from __future__ import annotations

from pathlib import Path

FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2"}
# Latin: Basic Latin, Latin-1, Latin Extended-A, common punctuation, super/subscripts, currency, letterlike
# symbols, arrows' neighbours that text uses (U+2000–U+206F, U+2070–U+20CF, U+2100–U+214F), and the
# replacement character.
LATIN = [*range(0x20, 0x7F), *range(0xA0, 0x180), *range(0x2000, 0x2070), *range(0x2070, 0x20D0),
         *range(0x2100, 0x2150), 0x0131, 0x0152, 0x0153, 0x02C6, 0x02DA, 0x02DC, 0xFEFF, 0xFFFD]


def outline_ext(font) -> str:
    """.otf for PostScript (CFF) outlines, .ttf for TrueType ones."""
    return ".otf" if "CFF " in font or "CFF2" in font else ".ttf"


def target_ext(src: Path, target: str) -> str:
    if target in ("woff2", "woff"):
        return "." + target
    from fontTools.ttLib import TTFont

    with TTFont(src, lazy=True) as font:
        return outline_ext(font)


def convert(src: Path, dest: Path, target: str, latin_only: bool = False) -> None:
    """``src`` as ``target`` ("woff2", "woff" or "desktop": its own .ttf/.otf) at ``dest``."""
    from fontTools import subset
    from fontTools.ttLib import TTFont

    from .images import VerificationFailed

    font = TTFont(src)
    glyphs = len(font.getGlyphOrder())
    if latin_only:
        options = subset.Options()
        options.layout_features = ["*"]           # (keep kerning, ligatures and the rest for what stays)
        options.name_IDs = ["*"]
        options.name_languages = ["*"]
        options.notdef_outline = True
        options.glyph_names = True
        options.hinting = True
        sub = subset.Subsetter(options)
        sub.populate(unicodes=LATIN)
        sub.subset(font)
    font.flavor = None if target == "desktop" else target
    font.save(dest)
    font.close()
    try:
        with TTFont(dest) as back:
            got = len(back.getGlyphOrder())
            if (back.flavor or "desktop") != target or not back.getBestCmap():
                raise VerificationFailed("The font can't be read back")
    except VerificationFailed:
        raise
    except Exception as e:  # noqa: BLE001
        raise VerificationFailed(f"The font can't be read back ({e})") from e
    if not latin_only and got != glyphs:
        raise VerificationFailed(f"The font has {got} glyphs instead of {glyphs}")


__all__ = ["FONT_EXTS", "convert", "target_ext", "outline_ext", "LATIN"]
