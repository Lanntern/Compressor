"""Windows notifications (toasts) instead of tray balloons: they wait in the notification centre, follow Do Not
Disturb, and can have buttons ("Show in History").

No new dependency: the app registers itself once under HKCU (an AppUserModelId entry with its name and icon,
which also works for the portable zip) and a ``compressor:`` link that the buttons open (handled by
app.open_target). Each toast is shown by a short, hidden Windows PowerShell call to the WinRT notification API.
Off in Settings, off Windows, or if anything fails, the caller falls back to the tray balloon.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .. import APP_NAME
from ..log import log
from .jumplist import APP_ID

SCHEME = "compressor"            # compressor:open/history  ->  --open=history
IS_WINDOWS = os.name == "nt"
_registered = False


def link(target: str) -> str:
    """The URL a button opens: ``target`` is an app.open_target target (history, cleanup, update...)."""
    return f"{SCHEME}:open/{target}"


def target_from_link(url: str) -> str:
    """``compressor:open/history`` -> ``history`` ("" for anything else)."""
    import re

    prefix = f"{SCHEME}:open/"
    url = url.strip().strip('"')
    target = url[len(prefix):].strip("/") if url.lower().startswith(prefix) else ""
    return target if re.fullmatch(r"[a-z]+(-[a-z]+)?", target) else ""     # a page name, nothing else


def toast_xml(title: str, message: str, actions=(), launch: str = "") -> str:
    """The toast's XML: a title, the message, and up to 3 (label, target) buttons."""
    buttons = "".join(
        f'<action content={quoteattr(label)} activationType="protocol" arguments={quoteattr(link(target))}/>'
        for label, target in list(actions)[:3])
    launch_attr = f' activationType="protocol" launch={quoteattr(link(launch))}' if launch else ""
    return (f"<toast{launch_attr}><visual><binding template=\"ToastGeneric\">"
            f"<text>{escape(title)}</text><text>{escape(message)}</text></binding></visual>"
            + (f"<actions>{buttons}</actions>" if buttons else "") + "</toast>")


def register() -> bool:
    """Make Windows show this app's toasts with its name and icon, and send compressor: links to it (per user,
    idempotent; links only for the installed/portable .exe, not when run from source)."""
    global _registered
    if _registered or not IS_WINDOWS:
        return _registered
    try:
        import winreg

        from ..journal import data_dir
        from .main_window import app_icon

        icon = data_dir() / "app-icon.png"
        if not icon.exists():
            app_icon().pixmap(256, 256).save(str(icon))
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
        if getattr(sys, "frozen", False):
            exe = Path(sys.executable)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{SCHEME}") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"URL:{APP_NAME}")
                winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{SCHEME}\DefaultIcon") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{exe}",0')
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{SCHEME}\shell\open\command") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{exe}" --url "%1"')
        _registered = True
    except Exception as e:  # noqa: BLE001
        log.info("Windows notifications unavailable: %s", e)
    return _registered


def unregister() -> None:
    """Remove what register() added (the uninstaller's clean-up)."""
    if not IS_WINDOWS:
        return
    import winreg

    for sub in (rf"Software\Classes\{SCHEME}\shell\open\command", rf"Software\Classes\{SCHEME}\shell\open",
                rf"Software\Classes\{SCHEME}\shell", rf"Software\Classes\{SCHEME}\DefaultIcon",
                rf"Software\Classes\{SCHEME}", rf"Software\Classes\AppUserModelId\{APP_ID}",
                # (Windows' own record of this app's notification settings, made when the first one was shown)
                rf"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\{APP_ID}"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            pass


_SCRIPT = """
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml(@'
{xml}
'@)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}').Show(
    [Windows.UI.Notifications.ToastNotification]::new($doc))
"""


def show(title: str, message: str, actions=(), launch: str = "") -> bool:
    """Show a toast (returns at once: PowerShell does the work in the background). False when it can't be shown
    this way, so the caller uses the tray balloon instead."""
    if not IS_WINDOWS or not register():
        return False
    try:
        script = _SCRIPT.format(xml=toast_xml(title, message, actions, launch), app_id=APP_ID)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        subprocess.Popen([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                          "-EncodedCommand", encoded], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except OSError as e:
        log.info("Toast not shown: %s", e)
        return False


__all__ = ["show", "register", "unregister", "toast_xml", "link", "target_from_link", "SCHEME"]
