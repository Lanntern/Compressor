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
_MENU_KEYS = (r"Software\Classes\*\shell\Compressor", r"Software\Classes\Directory\shell\Compressor")
# "Convert with Compressor": one submenu per kind of file (Windows' perceived types), with the usual formats.
_CONVERT_KEY = r"Software\Classes\SystemFileAssociations\{kind}\shell\CompressorConvert"
CONVERT_MENUS = {
    "image": [("JPEG", "jpeg"), ("PNG", "png"), ("WebP", "webp"), ("Icon (.ico)", "ico")],
    "video": [("MP4 (plays everywhere)", "mp4_h264"), ("GIF", "gif"), ("Animated WebP", "webp_anim"),
              ("Sound Only (MP3)", "audio_mp3")],
    "audio": [("MP3", "mp3"), ("AAC (.m4a)", "aac"), ("FLAC", "flac"), ("WAV", "wav")],
}
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command(*extra: str) -> list[str]:
    """How to start this app again (frozen .exe or ``python -m compressor``)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *extra]
    exe = Path(sys.executable)
    if IS_WINDOWS and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"  # no console window
    return [str(exe), "-m", "compressor", *extra]


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
    """(label, extra arguments) for the "Convert with Compressor" submenu of one kind of file: the usual formats,
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
    """Explorer: “Compress with Compressor” (current options + one item per preset) for files and folders, and
    “Convert with Compressor” for pictures, videos and audio (the usual formats, or Convert to pick another)."""
    if IS_WINDOWS:
        import winreg

        for kind in CONVERT_MENUS:
            key = _CONVERT_KEY.format(kind=kind)
            _delete_tree(winreg.HKEY_CURRENT_USER, key)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, "Convert with Compressor")
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
                winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, "Compress with Compressor")
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon)
                winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ, "")
            for i, (label, extra) in enumerate(_menu_entries()):
                item = key + rf"\shell\{i:02d}"
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, item) as k:
                    winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, label)
                    winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
                    if i == 0:
                        winreg.SetValueEx(k, "CommandFlags", 0, winreg.REG_DWORD, 0x40)  # separator after
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, item + r"\command") as k:
                    winreg.SetValueEx(k, None, 0, winreg.REG_SZ, _quote(launch_command(*extra)) + ' "%1"')
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


def uninstall_context_menu() -> None:
    if IS_WINDOWS:
        import winreg

        for key in (*_MENU_KEYS, *(_CONVERT_KEY.format(kind=k) for k in CONVERT_MENUS)):
            _delete_tree(winreg.HKEY_CURRENT_USER, key)
        return
    try:
        _desktop_file().unlink()
    except OSError:
        pass


def _desktop_file() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "applications" / "compressor.desktop"


def _desktop_entry(extra: str = "%F") -> str:
    exec_line = " ".join(f'"{p}"' if " " in p else p for p in launch_command()) + f" {extra}"
    return (
        "[Desktop Entry]\nType=Application\nName=Compressor\n"
        "Comment=Compress images, videos and audio safely\n"
        f"Exec={exec_line}\nTerminal=false\nCategories=Graphics;Utility;\n"
        "MimeType=image/jpeg;image/png;image/gif;image/webp;image/tiff;image/bmp;image/heic;"
        "image/avif;video/mp4;video/quicktime;video/x-matroska;video/x-msvideo;audio/mpeg;audio/flac;audio/x-wav;audio/mp4;audio/ogg;inode/directory;\n"
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
    return Path(base) / "autostart" / "compressor.desktop"


# ------------------------------------------------------------------ single instance

def _server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    return f"{APP_NAME}-{user}"


def send_to_running_instance(paths: list[str], show: bool = True, preset: str = "", convert: str = "",
                             open_target: str = "") -> bool:
    """Hand ``paths`` to an already-running Compressor. True if one answered."""
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
