"""Power: keep the computer awake while a job runs, and tell whether it's running on battery.

Windows: SetThreadExecutionState (per thread: call ``awake()`` on the thread doing the work). The screen may
still turn off; only sleep is held off. Linux: ``systemd-inhibit`` while the job runs. macOS: ``caffeinate``.
Everything here is best effort: if the platform says no, the job simply runs as before.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

ENABLED = True            # Settings › "Keep the computer awake while working"

_lock = threading.Lock()
_holders = 0
_inhibitor: subprocess.Popen | None = None


def _hold_other() -> None:
    global _inhibitor
    cmd = None
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i", "-w", str(os.getpid())]
    elif shutil.which("systemd-inhibit"):
        cmd = ["systemd-inhibit", "--what=sleep:idle", "--who=Compressor", "--why=Compressing files",
               "--mode=block", "sleep", "infinity"]
    if cmd:
        try:
            _inhibitor = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        except OSError:
            _inhibitor = None


def _release_other() -> None:
    global _inhibitor
    if _inhibitor is not None:
        try:
            _inhibitor.terminate()
            _inhibitor.wait(5)
        except Exception:  # noqa: BLE001
            pass
        _inhibitor = None


@contextmanager
def awake(enabled: bool = True) -> Iterator[None]:
    """Don't let the computer sleep until the block ends (nested and parallel uses are fine)."""
    global _holders
    if not (enabled and ENABLED):
        yield
        return
    win = None
    if os.name == "nt":
        try:
            import ctypes

            win = ctypes.windll.kernel32
            win.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        except Exception:  # noqa: BLE001
            win = None
    else:
        with _lock:
            _holders += 1
            if _holders == 1:
                _hold_other()
    try:
        yield
    finally:
        if win is not None:
            try:
                win.SetThreadExecutionState(_ES_CONTINUOUS)
            except Exception:  # noqa: BLE001
                pass
        elif os.name != "nt":
            with _lock:
                _holders -= 1
                if _holders == 0:
                    _release_other()


def on_battery() -> bool | None:
    """True on battery, False on mains power, None if it can't be told (desktops: no battery)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _Status(ctypes.Structure):
                _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                            ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                            ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

            st = _Status()
            if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
                return None
            if st.BatteryFlag & 128:          # no system battery
                return None
            return {0: True, 1: False}.get(st.ACLineStatus & 0xFF)
        except Exception:  # noqa: BLE001
            return None
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        if "InternalBattery" not in out:
            return None
        return "Battery Power" in out
    return linux_on_battery(Path("/sys/class/power_supply"))


def linux_on_battery(root: Path) -> bool | None:
    try:
        supplies = list(root.iterdir())
    except OSError:
        return None
    batteries = [s for s in supplies if _read(s / "type") == "Battery"]
    mains = [s for s in supplies if _read(s / "type") in ("Mains", "USB", "USB_C", "USB_PD")]
    if not batteries:
        return None
    if mains:
        return not any(_read(m / "online") == "1" for m in mains)
    return all(_read(b / "status") == "Discharging" for b in batteries)


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


__all__ = ["awake", "on_battery", "linux_on_battery"]


# ---------------------------------------------------------------- being a good neighbour (Windows)

_THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
_THREAD_MODE_BACKGROUND_END = 0x00020000


@contextmanager
def background():
    """Work nobody is waiting for (watched folders, the monthly check, re-compressing updated games): this
    thread runs in Windows' background mode, at low CPU, disk and memory priority, so whatever you're doing
    keeps the machine. Elsewhere: a lower scheduling priority for the thread where the OS has one."""
    started = False
    if os.name == "nt":
        try:
            import ctypes

            k = ctypes.windll.kernel32   # type: ignore[attr-defined]
            started = bool(k.SetThreadPriority(k.GetCurrentThread(), _THREAD_MODE_BACKGROUND_BEGIN))
        except Exception:  # noqa: BLE001
            started = False
    try:
        yield
    finally:
        if started:
            try:
                import ctypes

                k = ctypes.windll.kernel32   # type: ignore[attr-defined]
                k.SetThreadPriority(k.GetCurrentThread(), _THREAD_MODE_BACKGROUND_END)
            except Exception:  # noqa: BLE001
                pass


_efficiency = False


def set_efficiency_mode(on: bool) -> bool:
    """Windows 11's efficiency mode for the whole app (EcoQoS: efficient cores, lower clock; idle priority),
    for while it runs hidden in the tray with nothing you started. Returns whether it changed."""
    global _efficiency
    if os.name != "nt" or on == _efficiency:
        return False
    try:
        import ctypes

        class _Throttle(ctypes.Structure):
            _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]

        k = ctypes.windll.kernel32   # type: ignore[attr-defined]
        proc = k.GetCurrentProcess()
        state = _Throttle(1, 0x1, 0x1 if on else 0x0)           # PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        k.SetProcessInformation(proc, 4, ctypes.byref(state), ctypes.sizeof(state))   # ProcessPowerThrottling
        k.SetPriorityClass(proc, 0x40 if on else 0x20)          # IDLE / NORMAL_PRIORITY_CLASS
        _efficiency = on
        return True
    except Exception:  # noqa: BLE001 - older Windows: nothing to do
        return False
