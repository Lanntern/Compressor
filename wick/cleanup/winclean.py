"""Windows' own leftovers: the previous Windows installation, update leftovers, Delivery Optimization's
download cache, error reports, memory dumps, old temporary files.

System items belong to Windows and are removed with Windows' own tools (Disk Cleanup's handlers, DISM,
Delivery Optimization's cmdlet), all in one batch with one administrator prompt. They can't go to the
Recycle Bin and the app says so. Files in your own account (temporary files, your error reports) go to the
Recycle Bin like everything else.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from .common import Candidate, ProgressFn, ScanResult, cancelled, folder_size

IS_WINDOWS = os.name == "nt"
SYSTEM = "Windows (one administrator prompt)"
ACCOUNT = "Your account (Recycle Bin)"
_NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SAGESET = 4271   # our own Disk Cleanup profile number (cleanmgr /sagerun)

# key -> (title, what it is / what removing it means, Disk Cleanup handler names)
SYSTEM_ITEMS = {
    "windows_old": ("Previous Windows installation (Windows.old)",
                    "Kept after a big Windows update so you can go back to the old version; removing it means you "
                    "can't roll back. Removed by Windows' Disk Cleanup.", ["Previous Installations"]),
    "update_cleanup": ("Windows Update leftovers",
                       "Older versions of updated system components. Removed by Windows itself (DISM component "
                       "cleanup); installed updates can still be uninstalled.", []),
    "delivery_optimization": ("Delivery Optimization cache",
                              "Update files Windows keeps to share with other PCs; downloaded again if needed.", []),
    "error_reports": ("System error reports", "Crash reports Windows already sent or no longer needs.",
                      ["Windows Error Reporting Files"]),
    "memory_dumps": ("Memory dump files", "Snapshots taken when Windows crashed; only useful for debugging.",
                     ["System error memory dump files", "System error minidump files"]),
    "setup_logs": ("Windows upgrade logs and setup files",
                   "Logs and temporary files left by Windows upgrades.",
                   ["Windows Upgrade Log Files", "Temporary Setup Files", "Setup Log Files"]),
    "old_drivers": ("Old driver versions",
                    "Earlier versions of drivers Windows keeps after updating them (graphics drivers pile up). "
                    "Drivers in use stay; Windows' Disk Cleanup decides which are old.", ["Device Driver Packages"]),
    "compact_os": ("Compress Windows itself (Compact OS)",
                   "Windows' own compression for its system files and built-in apps: usually 2-4 GB, everything "
                   "keeps working. Undoing it later turns it off again.", []),
}


def supported() -> bool:
    return IS_WINDOWS


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _system_paths() -> dict[str, list[Path]]:
    windir = Path(_env("SystemRoot", r"C:\Windows"))
    drive = Path(windir.drive + "\\") if windir.drive else Path("C:\\")
    pdata = Path(_env("ProgramData", r"C:\ProgramData"))
    return {
        "windows_old": [drive / "Windows.old"],
        "delivery_optimization": [windir / "ServiceProfiles" / "NetworkService" / "AppData" / "Local" / "Microsoft"
                                  / "Windows" / "DeliveryOptimization" / "Cache"],
        "error_reports": [pdata / "Microsoft" / "Windows" / "WER" / "ReportArchive",
                          pdata / "Microsoft" / "Windows" / "WER" / "ReportQueue"],
        "memory_dumps": [windir / "MEMORY.DMP", windir / "Minidump", windir / "LiveKernelReports"],
        "old_drivers": [windir / "System32" / "DriverStore" / "FileRepository"],
        "compact_os": [windir],
        "setup_logs": [drive / "$WINDOWS.~BT", drive / "$Windows.~WS", windir / "Panther", windir / "Logs" / "CBS"],
    }


def _size(paths: list[Path], cancel) -> tuple[int, bool]:
    """(bytes we could measure, whether anything exists). Folders we can't fully read are under-counted."""
    total, exists = 0, False
    for p in paths:
        try:
            if p.is_file():
                total += p.stat().st_size
                exists = True
            elif p.is_dir():
                exists = True
                total += folder_size(p, cancel)[0]
        except OSError:
            continue
    return total, exists


