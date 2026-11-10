"""Caches that apps rebuild by themselves. (Developer caches and project build folders: devclean.py.)"""

from __future__ import annotations

import fnmatch
import glob
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .common import Candidate, ProgressFn, ScanResult, cancelled, folder_size

IS_WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class CacheSpec:
    name: str
    category: str              # Browsers | Apps | Games | Drivers (developer caches: devclean)
    patterns: tuple[str, ...]  # globs; env vars and ~ are expanded
    note: str                  # what happens after removal
    process: str = ""          # executable that must be closed first (lower-case, no .exe)
    warning: str = ""          # listed but never pre-selected, with this reason
    exclude: tuple[str, ...] = ()   # folder names (globs) the patterns must not match


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def specs() -> list[CacheSpec]:
    if IS_WINDOWS:
        L, R, H = _env("LOCALAPPDATA"), _env("APPDATA"), str(Path.home())
        chromium = ("Cache", "Code Cache", "GPUCache", "Service Worker/CacheStorage")
        s = [
            CacheSpec("Chrome cache", "Browsers",
                      tuple(f"{L}/Google/Chrome/User Data/*/{c}" for c in chromium),
                      "Web pages load a little slower the first time.", "chrome"),
            CacheSpec("Edge cache", "Browsers",
                      tuple(f"{L}/Microsoft/Edge/User Data/*/{c}" for c in chromium),
                      "Web pages load a little slower the first time.", "msedge"),
            CacheSpec("Brave cache", "Browsers",
                      tuple(f"{L}/BraveSoftware/Brave-Browser/User Data/*/{c}" for c in chromium),
                      "Web pages load a little slower the first time.", "brave"),
            CacheSpec("Firefox cache", "Browsers", (f"{L}/Mozilla/Firefox/Profiles/*/cache2",),
                      "Web pages load a little slower the first time.", "firefox"),
            CacheSpec("Discord cache", "Apps", tuple(f"{R}/discord/{c}" for c in ("Cache", "Code Cache", "GPUCache")),
                      "Images and emoji are downloaded again as needed.", "discord"),
            CacheSpec("Spotify cache", "Apps", (f"{L}/Spotify/Storage", f"{L}/Spotify/Data"),
                      "Songs stream again instead of playing from the cache. Downloaded playlists are kept.",
                      "spotify"),
            CacheSpec("Teams cache (classic)", "Apps",
                      tuple(f"{R}/Microsoft/Teams/{c}" for c in ("Cache", "Code Cache", "GPUCache", "Service Worker")),
                      "Teams rebuilds it when it starts.", "teams"),
            CacheSpec("Windows crash dumps", "Apps", (f"{L}/CrashDumps",), "Only useful for debugging crashes."),
            CacheSpec("NVIDIA shader cache", "Games", (f"{L}/NVIDIA/DXCache", f"{L}/NVIDIA/GLCache"),
                      "Games rebuild shaders; the first launch may stutter briefly."),
            CacheSpec("AMD shader cache", "Games", (f"{L}/AMD/DxCache", f"{L}/AMD/DxcCache", f"{L}/AMD/GLCache"),
                      "Games rebuild shaders; the first launch may stutter briefly."),
            CacheSpec("NVIDIA driver installers", "Drivers", ("C:/NVIDIA",),
                      "Leftover files from installing graphics drivers."),
            CacheSpec("AMD driver installers", "Drivers", ("C:/AMD",),
                      "Leftover files from installing graphics drivers."),
            # Creative apps
            CacheSpec("Lightroom Classic previews", "Creative apps",
                      tuple(f"{H}/Pictures/Lightroom/{d}*Previews.lrdata" for d in ("", "*/")),
                      "Lightroom builds previews again as you browse (slower at first). Your edits and photos "
                      "aren't in here.", "lightroom", exclude=("*smart previews.lrdata",)),
            CacheSpec("Adobe media cache", "Creative apps",
                      (f"{R}/Adobe/Common/Media Cache Files", f"{R}/Adobe/Common/Media Cache",
                       f"{R}/Adobe/Common/Peak Files"),
                      "Premiere Pro, After Effects and Media Encoder rebuild it when they open your media. Render "
                      "files aren't touched.", "adobe premiere pro|afterfx|adobe media encoder"),
            # Chat apps
            CacheSpec("Telegram media cache", "Chat apps",
                      (f"{R}/Telegram Desktop/tdata/user_data/cache", f"{R}/Telegram Desktop/tdata/user_data/media_cache"),
                      "Photos and videos you've seen; Telegram downloads them again when you open the chat. Your "
                      "login and chats aren't in here.", "telegram"),
            CacheSpec("WhatsApp media", "Chat apps",
                      (f"{L}/Packages/*WhatsAppDesktop*/LocalState/shared/transfers",),
                      "Photos, videos and files received in WhatsApp on this PC. Your chats aren't in here.",
                      "whatsapp", warning="Media that's no longer on your phone may not download again"),
            CacheSpec("Slack cache", "Chat apps",
                      tuple(f"{R}/Slack/{c}" for c in ("Cache", "Code Cache", "Service Worker/CacheStorage")),
                      "Slack downloads it again as needed.", "slack"),
            CacheSpec("Teams cache", "Chat apps",
                      tuple(f"{L}/Packages/MSTeams_8wekyb3d8bbwe/LocalCache/Microsoft/MSTeams/EBWebView/*/{c}"
                            for c in ("Cache", "Code Cache", "Service Worker/CacheStorage")),
                      "Teams downloads it again as needed.", "ms-teams"),
        ]
    else:
        C = _env("XDG_CACHE_HOME", str(Path.home() / ".cache"))
        cfg = _env("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        s = [
            CacheSpec("Chrome cache", "Browsers", (f"{C}/google-chrome",), "Web pages load a little slower the first time.",
                      "chrome"),
            CacheSpec("Chromium cache", "Browsers", (f"{C}/chromium",), "Web pages load a little slower the first time.",
                      "chromium"),
            CacheSpec("Firefox cache", "Browsers", (f"{C}/mozilla/firefox",), "Web pages load a little slower the "
                                                                               "first time.", "firefox"),
            CacheSpec("Discord cache", "Apps", tuple(f"{cfg}/discord/{c}" for c in ("Cache", "Code Cache", "GPUCache")),
                      "Images and emoji are downloaded again as needed.", "discord"),
            CacheSpec("Spotify cache", "Apps", (f"{C}/spotify",), "Songs stream again instead of playing from the "
                                                                   "cache.", "spotify"),
            CacheSpec("Thumbnail cache", "Apps", (f"{C}/thumbnails",), "File managers recreate thumbnails."),
            CacheSpec("Telegram media cache", "Chat apps",
                      tuple(f"~/.local/share/TelegramDesktop/tdata/user_data/{c}" for c in ("cache", "media_cache")),
                      "Photos and videos you've seen; Telegram downloads them again when you open the chat. Your "
                      "login and chats aren't in here.", "telegram"),
            CacheSpec("Slack cache", "Chat apps",
                      tuple(f"{cfg}/Slack/{c}" for c in ("Cache", "Code Cache", "Service Worker/CacheStorage")),
                      "Slack downloads it again as needed.", "slack"),
        ]
    s += _steam_specs()
    return s


