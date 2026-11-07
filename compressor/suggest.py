"""Folders worth watching: screenshots and screen/game recordings pile up fast and shrink well."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _obs_folders() -> list[Path]:
    """Where OBS Studio saves recordings, from its profiles (simple and advanced output modes)."""
    base = (Path(os.environ.get("APPDATA", "")) / "obs-studio" if sys.platform == "win32"
            else Path.home() / ".config" / "obs-studio")
    out = []
    for ini in base.glob("basic/profiles/*/basic.ini"):
        try:
            for line in ini.read_text("utf-8", "replace").splitlines():
                key, _, value = line.partition("=")
                if key.strip() in ("FilePath", "RecFilePath") and value.strip():
                    out.append(Path(value.strip()))
        except OSError:
            continue
    return out


def watch_suggestions(already: list[Path]) -> list[tuple[str, Path, str]]:
    """(label, folder, preset name or "") for folders that exist and aren't watched yet."""
    from .cleanup.common import known_folder

    pictures = known_folder("Pictures") or Path.home() / "Pictures"
    videos = known_folder("Videos") or Path.home() / "Videos"
    found = [("Screenshots", pictures / "Screenshots", "Screenshots"),
             ("Game captures (Xbox Game Bar)", videos / "Captures", ""),
             ("NVIDIA recordings", videos / "NVIDIA", "")]
    found += [("OBS recordings", p, "") for p in _obs_folders()]
    seen = {os.path.normcase(os.path.abspath(p)) for p in already}
    out = []
    for label, path, preset in found:
        key = os.path.normcase(os.path.abspath(path))
        if path.is_dir() and key not in seen:
            seen.add(key)
            out.append((label, path, preset))
    return out


__all__ = ["watch_suggestions"]