def _old_driver_bytes(cancel=None) -> int:
    """Driver packages with a newer copy of the same driver: every version but the newest of each
    "name.inf_arch_*" folder in the driver store (an estimate of what Disk Cleanup removes)."""
    repo = Path(_env("SystemRoot", r"C:\Windows")) / "System32" / "DriverStore" / "FileRepository"
    groups: dict[str, list[tuple[float, Path]]] = {}
    try:
        for e in os.scandir(repo):
            if e.is_dir(follow_symlinks=False) and ".inf_" in e.name:
                key = e.name.rsplit("_", 1)[0].lower()          # "nv_dispi.inf_amd64"
                groups.setdefault(key, []).append((e.stat().st_mtime, Path(e.path)))
    except OSError:
        return 0
    total = 0
    for versions in groups.values():
        if len(versions) < 2:
            continue
        for _mtime, folder in sorted(versions)[:-1]:            # all but the newest
            if cancelled(cancel):
                return total
            total += folder_size(folder, cancel)[0]
    return total


def compact_os_state() -> bool | None:
    """Is Windows already compressed with Compact OS? None when it can't be told."""
    try:
        out = subprocess.run(["compact.exe", "/compactos:query"], capture_output=True, text=True, timeout=60,
                             creationflags=_NOWINDOW).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return None
    if "not in the compact state" in out or "nicht im kompaktmodus" in out:
        return False
    if "in the compact state" in out:
        return True
    return None


