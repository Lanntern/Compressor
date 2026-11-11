"""Windows' Controlled folder access (ransomware protection): only apps on its allowed list may change files in
protected folders (Documents, Pictures, Videos, Music, Desktop, and any the person adds). Anything else gets
"access denied" (sometimes "not found"), which on its own reads like a broken file. This recognizes those
failures, so they can be explained.

Wick never adds itself to the allowed list: that needs administrator rights, and an app quietly allowing
itself defeats the protection. The person decides, in Windows Security."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from . import APP_NAME

_KEYS = (r"SOFTWARE\Policies\Microsoft\Windows Defender\Windows Defender Exploit Guard\Controlled Folder Access",
         r"SOFTWARE\Microsoft\Windows Defender\Windows Defender Exploit Guard\Controlled Folder Access")
_BLOCKING = {1}           # 1 = block (2 = audit only; 3/4 = disk sectors only)
_DENIED = {5, 13, -2147024891, 0x80070005}    # access denied (Windows code, errno, HRESULT forms)
_NOT_FOUND = {2, 3}                             # what some refusals say instead, for a file that's there

SHORT = "Blocked by Windows' Controlled folder access"
HOW = ("Windows' Controlled folder access (ransomware protection) only lets approved apps change files in "
       f"protected folders such as Documents and Pictures. To let {APP_NAME} change them, open Windows Security → "
       "Virus & threat protection → Manage ransomware protection → Allow an app through Controlled folder "
       f"access, and add {APP_NAME}. Or leave it on, and {APP_NAME} will skip those folders.")

_cache: tuple[float, dict] | None = None
_IS_WINDOWS = os.name == "nt"


def _state() -> dict:
    """{"on": bool, "folders": [Path], "apps": [str]} from the registry (read at most once a minute)."""
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < 60:
        return _cache[1]
    state: dict = {"on": False, "folders": [], "apps": []}
    if os.name == "nt":
        import winreg

        for key in _KEYS:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                    try:
                        value = int(winreg.QueryValueEx(k, "EnableControlledFolderAccess")[0])
                        state["on"] = state["on"] or value in _BLOCKING
                    except (OSError, ValueError):
                        pass
                    for sub, out in (("ProtectedFolders", "folders"), ("AllowedApplications", "apps")):
                        try:
                            with winreg.OpenKey(k, sub) as s:
                                i = 0
                                while True:
                                    try:
                                        state[out].append(winreg.EnumValue(s, i)[0])
                                    except OSError:
                                        break
                                    i += 1
                        except OSError:
                            pass
            except OSError:
                continue
        state["folders"] = [Path(os.path.expandvars(f)) for f in state["folders"]] + _default_folders()
    _cache = (time.monotonic(), state)
    return state


def _default_folders() -> list[Path]:
    """The folders Windows protects whatever the settings say (each user's and the public ones)."""
    home = Path.home()
    public = Path(os.environ.get("PUBLIC", r"C:\Users\Public"))
    names = ("Documents", "Pictures", "Videos", "Music", "Desktop", "Favorites")
    out = [home / n for n in names] + [public / n for n in names if n != "Favorites"]
    try:
        from .cleanup.common import known_folder     # (redirected folders: OneDrive's Pictures...)

        out += [p for n in names if (p := known_folder(n)) is not None]
    except Exception:  # noqa: BLE001
        pass
    return out


def enabled() -> bool:
    return bool(_state()["on"])


def _allowed() -> bool:
    me = os.path.normcase(os.path.abspath(sys.executable))
    return any(os.path.normcase(a) == me for a in _state()["apps"])


def protected(path: Path) -> bool:
    p = os.path.normcase(os.path.abspath(str(path)))
    for folder in _state()["folders"]:
        f = os.path.normcase(os.path.abspath(str(folder))).rstrip("\\/")
        if p == f or p.startswith(f + os.sep):
            return True
    return False


def blocked(error: BaseException, path: Path) -> bool:
    """Whether ``error``, raised while changing ``path``, is Controlled folder access refusing it."""
    if not _IS_WINDOWS or not isinstance(error, OSError):
        return False
    codes = {getattr(error, "winerror", None), error.errno, *(a for a in error.args if isinstance(a, int))}
    if not codes & _DENIED and not (codes & _NOT_FOUND and os.path.lexists(path)):
        return False
    try:
        return enabled() and not _allowed() and protected(path)
    except Exception:  # noqa: BLE001 - never let the explanation itself fail
        return False


def open_settings() -> None:
    """Windows Security (where the allowed apps are chosen)."""
    for target in ("windowsdefender:", "ms-settings:windowsdefender"):
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            return
        except OSError:
            continue


__all__ = ["SHORT", "HOW", "enabled", "protected", "blocked", "open_settings"]
