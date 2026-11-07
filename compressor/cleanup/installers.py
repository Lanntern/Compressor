"""Old installers and disk images (typically in Downloads)."""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from .common import Candidate, ProgressFn, ScanResult, cancelled, walk_files

# Package formats that only ever install something. (AppImage is excluded: it *is* the app.)
PACKAGE_EXTS = {".msi", ".msix", ".msixbundle", ".appx", ".appxbundle", ".deb", ".rpm", ".pkg", ".flatpakref"}
INSTALLER_EXTS = PACKAGE_EXTS | {".exe"}
# Words in a file name that mark an installer ("SteamSetup.exe", "vlc-installer.exe").
_NAME_HINT = re.compile(r"setup|install|(?:^|[^a-z])(?:inst|web[-_ ]?installer)(?:[^a-z]|$)", re.I)
# Signatures installer frameworks leave inside the .exe.
_MARKERS = (
    b"Inno Setup", b"Nullsoft.NSIS", b"NullsoftInst", b"Nullsoft Install System", b".wixburn", b"WixBundle",
    b"InstallShield", b";!@Install@!UTF-8!", b"7zS.sfx", b"WinRAR SFX", b"Setup Factory", b"InstallAware",
    b"Advanced Installer", b"Wise Installation", b"Squirrel.Windows", b"Smart Install Maker",
    "InstallShield".encode("utf-16-le"),
)
_UNINSTALLER = re.compile(r"unins", re.I)
_HEAD, _TAIL = 4 << 20, 1 << 20
IMAGE_EXTS = {".iso", ".img", ".dmg", ".vhd", ".vhdx"}
_WORD = re.compile(r"[a-z]{3,}")
_NOISE = {"setup", "installer", "install", "win", "windows", "x64", "x86", "amd64", "arm64", "release", "stable",
          "latest", "full", "offline", "online", "bit", "version", "update", "the", "and", "for", "app", "msi",
          "exe", "download", "user", "system", "beta"}


def installed_programs() -> list[str]:
    """Display names of installed programs (Windows uninstall registry)."""
    if os.name != "nt":
        return []
    import winreg

    names = []
    keys = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
    for hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sub:
                            names.append(str(winreg.QueryValueEx(sub, "DisplayName")[0]))
                    except OSError:
                        continue
        except OSError:
            continue
    return names


def match_installed(filename: str, programs: list[str]) -> str | None:
    """Best-effort: the installed program an installer file seems to belong to.

    Every distinctive word of the program's name must appear in the file name
    (file names glue words together: "SteamSetup.exe", "DiscordSetup.exe").
    """
    compact = re.sub(r"[^a-z]", "", Path(filename).stem.lower())
    best = None
    for name in programs:
        words = [w for w in _WORD.findall(name.lower()) if w not in _NOISE and len(w) >= 4][:3]
        if words and all(w in compact for w in words):
            if best is None or len("".join(words)) > len(best[1]):
                best = (name, "".join(words))
    return best[0] if best else None


def installer_evidence(path: Path) -> str | None:
    """Why ``path`` looks like an installer, or None if it looks like a program or game itself."""
    ext = path.suffix.lower()
    if ext in PACKAGE_EXTS:
        return "package"
    if ext != ".exe" or _UNINSTALLER.search(path.stem):
        return None
    if _NAME_HINT.search(path.stem):
        return "name"
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(_HEAD)
            if head[:2] != b"MZ":
                return None
            tail = b""
            if size > _HEAD:
                f.seek(max(_HEAD, size - _TAIL))
                tail = f.read(_TAIL)
    except OSError:
        return None
    return "signature" if any(m in head or m in tail for m in _MARKERS) else None


def find_installers(roots: list[Path], older_than_days: int = 30, progress: ProgressFn | None = None,
                    cancel: threading.Event | None = None) -> ScanResult:
    result = ScanResult("installers")
    cutoff = time.time() - older_than_days * 86400
    programs = installed_programs()
    n = 0
    # Program and game folders (anything with .dll/.so files, Program Files, Steam...) hold apps, not installers.
    for path, st in walk_files(roots, skip_program_folders=True, cancel=cancel):
        n += 1
        if progress and n % 300 == 0:
            progress(f"Looking at files… {n:,}", n, 0)
        ext = path.suffix.lower()
        if ext not in INSTALLER_EXTS and ext not in IMAGE_EXTS:
            continue
        if st.st_mtime > cutoff:
            continue
        age_days = int((time.time() - st.st_mtime) / 86400)
        age = f"{age_days // 30} months old" if age_days >= 60 else f"{age_days} days old"
        if ext in IMAGE_EXTS:
            result.items.append(Candidate(path, st.st_size, detail=f"Disk image, {age}", selected=False,
                                          group="Disk images"))
            continue
        if installer_evidence(path) is None:
            continue  # a program or game, not an installer
        installed = match_installed(path.name, programs)
        detail = f"Installer, {age}" + (f" · looks installed: {installed}" if installed else "")
        result.items.append(Candidate(path, st.st_size, detail=detail, group="Installers",
                                      extra={"installed": installed}))
    if cancelled(cancel):
        result.cancelled = True
    return result
