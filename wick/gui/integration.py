"""Desktop integration: Explorer/file-manager menu, start at login, single instance."""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .. import APP_NAME

IS_WINDOWS = os.name == "nt"
_MENU_KEYS = (r"Software\Classes\*\shell\Wick", r"Software\Classes\Directory\shell\Wick")
# "Convert with Wick": one submenu per kind of file (Windows' perceived types), with the usual formats.
_CONVERT_KEY = r"Software\Classes\SystemFileAssociations\{kind}\shell\WickConvert"
CONVERT_MENUS = {
    "image": [("JPEG", "jpeg"), ("PNG", "png"), ("WebP", "webp"), ("Icon (.ico)", "ico")],
    "video": [("MP4 (plays everywhere)", "mp4_h264"), ("GIF", "gif"), ("Animated WebP", "webp_anim"),
              ("Sound Only (MP3)", "audio_mp3")],
    "audio": [("MP3", "mp3"), ("AAC (.m4a)", "aac"), ("FLAC", "flac"), ("WAV", "wav")],
}
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command(*extra: str) -> list[str]:
    """How to start this app again (frozen .exe or ``python -m wick``)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *extra]
    exe = Path(sys.executable)
    if IS_WINDOWS and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"  # no console window
    return [str(exe), "-m", "wick", *extra]


def _quote(parts: list[str]) -> str:
    return " ".join(f'"{p}"' if not p.startswith('"') else p for p in parts)


# ------------------------------------------------------------------ context menu

def context_menu_installed() -> bool:
    if IS_WINDOWS:
        import winreg

        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MENU_KEYS[0]))
            return True
        except OSError:
            return False
    return _desktop_file().exists()


def _menu_entries() -> list[tuple[str, list[str]]]:
    """(label, extra arguments) for each item of the right-click submenu."""
    from .. import presets

    return [("With current options", [])] + [(name, ["--preset", name]) for name in presets.names()]


def _delete_tree(root, key: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(root, key) as k:
            subs = []
            i = 0
            while True:
                try:
                    subs.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except OSError:
        return
    for sub in subs:
        _delete_tree(root, key + "\\" + sub)
    try:
        winreg.DeleteKey(root, key)
    except OSError:
        pass


def _convert_items(kind: str, presets: dict | None = None) -> list[tuple[str, list[str]]]:
    """(label, extra arguments) for the "Convert with Wick" submenu of one kind of file: the usual formats,
    your Convert presets that convert this kind, then Other Format…"""
    from . import convert_presets

    if presets is None:
        try:
            from .main_window import open_qsettings

            presets = convert_presets.load(open_qsettings())
        except Exception:  # noqa: BLE001 - the menu still works without them
            presets = {}
    items = [(label, ["--convert", target]) for label, target in CONVERT_MENUS[kind]]
    items += [(name, ["--convert", f"preset:{name}"]) for name, p in sorted(presets.items(), key=lambda kv: kv[0].lower())
              if kind in convert_presets.kinds(p)]
    return items + [("Other Format…", ["--convert", "choose"])]


def install_context_menu() -> None:
    """Explorer: “Compress with Wick” (current options + one item per preset) for files and folders, and
    “Convert with Wick” for pictures, videos and audio (the usual formats, or Convert to pick another)."""
    if IS_WINDOWS:
        import winreg

        for kind in CONVERT_MENUS:
            key = _CONVERT_KEY.format(kind=kind)
            _delete_tree(winreg.HKEY_CURRENT_USER, key)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, "Convert with Wick")
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, launch_command()[0])
                winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ, "")
            items = _convert_items(kind)
            for i, (label, extra) in enumerate(items):
                item = key + rf"\shell\{i:02d}"
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, item) as k:
                    winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, label)
                    winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
                    if i in (len(CONVERT_MENUS[kind]) - 1, len(items) - 2):      # (formats | presets | other)
                        winreg.SetValueEx(k, "CommandFlags", 0, winreg.REG_DWORD, 0x40)  # separator after
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, item + r"\command") as k:
                    winreg.SetValueEx(k, None, 0, winreg.REG_SZ, _quote(launch_command(*extra)) + ' "%1"')
        icon = launch_command()[0]
        for key in _MENU_KEYS:
            _delete_tree(winreg.HKEY_CURRENT_USER, key)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, "Compress with Wick")
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon)
                winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ, "")
            entries = _menu_entries()
            if key == r"Software\Classes\Directory\shell\Wick":
                entries = [*entries, ("App/game folder compression…", ["--open=apps"])]
            for i, (label, extra) in enumerate(entries):
                item = key + rf"\shell\{i:02d}"
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, item) as k:
                    winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, label)
                    winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
                    if i == 0:
                        winreg.SetValueEx(k, "CommandFlags", 0, winreg.REG_DWORD, 0x40)  # separator after
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, item + r"\command") as k:
                    winreg.SetValueEx(k, None, 0, winreg.REG_SZ, _quote(launch_command(*extra)) + ' "%1"')
        install_modern_menu()
        return
    path = _desktop_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_desktop_entry(), encoding="utf-8")


def refresh_context_menu() -> None:
    """Rebuild the menu after presets change (only if it's installed)."""
    try:
        if context_menu_installed():
            install_context_menu()
    except OSError:
        pass


def uninstall_context_menu(wait: bool = False) -> None:
    if IS_WINDOWS:
        import winreg

        for key in (*_MENU_KEYS, *(_CONVERT_KEY.format(kind=k) for k in CONVERT_MENUS)):
            _delete_tree(winreg.HKEY_CURRENT_USER, key)
        uninstall_modern_menu(wait)
        return
    try:
        _desktop_file().unlink()
    except OSError:
        pass


# ------------------------------------------------------------------ Default apps and Open with
# Lists Wick in Settings › Apps › Default apps (with the file types it handles) and in Explorer's "Open
# with" for pictures, videos and music, without making it the default for anything. Opening a file with it adds
# the file to Compress. Per user, for the installed app (the installer's set-up and each user's first start).

_CAPABILITIES = r"Software\Lanntern\Wick\Capabilities"
_PROGIDS = {"Wick.Picture": "Picture", "Wick.Video": "Video", "Wick.Audio": "Music or audio file"}


def _file_types() -> dict[str, str]:
    """extension -> Wick's ProgID for it."""
    from .. import formats

    types = {ext: "Wick.Picture" for ext in formats._EXT_KIND}
    types.update({ext: "Wick.Video" for ext in formats.VIDEO_EXTS})
    types.update({ext: "Wick.Audio" for ext in formats.AUDIO_EXTS})
    return types


def installed_copy() -> bool:
    """Running from an installed copy (the installer put an uninstaller next to it), not portable or source."""
    return getattr(sys, "frozen", False) and (Path(sys.executable).parent / "unins000.exe").is_file()


def file_types_registered() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\RegisteredApplications") as k:
            return winreg.QueryValueEx(k, APP_NAME)[0] == _CAPABILITIES
    except OSError:
        return False


def register_file_types(exe: str | None = None) -> None:
    """Add Wick to Default apps and Open with (``exe``: the program to start; this one by default)."""
    if not IS_WINDOWS:
        return
    import winreg

    command = _quote([exe] if exe else launch_command()) + ' "%1"'
    icon = (exe or launch_command()[0]) + ",0"
    hk = winreg.HKEY_CURRENT_USER
    for progid, kind in _PROGIDS.items():
        with winreg.CreateKey(hk, rf"Software\Classes\{progid}") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f"{kind} (compress with {APP_NAME})")
            winreg.SetValueEx(k, "FriendlyTypeName", 0, winreg.REG_SZ, kind)
        with winreg.CreateKey(hk, rf"Software\Classes\{progid}\DefaultIcon") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, icon)
        with winreg.CreateKey(hk, rf"Software\Classes\{progid}\shell\open") as k:
            winreg.SetValueEx(k, "FriendlyAppName", 0, winreg.REG_SZ, APP_NAME)
        with winreg.CreateKey(hk, rf"Software\Classes\{progid}\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)
    with winreg.CreateKey(hk, _CAPABILITIES) as k:
        winreg.SetValueEx(k, "ApplicationName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(k, "ApplicationDescription", 0, winreg.REG_SZ,
                          "Shrink photos, videos and music safely: every result verified, every change undoable.")
        winreg.SetValueEx(k, "ApplicationIcon", 0, winreg.REG_SZ, icon)
    exe_name = Path(exe or launch_command()[0]).name
    with winreg.CreateKey(hk, _CAPABILITIES + r"\FileAssociations") as assoc, \
            winreg.CreateKey(hk, rf"Software\Classes\Applications\{exe_name}\SupportedTypes") as supported:
        for ext, progid in _file_types().items():
            winreg.SetValueEx(assoc, ext, 0, winreg.REG_SZ, progid)
            winreg.SetValueEx(supported, ext, 0, winreg.REG_SZ, "")
            with winreg.CreateKey(hk, rf"Software\Classes\{ext}\OpenWithProgids") as k:
                winreg.SetValueEx(k, progid, 0, winreg.REG_NONE, b"")    # listed in Open with, not the default
    with winreg.CreateKey(hk, rf"Software\Classes\Applications\{exe_name}") as k:
        winreg.SetValueEx(k, "FriendlyAppName", 0, winreg.REG_SZ, APP_NAME)
    with winreg.CreateKey(hk, r"Software\RegisteredApplications") as k:
        winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _CAPABILITIES)
    _associations_changed()