def steam_libraries() -> list[Path]:
    """Steam library folders (from libraryfolders.vdf)."""
    roots = []
    if IS_WINDOWS:
        roots = [Path(p) for p in (_env("ProgramFiles(x86)", "C:/Program Files (x86)") + "/Steam",
                                   _env("ProgramFiles", "C:/Program Files") + "/Steam")]
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                roots.insert(0, Path(winreg.QueryValueEx(k, "SteamPath")[0]))
        except OSError:
            pass
    else:
        roots = [Path.home() / ".steam/steam", Path.home() / ".local/share/Steam"]
    libs: list[Path] = []
    for r in roots:
        vdf = r / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            import re

            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text("utf-8", "replace")):
                libs.append(Path(m.group(1).replace("\\\\", "\\")))
        if (r / "steamapps").is_dir():
            libs.append(r)
    out, seen = [], set()
    for lib in libs:
        k = os.path.normcase(str(lib))
        if k not in seen and (lib / "steamapps").is_dir():
            seen.add(k)
            out.append(lib)
    return out


def _steam_specs() -> list[CacheSpec]:
    out = []
    for lib in steam_libraries():
        out.append(CacheSpec(f"Steam shader cache ({lib.drive or lib})", "Games",
                             (str(lib / "steamapps" / "shadercache"),),
                             "Steam rebuilds shaders; games may stutter briefly the first time.", "steam"))
    if steam_libraries():
        main = steam_libraries()[0]
        out.append(CacheSpec("Steam crash dumps & logs", "Games", (str(main / "dumps"), str(main / "logs")),
                             "Only useful for debugging crashes."))
    return out


