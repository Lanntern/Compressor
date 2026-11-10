"""Write the app icon (wick.gui.main_window.app_icon, drawn in code) as a multi-size .ico for the .exe and
the installer. Usage: python packaging/make_icon.py OUT.ico"""

import io
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def main(out: str) -> int:
    app = QApplication([])  # noqa: F841 - the icon is drawn with Qt
    from wick.gui.main_window import app_icon

    icon = app_icon()
    sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
    frames = []
    for size in sizes:
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        icon.pixmap(size, size).save(buf, "PNG")
        frames.append(Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA"))
    frames[-1].save(out, format="ICO", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "Wick.ico"))
