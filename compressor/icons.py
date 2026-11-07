"""Icons for websites and apps: .ico files with several sizes, a website's icon set, and smaller SVGs.

An .ico holds the same picture at several sizes so each place shows a sharp one (a browser tab uses 16 or 32
pixels, a Windows shortcut 48, a high-DPI taskbar more). Each size is drawn from the source on its own (an SVG
is drawn at that size; a photo is scaled down with a Lanczos filter) and stored as a PNG made as small as
possible (oxipng): every browser and Windows since Vista read PNG icons.

Website icon set, as recommended in "How to Favicon in 2024" (Evil Martians) and by browser makers: favicon.ico
(16, 32 and 48), icon.svg (when the source is a drawing), apple-touch-icon.png (180, on a solid background:
iOS shows transparency as black), icon-192.png and icon-512.png for the web app manifest, which is written too.

SVG optimisation removes only what can't change how the drawing looks: comments, editor data (Inkscape,
Illustrator, Sketch...), metadata, unused IDs and namespaces, layout whitespace, and digits beyond what the
drawing's size can show. The result is drawn and compared with the original before it's used.
"""

from __future__ import annotations

import io
import json
import math
import re
import struct
from pathlib import Path
from typing import Any

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
ICO_PRESETS = {
    "web": ("Website (16, 32, 48)", (16, 32, 48)),
    "mediawiki": ("MediaWiki (16, 32, 48, 64)", (16, 32, 48, 64)),
    "windows": ("Windows app (16 to 256)", (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)),
}


def preset_of(sizes: tuple[int, ...]) -> str:
    """The preset with exactly these sizes, or "custom"."""
    return next((k for k, (_label, s) in ICO_PRESETS.items() if tuple(sorted(sizes)) == s), "custom")


# ---------------------------------------------------------------- drawing at a size