def unregister_file_types(exe_name: str = "Wick.exe") -> None:
    if not IS_WINDOWS:
        return
    import winreg

    hk = winreg.HKEY_CURRENT_USER
    for ext, progid in _file_types().items():
        try:
            with winreg.OpenKey(hk, rf"Software\Classes\{ext}\OpenWithProgids", 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, progid)
        except OSError:
            pass
    try:
        with winreg.OpenKey(hk, r"Software\RegisteredApplications", 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
    except OSError:
        pass
    for key in (*(rf"Software\Classes\{p}" for p in _PROGIDS), r"Software\Lanntern\Wick",
                rf"Software\Classes\Applications\{exe_name}"):
        _delete_tree(hk, key)
    _associations_changed()


def _associations_changed() -> None:
    try:
        import ctypes

        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # type: ignore[attr-defined]  # SHCNE_ASSOCCHANGED
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ Windows 11's main menu
# Windows 11 shows only packaged IExplorerCommand handlers in its main right-click menu; everything else waits
# under "Show more options". The build ships shell/WickShell.dll (packaging/shell/WickShell.cpp) and
# a small unsigned sparse package (packaging/make_shell_package.py) that gives this folder that identity. It
# reads the classic menu's keys above, so both menus always match. Registered per user, only when it changed.

MODERN_PACKAGE = "Lanntern.Wick.ContextMenu"
_MODERN_MARKER = r"Software\Wick\Shell"


def _modern_menu_files() -> tuple[Path, Path] | None:
    """(the app folder, the package) when this build has them (installed or portable, Windows 11)."""
    if not IS_WINDOWS or not getattr(sys, "frozen", False):
        return None
    try:
        if sys.getwindowsversion().build < 22000:  # type: ignore[attr-defined]
            return None
    except AttributeError:
        return None
    base = Path(sys.executable).resolve().parent
    pkg = base / "shell" / "WickShell.msix"
    return (base, pkg) if pkg.is_file() and (base / "shell" / "WickShell.dll").is_file() else None


def _ps_literal(text) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _powershell(script: str, wait: bool) -> bool:
    """Run a PowerShell script hidden; ``wait`` for it (True if it succeeded), or start it and return True.
    The script is passed as plain text on standard input (readable by anything inspecting it, unlike an encoded
    command). Bypass is needed here: the Appx cmdlets load module files, which Windows' default policy blocks."""
    import subprocess

    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    text = "$ErrorActionPreference = 'Stop'\n" + script + "\n"
    args = [str(exe), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "-"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if wait:
            return subprocess.run(args, input=text, capture_output=True, text=True, timeout=120,
                                  creationflags=flags).returncode == 0
        p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             text=True, creationflags=flags)
        assert p.stdin is not None
        p.stdin.write(text)
        p.stdin.close()
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _modern_marker() -> str:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MODERN_MARKER) as k:
            return str(winreg.QueryValueEx(k, "Registered")[0])
    except OSError:
        return ""


