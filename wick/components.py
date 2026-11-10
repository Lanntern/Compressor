"""Versions of everything Wick bundles or runs (shown in About; copied for support)."""

from __future__ import annotations

import importlib.metadata as md
import re
from functools import lru_cache

from . import tools


def _tool_version(name: str, args: list[str], pattern: str) -> str | None:
    exe = tools.find_tool(name)
    if not exe:
        return None
    try:
        proc = tools.run([exe, *args], timeout=20, check=False)
    except Exception:  # noqa: BLE001
        return "installed"
    m = re.search(pattern, (proc.stdout or "") + (proc.stderr or ""))
    return m.group(1) if m else "installed"


def _dist(name: str) -> str | None:
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


@lru_cache(maxsize=1)
def versions() -> dict[str, str]:
    """Component name (as in the credits list) -> version; missing components are left out."""
    out: dict[str, str | None] = {
        "FFmpeg": _tool_version("ffmpeg", ["-version"], r"ffmpeg version (\S+)"),
        "libjxl": _tool_version("cjxl", ["--version"], r"cjxl v?(\d+\.\d+\.\d+)"),
        "jpegli": _tool_version("cjpegli", ["--version"], r"v?(\d+\.\d+\.\d+)"),
        "gifsicle": _tool_version("gifsicle", ["--version"], r"Gifsicle (\d+\.\d+(?:\.\d+)?)"),
        "Pandoc": _tool_version("pandoc", ["--version"], r"pandoc (\d+(?:\.\d+)+)"),
        "Pillow": _dist("Pillow"),
        "pillow-heif": _dist("pillow-heif"),
        "pikepdf": _dist("pikepdf"),
        "py7zr": _dist("py7zr"),
        "rawpy (LibRaw)": _dist("rawpy"),
        "fontTools": _dist("fonttools"),
        "Brotli": _dist("brotli"),
        "NumPy": _dist("numpy"),
        "lxml": _dist("lxml"),
        "Send2Trash": _dist("Send2Trash"),
        "mozjpeg-lossless-optimization": _dist("mozjpeg-lossless-optimization"),
        "pyoxipng": _dist("pyoxipng"),
        "imagequant": _dist("imagequant"),
        "PySide6": _dist("PySide6"),
    }
    try:
        from PySide6.QtCore import qVersion

        out["Qt"] = qVersion()
    except ImportError:
        pass
    try:
        import pillow_heif

        out["libheif"] = pillow_heif.libheif_version()
    except Exception:  # noqa: BLE001
        pass
    try:
        import pikepdf

        out["qpdf"] = pikepdf.__libqpdf_version__
    except Exception:  # noqa: BLE001
        pass
    try:
        from PIL import features

        for lib, label in (("webp", "libwebp"), ("libjpeg_turbo", "libjpeg-turbo"), ("zlib", "zlib")):
            v = features.version(lib)
            if v:
                out[label] = v
    except Exception:  # noqa: BLE001
        pass
    return {k: v for k, v in out.items() if v}


def report() -> str:
    from . import APP_NAME, __version__

    lines = [f"{APP_NAME} {__version__}"] + [f"{k} {v}" for k, v in versions().items()]
    return "\n".join(lines)


__all__ = ["versions", "report"]
