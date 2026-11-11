"""What Wick is built from: components, what they do here, and their licenses."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Credit:
    name: str
    purpose: str
    license: str
    url: str
    bundled: str = "library"   # library (inside the app) | tool (separate program in tools/) | code (adapted)
    #                            | download (a separate program, downloaded when first needed: addons.py)


CREDITS: tuple[Credit, ...] = (
    Credit("Python", "Runs the app", "PSF License", "https://www.python.org/"),
    Credit("Qt / PySide6", "User interface", "LGPL-3.0", "https://www.qt.io/qt-for-python"),
    Credit("Pillow", "Reading and writing images", "MIT-CMU (HPND)", "https://python-pillow.org/"),
    Credit("pillow-heif / libheif", "HEIC and HEIF photos", "BSD-3-Clause / LGPL-3.0",
           "https://github.com/bigcat88/pillow_heif"),
    Credit("mozjpeg-lossless-optimization", "Lossless JPEG optimization (mozjpeg)", "BSD-3-Clause / IJG",
           "https://github.com/wanadev/mozjpeg-lossless-optimization"),
    Credit("libimagequant", "Lossy PNG (color quantisation)", "GPL-3.0 or commercial",
           "https://pngquant.org/lib/"),
    Credit("oxipng / pyoxipng", "Lossless PNG optimization", "MIT", "https://github.com/shssoichiro/oxipng"),
    Credit("NumPy", "Image quality measurement", "BSD-3-Clause", "https://numpy.org/"),
    Credit("py7zr", "Checking 7-Zip archives", "LGPL-2.1-or-later", "https://github.com/miurahr/py7zr"),
    Credit("pikepdf", "Optimizing PDFs", "MPL-2.0", "https://github.com/pikepdf/pikepdf"),
    Credit("qpdf", "Rewriting PDFs losslessly (inside pikepdf)", "Apache-2.0", "https://qpdf.sourceforge.io/"),
    Credit("lxml", "Reading PDF metadata (used by pikepdf)", "BSD-3-Clause", "https://lxml.de/"),
    Credit("Send2Trash", "Moving files to the Recycle Bin", "BSD-3-Clause", "https://github.com/arsenetar/send2trash"),
    Credit("ssimulacra2 (Python)", "Perceptual image quality (Smart quality)", "BSD-3-Clause",
           "https://github.com/hwanseung2/ssimulacra2", "code"),
    Credit("FFmpeg", "Video and audio: decoding, encoding, measuring", "GPL-3.0 (this build)",
           "https://ffmpeg.org/", "tool"),
    Credit("x265, SVT-AV1, libopus, LAME, libvmaf", "Encoders and VMAF inside FFmpeg (Gyan.dev \"full\" build)",
           "GPL-2.0+ / BSD / BSD / LGPL / BSD", "https://www.gyan.dev/ffmpeg/builds/", "tool"),
    Credit("libjxl (cjxl, djxl, cjpegli, ssimulacra2, butteraugli)", "JPEG XL, jpegli, SSIMULACRA2 and Butteraugli",
           "BSD-3-Clause", "https://github.com/libjxl/libjxl", "tool"),
    Credit("gifsicle", "GIF optimization", "GPL-2.0", "https://www.lcdf.org/gifsicle/", "tool"),
    Credit("rawpy / LibRaw", "Reading camera RAW photos", "MIT / LGPL-2.1 or CDDL-1.0",
           "https://github.com/letmaik/rawpy"),
    Credit("Pandoc", "Converting documents", "GPL-2.0-or-later", "https://pandoc.org/", "download"),
    Credit("fontTools", "Converting fonts (WOFF2, Latin-only)", "MIT", "https://github.com/fonttools/fonttools"),
    Credit("Brotli", "WOFF2 compression", "MIT", "https://github.com/google/brotli"),
)

PRIVACY = ("Wick works entirely on this computer. It has no accounts, no telemetry and no ads, and it never "
           "sends your files or anything about them anywhere. It goes online only to check for a newer version "
           "(weekly by default; Settings → About) and, when you ask for it, to download Pandoc for "
           "converting documents.")

LICENSE_NOTE = ("Wick is free software, © Lanntern, licensed under the GNU GPL version 3 (LICENSE), with "
                "NO WARRANTY. Encoders in the tools folder are separate, unmodified programs that Wick runs; "
                "their license files are next to them. Where to get the source code of every GPL component: "
                "THIRD-PARTY-NOTICES.md.")


PRIVACY_URL = "https://github.com/Lanntern/Wick/blob/main/PRIVACY.md"
DONATE_URL = "https://ko-fi.com/lanntern"     # Wick is free; donations are welcome, never required


__all__ = ["Credit", "CREDITS", "PRIVACY", "PRIVACY_URL", "LICENSE_NOTE", "DONATE_URL"]