def install_modern_menu(wait: bool = False) -> bool:
    """Add Wick to Windows 11's main right-click menu for this user (does nothing when it's already there
    for this version and folder, or the build/Windows has no such menu)."""
    from .. import __version__

    files = _modern_menu_files()
    if files is None:
        return False
    base, pkg = files
    marker = f"{__version__}|{base}"
    if _modern_marker() == marker:
        return True
    script = (f"Add-AppxPackage -Path {_ps_literal(pkg)} -ExternalLocation {_ps_literal(base)} -AllowUnsigned "
              f"-ForceUpdateFromAnyVersion\n"
              f"New-Item -Force -Path {_ps_literal('HKCU:' + chr(92) + _MODERN_MARKER)} | Out-Null\n"
              f"Set-ItemProperty -Path {_ps_literal('HKCU:' + chr(92) + _MODERN_MARKER)} -Name Registered "
              f"-Value {_ps_literal(marker)}")
    return _powershell(script, wait)


def uninstall_modern_menu(wait: bool = False) -> bool:
    """Take Wick out of Windows 11's main menu for this user (the classic menu is handled separately)."""
    if not IS_WINDOWS or not _modern_marker():
        return False
    script = (f"Get-AppxPackage -Name {_ps_literal(MODERN_PACKAGE)} | Remove-AppxPackage\n"
              f"Remove-Item -Force -ErrorAction SilentlyContinue -Path "
              f"{_ps_literal('HKCU:' + chr(92) + _MODERN_MARKER)}")
    return _powershell(script, wait)


