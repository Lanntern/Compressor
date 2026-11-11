"""Download the command-line encoders Wick uses into ./tools: exact versions, checked by SHA-256.

Windows (default on Windows):
    ffmpeg/ffprobe  Gyan.dev "full" shared build of an FFmpeg release (NVENC, SVT-AV1, x265, VMAF)
    cjxl/djxl       libjxl release (+ ssimulacra2); cjpegli from the last libjxl release that ships it
    gifsicle        eternallybored.org Windows build

Linux: ffmpeg and gifsicle come from your package manager (it keeps them patched)
    sudo apt install ffmpeg gifsicle
and `--libjxl` downloads the static libjxl tools (cjxl/djxl/cjpegli), newer than most distributions ship.

Every download is checked against the checksum recorded below and refused if it differs: a changed or
replaced file never reaches the app. To update a tool, change its version, URL and checksum together
(scripts/check_updates.py reports when a newer upstream release exists).

Usage:  python scripts/fetch_tools.py [--dest tools] [--force] [--libjxl] [--pandoc] [--any-os] [--allow-unpinned]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import stat
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Download:
    tool: str                 # upstream project (see check_updates.UPSTREAM)
    version: str
    url: str
    sha256: str | None        # None: not pinned yet (refused unless --allow-unpinned)
    files: dict = field(default_factory=dict)   # archive basename -> output name ("?x": optional, None: keep name)
    required: bool = True
    held: str = ""            # why it deliberately isn't the newest upstream version (shown by check_updates)


WINDOWS = [
    Download("ffmpeg", "9.0.2",
             "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-full_build-shared.zip",
             "8d31e162f1616e37aab3fa2db991b97e1b8dbeb1c8465fd81a81be16ff91b328",
             {"ffmpeg.exe": "ffmpeg.exe", "ffprobe.exe": "ffprobe.exe", "LICENSE": "LICENSE-ffmpeg.txt",
              "*.dll": None}),
    Download("libjxl", "0.12.0",
             "https://github.com/libjxl/libjxl/releases/download/v0.12.0/jxl-x64-windows-static.zip",
             "3025d7e308390796d20492322e606bc92decaee7b6bc99d3f7547870ae5db7de",
             {"cjxl.exe": "cjxl.exe", "djxl.exe": "djxl.exe", "ssimulacra2.exe": "?ssimulacra2.exe",
              "butteraugli_main.exe": "?butteraugli_main.exe", "LICENSE.libjxl": "LICENSE-libjxl.txt"}),
    # jpegli moved out of libjxl after 0.11; 0.11.2 is the newest release with a Windows cjpegli.
    Download("jpegli", "0.11.2",
             "https://github.com/libjxl/libjxl/releases/download/v0.11.2/jxl-x64-windows-static.zip",
             "97dc815bdd99ba243d8502050357342cf649251a5df069f8c3daee6828cbe0ce",
             {"cjpegli.exe": "cjpegli.exe"}, required=False,
             held="newest libjxl release that still ships cjpegli for Windows"),
    Download("gifsicle", "1.95", "https://eternallybored.org/misc/gifsicle/releases/gifsicle-1.95-win64.zip",
             "7e47dd0bfd5ee47f911464c57faeed89a8709a7625dd1c449b16579889539ee8",   # as first downloaded by CI
             {"gifsicle.exe": "gifsicle.exe", "COPYING": "LICENSE-gifsicle.txt"}, required=False),
]

# libjxl stopped publishing static Linux builds after 0.11.1.
LINUX_LIBJXL = Download(
    "libjxl", "0.11.1",
    "https://github.com/libjxl/libjxl/releases/download/v0.11.1/jxl-linux-x86_64-static-v0.11.1.tar.gz",
    "7ba87d09f220568a7e84c2a62e9fa8be608443930dec10b2799271d4cf032293",
    {"cjxl": "cjxl", "djxl": "djxl", "cjpegli": "cjpegli", "ssimulacra2": "?ssimulacra2",
     "butteraugli_main": "?butteraugli_main"},
    held="libjxl publishes no static Linux builds after 0.11.1 (Linux users get libjxl from their distribution)")

# ffmpeg/ffprobe for Linux CI: a static build of the same FFmpeg release as on Windows (BtbN's GPL build: SVT-AV1,
# x265, x264, Opus, LAME, libvpx, VMAF). Pinned to a month's last build, which BtbN keeps for two years. CI used
# the distribution's ffmpeg, so an Ubuntu mirror outage blocked every merge.
LINUX_FFMPEG = Download(
    "ffmpeg", "9.0.2",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-31-12-59/"
    "ffmpeg-n9.0.2-39-g7adfbe40dc-linux64-gpl-9.0.tar.xz",
    "73e29a56974ae5a651133a04584c7483b21afe941e17dc91d79814a758b608d3",
    {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "LICENSE.txt": "LICENSE-ffmpeg.txt"})

# Pandoc (documents): on Windows the app downloads it when first needed (wick/addons.py pins the same
# version); this is for Linux CI and builds. Distribution packages may be too old to run --sandbox.
LINUX_PANDOC = Download(
    "pandoc", "3.12.1",
    "https://github.com/jgm/pandoc/releases/download/3.12.1/pandoc-3.12.1-linux-amd64.tar.gz",
    "d0c90410e90204c9ca83b8539fac5c7aed01fd537207e4585849f8abc5df20b8",
    {"pandoc": "pandoc"})


LICENSES = Path(__file__).resolve().parent.parent / "packaging" / "licenses"   # fallback license texts


class ChecksumMismatch(Exception):
    pass


def download(url: str) -> bytes:
    print(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "wick-fetch-tools"})
    with urllib.request.urlopen(req, timeout=300) as r:
        total = int(r.headers.get("Content-Length") or 0)
        buf = io.BytesIO()
        while chunk := r.read(1 << 20):
            buf.write(chunk)
            if total and sys.stdout.isatty():
                print(f"\r  {buf.tell() / total * 100:5.1f}% of {total / 1e6:.0f} MB", end="")
        if total and sys.stdout.isatty():
            print()
        return buf.getvalue()


def verify(d: Download, data: bytes, allow_unpinned: bool) -> None:
    got = hashlib.sha256(data).hexdigest()
    print(f"  sha256 {got}")
    if d.sha256 is None:
        if not allow_unpinned:
            raise ChecksumMismatch(f"{d.tool} {d.version} has no recorded checksum yet (sha256 {got}); record "
                                   "it in scripts/fetch_tools.py, or pass --allow-unpinned")
        print("  WARNING: not pinned; accepted because of --allow-unpinned")
    elif got != d.sha256:
        raise ChecksumMismatch(f"{d.tool} {d.version}: checksum {got} doesn't match the recorded {d.sha256}")


def _members(data: bytes, url: str):
    """(name, file-like opener) pairs from a zip or a tar (.gz, .xz)."""
    if url.endswith(".zip"):
        try:
            import zipfile_deflate64  # noqa: F401 - teaches zipfile Deflate64 (libjxl's Windows zips use it)
        except ImportError:
            pass
        z = zipfile.ZipFile(io.BytesIO(data))
        if any(i.compress_type == 9 for i in z.infolist()) and "zipfile_deflate64" not in sys.modules:
            raise RuntimeError("this archive uses Deflate64: pip install --require-hashes -r requirements-build.txt")
        for info in z.infolist():
            if not info.is_dir():
                yield info.filename, (lambda i=info: z.open(i))
    else:
        t = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        for m in t.getmembers():
            if m.isfile():
                yield m.name, (lambda m=m: t.extractfile(m))


def fetch(d: Download, dest: Path, force: bool, allow_unpinned: bool = False) -> bool:
    wanted = d.files
    outputs = [dest / n for n in wanted.values() if n and not n.startswith(("LICENSE", "?"))]
    stamp = dest / f".{d.tool}.version"
    if not force and outputs and all(p.exists() for p in outputs) and \
            stamp.exists() and stamp.read_text().strip() == f"{d.version} {d.sha256}":
        print(f"Already have {d.tool} {d.version}")
        return True
    try:
        data = download(d.url)
        verify(d, data, allow_unpinned)
    except ChecksumMismatch as e:
        print(f"  REFUSED: {e}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  failed: {e}")
        return False
    found = set()
    for name, opener in _members(data, d.url):
        base = name.rsplit("/", 1)[-1]
        if base in wanted:
            out_name = (wanted[base] or base).lstrip("?")
        elif "*.dll" in wanted and base.lower().endswith(".dll") and "/bin/" in name:
            out_name = base
        else:
            continue
        with opener() as src, open(dest / out_name, "wb") as dst:
            shutil.copyfileobj(src, dst)
        if os.name != "nt":
            os.chmod(dest / out_name, os.stat(dest / out_name).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        found.add(base)
    for m, out in wanted.items():
        # A license the archive doesn't carry (gifsicle's Windows zip): ship the copy kept in packaging/licenses.
        if out and out.startswith("LICENSE") and m not in found and (LICENSES / out).is_file():
            shutil.copyfile(LICENSES / out, dest / out)
    missing = [m for m, out in wanted.items()
               if m != "*.dll" and out and not out.startswith(("LICENSE", "?")) and m not in found]
    if missing:
        print(f"  {', '.join(missing)} not found in the archive")
        return False
    stamp.write_text(f"{d.version} {d.sha256}")
    print(f"  -> {d.tool} {d.version}: {', '.join(sorted(found))}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", type=Path, default=Path(__file__).resolve().parent.parent / "tools")
    ap.add_argument("--force", action="store_true", help="re-download even if present")
    ap.add_argument("--any-os", action="store_true", help="fetch the Windows tools even when not on Windows")
    ap.add_argument("--libjxl", action="store_true", help="Linux: download the static libjxl tools")
    ap.add_argument("--pandoc", action="store_true", help="Linux: download Pandoc (document conversion)")
    ap.add_argument("--ffmpeg", action="store_true", help="Linux: download a static ffmpeg/ffprobe (as CI uses)")
    ap.add_argument("--allow-unpinned", action="store_true", help="accept downloads with no recorded checksum")
    a = ap.parse_args()
    a.dest.mkdir(parents=True, exist_ok=True)

    if os.name == "nt" or a.any_os:
        ok = True
        for d in WINDOWS:
            if not fetch(d, a.dest, a.force, a.allow_unpinned):
                print(("ERROR: " if d.required else "WARNING: ") + f"couldn't get {d.tool} {d.version}",
                      file=sys.stderr)
                ok = ok and not d.required
        print(f"Tools are in {a.dest}")
        return 0 if ok else 1

    if a.libjxl or a.pandoc or a.ffmpeg:
        for d, wanted in ((LINUX_LIBJXL, a.libjxl), (LINUX_PANDOC, a.pandoc), (LINUX_FFMPEG, a.ffmpeg)):
            if wanted and not fetch(d, a.dest, a.force, a.allow_unpinned):
                return 1
        print(f"Tools are in {a.dest}")
        return 0
    print("On Linux, install the tools with your package manager, e.g.:\n"
          "  sudo apt install ffmpeg gifsicle libjxl-tools\n"
          "and optionally `python scripts/fetch_tools.py --libjxl` for newer cjxl/djxl + cjpegli.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
