"""Which programs can open the formats we write, so nobody converts photos into files they can't view."""

from __future__ import annotations

import glob
import os
import sys
from functools import lru_cache

# Programs known to open JPEG XL (current versions). Kept short and conservative.
JXL_OPENS = "Safari, Photoshop, Lightroom, Affinity Photo, GIMP, Krita, darktable, XnView, IrfanView"
JXL_DOESNT = "most other browsers, websites, social media and chat apps, and older software"


@lru_cache(maxsize=1)
def jxl_viewable_here() -> bool | None:
    """Can this computer show JPEG XL files in its own photo viewer and file thumbnails?
    True / False, or None when it can't be told."""
    if sys.platform == "win32":
        try:
            import winreg

            # Windows Imaging Component decoders: Photos and Explorer thumbnails use these. Microsoft's
            # "JPEG XL Image Extension" (Store) registers one.
            root = r"CLSID\{7ED96837-96F0-4812-B211-F13C24117ED3}\Instance"
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, root) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as dec:
                            name = str(winreg.QueryValueEx(dec, "FriendlyName")[0])
                    except OSError:
                        continue
                    if "jpeg xl" in name.lower() or "jxl" in name.lower():
                        return True
            return False
        except OSError:
            return None
    # Linux desktops: a gdk-pixbuf loader (GNOME, Xfce...) or a Qt image plugin (KDE).
    patterns = ["/usr/lib*/gdk-pixbuf-2.0/*/loaders/*jxl*", "/usr/lib/*/gdk-pixbuf-2.0/*/loaders/*jxl*",
                "/usr/lib*/qt*/plugins/imageformats/*jxl*", "/usr/lib/*/qt*/plugins/imageformats/*jxl*"]
    if any(glob.glob(p) for p in patterns):
        return True
    return False if os.path.isdir("/usr/lib") else None


def jxl_note() -> str:
    """One paragraph for the options and the confirmation."""
    here = jxl_viewable_here()
    if sys.platform == "win32":
        pc = ("This PC can show them (the JPEG XL extension is installed)." if here else
              "This PC can't show them yet: get Microsoft's free “JPEG XL Image Extension” from the Microsoft "
              "Store for Photos and thumbnails." if here is False else "")
    else:
        pc = ("This computer's image viewer can show them." if here else
              "This computer's image viewer may not show them (install libjxl's image loader)." if here is False
              else "")
    return (f"JPEG XL opens in {JXL_OPENS}, but not yet in {JXL_DOESNT}. " + pc
            + " JPEGs converted losslessly can be turned back into the exact original at any time (History).")


__all__ = ["jxl_viewable_here", "jxl_note", "JXL_OPENS", "JXL_DOESNT"]