def _desktop_file() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "applications" / "wick.desktop"


def _desktop_entry(extra: str = "%F") -> str:
    exec_line = " ".join(f'"{p}"' if " " in p else p for p in launch_command()) + f" {extra}"
    return (
        "[Desktop Entry]\nType=Application\nName=Wick\n"
        "Comment=Compress images, videos and audio safely\n"
        f"Exec={exec_line}\nTerminal=false\nCategories=Graphics;Utility;\n"
        "MimeType=image/jpeg;image/png;image/gif;image/webp;image/tiff;image/bmp;image/heic;"
        "image/avif;video/mp4;video/quicktime;video/x-matroska;video/x-msvideo;"
        "audio/mpeg;audio/flac;audio/x-wav;audio/mp4;audio/ogg;inode/directory;\n"
    )


# ------------------------------------------------------------------ start at login

def autostart_enabled() -> bool:
    if IS_WINDOWS:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
                winreg.QueryValueEx(k, APP_NAME)
            return True
        except OSError:
            return False
    return _autostart_file().exists()


def set_autostart(enabled: bool) -> None:
    if IS_WINDOWS:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            if enabled:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _quote(launch_command("--background")))
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except OSError:
                    pass
        return
    path = _autostart_file()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_desktop_entry("--background"), encoding="utf-8")
    else:
        try:
            path.unlink()
        except OSError:
            pass


def _autostart_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / "wick.desktop"


# ------------------------------------------------------------------ single instance

def _server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    return f"{APP_NAME}-{user}"


def send_to_running_instance(paths: list[str], show: bool = True, preset: str = "", convert: str = "",
                             open_target: str = "") -> bool:
    """Hand ``paths`` to an already-running Wick. True if one answered."""
    sock = QLocalSocket()
    sock.connectToServer(_server_name())
    if not sock.waitForConnected(500):
        return False
    sock.write(json.dumps({"paths": paths, "show": show, "preset": preset, "convert": convert,
                           "open": open_target}).encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


class InstanceServer(QObject):
    """Receives paths from later launches (e.g. Explorer's context menu)."""

    # paths, show window, preset ("": current options), convert to, page to open (app.open_target)
    paths_received = Signal(list, bool, str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        QLocalServer.removeServer(_server_name())  # stale socket after a crash (Unix)
        self.server.listen(_server_name())
        self.server.newConnection.connect(self._on_connection)

    def _on_connection(self) -> None:
        sock = self.server.nextPendingConnection()
        buf = bytearray()

        def read():
            buf.extend(bytes(sock.readAll()))

        def done():
            read()
            try:
                msg = json.loads(bytes(buf).decode("utf-8"))
                self.paths_received.emit(list(msg.get("paths", [])), bool(msg.get("show", True)),
                                         str(msg.get("preset") or ""), str(msg.get("convert") or ""),
                                         str(msg.get("open") or ""))
            except ValueError:
                pass
            sock.deleteLater()

        sock.readyRead.connect(read)
        sock.disconnected.connect(done)