def _square(img, size: int):
    """``img`` scaled to fit ``size``×``size``, centred on a transparent square."""
    from PIL import Image

    img = img.convert("RGBA")
    if img.size != (size, size):
        scale = size / max(img.size)
        w, h = max(1, round(img.width * scale)), max(1, round(img.height * scale))
        img = img.resize((w, h), Image.LANCZOS, reducing_gap=3.0)
        if (w, h) != (size, size):
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            canvas.paste(img, ((size - w) // 2, (size - h) // 2))
            img = canvas
    return img


def draw_svg(path_or_data: Path | bytes, size: int):
    """An SVG drawn to fit ``size``×``size`` (centred, transparent around it) as a Pillow RGBA image."""
    from PIL import Image
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    r = QSvgRenderer(QByteArray(path_or_data)) if isinstance(path_or_data, bytes) else QSvgRenderer(str(path_or_data))
    if not r.isValid():
        raise ValueError("The SVG can't be read")
    box = r.viewBoxF()
    w, h = (box.width(), box.height()) if box.width() > 0 else (r.defaultSize().width(), r.defaultSize().height())
    scale = size / max(w, h, 1e-9)
    dw, dh = w * scale, h * scale
    image = QImage(size, size, QImage.Format_RGBA8888)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    r.render(painter, QRectF((size - dw) / 2, (size - dh) / 2, dw, dh))
    painter.end()
    return Image.frombuffer("RGBA", (size, size), bytes(image.constBits()), "raw", "RGBA",
                            image.bytesPerLine(), 1).copy()


def source_at(src: Path, size: int, picture=None):
    """The source drawn at ``size`` (SVG) or scaled to it (anything Pillow opens, or ``picture``)."""
    if src.suffix.lower() == ".svg":
        return draw_svg(src, size)
    if picture is None:
        picture = load_picture(src)
    return _square(picture, size)


def load_picture(src: Path):
    """The largest picture in the file (an icon's biggest size, a photo turned upright)."""
    from PIL import Image, ImageOps

    with Image.open(src) as im:               # (an .ico opens on its largest size)
        im.load()
        return (ImageOps.exif_transpose(im) or im).convert("RGBA")


def png_bytes(img) -> bytes:
    """A small PNG of ``img`` (lossless: oxipng)."""
    buf = io.BytesIO()
    img.save(buf, "PNG")
    data = buf.getvalue()
    try:
        import oxipng

        return oxipng.optimize_from_memory(data, level=4, strip=oxipng.StripChunks.safe())
    except Exception:  # noqa: BLE001 - an unoptimised PNG is still right
        return data


# ---------------------------------------------------------------- .ico

def ico_bytes(images: dict[int, object]) -> bytes:
    """An .ico from {size: RGBA image}; every entry a PNG (smallest first, as Windows expects)."""
    sizes = sorted(images)
    blobs = [png_bytes(images[s]) for s in sizes]
    out = io.BytesIO()
    out.write(struct.pack("<HHH", 0, 1, len(sizes)))
    offset = 6 + 16 * len(sizes)
    for s, blob in zip(sizes, blobs, strict=True):
        b = 0 if s >= 256 else s                    # (0 means 256)
        out.write(struct.pack("<BBBBHHII", b, b, 0, 0, 1, 32, len(blob), offset))
        offset += len(blob)
    for blob in blobs:
        out.write(blob)
    return out.getvalue()


def make_ico(src: Path, sizes: tuple[int, ...], dst: Path) -> list[int]:
    """Write an .ico of ``src`` at ``sizes`` (a picture isn't enlarged past its own size: those sizes are left
    out, unless that leaves none). Returns the sizes written."""
    picture = None if src.suffix.lower() == ".svg" else load_picture(src)
    sizes = tuple(sorted(set(sizes))) or ICO_PRESETS["web"][1]
    if picture is not None:
        sizes = tuple(s for s in sizes if s <= max(picture.size)) or sizes[:1]
    dst.write_bytes(ico_bytes({s: source_at(src, s, picture) for s in sizes}))
    return list(sizes)


ICNS_SIZES = (16, 32, 64, 128, 256, 512, 1024)     # (the Retina sizes are the next size up, drawn sharp)
CURSOR_SIZES = (32, 48, 64)                         # (standard, and for high-DPI screens)


def make_icns(src: Path, dst: Path) -> list[int]:
    """A macOS icon (.icns) of ``src``: each size drawn on its own, as for .ico."""
    picture = None if src.suffix.lower() == ".svg" else load_picture(src)
    images = {s: source_at(src, s, picture) for s in ICNS_SIZES}
    images[1024].save(dst, "ICNS", append_images=[images[s] for s in ICNS_SIZES if s != 1024])
    return list(ICNS_SIZES)


def make_cur(src: Path, dst: Path, hotspot: tuple[int, int] = (0, 0)) -> list[int]:
    """A Windows cursor (.cur): like an .ico (32, 48 and 64 pixels, not enlarged past the picture), with the
    point that clicks (the hotspot) at the top left, where arrow pointers have it."""
    picture = None if src.suffix.lower() == ".svg" else load_picture(src)
    sizes = tuple(s for s in CURSOR_SIZES if picture is None or s <= max(picture.size)) or CURSOR_SIZES[:1]
    sizes = tuple(sorted(sizes))
    blobs = [_dib(source_at(src, s, picture)) for s in sizes]
    out = io.BytesIO()
    out.write(struct.pack("<HHH", 0, 2, len(sizes)))         # type 2: cursor
    offset = 6 + 16 * len(sizes)
    for s, blob in zip(sizes, blobs, strict=True):
        b = 0 if s >= 256 else s
        # (in a cursor, the entry's planes and bit count fields hold the hotspot)
        out.write(struct.pack("<BBBBHHII", b, b, 0, 0, round(hotspot[0] * s), round(hotspot[1] * s), len(blob),
                              offset))
        offset += len(blob)
    for blob in blobs:
        out.write(blob)
    dst.write_bytes(out.getvalue())
    return list(sizes)


def _dib(img) -> bytes:
    """A 32-bit bitmap as icons and cursors store it: BGRA rows bottom-up, then an all-clear AND mask (the
    alpha channel does the masking)."""
    w, h = img.size
    rgba = img.convert("RGBA")
    r, g, b, a = rgba.split()
    from PIL import Image

    rows = Image.merge("RGBA", (b, g, r, a)).transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes()
    mask = bytes(((w + 31) // 32) * 4 * h)
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(rows) + len(mask), 0, 0, 0, 0)
    return header + rows + mask


def ico_sizes(path: Path) -> list[int]:
    """The sizes stored in an .ico."""
    data = path.read_bytes()
    _r, kind, n = struct.unpack_from("<HHH", data, 0)
    return sorted((data[6 + 16 * i] or 256) for i in range(n)) if kind in (1, 2) else []


# ---------------------------------------------------------------- website icon set

WEB_FILES = ("favicon.ico", "icon.svg", "apple-touch-icon.png", "icon-192.png", "icon-512.png", "site.webmanifest")


def web_icons(src: Path, dest: Path, background: tuple[int, int, int] = (255, 255, 255)) -> list[Path]:
    """Write a website's icon set for ``src`` into ``dest``. Returns the files written."""
    from PIL import Image

    dest.mkdir(parents=True, exist_ok=True)
    is_svg = src.suffix.lower() == ".svg"
    picture = None if is_svg else load_picture(src)
    made = []

    def write(name: str, data: bytes) -> None:
        p = dest / name
        p.write_bytes(data)
        made.append(p)

    write("favicon.ico", ico_bytes({s: source_at(src, s, picture) for s in (16, 32, 48)}))
    if is_svg:
        write("icon.svg", optimize_svg(src.read_bytes()))
    # iOS shows transparency as black and rounds the corners itself: a solid square with a little margin.
    touch = Image.new("RGBA", (180, 180), background + (255,))
    inner = source_at(src, 140, picture)
    touch.alpha_composite(inner, (20, 20))
    write("apple-touch-icon.png", png_bytes(touch.convert("RGB")))
    for s in (192, 512):
        write(f"icon-{s}.png", png_bytes(source_at(src, s, picture)))
    manifest = {"icons": [{"src": f"/icon-{s}.png", "type": "image/png", "sizes": f"{s}x{s}"} for s in (192, 512)]}
    write("site.webmanifest", (json.dumps(manifest, indent=2) + "\n").encode())
    return made


def web_snippet(has_svg: bool) -> str:
    """The lines for the page's <head>."""
    lines = ['<link rel="icon" href="/favicon.ico" sizes="32x32">']
    if has_svg:
        lines.append('<link rel="icon" href="/icon.svg" type="image/svg+xml">')
    lines += ['<link rel="apple-touch-icon" href="/apple-touch-icon.png">',
              '<link rel="manifest" href="/site.webmanifest">']
    return "\n".join(lines)


# ---------------------------------------------------------------- SVG optimisation

_EDITOR_NS = (
    "http://www.inkscape.org/namespaces/inkscape", "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd",
    "http://ns.adobe.com/AdobeIllustrator/10.0/", "http://ns.adobe.com/Extensibility/1.0/",
    "http://ns.adobe.com/Graphs/1.0/", "http://ns.adobe.com/SaveForWeb/1.0/", "http://ns.adobe.com/Variables/1.0/",
    "http://ns.adobe.com/ImageReplacement/1.0/", "http://ns.adobe.com/GenericCustomNamespace/1.0/",
    "http://ns.adobe.com/XPath/1.0/", "http://www.bohemiancoding.com/sketch/ns", "http://www.serif.com/",
    "http://www.vectornator.io", "http://creativecommons.org/ns#", "http://purl.org/dc/elements/1.1/",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
)
_SVG = "http://www.w3.org/2000/svg"
_XLINK = "http://www.w3.org/1999/xlink"
_TEXT_ELEMENTS = {"text", "tspan", "textPath", "style", "script", "title", "desc", "foreignObject"}
_NUMERIC_ATTRS = {"d", "points", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height",
                  "transform", "stroke-width", "fx", "fy", "offset", "gradientTransform", "patternTransform", "viewBox"}
_NUMBER = re.compile(r"-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


def _decimals(size: float) -> int:
    """Digits after the point that still matter for a drawing this big (1/10,000 of its size)."""
    return max(0, min(6, math.ceil(4 - math.log10(max(size, 1e-6)))))


def _round_numbers(text: str, decimals: int) -> str:
    def fix(m: re.Match) -> str:
        s = m.group(0)
        if "e" in s or "E" in s:
            return s
        v = round(float(s), decimals)
        out = f"{v:.{decimals}f}".rstrip("0").rstrip(".") if decimals else str(int(v))
        if out in ("-0", ""):
            out = "0"
        if out.startswith("0.") and len(out) > 2:
            out = out[1:]                       # 0.5 -> .5
        elif out.startswith("-0.") and len(out) > 3:
            out = "-" + out[2:]
        return out
    return _NUMBER.sub(fix, text)


def optimize_svg(data: bytes) -> bytes:
    """A smaller SVG that looks the same (see the module's notes). Raises ValueError for one it can't read."""
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True, remove_pis=True,
                             huge_tree=False, recover=False)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as e:
        raise ValueError(f"The SVG can't be read ({e})") from e
    if etree.QName(root).localname != "svg":
        raise ValueError("Not an SVG drawing")

    # Editor data: elements and attributes in editors' namespaces, and <metadata>.
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        q = etree.QName(el)
        if q.namespace in _EDITOR_NS or (q.namespace == _SVG and q.localname == "metadata"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
            continue
        for attr in list(el.attrib):
            if etree.QName(attr).namespace in _EDITOR_NS:
                del el.attrib[attr]

    # IDs nothing refers to (kept when a stylesheet or script might use them).
    has_style_or_script = any(isinstance(el.tag, str) and etree.QName(el).localname in ("style", "script")
                              for el in root.iter())
    if not has_style_or_script:
        text = etree.tostring(root).decode("utf-8", "replace")
        used = set(re.findall(r"#([A-Za-z_][\w.:-]*)", text))
        used |= set(re.findall(r"(?:begin|end)=\"([A-Za-z_][\w.:-]*)\.", text))
        for el in root.iter():
            if isinstance(el.tag, str) and el.get("id") is not None and el.get("id") not in used:
                del el.attrib["id"]

    # Numbers: as precise as the drawing's size needs.
    vb = (root.get("viewBox") or "").replace(",", " ").split()
    try:
        size = max(float(vb[2]), float(vb[3])) if len(vb) == 4 else \
            max(float(re.sub(r"[a-z%]+$", "", root.get("width", "100"))),
                float(re.sub(r"[a-z%]+$", "", root.get("height", "100"))))
    except ValueError:
        size = 100.0
    decimals = _decimals(size)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            name = etree.QName(attr).localname
            if name in _NUMERIC_ATTRS:
                # (a transform's scale multiplies everything: it keeps more digits; offsets run from 0 to 1)
                d = min(8, decimals + 3) if name.endswith(("transform", "Transform")) else \
                    max(decimals, 3) if name == "offset" else decimals
                el.attrib[attr] = _round_numbers(el.attrib[attr], d)

    # Layout whitespace between tags (not inside text, styles or xml:space="preserve").
    def keep_space(el) -> bool:
        while el is not None:
            if el.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve" or \
                    etree.QName(el).localname in _TEXT_ELEMENTS:
                return True
            el = el.getparent()
        return False

    for el in root.iter():
        if not isinstance(el.tag, str) or keep_space(el):
            continue
        if el.text is not None and not el.text.strip():
            el.text = None
        for child in el:
            if child.tail is not None and not child.tail.strip():
                child.tail = None

    etree.cleanup_namespaces(root, top_nsmap={None: _SVG, "xlink": _XLINK} if root.nsmap.get("xlink") else None)
    return etree.tostring(root, encoding="utf-8", xml_declaration=False)


def looks_same(a: bytes, b: bytes, size: int = 256) -> bool:
    """Both SVGs drawn at ``size`` match (no pixel off by more than a rounding step)."""
    from PIL import ImageChops

    x, y = draw_svg(a, size), draw_svg(b, size)
    diff: Any = ImageChops.difference(x, y).getextrema()      # ((low, high) per channel)
    return all(hi <= 8 for _lo, hi in diff)


__all__ = ["ICO_PRESETS", "ICO_SIZES", "make_ico", "ico_bytes", "ico_sizes", "web_icons", "web_snippet",
           "optimize_svg", "looks_same", "draw_svg", "preset_of", "WEB_FILES"]
