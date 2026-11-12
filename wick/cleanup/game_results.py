"""Opt-in, read-only public CompactGUI estimates. No installed-game list is sent anywhere."""

import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path

URL = "https://raw.githubusercontent.com/IridiumIO/CompactGUI/database/database.json"
PAGE = "https://github.com/IridiumIO/CompactGUI/wiki/Community-Compression-Results"
MODES = ("xpress4k", "xpress8k", "xpress16k", "lzx")
LIMIT = 32 * 1024 * 1024


def cache_path() -> Path:
    from ..journal import data_dir

    return data_dir() / "game-estimates.json"


def parse(data) -> dict:
    if not isinstance(data, list):
        raise ValueError("Community results must be a list")
    out = {}
    for game in data:
        if not isinstance(game, dict):
            continue
        try:
            steam_id = int(game["SteamID"])
        except (KeyError, TypeError, ValueError):
            continue
        modes = {}
        rows = game.get("CompressionResults", [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            try:
                mode = int(row["CompType"])
                before, after = float(row["BeforeBytes"]), float(row["AfterBytes"])
                if 0 <= mode < len(MODES) and before > 0 and 0.01 <= after / before <= 2:
                    modes[MODES[mode]] = {"ratio": after / before, "samples": int(row.get("TotalResults", 0))}
            except (KeyError, TypeError, ValueError):
                continue
        out[steam_id] = {"name": str(game.get("GameName", steam_id)), "modes": modes,
                         "confidence": str(game.get("Confidence", "Unknown"))}
    return out


def load() -> dict:
    path = cache_path()
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > LIMIT:
            raise ValueError("Community result cache is too large")
        return parse(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as error:
        from ..log import log

        log.warning("Cannot read community game estimates: %s", error)
        return {}


def download() -> dict:
    if os.environ.get("WICK_NO_NETWORK"):
        raise RuntimeError("Network access is disabled")
    request = urllib.request.Request(URL, headers={"User-Agent": "Wick game estimates"})
    with urllib.request.urlopen(request, timeout=15) as response:
        content = response.read(LIMIT + 1)
    if len(content) > LIMIT:
        raise ValueError("Community results exceed the download limit")
    return save(content)


def save(content: bytes) -> dict:
    if len(content) > LIMIT:
        raise ValueError("Community results exceed the download limit")
    parsed = parse(json.loads(content.decode("utf-8-sig")))
    path = cache_path()
    fd, name = tempfile.mkstemp(prefix="game-estimates-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return parsed


def steam_id(folder: Path) -> int | None:
    if folder.parent.name.lower() != "common":
        return None
    for manifest in folder.parent.parent.glob("appmanifest_*.acf"):
        try:
            if manifest.is_symlink() or manifest.stat().st_size > 256 * 1024:
                continue
            contents = manifest.read_text(encoding="utf-8", errors="replace")
            name = re.search(r'"installdir"\s+"([^"\r\n]+)"', contents, re.I)
            app = re.search(r'"appid"\s+"(\d+)"', contents, re.I)
            if name and app and name[1].casefold() == folder.name.casefold():
                return int(app[1])
        except OSError:
            continue
    return None