def _delivery_cache_size() -> int:
    """Delivery Optimization reports its own cache size (its folder is readable only by the service)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-DeliveryOptimizationPerfSnap).CacheSizeBytes"], capture_output=True, text=True,
                             timeout=30, creationflags=_NOWINDOW).stdout.strip()
        return int(out.splitlines()[-1]) if out else 0
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0


def _old_temp_files(days: int, cancel, limit: int = 20000) -> tuple[list[Path], int]:
    """Your temporary files not touched for ``days`` (anything in use is skipped when moving)."""
    temp = Path(_env("TEMP") or _env("TMP") or "")
    if not temp.is_dir():
        return [], 0
    cutoff = time.time() - days * 86400
    out, total = [], 0
    for dirpath, _dirs, files in os.walk(temp):
        if cancelled(cancel) or len(out) >= limit:
            break
        for name in files:
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            if max(st.st_mtime, st.st_atime) < cutoff:
                out.append(p)
                total += st.st_size
    return out, total


def find(progress: ProgressFn | None = None, cancel: threading.Event | None = None,
         temp_days: int = 7) -> ScanResult:
    result = ScanResult("winclean")
    if not supported():
        result.notes.append("Available on Windows only.")
        return result
    paths = _system_paths()
    for key, (title, detail, _handlers) in SYSTEM_ITEMS.items():
        if cancelled(cancel):
            result.cancelled = True
            return result
        if progress:
            progress(f"Measuring {title}", 0, 0)
        if key == "update_cleanup":
            size, exists = 0, True          # only Windows knows (DISM's analysis needs administrator rights)
            detail += " Size known only after cleaning; often several hundred MB."
        elif key == "old_drivers":
            size = _old_driver_bytes(cancel)
            exists = size > 0
        elif key == "compact_os":
            state = compact_os_state()
            if state is not False:          # already compact, or can't tell
                continue
            size, exists = 2 * 1024 ** 3, True
            detail += " (The size is Windows' typical saving.)"
        elif key == "delivery_optimization":
            size = _delivery_cache_size()
            exists = size > 0
        else:
            size, exists = _size(paths.get(key, []), cancel)
        if not exists or (key != "update_cleanup" and size < 1024 * 1024):
            continue
        where = paths.get(key, [Path("windows") / key])[0]
        warning = "You can't go back to the previous Windows version afterwards" if key == "windows_old" else ""
        removed_by = (" Done by Windows (compact.exe)." if key == "compact_os" else
                      " Removed by Windows (not to the Recycle Bin).")
        result.items.append(Candidate(
            where, size, title=title, detail=detail + removed_by,
            is_dir=True, group=SYSTEM, warning=warning, selected=key != "compact_os",   # a setting: your call
            extra={"system": key, "location": str(where)}))
    # Your own account: to the Recycle Bin.
    files, size = _old_temp_files(temp_days, cancel)
    if size >= 1024 * 1024:
        temp = Path(_env("TEMP") or _env("TMP"))
        result.items.append(Candidate(
            temp, size, title=f"Temporary files older than {temp_days} days",
            detail=f"{len(files):,} files programs left behind. Files still in use are skipped.", is_dir=True,
            group=ACCOUNT, extra={"paths": [str(p) for p in files], "location": str(temp)}))
    wer = Path(_env("LOCALAPPDATA")) / "Microsoft" / "Windows" / "WER"
    size, exists = _size([wer / "ReportArchive", wer / "ReportQueue"], cancel)
    if exists and size >= 1024 * 1024:
        result.items.append(Candidate(
            wer, size, title="Your error reports", detail="Crash reports from programs you ran.", is_dir=True,
            group=ACCOUNT, extra={"paths": [str(wer / "ReportArchive"), str(wer / "ReportQueue")],
                                  "location": str(wer)}))
    result.notes.append("The page file and hibernation file aren't listed: Windows needs them (turn hibernation "
                        "off in Power settings if you never use it).")
    return result


def script(keys: list[str]) -> str:
    """The PowerShell that removes the chosen system items, run once with administrator rights."""
    lines = ["$ErrorActionPreference = 'Continue'"]
    handlers = [h for k in keys for h in SYSTEM_ITEMS.get(k, ("", "", []))[2]]
    if handlers:
        base = r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches"
        flag = f"StateFlags{SAGESET:04d}"
        for h in handlers:
            lines.append(f"if (Test-Path '{base}\\{h}') {{ Set-ItemProperty -Path '{base}\\{h}' -Name {flag} "
                         f"-Value 2 -Type DWord }}")
        lines.append(f"Start-Process -FilePath cleanmgr.exe -ArgumentList '/sagerun:{SAGESET}' -Wait")
        for h in handlers:   # leave Disk Cleanup's settings as they were
            lines.append(f"if (Test-Path '{base}\\{h}') {{ Remove-ItemProperty -Path '{base}\\{h}' -Name {flag} "
                         f"-ErrorAction SilentlyContinue }}")
    if "update_cleanup" in keys:
        lines.append("Start-Process -FilePath dism.exe -ArgumentList '/Online','/Cleanup-Image',"
                     "'/StartComponentCleanup' -Wait -WindowStyle Hidden")
    if "delivery_optimization" in keys:
        lines.append("Delete-DeliveryOptimizationCache -Force -ErrorAction SilentlyContinue")
    if "compact_os" in keys:
        lines.append("Start-Process -FilePath compact.exe -ArgumentList '/compactos:always' -Wait -WindowStyle Hidden")
    return "\n".join(lines)


def undo_compact_os() -> str:
    """Turn Compact OS off again (one administrator prompt)."""
    cmd = ["powershell", "-NoProfile", "-Command",
           "$p = Start-Process -FilePath compact.exe -ArgumentList '/compactos:never' -Verb RunAs -WindowStyle Hidden "
           "-Wait -PassThru; exit $p.ExitCode"]
    proc = subprocess.run(cmd, creationflags=_NOWINDOW, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("compact /compactos:never didn't finish (was the administrator prompt declined?)")
    return "Compact OS turned off"


def run_system(keys: list[str], cancel: threading.Event | None = None) -> tuple[bool, str]:
    """Run the batch elevated (one UAC prompt) and wait. (ok, message)."""
    if not keys:
        return True, ""
    from ..fileops import remove_quietly, temp_name

    ps1 = temp_name(Path(_env("TEMP") or "."), ".ps1", "-winclean")
    ps1.write_text(script(keys), encoding="utf-8-sig")
    try:
        cmd = ["powershell", "-NoProfile", "-Command",
               "$p = Start-Process -FilePath powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass',"
               f"'-File','{ps1}' -Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode"]
        proc = subprocess.run(cmd, creationflags=_NOWINDOW, capture_output=True, text=True)
        if proc.returncode != 0:
            msg = (proc.stderr or "").strip().splitlines()
            if any("canceled by the user" in m.lower() or "cancelled" in m.lower() for m in msg):
                return False, "The administrator prompt was declined"
            return False, (msg[-1] if msg else f"Windows' cleanup ended with code {proc.returncode}")[:200]
        return True, "Removed by Windows"
    finally:
        remove_quietly(ps1)


__all__ = ["find", "run_system", "script", "supported", "SYSTEM_ITEMS"]