def running_processes() -> set[str]:
    names: set[str] = set()
    try:
        if IS_WINDOWS:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            for line in out.splitlines():
                if line.startswith('"'):
                    names.add(line.split('","')[0].strip('"').lower().removesuffix(".exe"))
        else:
            for d in Path("/proc").iterdir():
                if d.name.isdigit():
                    try:
                        names.add((d / "comm").read_text().strip().lower())
                    except OSError:
                        pass
    except Exception:  # noqa: BLE001
        pass
    return names


LIGHTROOM_BACKUPS_KEPT = 2


def lightroom_backups(base: Path | None = None) -> list[Candidate]:
    """Lightroom Classic backs up its catalog into a new dated folder every time and never removes old
    ones. All but the newest few are listed (each is a complete copy of the catalog, often gigabytes)."""
    base = base or Path.home() / "Pictures" / "Lightroom"
    found: list[tuple[float, Path]] = []
    for backups in [base / "Backups", *base.glob("*/Backups")]:
        if not backups.is_dir():
            continue
        for d in backups.iterdir():
            # Each backup folder holds a catalog (.lrcat, or zipped since Lightroom Classic 9).
            if d.is_dir() and any(f.suffix.lower() in (".lrcat", ".zip") for f in d.iterdir() if f.is_file()):
                found.append((d.stat().st_mtime, d))
    found.sort(reverse=True)
    out = []
    for _mtime, d in found[LIGHTROOM_BACKUPS_KEPT:]:
        size, count = folder_size(d)
        if size >= 1024 * 1024:
            out.append(Candidate(d, size, title=f"Lightroom catalog backup {d.name}", group="Creative apps",
                                 detail=f"An older backup of your Lightroom catalog; the {LIGHTROOM_BACKUPS_KEPT} "
                                        "newest stay.", is_dir=True, extra={"files": count, "location": str(d)}))
    return out


def find_caches(progress: ProgressFn | None = None, cancel: threading.Event | None = None,
                spec_list: list[CacheSpec] | None = None, measure=folder_size) -> ScanResult:
    """``measure(path, cancel) -> (bytes freed, file count)``."""
    result = ScanResult("caches")
    running = running_processes()
    todo = spec_list if spec_list is not None else specs()
    for i, spec in enumerate(todo):
        if cancelled(cancel):
            result.cancelled = True
            break
        if progress:
            progress(f"Measuring {spec.name}", i, len(todo))
        for pattern in spec.patterns:
            for match in sorted(glob.glob(os.path.expanduser(pattern))):
                p = Path(match)
                if not p.is_dir() or any(fnmatch.fnmatch(p.name.lower(), x.lower()) for x in spec.exclude):
                    continue
                size, count = measure(p, cancel)
                if size < 1024 * 1024:
                    continue
                warn = spec.warning
                names = [x for x in spec.process.split("|") if x]   # several programs can share a cache
                if names and any(n == x or n.startswith(x) for x in names for n in running):
                    warn = f"Close {spec.name.split(' ')[0]} first"
                result.items.append(Candidate(p, size, title=spec.name, detail=spec.note, is_dir=True,
                                              group=spec.category, warning=warn,
                                              extra={"files": count, "location": str(p)}))
    if spec_list is None and not cancelled(cancel):
        try:
            result.items += lightroom_backups()
        except OSError:
            pass
    return result
