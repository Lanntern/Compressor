"""Windows notifications (toasts) instead of tray balloons: they wait in the notification centre, follow Do Not
Disturb, and can have buttons ("Show in History").

No new dependency: the app registers itself once under HKCU (an AppUserModelId entry with its name and icon,
which also works for the portable zip) and a ``wick:`` link that the buttons open (handled by
app.open_target). Each toast goes straight to Windows' notification API (WinRT, through ctypes: see winrt.py).
Off in Settings, off Windows, or if anything fails, the caller falls back to the tray balloon.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .. import APP_NAME
from ..log import log
from .jumplist import APP_ID

SCHEME = "wick"            # wick:open/history  ->  --open=history
IS_WINDOWS = os.name == "nt"
_registered = False


def link(target: str) -> str:
    """The URL a button opens: ``target`` is an app.open_target target (history, cleanup, update...)."""
    return f"{SCHEME}:open/{target}"


def target_from_link(url: str) -> str:
    """``wick:open/history`` -> ``history`` ("" for anything else)."""
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
    """Make Windows show this app's toasts with its name and icon, and send wick: links to it (per user,
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


IID_IToastNotificationManagerStatics = "50ac103f-d235-4598-bbef-98fe4d1a3ad4"
IID_IToastNotificationFactory = "04124b20-82c6-4229-b109-fd9ed4662b53"
IID_IXmlDocument = "f7f3a506-1e87-42d6-bcfb-b8c809fa5494"
IID_IXmlDocumentIO = "6cd0e74e-ee65-4489-9ebf-ca43e87ba637"


def _build(xml: str):
    """The notifier for this app and the toast to show (WinRT objects; the caller releases both)."""
    import ctypes

    from . import winrt

    made = []
    try:
        statics = winrt.factory("Windows.UI.Notifications.ToastNotificationManager",
                                IID_IToastNotificationManagerStatics)
        made.append(statics)
        with winrt.hstring(APP_ID) as app_id:
            notifier = winrt.call_out(statics, 7, (ctypes.c_void_p,), app_id)        # CreateToastNotifierWithId
        made.append(notifier)
        doc = winrt.activate("Windows.Data.Xml.Dom.XmlDocument")
        made.append(doc)
        io = doc.query(IID_IXmlDocumentIO)
        made.append(io)
        with winrt.hstring(xml) as text:
            io.call(6, (ctypes.c_void_p,), text)                                       # LoadXml
        xml_doc = doc.query(IID_IXmlDocument)
        made.append(xml_doc)
        toast_factory = winrt.factory("Windows.UI.Notifications.ToastNotification", IID_IToastNotificationFactory)
        made.append(toast_factory)
        toast = winrt.call_out(toast_factory, 6, (ctypes.c_void_p,), xml_doc.ptr)     # CreateToastNotification
        made.remove(notifier)                    # (handed to the caller)
        return notifier, toast
    finally:
        for com in made:
            com.release()


def show(title: str, message: str, actions=(), launch: str = "") -> bool:
    """Show a toast through Windows' own notification API. False when it can't be shown this way, so the
    caller uses the tray balloon instead."""
    if not IS_WINDOWS or not register():
        return False
    import ctypes

    notifier = toast = None
    try:
        notifier, toast = _build(toast_xml(title, message, actions, launch))
        notifier.call(6, (ctypes.c_void_p,), toast.ptr)                                 # Show
        return True
    except OSError as e:
        log.info("Toast not shown: %s", e)
        return False
    finally:
        for com in (toast, notifier):
            if com is not None:
                com.release()


__all__ = ["show", "register", "unregister", "toast_xml", "link", "target_from_link", "SCHEME"]
