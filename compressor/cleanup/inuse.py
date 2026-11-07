"""Which projects have something running in them right now: a terminal open in the folder, a dev server
(``npm run dev`` runs node from the project's node_modules), a program built there, a venv's Python.

Linux reads /proc. Windows reads each process's working folder and command line from its process
parameters (what Task Manager's "command line" column shows); processes of other users are skipped.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_SHELLS = {"cmd", "powershell", "pwsh", "bash", "zsh", "fish", "sh", "dash", "nu", "wsl", "tmux", "screen"}


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    exe: str = ""
    cwd: str = ""
    cmdline: str = ""


def _linux() -> list[Proc]:
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        base = f"/proc/{d}"
        try:
            with open(f"{base}/stat", "rb") as f:
                stat = f.read().decode("utf-8", "replace")
            name = stat[stat.index("(") + 1:stat.rindex(")")]
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError):
            continue
        p = Proc(int(d), ppid, name)
        for attr, link in (("cwd", "cwd"), ("exe", "exe")):
            try:
                setattr(p, attr, os.readlink(f"{base}/{link}"))
            except OSError:
                pass
        try:
            with open(f"{base}/cmdline", "rb") as f:
                p.cmdline = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            pass
        out.append(p)
    return out


def _windows() -> list[Proc]:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.ReadProcessMemory.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_size_t))
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)      # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    procs: list[Proc] = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            procs.append(Proc(e.th32ProcessID, e.th32ParentProcessID, os.path.splitext(e.szExeFile)[0]))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)

    wide = ctypes.sizeof(ctypes.c_void_p) == 8

    def read(h, addr: int, size: int) -> bytes | None:
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)) or got.value != size:
            return None
        return buf.raw

    def ustr(h, addr: int) -> str:
        raw = read(h, addr, 16)                      # UNICODE_STRING: Length, MaximumLength, (pad), Buffer
        if raw is None:
            return ""
        length = int.from_bytes(raw[0:2], "little")
        ptr = int.from_bytes(raw[8:16], "little")
        if not length or not ptr or length > 65534:
            return ""
        data = read(h, ptr, length)
        return data.decode("utf-16-le", "replace") if data else ""

    for p in procs:
        if p.pid in (0, 4):
            continue
        h = k32.OpenProcess(0x1000 | 0x10, False, p.pid)   # QUERY_LIMITED_INFORMATION | VM_READ
        if not h:
            continue
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                p.exe = buf.value
            wow = wintypes.BOOL(False)
            k32.IsWow64Process(h, ctypes.byref(wow))
            if not wide or wow.value:
                continue                              # 32-bit process: different layout; the program path will do
            pbi = (ctypes.c_void_p * 6)()             # PROCESS_BASIC_INFORMATION; PebBaseAddress is the 2nd field
            if ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None) != 0:
                continue
            peb = pbi[1] or 0
            raw = read(h, peb + 0x20, 8)               # PEB.ProcessParameters
            if raw is None:
                continue
            params = int.from_bytes(raw, "little")
            p.cwd = ustr(h, params + 0x38)             # CurrentDirectory.DosPath
            p.cmdline = ustr(h, params + 0x70)         # CommandLine
        except OSError:
            pass
        finally:
            k32.CloseHandle(h)
    return procs


def processes() -> list[Proc]:
    try:
        if sys.platform == "win32":
            return _windows()
        if os.path.isdir("/proc"):
            return _linux()
    except Exception:  # noqa: BLE001 - a nicety: never fail a scan over it
        pass
    return []


def _key(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)).rstrip("\\/") + os.sep


def busy_projects(projects: list[Path], procs: list[Proc] | None = None) -> dict[Path, str]:
    """Each project with something running in it → why ("a terminal is open in it", "node is running from
    it"). This app's own processes don't count."""
    procs = processes() if procs is None else procs
    me = os.getpid()
    ours = {me} | {p.pid for p in procs if p.ppid == me}
    keys = sorted(((_key(str(p)), p) for p in projects), key=lambda kp: -len(kp[0]))   # most specific first
    out: dict[Path, str] = {}
    for proc in procs:
        if proc.pid in ours:
            continue
        name = (os.path.splitext(os.path.basename(proc.exe))[0] if proc.exe else proc.name) or "a program"
        cwd = _key(proc.cwd) if proc.cwd else ""
        exe = _key(proc.exe) if proc.exe else ""
        cmd = os.path.normcase(proc.cmdline)
        for key, project in keys:
            if project in out:
                continue
            if cwd.startswith(key):
                out[project] = ("a terminal is open in it" if name.lower() in _SHELLS
                                else f"{name} is running in it")
            elif exe.startswith(key) or (key in cmd or key.rstrip(os.sep) + " " in cmd + " "):
                out[project] = f"{name} is running from it"
            else:
                continue
            break
    return out


__all__ = ["processes", "busy_projects", "Proc"]
