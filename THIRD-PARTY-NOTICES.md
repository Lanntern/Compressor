# Third-party notices

Compressor is © Lanntern and licensed under the GNU General Public License, version 3 only (see `LICENSE`).
It is built from, and ships with, the components below. Each keeps its own licence; licence texts for the
command-line tools are in the `tools` folder next to `Compressor.exe` (`LICENSE-*.txt`).

The exact versions in a release are pinned in that release's source: `scripts/fetch_tools.py` (command-line
tools, each checked by SHA-256) and `requirements.txt` (Python libraries, each checked by hash). In the app,
Settings → About → About Compressor → **Copy Versions** lists the versions actually installed.

## Command-line tools (separate, unmodified programs in `tools/`)

Compressor runs these as separate programs; it does not link to them.

| Tool | Licence | Binary from | Source code |
|---|---|---|---|
| FFmpeg, ffprobe (with x265, SVT-AV1, libopus, LAME, libvmaf and the other libraries of the build) | GPL-3.0 (this build; the libraries inside are GPL-2.0+, LGPL or BSD) | [Gyan.dev "full" shared build](https://www.gyan.dev/ffmpeg/builds/) | FFmpeg: `https://ffmpeg.org/releases/ffmpeg-<version>.tar.xz`; the build's libraries and their versions: listed on the Gyan.dev builds page and in `ffmpeg -version` |
| cjxl, djxl, ssimulacra2 (libjxl); cjpegli (libjxl 0.11.2) | BSD-3-Clause | [libjxl releases](https://github.com/libjxl/libjxl/releases) | https://github.com/libjxl/libjxl |
| gifsicle | GPL-2.0 | [eternallybored.org](https://eternallybored.org/misc/gifsicle/) | `https://github.com/kohler/gifsicle/archive/refs/tags/v<version>.tar.gz` |
| Pandoc (downloaded from its own release page when you first convert a document; not shipped) | GPL-2.0-or-later | [Pandoc releases](https://github.com/jgm/pandoc/releases) | https://github.com/jgm/pandoc |

## Libraries inside the app

| Library | Licence | Source code |
|---|---|---|
| Python | PSF License | https://www.python.org/downloads/source/ |
| Qt / PySide6 | LGPL-3.0 | https://download.qt.io/official_releases/ (Qt), https://code.qt.io/cgit/pyside/pyside-setup.git/ (PySide6) |
| libimagequant (imagequant) | GPL-3.0-or-later | https://github.com/ImageOptim/libimagequant, https://github.com/ImageOptim/imagequant-python |
| py7zr | LGPL-2.1-or-later | https://github.com/miurahr/py7zr |
| pillow-heif / libheif, libde265 | BSD-3-Clause / LGPL-3.0 | https://github.com/bigcat88/pillow_heif, https://github.com/strukturag/libheif |
| rawpy / LibRaw | MIT / LGPL-2.1 or CDDL-1.0 | https://github.com/letmaik/rawpy, https://github.com/LibRaw/LibRaw |
| pikepdf / qpdf | MPL-2.0 / Apache-2.0 | https://github.com/pikepdf/pikepdf, https://github.com/qpdf/qpdf |
| Pillow | MIT-CMU (HPND) | https://github.com/python-pillow/Pillow |
| mozjpeg-lossless-optimization (mozjpeg) | BSD-3-Clause / IJG | https://github.com/wanadev/mozjpeg-lossless-optimization |
| pyoxipng (oxipng) | MIT | https://github.com/nfrechette/pyoxipng |
| NumPy | BSD-3-Clause | https://github.com/numpy/numpy |
| lxml | BSD-3-Clause | https://github.com/lxml/lxml |
| Send2Trash | BSD-3-Clause | https://github.com/arsenetar/send2trash |
| fontTools, Brotli | MIT | https://github.com/fonttools/fonttools, https://github.com/google/brotli |

The Qt libraries are separate DLLs in the app folder (`_internal\PySide6`); you may replace them with your
own build of the same Qt version.

## Adapted code

`compressor/_ssimulacra2.py` is adapted from the `ssimulacra2` Python package (its one SciPy call replaced by
NumPy). Its licence:

```text
BSD 3-Clause License

Copyright (c) 2025, Pacidus
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Getting the source code

Compressor's own source for every release is in this repository, at that release's tag.

For the GPL and LGPL components above, use the source links in the tables (pick the version your release lists).
If a link no longer works, or you would like the source sent to you, email **lanntern@proton.me** with the
Compressor version you have: for at least three years after a release, Lanntern will provide the complete
corresponding source code of the GPL and LGPL components shipped with it, for no more than the cost of
sending it.
