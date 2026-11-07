"""WSL and Docker Desktop virtual disks: they grow but never shrink by themselves.

Each WSL distro (and Docker Desktop's data) lives in one .vhdx file. Deleting files inside Linux frees
space inside it, but the file keeps its size. Compacting gives that space back to Windows:

1. trim each distro's free space (``fstrim``), so the disk knows which blocks are unused;
2. shut WSL down completely (``wsl --shutdown``): every distro, and everything running in them;
3. Windows' ``diskpart`` compacts each disk (attached read-only), with one administrator prompt.

Nothing inside the disks changes. Docker Desktop has to be quit first; the app refuses while it runs.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .common import Candidate, cancelled

IS_WINDOWS = os.name == "nt"
GROUP = "WSL and Docker virtual disks"
MIN_FILE = 2 * 1024 ** 3          # smaller disks aren't worth a WSL shutdown
_NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _wsl(*args: str, timeout: float = 60) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["wsl.exe", *args], capture_output=True, timeout=timeout, creationflags=_NOWINDOW)
    except (OSError, subprocess.SubprocessError):
        return None


def _text(raw: bytes) -> str:
    """wsl.exe prints UTF-16 when talking about itself, UTF-8 for programs run inside a distro."""
    if raw[1:2] == b"\0" or raw.count(b"\0") > len(raw) // 4:
        return raw.decode("utf-16-le", "replace")
    return raw.decode("utf-8", "replace")


def distros() -> list[tuple[str, Path]]:
    """(name, .vhdx) of every WSL distro, from Windows' list of them."""
    if not IS_WINDOWS:
        return []
    import winreg

    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss") as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                        name = str(winreg.QueryValueEx(k, "DistributionName")[0])
                        base = str(winreg.QueryValueEx(k, "BasePath")[0]).removeprefix("\\\\?\\")
                        try:
                            vhd = str(winreg.QueryValueEx(k, "VhdFileName")[0])
                        except OSError:
                            vhd = "ext4.vhdx"
                except OSError:
                    continue
                path = Path(base) / vhd
                if path.is_file():
                    out.append((name, path))
    except OSError:
        pass
    return out


def docker_disks() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    found = list((local / "Docker" / "wsl" / "disk").glob("*.vhdx")) + [local / "Docker" / "wsl" / "data" / "ext4.vhdx"]
    return [p for p in found if p.is_file()]


def running() -> list[str]:
    proc = _wsl("--list", "--running", "--quiet", timeout=20)
    if proc is None or proc.returncode != 0:
        return []
    return [x.strip() for x in _text(proc.stdout).splitlines() if x.strip()]


def docker_desktop_running() -> bool:
    from .caches import running_processes

    names = running_processes()
    return any(n.startswith("docker desktop") or n == "com.docker.backend" for n in names)


def _used(name: str) -> int | None:
    """Bytes in use inside a running distro's disk."""
    proc = _wsl("-d", name, "-u", "root", "-e", "df", "-B1", "--output=used", "/", timeout=30)
    if proc is None or proc.returncode != 0:
        return None
    nums = [x for x in _text(proc.stdout).split() if x.isdigit()]
    return int(nums[-1]) if nums else None


def find() -> list[Candidate]:
    """Virtual disks worth compacting, never pre-selected (compacting stops everything in WSL)."""
    if not IS_WINDOWS:
        return []
    live = set(running())
    items = []
    seen = set()
    for name, path in distros():
        seen.add(os.path.normcase(str(path)))
        size = path.stat().st_size
        if size < MIN_FILE:
            continue
        used = _used(name) if name in live else None
        if used is not None:
            gain = max(0, size - used)
            if gain < 1024 ** 3:
                continue                       # less than 1 GB to gain: not worth shutting WSL down
            detail = f"About {gain / 1024 ** 3:.1f} GB inside it is free and can go back to Windows."
        else:
            gain = 0
            detail = "How much comes back is measured while compacting (it isn't running now)."
        items.append(Candidate(path, gain, title=f"{name} (WSL)", detail=detail + " Nothing inside changes.",
                               is_dir=False, group=GROUP, selected=False,
                               extra={"vdisk": "wsl", "distro": name, "file_size": size, "location": str(path)}))
    for path in docker_disks():
        if os.path.normcase(str(path)) in seen or path.stat().st_size < MIN_FILE:
            continue
        items.append(Candidate(path, 0, title="Docker Desktop data", is_dir=False, group=GROUP, selected=False,
                               detail="Space freed by removed images and containers goes back to Windows; measured "
                                      "while compacting. Nothing inside changes.",
                               extra={"vdisk": "docker", "file_size": path.stat().st_size, "location": str(path)}))
    return items


def diskpart_script(paths: list[Path]) -> str:
    lines = []
    for p in paths:
        lines += [f'select vdisk file="{p}"', "attach vdisk readonly", "compact vdisk", "detach vdisk"]
    return "\n".join(lines) + "\nexit\n"


def compact(items: list[Candidate], on_line: Callable[[str], None] | None = None,
            cancel: threading.Event | None = None) -> list[tuple[Candidate, bool, str, int]]:
    """Trim, shut WSL down, compact. Returns (item, ok, message, bytes freed) for each."""
    from ..fileops import remove_quietly, temp_name

    def say(msg: str) -> None:
        if on_line:
            on_line(msg)

    if docker_desktop_running():
        return [(c, False, "Quit Docker Desktop first (right-click its icon by the clock → Quit Docker Desktop)", 0)
                for c in items]
    for c in items:
        if cancelled(cancel):
            return [(x, False, "Stopped", 0) for x in items]
        if c.extra.get("vdisk") == "wsl":
            say(f"Trimming free space in {c.extra['distro']}…")
            _wsl("-d", c.extra["distro"], "-u", "root", "-e", "fstrim", "-av", timeout=900)
    say("Shutting WSL down…")
    _wsl("--shutdown", timeout=120)
    time.sleep(3)
    before = {c.path: c.path.stat().st_size for c in items if c.path.is_file()}
    script = temp_name(Path(os.environ.get("TEMP") or "."), ".txt", "-diskpart")
    script.write_text(diskpart_script([c.path for c in items]), encoding="ascii", errors="replace")
    say("Compacting (Windows asks for administrator permission)…")
    try:
        cmd = ["powershell", "-NoProfile", "-Command",
               f"$p = Start-Process -FilePath diskpart.exe -ArgumentList '/s','{script}' -Verb RunAs "
               "-WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode"]
        proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NOWINDOW)
    except OSError as e:
        return [(c, False, str(e), 0) for c in items]
    finally:
        remove_quietly(script)
    if proc.returncode != 0:
        msg = "The administrator prompt was declined" if "cancel" in (proc.stderr or "").lower() else \
            f"diskpart couldn't compact the disks (code {proc.returncode}); is something still using them?"
        return [(c, False, msg, 0) for c in items]
    out = []
    for c in items:
        try:
            freed = max(0, before.get(c.path, 0) - c.path.stat().st_size)
        except OSError:
            freed = 0
        out.append((c, True, f"Compacted: {freed / 1024 ** 3:.1f} GB back" if freed else "Compacted", freed))
    return out


__all__ = ["find", "compact", "running", "docker_desktop_running", "diskpart_script", "distros", "GROUP"]
