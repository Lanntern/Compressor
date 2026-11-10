"""Every page and dialog, at the smallest, a medium and a large window, in light and dark: is anything
squeezed, cut off, overlapping or sticking out? Run by tests/test_layout.py once per system text size
(PT=<points>, set before the app starts); prints one line per problem and "issues: N" last.
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
HOME = tempfile.mkdtemp(prefix="layout-check-")
os.environ.update(WICK_NO_NETWORK="1", WICK_SETTINGS_FILE=os.path.join(HOME, "s.ini"),
                  WICK_DATA_DIR=os.path.join(HOME, "appdata"))
if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QThread  # noqa: E402
from PySide6.QtGui import QFont, QFontMetrics  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton, QAbstractItemView, QAbstractScrollArea, QApplication, QComboBox, QLabel, QMenu, QScrollBar,
    QSizePolicy, QTabBar, QToolButton, QWidget,
)

app = QApplication([])
if os.environ.get("PT"):
    font = app.font()
    font.setPointSizeF(float(os.environ["PT"]))
    app.setFont(font)

media = Path(HOME) / "Pictures"
media.mkdir()
for i in range(3):
    Image.effect_noise((200, 150), 40).convert("RGB").save(media / f"a_rather_long_photo_name_from_a_camera_{i}.png")
Image.effect_noise((200, 150), 40).convert("RGB").save(
    media / ("PXL_20240612_183512345.PORTRAIT.ORIGINAL_with_some_extra_words_from_an_export_" * 2 + ".png"))
from wick import tools  # noqa: E402

if tools.find_tool("ffmpeg"):
    tools.run([tools.find_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
               "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media / "clip.mp4")], timeout=120)

qs = QSettings(os.environ["WICK_SETTINGS_FILE"], QSettings.IniFormat)
qs.setValue("home/auto_analyze", False)
qs.setValue("welcome_done", True)
qs.sync()
del qs

from wick.gui.about_dialog import AboutDialog  # noqa: E402
from wick.gui.language_dialog import LanguageDialog  # noqa: E402
from wick.gui.main_window import MainWindow, ShortcutsDialog  # noqa: E402
from wick.gui.preview import PreviewDialog  # noqa: E402
from wick.gui.theme import apply_theme  # noqa: E402
from wick.gui.welcome import WelcomeDialog  # noqa: E402
from wick.gui.widgets import _BodyScroll  # noqa: E402
from wick.scanner import scan  # noqa: E402
from wick.settings import Settings  # noqa: E402

class _StrayWindows(QObject):
    """A widget shown before it has a parent becomes a window of its own for a moment (a flash on screen)."""

    seen: list[str] = []

    def eventFilter(self, obj, ev):  # noqa: N802
        if ev.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow() and obj.parentWidget() is None \
                and type(obj).__name__ not in ("MainWindow", "QTipLabel", "QMenu", "QComboBoxPrivateContainer"):
            self.seen.append(f"{type(obj).__name__}({(obj.text() if hasattr(obj, 'text') else '')[:30]!r})")
        return False


_stray = _StrayWindows()
QApplication.instance().installEventFilter(_stray)
w = MainWindow()
w.show()


def settle(limit: float = 30) -> None:
    end = time.time() + limit
    while time.time() < end:
        app.processEvents()
        if not [t for t in w.findChildren(QThread) if t.isRunning()]:
            break
        time.sleep(0.03)
    for _ in range(4):
        app.processEvents()


def in_scroll(x: QWidget) -> bool:
    p = x.parentWidget()
    while p is not None:
        if isinstance(p, QAbstractScrollArea):
            return True
        p = p.parentWidget()
    return False


def name(x: QWidget) -> str:
    label = x.accessibleName() or getattr(x, "text", lambda: "")() or x.objectName()
    return f"{type(x).__name__}({label[:30]!r})"


found: dict[str, set] = {}


def check(root: QWidget, where: str) -> None:
    def flag(problem: str) -> None:
        found.setdefault(problem, set()).add(where)

    for sc in root.findChildren(_BodyScroll):
        if sc.isVisible() and sc.widget().width() > sc.viewport().width() + 1:
            flag(f"page content cut off at the right ({sc.widget().width()} > {sc.viewport().width()})")
    for t in root.findChildren(QTabBar):
        bold = QFont(t.font())
        bold.setWeight(QFont.DemiBold)
        fm = QFontMetrics(bold)
        clipped = any(t.tabRect(i).width() < fm.horizontalAdvance(t.tabText(i)) + 8 for i in range(t.count()))
        if t.isVisible() and (clipped or sum(t.tabRect(i).width() for i in range(t.count())) > t.width() + 1):
            flag(f"tab bar too narrow for its tabs: {name(t)}")
    for b in root.findChildren(QAbstractButton):
        if b.isVisible() and b.text() and b.width() < b.sizeHint().width() - 2 and not isinstance(b, QToolButton):
            flag(f"button squeezed: {b.text()!r} ({b.width()} < {b.sizeHint().width()})")
    for c in root.findChildren(QComboBox):
        if c.isVisible() and c.width() < c.minimumSizeHint().width() - 2:
            flag(f"drop-down squeezed: {name(c)}")
    for lab in root.findChildren(QLabel):
        if not lab.isVisible() or not lab.text() or lab.width() <= 0:
            continue
        if lab.wordWrap():
            if lab.heightForWidth(lab.width()) > lab.height() + 1:
                flag(f"wrapped text cut off: {lab.text()[:50]!r}")
        elif (lab.pixmap().isNull() and lab.sizePolicy().horizontalPolicy() != QSizePolicy.Ignored
              and lab.width() < lab.sizeHint().width() - 2 and lab.textFormat() != Qt.RichText):
            flag(f"label cut: {lab.text()[:60]!r} ({lab.width()} < {lab.sizeHint().width()})")
    for parent in [root, *root.findChildren(QWidget)]:
        if not parent.isVisible() or isinstance(parent, (QAbstractItemView, QTabBar, QComboBox)):
            continue
        kids = [k for k in parent.children() if isinstance(k, QWidget) and k.isVisible() and not k.isWindow()
                and not isinstance(k, (QScrollBar, QMenu)) and k.width() > 2 and k.height() > 2
                # (the window buttons sit over the title band on purpose; pages keep that band clear)
                and type(k).__name__ not in ("ListPlaceholder", "QFocusFrame", "QRubberBand", "CaptionButtons")
                and not k.testAttribute(Qt.WA_TransparentForMouseEvents)]
        for i, a in enumerate(kids):
            for b in kids[i + 1:]:
                inter = a.geometry().intersected(b.geometry())
                if inter.width() > 3 and inter.height() > 3:
                    flag(f"overlap: {name(a)} / {name(b)} in {type(parent).__name__}")
    # Accessibility: every control a screen reader lands on has a name, and the keyboard can reach it.
    from PySide6.QtWidgets import QAbstractSlider, QAbstractSpinBox, QLineEdit

    from wick.gui.header import Header

    for x in root.findChildren(QWidget):
        if (not x.isVisible() or not isinstance(x, (QAbstractButton, QComboBox, QAbstractSpinBox, QLineEdit,
                                                     QAbstractSlider, QAbstractItemView, QTabBar))
                or isinstance(x, QScrollBar) or x.objectName().startswith("qt_")):
            continue
        label = x.accessibleName() or (x.text().replace("&", "") if isinstance(x, QAbstractButton) else "")
        label = label or (x.placeholderText() if isinstance(x, QLineEdit) else "")
        label = label or next((lab.text() for lab in x.window().findChildren(QLabel) if lab.buddy() is x), "")
        if not label.strip():
            flag(f"no name for screen readers: {type(x).__name__} (tooltip {x.toolTip()[:40]!r})")
        # (Title-bar buttons aren't in the tab order on Windows either: Alt+F4 and Win+arrows do their job.)
        if x.focusPolicy() == Qt.NoFocus and not isinstance(x, (QTabBar, Header)) \
                and type(x).__name__ != "CaptionButton":
            flag(f"not reachable by keyboard: {label or type(x).__name__}")
    for bar in root.findChildren(QTabBar):
        if bar.isVisible() and bar.count() and bar.height() < max(bar.tabRect(i).height() for i in range(bar.count())):
            flag(f"tabs cut off: {bar.accessibleName() or 'tab bar'} ({bar.height()} < {bar.tabRect(0).height()})")
    for x in root.findChildren(QWidget):
        p = x.parentWidget()
        if (x.isVisible() and p is not None and not x.isWindow() and not in_scroll(x) and not isinstance(x, QMenu)
                and p.width() > 0 and x.geometry().right() > p.width() + 1 and type(p).__name__ != "QStackedWidget"):
            flag(f"sticks out of its container: {name(x)} in {type(p).__name__}")


pages = []
for idx in range(w.pages.count()):
    w._go(idx)
    settle()
    page = w.pages.currentWidget()
    if type(page).__name__ in ("CompressTab", "ConvertTab", "MetadataTab"):
        page.add_paths([str(media)])
        settle(60)
        if type(page).__name__ == "CompressTab":
            page.start_job(confirm=False)        # leaves its outcome showing (long names)
            settle(120)
    pages.append(idx)

preview_settings = Settings(image_format="webp", png_mode="lossy", min_image_kb=0, min_video_mb=0, video_codec="hevc",
                            video_encoder="cpu")
for theme in ("light", "dark"):
    apply_theme(theme)
    for size in ((w.minimumWidth(), max(w.minimumHeight(), 600)), (1100, 760), (1700, 1050)):
        w.resize(*size)
        settle()
        for idx in pages:
            w._go(idx)
            settle(5)
            page = w.pages.currentWidget()
            label = f"{theme} {w.width()}x{w.height()}"
            if type(page).__name__ == "CleanupTab":
                for i in range(page.tabs.count()):
                    page.tabs.setCurrentIndex(i)
                    settle(5)
                    check(w, f"{label} Clean Up/{page.tabs.tabText(i)}")
            else:
                check(w, f"{label} {type(page).__name__}")
    entries = scan([media], preview_settings).entries
    for e in entries[:1] + [x for x in entries if x.kind == "video"][:1]:
        d = PreviewDialog(e, preview_settings, w)
        d.show()
        settle(120)
        for size in ((d.minimumSizeHint().width(), d.minimumSizeHint().height()), (900, 650), (1300, 900)):
            d.resize(*size)
            settle(3)
            check(d, f"{theme} Preview ({e.kind}) {d.width()}x{d.height()}")
        d.done(0)
    for make in (AboutDialog, ShortcutsDialog, lambda p: LanguageDialog([], p), WelcomeDialog):
        d = make(w)
        d.show()
        settle()
        for size in ((d.minimumSizeHint().width(), d.minimumSizeHint().height()), (d.width(), d.height())):
            d.resize(*size)
            settle(3)
            check(d, f"{theme} {type(d).__name__} {d.width()}x{d.height()}")
        d.done(0)

QApplication.instance().removeEventFilter(_stray)
for name in sorted(set(_StrayWindows.seen)):
    found.setdefault(f"shown as a stray window: {name}", set()).add("startup or page change")
for problem, where in sorted(found.items()):
    print(f"- {problem}\n    at: {', '.join(sorted(where)[:4])}{' …' if len(where) > 4 else ''}")
print(f"issues: {len(found)}")
w._quitting = True
w.close()
shutil.rmtree(HOME, ignore_errors=True)
