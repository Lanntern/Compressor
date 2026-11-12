"""Drives the real main window (headless on Linux): every page, a small compression job, presets,
watch-folder options, the preview dialog and the Settings page."""

import os
import time

import pytest

pytest.importorskip("PySide6")
if os.name != "nt":
    # Headless Linux/macOS CI. Windows runners have a desktop session, and Qt's offscreen
    # backend there lacks the system fonts (text measures far too wide).
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402


def _wait(cond, timeout=60.0) -> bool:
    from PySide6.QtWidgets import QApplication

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            # A finished thread's last signals (its results) may still be queued: deliver them too.
            for _ in range(5):
                QApplication.processEvents()
                time.sleep(0.01)
            return True
        time.sleep(0.02)
    return cond()


def _widest(widget) -> str:
    """The chain of widest descendants, to say which control makes a page too wide."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    from PySide6.QtWidgets import QScrollArea

    chain = []
    while True:
        if isinstance(widget, QScrollArea) and widget.widget() is not None:
            widget = widget.widget()        # look at the content, not the scroll bars
        kids = [c for c in widget.findChildren(QWidget, options=Qt.FindDirectChildrenOnly)
                if not c.isWindow() and not c.isHidden()]
        if not kids:
            break
        widget = max(kids, key=lambda c: c.minimumSizeHint().width())
        name = widget.accessibleName() or widget.objectName() or getattr(widget, "text", lambda: "")()
        chain.append(f"{type(widget).__name__}({name!r}, {widget.minimumSizeHint().width()})")
    return " > ".join(chain)


@pytest.fixture(scope="module")
def win(tmp_path_factory):
    ini = tmp_path_factory.mktemp("qsettings") / "settings.ini"
    os.environ["WICK_SETTINGS_FILE"] = str(ini)
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    qs = QSettings(str(ini), QSettings.IniFormat)
    qs.setValue("home/auto_analyze", False)  # don't scan the real Pictures folder
    qs.setValue("welcome_done", True)        # (the welcome has its own test)
    qs.sync()
    del qs

    from wick.gui.main_window import MainWindow
    from wick.gui.theme import apply_theme

    apply_theme("light")  # as the app does: sets the UI font and stylesheet, which drive the layout
    w = MainWindow()
    w.show()
    _wait(lambda: w.isVisible(), 5)
    yield w
    w._quitting = True
    w.close()
    app.processEvents()
    os.environ.pop("WICK_SETTINGS_FILE", None)


@pytest.fixture
def no_dialogs(monkeypatch):
    """Answer every message box with the default action instead of blocking."""
    shown = []
    import importlib
    import pkgutil

    import wick.gui as gui

    def fake_inform(_parent, text, info=""):
        shown.append(text)

    def fake_ask(_parent, text, info="", action="OK", destructive=False):
        shown.append(text)
        return True

    # Every page module that imported the helpers (not only the ones a test happens to use).
    mods = [importlib.import_module(f"wick.gui.{m.name}") for m in pkgutil.iter_modules(gui.__path__)]
    for mod in mods:
        for name, fn in (("inform", fake_inform), ("ask", fake_ask), ("warn", fake_inform)):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, fn)
    return shown


def test_every_page_opens_at_any_size(win):
    from PySide6.QtWidgets import QApplication

    from wick.gui.style import text_scale

    k = text_scale()  # with larger system text, the same layout needs a proportionally larger window
    for width, height in ((int(1320 * k), int(840 * k)), (int(900 * k), int(640 * k)), (int(1920 * k), 1200)):
        win.resize(width, height)
        for i in range(win.pages.count()):
            win.sidebar.list.setCurrentRow(i)
            for _ in range(3):                  # (a page that rearranges itself for the width settles in a turn)
                QApplication.processEvents()
            page = win.pages.currentWidget()
            assert page is win.pages.widget(i)
            assert page.isVisible()
            # Nothing is wider than the window (no clipped layouts), and page content fits the width it's
            # given (it adapts: rows wrap, toolbars compact) instead of needing to scroll sideways.
            assert page.minimumSizeHint().width() <= max(width, win.minimumWidth()), \
                f"text scale {k:.2f}: {_widest(page)}"
            from wick.gui.widgets import _BodyScroll

            for sc in page.findChildren(_BodyScroll):
                if sc.isVisible():
                    assert sc.widget().width() <= sc.viewport().width() + 1, \
                        f"text scale {k:.2f}, {width}px: {_widest(sc)}"


def test_compress_job_end_to_end(win, tmp_path, no_dialogs):
    from wick.settings import Settings

    folder = tmp_path / "Bilder åäö"
    folder.mkdir()
    img = Image.effect_noise((480, 360), 40).convert("RGB")
    img.save(folder / "photo.jpg", quality=95)
    Image.linear_gradient("L").resize((400, 400)).convert("RGB").save(folder / "shot.png")

    s = Settings(image_format="keep", jpeg_mode="lossless", png_mode="lossless", output_mode="suffix",
                 min_image_kb=0, require_savings=False, video_codec="none")
    win.compress.panel.load(s)
    win.compress.clear()
    win.compress.add_paths([str(folder)])
    assert _wait(lambda: not win.compress.busy() and win.compress.model.rows, 60)
    assert len(win.compress.model.rows) == 2

    win.compress.start_job(confirm=False)
    assert _wait(lambda: not win.compress.busy() and all(r.result for r in win.compress.model.rows), 120)
    _wait(lambda: win.compress.job_status.title.text().startswith("Done"), 10)   # (the result arrives as an event)
    outputs = sorted(p.name for p in folder.iterdir())
    assert any("_compressed" in n for n in outputs), outputs
    assert (folder / "photo.jpg").exists()  # suffix mode keeps the originals
    status = win.compress.job_status
    assert not status.isHidden() and status.title.text().startswith("Done: 2 of 2 files compressed")
    assert "Saved in" in status.detail.text() and "kept" in status.detail.text()
    # Clear goes back to the start: empty list, the drop zone, and the outcome card gone.
    assert win.compress.clear_btn.isEnabled()
    win.compress.clear_btn.click()
    assert not win.compress.model.rows and status.isHidden() and win.compress.stack.currentIndex() == 0
    assert not win.compress.clear_btn.isEnabled()


def test_one_file_next_to_original_says_what_happened(win, tmp_path, no_dialogs):
    """"Next to originals" used to make the Compress button do nothing (an error inside its handler)."""
    from wick.settings import Settings

    Image.effect_noise((320, 240), 40).convert("RGB").save(tmp_path / "one.png")
    win.compress.panel.load(Settings(image_format="webp", png_mode="lossy", output_mode="beside", min_image_kb=0,
                                     require_savings=False, video_codec="none"))
    win.compress.clear()
    win.compress.add_paths([str(tmp_path / "one.png")])
    assert _wait(lambda: not win.compress.busy() and win.compress.model.rows, 60)
    steps = []
    win.compress.compress_btn.click()
    assert win.compress.job_thread is not None and win.compress.busy()
    win.compress.job_thread.file_step.connect(lambda _e, m: steps.append(m))
    assert _wait(lambda: not win.compress.busy(), 120)
    _wait(lambda: win.compress.job_status.show_btn.isHidden() is False, 5)
    status = win.compress.job_status
    assert status.title.text() == "Done: one.webp", status.title.text()
    assert f"Saved in {tmp_path}" in status.detail.text() or "Saved in ~" in status.detail.text()
    assert not status.show_btn.isHidden() and (tmp_path / "one.webp").exists() and (tmp_path / "one.png").exists()


def test_a_failure_says_at_which_step(win, tmp_path, no_dialogs, monkeypatch):
    from wick import engine, steps
    from wick.settings import Settings

    def broken(*_a, **_k):
        steps.report("Encoding WebP")
        raise RuntimeError("encoder crashed")
    monkeypatch.setattr(engine, "convert_image", broken)
    Image.effect_noise((64, 48), 40).convert("RGB").save(tmp_path / "bad.png")
    win.compress.panel.load(Settings(image_format="webp", png_mode="lossy", output_mode="suffix", min_image_kb=0,
                                     require_savings=False, video_codec="none"))
    win.compress.clear()
    win.compress.add_paths([str(tmp_path / "bad.png")])
    assert _wait(lambda: not win.compress.busy() and win.compress.model.rows, 60)
    assert win.compress.start_job(confirm=False)
    assert _wait(lambda: not win.compress.busy(), 60)
    _wait(lambda: win.compress.job_status.copy_btn.isHidden() is False, 5)
    status = win.compress.job_status
    assert status.title.text() == "Couldn't finish bad.png"
    assert "While encoding WebP: encoder crashed" in status.detail.text()
    assert not status.copy_btn.isHidden() and not status.log_btn.isHidden()
    assert "step: Encoding WebP" in status._details
    row = win.compress.model.rows[0]
    assert "(while encoding WebP)" in win.compress.model._status_text(row)


def test_presets_apply_and_reach_the_panel(win):
    panel = win.compress.panel
    assert panel.apply_preset("For sharing")
    s = panel.settings()
    assert (s.image_format, s.resize_long_edge, s.metadata, s.video_max_height) == ("webp", 2560, "strip_private",
                                                                                    1080)
    assert panel.preset.currentData() == "For sharing"
    assert not panel.apply_preset("No such preset")

    # Explorer's "Compress with <preset>" goes through receive_paths.
    win.receive_paths([], show=False, preset="Photo archive")
    assert panel.settings().image_format == "jxl" and panel.settings().jpeg_mode == "lossless"


def test_jump_list_and_tray_open_pages(win):
    """--open=TARGET (taskbar/Start jump list, tray menu) shows that page, in this window or a running one."""
    for target, attr in (("history", "history"), ("cleanup", "cleanup"), ("settings", "app_settings"),
                         ("compress", "compress")):
        win.receive_paths([], show=False, open_target=target)
        assert win.pages.currentWidget() is getattr(win, attr), target
    win.receive_paths([], show=False, open_target="no-such-page")         # ignored
    assert win.pages.currentWidget() is win.compress


def test_support_link_and_one_time_prompt(win):
    from wick.gui import support

    win.sidebar.support_btn.click()                                         # any time, from the sidebar
    dlg = win._support_dialog
    assert dlg.isVisible() and not dlg.isModal() and dlg.star.text() and dlg.donate.text()
    dlg.close()
    qs = win.qsettings
    qs.setValue(support.FIRST_SEEN, 0.0)
    qs.setValue(support.SAVED_MOST, 11 * 1024 ** 3)
    qs.remove(support.ASKED)
    win.show_window()
    win._maybe_ask_support()
    assert win._support_dialog.isVisible() and qs.value(support.ASKED, False, bool)
    win._support_dialog.close()
    shown = win._support_dialog
    win._maybe_ask_support()                                                # asked once only
    assert win._support_dialog is shown
    qs.remove(support.SAVED_MOST)
    qs.remove(support.FIRST_SEEN)


def test_taskbar_mirrors_the_job(win):
    """The taskbar button follows a job panel: moving, paused, finished or failed; its Pause and Stop buttons
    press the panel's own. On Windows the real taskbar calls must succeed too."""
    from pathlib import Path
    from types import SimpleNamespace

    from wick.engine import DONE, FAILED
    from wick.gui import taskbar as tbmod

    tb, panel = win.taskbar, win.compress.job_status
    panel.start(2, 2000)
    assert tb._jobs[id(panel)][1] == "indeterminate" and tb.active_panel() is panel
    tb.clicked(tbmod.PAUSE_ID)                                    # the Pause button in the taskbar preview
    assert panel.pause_btn.isChecked() and tb._jobs[id(panel)][1] == "paused"
    tb.clicked(tbmod.PAUSE_ID)
    assert not panel.pause_btn.isChecked()
    entry = SimpleNamespace(path=Path("a.jpg"), size=1000)
    ok = SimpleNamespace(entry=entry, status=DONE, output=None, message="", step="", new_size=500, saved=500)
    panel.finish([ok])
    assert id(panel) not in tb._jobs and tb.active_panel() is None
    panel.start(1, 1000)
    bad = SimpleNamespace(entry=entry, status=FAILED, output=None, message="boom", step="Encoding", new_size=0,
                          saved=0)
    panel.finish([bad])
    assert tb._jobs[id(panel)][1] == "error"                      # red until the window is looked at
    tb.clear_error()
    assert not tb._jobs
    if os.name == "nt":
        assert tb._list is not None and not tb._failed, tb.error  # ITaskbarList3 answered
    panel.hide()


def test_tray_menu(win):
    if win.tray is None:
        pytest.skip("no system tray here")
    actions = {a.text(): a for a in win._tray_menu.actions() if a.text()}
    assert win._tray_menu.defaultAction() is actions["Open Wick"]
    assert {"Compress Files…", "Convert Files…", "Clean Up", "History", "Watch Folders", "Check for Updates",
            "About Wick", "Quit"} <= set(actions)
    actions["History"].trigger()
    assert win.pages.currentWidget() is win.history


def test_watch_folder_uses_its_preset(win, tmp_path):
    from PySide6.QtCore import Qt

    watch = win.watch
    folder = tmp_path / "Inbox"
    folder.mkdir()
    watch._add_item(str(folder))
    watch._save()
    item = watch.folders.item(watch.folders.count() - 1)
    watch.folders.clearSelection()
    item.setSelected(True)
    watch.preset.setCurrentIndex(watch.preset.findData("Screenshots"))
    watch._preset_chosen(watch.preset.currentIndex())
    assert "Screenshots" in item.text() and item.data(Qt.UserRole) == str(folder)

    s = watch.settings_for(str(folder / "sub" / "new.png"))
    assert s.image_format == "jxl" and s.video_codec == "none"
    other = watch.settings_for(str(tmp_path / "elsewhere.png"))
    assert other.to_dict() == win.compress.panel.settings().to_dict()

    # Survives a restart of the page (stored in settings).
    assert "Screenshots" in win.qsettings.value("watch_presets", "", str)
    watch.folders.setCurrentItem(item)
    watch.remove_folder()
    assert str(folder) not in win.qsettings.value("watch_presets", "", str)


def test_image_preview_dialog(win, tmp_path):
    from wick.gui.preview import PreviewDialog
    from wick.scanner import scan
    from wick.settings import Settings

    Image.effect_noise((320, 240), 30).convert("RGB").save(tmp_path / "p.png")
    s = Settings(image_format="webp", png_mode="lossy", image_quality=80, min_image_kb=0)
    entry = scan([tmp_path], s).entries[0]
    dlg = PreviewDialog(entry, s, win)
    assert _wait(lambda: dlg.worker is not None and dlg.worker.isFinished(), 120)
    _wait(lambda: "smaller" in dlg.info.text() or "larger" in dlg.info.text(), 5)
    assert "smaller" in dlg.info.text() or "larger" in dlg.info.text(), dlg.info.text()
    assert dlg.view.before is not None and dlg.view.after is not None
    # Split and Side by Side are both always one click away; the line's slider only shows for Split.
    assert dlg.mode.tabText(dlg.mode.currentIndex()) == "Split" and not dlg.split.isHidden()
    dlg.mode.setCurrentIndex(1)
    assert dlg.view.side_by_side and dlg.split.isHidden()
    from PySide6.QtCore import QPointF

    dlg.view._zoom_at(QPointF(10, 10), 2.0)          # zoom works side by side too
    assert not dlg.view._fit
    dlg.mode.setCurrentIndex(0)
    assert not dlg.view.side_by_side and not dlg.split.isHidden() and dlg.split.isEnabled()
    assert dlg.save_as.isEnabled() and dlg.compress.isEnabled()
    dlg.reject()


def test_preview_quality_can_always_be_chosen(win, tmp_path):
    """Smart quality used to lock the preview's quality. Now: its targets, or a Custom value."""
    from wick.gui.preview import PreviewDialog
    from wick.scanner import scan
    from wick.settings import Settings

    Image.effect_noise((200, 150), 30).convert("RGB").save(tmp_path / "p.png")
    s = Settings(image_format="webp", png_mode="lossy", smart_quality=True, smart_target="high", min_image_kb=0)
    entry = scan([tmp_path], s).entries[0]
    dlg = PreviewDialog(entry, s, win)
    assert _wait(lambda: dlg.worker is not None and dlg.worker.isFinished(), 120)
    assert dlg.quality.isEnabled() and dlg.quality.currentText() == "High" and dlg.value.isHidden()
    assert dlg.mode.minimumSizeHint().width() >= dlg.mode.sizeHint().width() - 2   # never squeezed into arrows
    dlg.quality.setCurrentIndex(dlg.quality.findData("custom"))
    dlg._quality_picked(0)
    assert not dlg.value.isHidden()
    _wait(lambda: dlg.worker.isFinished(), 120)
    dlg.value.setValue(42)
    chosen = dlg._chosen_settings()
    assert not chosen.smart_quality and chosen.image_quality == 42
    win.compress.panel.load(s)
    dlg.quality_chosen.connect(lambda st: win.compress._use_quality(st, video=False))
    dlg._use()                                       # "Use for All Files" reaches the options
    applied = win.compress.panel.settings()
    assert not applied.smart_quality and applied.image_quality == 42


def test_preview_says_lossless_instead_of_a_quality(win, tmp_path):
    from wick.gui.preview import PreviewDialog
    from wick.scanner import scan
    from wick.settings import Settings

    Image.effect_noise((200, 150), 30).convert("RGB").save(tmp_path / "p.jpg", quality=95)
    s = Settings(image_format="jxl", jpeg_mode="lossless", min_image_kb=0)
    entry = scan([tmp_path], s).entries[0]
    dlg = PreviewDialog(entry, s, win)
    assert not dlg.lossless_note.isHidden() and dlg.quality.isHidden() and dlg.use.isHidden()
    assert _wait(lambda: dlg.worker is not None and dlg.worker.isFinished(), 120)
    dlg.reject()


def test_preview_save_as_keeps_the_original(win, tmp_path, no_dialogs, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from wick.gui.preview import PreviewDialog
    from wick.settings import Settings

    Image.effect_noise((320, 240), 30).convert("RGB").save(tmp_path / "p.png")
    s = Settings(image_format="webp", png_mode="lossy", image_quality=80, min_image_kb=0, output_mode="replace",
                 require_savings=False, video_codec="none")
    win.compress.panel.load(s)
    win.compress.clear()
    win.compress.add_paths([str(tmp_path / "p.png")])
    assert _wait(lambda: not win.compress.busy() and win.compress.model.rows, 60)
    entry = win.compress.model.rows[0].entry
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(tmp_path / "out" / "x"), "")))
    dlg = PreviewDialog(entry, win.compress.panel.settings(), win)
    dlg.save_as_requested.connect(lambda st, d: win.compress.compress_one(entry, st, d))
    assert _wait(lambda: dlg.save_as.isEnabled(), 120)
    dlg.save_as.click()
    assert _wait(lambda: not win.compress.busy(), 120)
    assert (tmp_path / "out" / "x.webp").exists()
    assert (tmp_path / "p.png").exists()            # Save As never replaces the original, even in Replace mode
    _wait(lambda: win.compress.job_status.title.text().startswith("Done"), 10)   # (the result arrives as an event)
    assert win.compress.job_status.title.text() == "Done: x.webp"


def test_preview_offers_compress_only_when_the_result_would_be_kept(win, tmp_path, no_dialogs, monkeypatch):
    """A result that wouldn't be kept (larger, or below the minimum saving) can't be "compressed" (it would do
    nothing), but Save As still saves it: the person chose that file."""
    from PySide6.QtWidgets import QFileDialog

    from wick.gui.preview import PreviewDialog
    from wick.settings import Settings

    Image.effect_noise((320, 240), 30).convert("RGB").save(tmp_path / "p.png")
    s = Settings(image_format="webp", png_mode="lossy", image_quality=80, min_image_kb=0, output_mode="beside",
                 require_savings=True, min_savings_pct=99.5, video_codec="none")
    win.compress.panel.load(s)
    win.compress.clear()
    win.compress.add_paths([str(tmp_path / "p.png")])
    assert _wait(lambda: not win.compress.busy() and win.compress.model.rows, 60)
    entry = win.compress.model.rows[0].entry
    dlg = PreviewDialog(entry, win.compress.panel.settings(), win)
    assert _wait(lambda: dlg.save_as.isEnabled(), 120)
    assert not dlg.compress.isEnabled() and "minimum" in dlg.compress.toolTip()
    dlg._describe(entry.size * 2, (320, 240), "WebP")
    assert not dlg.compress.isEnabled() and "larger" in dlg.compress.toolTip()
    dlg._describe(entry.size // 1000, (320, 240), "WebP")
    assert dlg.compress.isEnabled()
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(tmp_path / "out" / "x"), "")))
    dlg.save_as_requested.connect(lambda st, d: win.compress.compress_one(entry, st, d))
    dlg.save_as.click()
    assert _wait(lambda: not win.compress.busy(), 120)
    assert (tmp_path / "out" / "x.webp").exists() and (tmp_path / "p.png").exists()


def test_settings_page_data_and_diagnostics(win, no_dialogs):
    from PySide6.QtWidgets import QApplication

    from wick import index

    page = win.app_settings
    win.go_to(page)
    QApplication.processEvents()
    page._index_toggled(False)
    assert not index._enabled
    page._index_toggled(True)
    assert index._enabled
    page.history_keep.setCurrentIndex(page.history_keep.findData(30))
    page.history_keep.activated.emit(page.history_keep.currentIndex())
    assert win.qsettings.value("history_days", 0, int) == 30
    win.qsettings.setValue("history_days", 0)

    page._copy_diagnostics()
    text = QApplication.clipboard().text()
    assert text.startswith("Wick ") and "Tools:" in text


def test_options_panel_fits_its_column(win):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLabel, QScrollArea, QSpinBox

    win.resize(1440, 900)
    win.go_to(win.compress)
    win.compress.inspector.setVisible(True)
    panel = win.compress.panel
    panel.apply_preset("For sharing")  # long preset name and description
    for sec in panel.sections:
        sec.toggle.setChecked(True)
    for _ in range(5):
        QApplication.processEvents()
    scroll = panel.parentWidget()
    while scroll is not None and not isinstance(scroll, QScrollArea):
        scroll = scroll.parentWidget()
    viewport = scroll.viewport() if scroll is not None else win.compress.inspector
    limit = viewport.width()
    widgets = [w for cls in (QAbstractButton, QComboBox, QSpinBox, QLabel) for w in panel.findChildren(cls)]
    assert widgets
    for w in widgets:
        if w.isVisible():
            right = w.mapTo(viewport, QPoint(w.width(), 0)).x()
            assert right <= limit, f"{type(w).__name__} {w.accessibleName() or getattr(w, 'text', lambda: '')()!r}"


def test_lists_sort_select_all_and_menu(win, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidget

    from wick.gui.filelist import SortItem, add_header_checkbox, file_menu, make_sortable, set_sort_keys

    tree = QTreeWidget()
    tree.setHeaderLabels(["Name", "Size"])
    header = make_sortable(tree)
    for name, size in (("b.png", 2048), ("a.png", 10), ("c.png", 1_000_000)):
        it = SortItem([name, f"{size} B"])
        set_sort_keys(it, {1: size})
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(0, Qt.Unchecked)
        tree.addTopLevelItem(it)
    tree.sortByColumn(1, Qt.DescendingOrder)   # by size, not by the text "1000000 B" < "2048 B"
    assert [tree.topLevelItem(i).text(0) for i in range(3)] == ["c.png", "b.png", "a.png"]
    tree.sortByColumn(0, Qt.AscendingOrder)
    assert tree.topLevelItem(0).text(0) == "a.png"

    changed = []
    add_header_checkbox(tree, 0, lambda: changed.append(1))
    column, state, toggle = header._check
    assert state() == Qt.Unchecked
    toggle()
    assert state() == Qt.Checked and changed
    tree.topLevelItem(0).setCheckState(0, Qt.Unchecked)
    assert state() == Qt.PartiallyChecked
    toggle()
    assert state() == Qt.Checked

    f = tmp_path / "x.txt"
    f.write_text("x")
    labels = [a.text() for a in file_menu(win, [f], [("Extra", lambda: None)]).actions() if a.text()]
    assert labels[:3] == ["Open", labels[1], "Copy Path"] and "Extra" in labels

    # Compress table: the header checkbox drives the model.
    h = win.compress.table.horizontalHeader()
    assert h._check is not None


def test_saved_so_far_hidden_until_something_saved(win):
    sb = win.sidebar
    sb.set_saved(None)
    assert not sb.foot_value.isVisibleTo(sb)
    sb.set_saved("1.2 GB")
    assert sb.foot_value.isVisibleTo(sb) or sb.compact
    win.history.refresh()


def test_cleanup_keeps_unticked_items_and_switches_kept_copy(win, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt

    import wick.gui.cleanup_section as ct
    from wick.cleanup.actions import Outcome

    monkeypatch.setattr(ct, "inform", lambda *a, **k: None)
    for folder in ("a", "b", "c", "d"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "photo.bin").write_bytes(b"x" * 300_000)
    sec = next(s for s in win.cleanup.sections if s.tool.key == "duplicates")
    sec.folders = [tmp_path]
    sec.extra_min.set_bytes(1024)
    sec.scan()
    assert _wait(lambda: not sec.busy() and sec.result is not None, 60)
    rows = sec._items()
    assert len(rows) == 3 and all(it.checkState(0) == Qt.Checked for it, _ in rows)

    # Untick one, act on another: the unticked one must stay unticked afterwards.
    (keep_it, kept), (_, acted) = rows[0], rows[1]
    keep_it.setCheckState(0, Qt.Unchecked)
    sec._applied([Outcome(acted, True, "Moved to the Recycle Bin", acted.size)], None)
    states = {c.path: it.checkState(0) for it, c in sec._items()}
    assert states[kept.path] == Qt.Unchecked and acted.path not in states

    # Keep a different copy: it leaves the removable list and the old keeper joins it.
    old_keeper = kept.keeper
    target = next(c for _, c in sec._items() if c.path != kept.path)
    sec.keep_instead(target)
    paths = {c.path for _, c in sec._items()}
    assert target.path not in paths and old_keeper in paths
    assert all(c.keeper == target.path for _, c in sec._items())
    assert {c.path: it.checkState(0) for it, c in sec._items()}[kept.path] == Qt.Unchecked


def test_duplicate_groups_open_and_rows_can_be_chosen(win, tmp_path, monkeypatch):
    """Groups open by themselves; the header arrow closes and opens them all; a group's box ticks its removable
    copies but never the copy that stays; a right-click acts on just the chosen file; nothing done is said so."""
    from PySide6.QtCore import QPoint, Qt

    import wick.gui.cleanup_section as cs
    from wick.cleanup.actions import Outcome
    from wick.cleanup.common import Candidate, ScanResult

    sec = next(s for s in win.cleanup.sections if s.tool.key == "duplicates")
    sec.folders = [tmp_path]
    keeper = tmp_path / "keep.jpg"
    items = [Candidate(tmp_path / f"g{g}" / f"copy{k}.jpg", 1000, group=str(g), keeper=keeper, selected=True)
             for g in range(60) for k in range(2)]
    sec._scanned(ScanResult("duplicates", items, []))
    groups = sec._groups()
    assert len(groups) == 60 and all(g.isExpanded() for g in groups)      # (40+ groups used to stay closed)
    header = sec.tree.header()
    assert sec._all_open() is True and header._expander_width(0) > 0
    sec.open_all(False)
    assert not any(g.isExpanded() for g in groups) and sec._all_open() is False
    header.mousePressEvent(type("E", (), {"button": lambda s: Qt.LeftButton,
                                          "position": lambda s: QPoint(2, 5).toPointF(), "accept": lambda s: None})())
    assert all(g.isExpanded() for g in groups)                              # the header arrow opens them all

    g = groups[0]
    keep_row = next(g.child(i) for i in range(g.childCount()) if not g.child(i).flags() & Qt.ItemIsUserCheckable)
    copies = [g.child(i) for i in range(g.childCount()) if g.child(i).flags() & Qt.ItemIsUserCheckable]
    assert g.flags() & Qt.ItemIsUserCheckable and g.checkState(0) == Qt.Checked     # a box, showing its rows
    g.setCheckState(0, Qt.Unchecked)
    assert all(c.checkState(0) == Qt.Unchecked for c in copies)
    assert keep_row.data(0, Qt.CheckStateRole) is None                      # the copy that stays: never ticked
    copies[0].setCheckState(0, Qt.Checked)
    assert g.checkState(0) == Qt.PartiallyChecked                           # one of its copies chosen
    g.setCheckState(0, Qt.Checked)
    assert all(c.checkState(0) == Qt.Checked for c in copies) and keep_row.data(0, Qt.CheckStateRole) is None
    assert len(sec._items(True)) == len(items) and keeper not in [c.path for _, c in sec._items()]

    ran: list = []
    monkeypatch.setattr(cs, "ask", lambda *a, **k: True)
    monkeypatch.setattr(cs._Apply, "start", lambda self: ran.append([c.path.name for c in self.items]))
    sec.run_action(cs.actions.TRASH, only=[items[0]])
    assert ran == [["copy0.jpg"]]                                          # just that file, whatever is ticked
    sec.worker = None

    shown: list = []
    monkeypatch.setattr(cs, "inform", lambda _w, title, *_a, **_k: shown.append(title))
    sec._applied([Outcome(items[1], False, "In use")], None)
    assert shown == ["Nothing was changed"]                                 # not "Freed 0 B from 0 items"


def test_window_resize_does_not_freeze_columns(win):
    """Resizing the window with the mouse held resizes the last (stretching) column: that isn't the person
    choosing widths, so columns keep fitting their content."""
    from PySide6.QtWidgets import QTreeWidget

    from wick.gui.filelist import make_sortable

    tree = QTreeWidget()
    tree.setHeaderLabels(["Name", "Folder", "Size"])
    make_sortable(tree)
    tree._fitter._resized(2)                         # (last column, as the window grows)
    tree.header()._dragging = True
    tree._fitter._resized(2)
    assert not tree._fitter.user_sized
    tree._fitter._resized(0)                         # a real drag on a column edge
    assert tree._fitter.user_sized


def test_columns_fit_long_names_when_there_is_room(win):
    from PySide6.QtWidgets import QApplication, QTreeWidget

    from wick.gui.filelist import SortItem, filling, make_sortable

    tree = QTreeWidget()
    tree.setHeaderLabels(["Name", "Folder", "Size"])
    make_sortable(tree)
    tree.resize(1400, 400)
    tree.show()
    long_name = "A very long file name that would normally be cut off in the first column.jpeg"
    with filling(tree):
        tree.addTopLevelItem(SortItem([long_name, "Pictures", "1 MB"]))
    _wait(lambda: False, 0.2)
    QApplication.processEvents()
    assert tree.columnWidth(0) >= tree.fontMetrics().horizontalAdvance(long_name)
    tree.resize(500, 400)
    _wait(lambda: False, 0.2)
    assert tree.columnWidth(0) + tree.columnWidth(1) <= 500
    tree.close()


def test_options_only_for_the_kinds_of_files_listed(win):
    panel = win.compress.panel
    sec = {s.toggle.text(): s for s in panel.sections}
    panel.set_file_kinds({"video"})
    assert not sec["Images"].isVisibleTo(panel) and not sec["Audio"].isVisibleTo(panel)
    assert sec["Videos"].isVisibleTo(panel)
    panel.set_file_kinds(None)
    assert sec["Images"].isVisibleTo(panel) and sec["Audio"].isVisibleTo(panel)

    # A kind that's switched off keeps only its on/off control.
    panel.video_codec.setCurrentIndex(panel.video_codec.findData("none"))
    sec["Videos"].toggle.setChecked(True)
    assert not panel.video_quality.isVisibleTo(panel) and panel.video_codec.isVisibleTo(panel)
    panel.video_codec.setCurrentIndex(panel.video_codec.findData("av1"))
    assert panel.video_quality.isVisibleTo(panel)


def test_after_column_says_size_and_change():
    from wick.gui.file_model import _pct

    assert _pct(231, 149) == "−35%" and _pct(100, 110) == "+10%"


def test_keyboard_basics(win, tmp_path):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    for i in range(3):
        Image.new("RGB", (64 + i, 48), "teal").save(tmp_path / f"k{i}.png")
    conv = win.converter
    conv.clear()
    conv.add_paths([str(tmp_path)])
    assert _wait(lambda: not conv.busy() and conv.tree.topLevelItemCount() == 3, 30)

    # Ctrl+3 opens Convert with the focus in its list, and a row highlighted.
    for w in QApplication.topLevelWidgets():   # earlier tests may have left another window active
        if w is not win and w.isVisible() and w.isWindow():
            w.close()
    win.raise_()
    win.activateWindow()
    QTest.qWaitForWindowActive(win, 2000)
    win.sidebar.list.setFocus()
    QTest.keyClick(win.sidebar.list, Qt.Key_3, Qt.ControlModifier)
    _wait(lambda: win.pages.currentWidget() is conv and conv.tree.hasFocus(), 5)
    from PySide6.QtWidgets import QApplication as _A
    assert win.pages.currentWidget() is conv, (win.pages.currentWidget(), _A.focusWidget(), _A.activeWindow())
    assert conv.tree.hasFocus(), (_A.focusWidget(), conv.tree.isVisible(), conv.tree.model().rowCount())
    assert conv.tree.selectedItems()

    ticked = lambda: sum(conv.tree.topLevelItem(i).checkState(0) == Qt.Checked for i in range(3))  # noqa: E731
    QTest.keyClick(conv.tree, Qt.Key_A, Qt.ControlModifier | Qt.ShiftModifier)
    assert ticked() == 0
    QTest.keyClick(conv.tree, Qt.Key_A, Qt.ControlModifier)
    assert ticked() == 3
    QTest.keyClick(conv.tree, Qt.Key_Space)                      # Space toggles the current row
    assert ticked() == 2

    # The context menu opens for the current row from the keyboard.
    opened = []

    def close_menu():
        m = QApplication.activePopupWidget()
        if m is not None:
            opened.append([a.text() for a in m.actions()])
            m.close()
    QTimer.singleShot(300, close_menu)
    conv.tree._keyboard.menu_for_current()
    assert opened and "Copy Path" in opened[0]


def test_about_dialog(win):
    from PySide6.QtCore import Qt

    from wick.about import CREDITS
    from wick.gui.about_dialog import AboutDialog

    d = AboutDialog(win)
    d.show()
    assert d.tree.topLevelItemCount() == len(CREDITS)
    assert any("FFmpeg" in d.tree.topLevelItem(i).text(0) for i in range(len(CREDITS)))
    # Deliver Esc to the dialog itself (window activation isn't reliable on CI desktops).
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    # (As in the app, where the modal dialog has the focus: leftover focus in the main window from earlier
    # tests would otherwise let that page's own Esc shortcut take the key.)
    if QApplication.focusWidget() is not None:
        QApplication.focusWidget().clearFocus()
    QApplication.sendEvent(d, QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert not d.isVisible()


def test_pages_built_on_demand_and_history_rows_on_expand(win):
    from PySide6.QtWidgets import QApplication

    assert win.built("converter") is not None or "converter" in win._placeholders
    page = win.app_settings                      # builds on first use
    assert win.built("app_settings") is page and win.pages.indexOf(page) >= 0
    win.go_to(win.history)
    QApplication.processEvents()
    tree = win.history.tree
    runs = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount()) if tree.topLevelItem(i).data(0, 0x0100)]
    if runs:
        top = runs[0]
        assert top.childCount() == 0             # file rows only appear when the run is opened
        top.setExpanded(True)
        assert top.childCount() > 0


def test_title_bar_areas():
    """The app's own title bar: which part of the window drags, resizes, maximizes or is ordinary content."""
    from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton, QWidget

    from wick.gui import winframe as wf
    from wick.gui.style import Size

    QApplication.instance() or QApplication([])
    w = QMainWindow()
    w.setCentralWidget(QWidget())
    w.resize(800, 500)
    frame = wf.WindowFrame(w)
    in_band = QPushButton("Back", w.centralWidget())
    in_band.setGeometry(300, 4, 60, 30)
    frame.place()
    b = frame.buttons
    assert b.geometry().right() == w.width() - 1 and b.y() == 0
    mid = Size.TITLE_BAR // 2
    assert frame.area_at(400, mid) == wf.HTCAPTION                 # empty title strip: drag, Snap, menu
    assert frame.area_at(b.x() + b.max.x() + 20, mid) == wf.HTMAXBUTTON   # Snap Layouts flyout
    assert frame.area_at(b.x() + b.close.x() + 20, mid) == wf.HTCLIENT    # our close button gets the click
    assert frame.area_at(b.x() + b.min.x() + 20, mid) == wf.HTCLIENT
    assert frame.area_at(320, mid) == wf.HTCLIENT                  # a control in the strip stays clickable
    assert frame.area_at(400, Size.TITLE_BAR + 10) == wf.HTCLIENT  # page content
    assert frame.area_at(1, 1) == wf.HTTOPLEFT
    assert frame.area_at(1, 250) == wf.HTLEFT
    assert frame.area_at(799, 499) == wf.HTBOTTOMRIGHT
    assert frame.area_at(400, 2) == wf.HTTOP
    w.close()


def test_title_bar_on_windows(win):
    """Windows only (real desktop on CI): the whole window is client area, Windows hit-tests our strip and
    buttons, maximizing fills the work area exactly. Screenshots go to WICK_SHOTS_DIR if set."""
    if win.frame is None or not win.frame.native:
        pytest.skip("the app's own title bar is used on Windows")
    import ctypes
    from ctypes import wintypes
    from pathlib import Path

    from PySide6.QtWidgets import QApplication

    from wick.gui import winframe as wf
    from wick.gui.style import Size
    from wick.gui.theme import apply_theme

    user32 = ctypes.windll.user32
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    hwnd = int(win.winId())
    shots = os.environ.get("WICK_SHOTS_DIR")

    def rects():
        wr, cr = wintypes.RECT(), wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        user32.GetClientRect(hwnd, ctypes.byref(cr))
        tl = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(tl))
        return wr, (tl.x, tl.y, tl.x + cr.right, tl.y + cr.bottom)

    def hit(lx, ly):
        dpr = win.devicePixelRatioF()
        _, (cx, cy, _, _) = rects()
        x, y = int(cx + lx * dpr), int(cy + ly * dpr)
        return user32.SendMessageW(hwnd, 0x0084, 0, (y & 0xFFFF) << 16 | (x & 0xFFFF))

    def shot(name):
        if not shots:
            return
        QApplication.processEvents()
        time.sleep(0.4)
        QApplication.processEvents()
        g = win.frameGeometry()
        pad = 24
        img = win.screen().grabWindow(0, g.x() - pad, g.y() - pad, g.width() + 2 * pad, g.height() + 2 * pad)
        Path(shots).mkdir(parents=True, exist_ok=True)
        img.save(str(Path(shots) / f"{name}.png"))

    win.showNormal()
    win.resize(1280, 800)
    win.raise_()
    win.activateWindow()
    _wait(lambda: win.isActiveWindow(), 5)
    wr, cr = rects()
    assert (wr.left, wr.top, wr.right, wr.bottom) == cr, "the whole window should be client area"
    b = win.frame.buttons
    mid = Size.TITLE_BAR // 2
    assert hit(win.width() // 2, mid) == wf.HTCAPTION
    assert hit(b.x() + b.max.x() + 20, mid) == wf.HTMAXBUTTON
    assert hit(b.x() + b.close.x() + 20, mid) == wf.HTCLIENT
    assert hit(1, 1) == wf.HTTOPLEFT
    assert hit(win.width() // 2, win.height() // 2) == wf.HTCLIENT
    shot("window-light")

    user32.SendMessageW(hwnd, 0x0112, 0xF030, 0)   # SC_MAXIMIZE, as the button does
    assert _wait(lambda: win.isMaximized(), 5)

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    user32.MonitorFromWindow.restype = ctypes.c_void_p
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(ctypes.c_void_p(user32.MonitorFromWindow(hwnd, 2)), ctypes.byref(mi))
    work = (mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom)
    _wait(lambda: rects()[1] == work, 3)
    assert rects()[1] == work, f"maximized client {rects()[1]} should be the work area {work}"
    assert hit(1, 1) == wf.HTCAPTION                # no resize edges when maximized
    assert b.max.accessibleName() == "Restore"
    shot("window-maximized")

    user32.SendMessageW(hwnd, 0x0112, 0xF120, 0)   # SC_RESTORE
    assert _wait(lambda: not win.isMaximized(), 5)
    apply_theme("dark")
    win.refresh_colors()
    shot("window-dark")
    apply_theme("light")
    win.refresh_colors()


def test_columns_keep_a_minimum_width():
    """Dragging a column narrow stops where its heading still fits, and short values (sizes) stay whole."""
    from PySide6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem

    from wick.gui.filelist import make_sortable

    QApplication.instance() or QApplication([])
    tree = QTreeWidget()
    tree.setHeaderLabels(["Name", "Size", "Details"])
    for i in range(20):
        QTreeWidgetItem(tree, [f"A rather long file name number {i}.jpg", "120.0 MB", "Lossless, reversible"])
    tree.resize(900, 400)
    tree.show()
    header = make_sortable(tree)
    QApplication.processEvents()
    fm = tree.fontMetrics()
    header.resizeSection(1, 5)                      # what a drag to the left edge asks for
    assert header.sectionSize(1) >= fm.horizontalAdvance("120.0 MB")
    header.resizeSection(0, 5)
    assert header.sectionSize(0) >= max(header.MIN_TEXT, fm.horizontalAdvance("Name"))
    assert header.sectionSize(0) < fm.horizontalAdvance("A rather long file name number 10.jpg")  # long text may elide
    tree.close()


def test_language_choice_shows_with_only_my_languages(win):
    panel = win.compress.panel
    panel.video_codec.setCurrentIndex(panel.video_codec.findData("auto"))
    panel.video_tracks.setCurrentIndex(panel.video_tracks.findData("all"))
    assert not panel.languages_btn.isVisibleTo(panel)
    panel.video_tracks.setCurrentIndex(panel.video_tracks.findData("mine"))
    assert panel.languages_btn.isVisibleTo(panel) and panel.languages_btn.text()
    s = panel.settings()
    assert s.video_tracks == "mine" and s.video_codec == "auto"
    panel.video_tracks.setCurrentIndex(panel.video_tracks.findData("all"))


def test_update_check_setting_and_notice(win):
    from wick import updates
    from wick.gui.widgets import HelpTip

    page = win.app_settings
    assert page.update_frequency.currentData() == "weekly"  # the default
    page.update_frequency.setCurrentIndex(page.update_frequency.findData("monthly"))
    page.update_frequency.activated.emit(page.update_frequency.currentIndex())
    assert updates.frequency(page.qs) == "monthly"            # saved as soon as it's chosen
    page.update_frequency.setCurrentIndex(page.update_frequency.findData("weekly"))
    page.update_frequency.activated.emit(page.update_frequency.currentIndex())
    tips = [t for t in page.findChildren(HelpTip) if t.help_text == updates.HELP]
    assert tips and tips[0].accessibleDescription() == updates.HELP
    win.go_to(page)
    tips[0].click()                                         # a popover, not a tooltip: it stays and wraps
    assert tips[0].is_open()
    pop = tips[0]._popover
    assert pop.width() <= 400 * 2 and pop.body.text() == updates.HELP and pop.body.height() > pop.body.fontMetrics().height()
    assert pop.sticky
    pop.close()
    # Hovering shows the same card without taking the focus; leaving closes it; clicking while hovered keeps it.
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QEnterEvent
    from PySide6.QtWidgets import QApplication

    tip = tips[0]
    QApplication.sendEvent(tip, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
    _wait(lambda: tip.is_open(), 2)
    assert not tip._popover.sticky and tip._popover.body.text() == updates.HELP
    QApplication.sendEvent(tip, QEvent(QEvent.Leave))
    _wait(lambda: not tip.is_open(), 2)
    QApplication.sendEvent(tip, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
    _wait(lambda: tip.is_open(), 2)
    tip.click()
    assert tip.is_open() and tip._popover.sticky
    QApplication.sendEvent(tip, QEvent(QEvent.Leave))
    assert tip.is_open()                                    # pinned by the click: leaving doesn't close it
    tip._popover.close()
    win.sidebar.set_update("99.0", security=True)
    assert win.sidebar.update_btn.isVisibleTo(win.sidebar) and "security" in win.sidebar.update_btn.text().lower()
    win.sidebar.set_update(None)
    assert not win.sidebar.update_btn.isVisibleTo(win.sidebar)


def test_about_lists_versions(win):
    from wick.gui.about_dialog import AboutDialog

    dlg = AboutDialog(win)
    rows = {dlg.tree.topLevelItem(i).text(0): dlg.tree.topLevelItem(i).text(1)
            for i in range(dlg.tree.topLevelItemCount())}
    assert rows["Pillow"].split(".")[0].isdigit() and rows["Python"]
    dlg.close()


def test_dragging_a_column_edge_moves_that_edge(win, tmp_path):
    """The Compress list: the dragged edge follows the pointer, the columns always fill the list exactly,
    hidden columns leave no gap, and no drag pushes the last column out of sight."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from wick.gui.file_model import C_NAME, C_STATUS

    for i in range(3):
        Image.effect_noise((120, 90), 40).convert("RGB").save(tmp_path / f"p{i}.png")
    t = win.compress
    win.go_to(t)
    t.clear()
    t.add_paths([str(tmp_path)])
    assert _wait(lambda: not t.busy() and t.model.rows, 60)
    for _ in range(5):
        QApplication.processEvents()
    h = t.table.horizontalHeader()
    vp = h.viewport()
    shown = [h.logicalIndex(v) for v in range(h.count()) if not h.isSectionHidden(h.logicalIndex(v))]

    def edge(c):
        return h.sectionViewportPosition(c) + h.sectionSize(c)

    def total():
        return sum(h.sectionSize(c) for c in shown)

    assert total() == vp.width()
    for c in shown[1:]:   # no gap left by hidden columns
        prev = shown[shown.index(c) - 1]
        assert h.sectionViewportPosition(c) == edge(prev)

    def drag(col, dx):
        x, y = edge(col) - 1, h.height() // 2
        QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, QPoint(x, y))
        for step in range(1, 9):
            QTest.mouseMove(vp, QPoint(x + dx * step // 8, y))
        QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, QPoint(x + dx, y))
        QApplication.processEvents()

    for col, dx in ((C_NAME, 30), (C_NAME, -20)):
        before = edge(col)
        drag(col, dx)
        assert edge(col) - before == dx, (col, dx, edge(col) - before)
        assert total() == vp.width()
    drag(C_NAME, 3000)                                            # far too far: stops in time
    assert h.sectionSize(C_STATUS) >= h.min_width(C_STATUS) and total() == vp.width()


def test_pause_button_and_battery_pause(win, tmp_path, no_dialogs):
    from wick.settings import Settings

    for i in range(4):
        Image.effect_noise((200, 150), 40).convert("RGB").save(tmp_path / f"p{i}.png")
    t = win.compress
    t.panel.load(Settings(image_format="webp", png_mode="lossy", output_mode="suffix", min_image_kb=0,
                          require_savings=False, video_codec="none"))
    t.clear()
    t.add_paths([str(tmp_path)])
    assert _wait(lambda: not t.busy() and t.model.rows, 60)
    win.qsettings.setValue("pause_on_battery", True)
    win.on_battery = True                        # unplugged before the job starts: it waits
    assert t.start_job(confirm=False)
    assert t.job_thread.pause.is_set() and "battery" in t.job_status.detail.text().lower()
    win.on_battery = False
    win.power_changed.emit(False)                # plugged in: it continues
    assert not t.job_thread.pause.is_set()
    t.job_status.pause_btn.click()               # the user pauses: stays paused even on mains power
    assert t.job_thread.pause.is_set() and t.job_status.pause_btn.text() == "Resume"
    win.power_changed.emit(False)
    assert t.job_thread.pause.is_set()
    t.job_status.pause_btn.click()
    assert not t.job_thread.pause.is_set()
    assert _wait(lambda: not t.busy(), 120)
    win.qsettings.setValue("pause_on_battery", False)


def test_an_unfinished_job_can_be_resumed(win, tmp_path, no_dialogs):
    from wick import pending
    from wick.settings import Settings

    files = []
    for i in range(3):
        f = tmp_path / f"r{i}.png"
        Image.effect_noise((200, 150), 40).convert("RGB").save(f)
        files.append(str(f))
    s = Settings(image_format="webp", png_mode="lossy", output_mode="suffix", min_image_kb=0, require_savings=False,
                 video_codec="none")
    pending.start("compress", s.to_dict(), str(tmp_path), files)   # as if the app closed after the first file
    pending.done("compress", files[0])
    t = win.compress
    t._check_pending()
    assert not t.resume_banner.isHidden() and "2 of 3" in t.resume_banner.detail.text()
    t.resume_banner.resume_btn.click()
    assert _wait(lambda: t.job_thread is not None and not t.busy() and t.model.rows
                 and all(r.result for r in t.model.rows), 120)
    assert sorted(p.name for p in tmp_path.glob("*_compressed.webp")) == ["r1_compressed.webp", "r2_compressed.webp"]
    assert pending.load("compress") is None                       # done: nothing left to resume


def test_history_marks_what_cant_be_undone(win, tmp_path, no_dialogs):
    import time as _time

    from wick import journal

    w = journal.RunWriter(tmp_path, {"tool": "compress"})
    gone = tmp_path / "gone.png"                      # never in the bin: as if it had been emptied
    out = tmp_path / "gone.webp"
    out.write_bytes(b"x" * 10)
    w.add(journal.FileRecord(str(gone), str(out), 1000, 10, "png", "PNG → WebP", False, _time.time()))
    w.close()
    h = win.history
    win.go_to(h)
    h.refresh(force=True)
    assert _wait(lambda: any((r, str(gone)) in h.blocked for r in [w.run.run_id]), 30)
    top = next(h.tree.topLevelItem(i) for i in range(h.tree.topLevelItemCount())
               if h.tree.topLevelItem(i).data(0, 256)[1].run_id == w.run.run_id)
    assert "can't be undone" in top.text(4)
    h.tree.clearSelection()
    top.setSelected(True)
    h.undo_selected()                                 # nothing undoable: says why, changes nothing
    assert any("Nothing here can be undone" in m for m in no_dialogs)


def test_reports_from_a_job_and_from_history(win, tmp_path, no_dialogs, monkeypatch):
    import csv

    from PySide6.QtWidgets import QFileDialog

    import wick.gui.file_model as fm
    from wick.settings import Settings

    for i in range(2):
        Image.effect_noise((200, 150), 40).convert("RGB").save(tmp_path / f"q{i}.png")
    t = win.compress
    t.panel.load(Settings(image_format="webp", png_mode="lossy", output_mode="suffix", min_image_kb=0,
                          require_savings=False, video_codec="none"))
    t.clear()
    t.add_paths([str(tmp_path / "q0.png"), str(tmp_path / "q1.png")])
    assert _wait(lambda: not t.busy() and t.model.rows, 60)
    assert t.start_job(confirm=False)
    assert _wait(lambda: not t.busy(), 120)
    _wait(lambda: not t.job_status.report_btn.isHidden(), 10)
    out = tmp_path / "report"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    monkeypatch.setattr(fm, "open_in_file_manager", lambda *a, **k: None)
    t.job_status.report_btn.click()
    with open(tmp_path / "report.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0][0] == "File" and sorted(r[0] for r in rows[1:]) == ["q0.png", "q1.png"]
    assert all(r[2] == "Done" and int(r[3]) > int(r[4]) and r[7].endswith(".webp") for r in rows[1:])

    win.go_to(win.history)
    win.history.refresh(force=True)
    out2 = tmp_path / "history.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out2), "")))
    win.history.tree.clearSelection()
    win.history.export_report()                      # nothing selected: everything in History
    with open(out2, encoding="utf-8-sig", newline="") as f:
        names = [r[0] for r in csv.reader(f)][1:]
    assert {"q0.png", "q1.png"} <= set(names)


def test_welcome_once_then_a_first_look(win, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from wick.gui.welcome import WelcomeDialog

    looked = []
    monkeypatch.setattr(win.home, "analyze", lambda background=False: looked.append(True))
    win.qsettings.setValue("welcome_done", False)
    win._welcome()
    dlg = win._welcome_dialog
    assert isinstance(dlg, WelcomeDialog) and dlg.isVisible()
    texts = " ".join(lab.text() for lab in dlg.findChildren(__import__("PySide6.QtWidgets").QtWidgets.QLabel))
    assert "stay on this computer" in texts and "can be undone" in texts
    dlg.accept()
    QApplication.processEvents()
    assert looked and win.qsettings.value("welcome_done", False, bool)
    assert win.pages.currentWidget() is win.home


def test_where_your_space_goes(win, tmp_path):
    from wick import space
    from wick.gui.space_card import SpaceCard

    b = space.Breakdown("C:", total=1000, free=300, sizes={space.PHOTOS: 200, space.VIDEOS: 100, space.CACHES: 50})
    assert b.used == 700 and b.other == 350
    card = SpaceCard()
    card.show_breakdown(b)
    labels = [w.text() for w in card._legend_widgets if hasattr(w, "text")]
    assert "Photos" in labels and "Everything else" in labels and "Music" not in labels   # empty ones left out
    assert "C: drive" in card.summary.text()
    win.home.space_card.act.emit(space.CACHES)              # "Clean Up" → Clean Up › Caches
    assert win.pages.currentWidget() is win.cleanup
    assert win.cleanup.tabs.tabText(win.cleanup.tabs.currentIndex()) == "Caches"


def test_monthly_check_only_when_on_and_due(win, monkeypatch):
    import time as _time

    from wick import overview, space

    calls, messages = [], []
    monkeypatch.setattr(win.home, "analyze", lambda background=False: calls.append(1))
    monkeypatch.setattr(win, "tray_message", lambda m, force=False: messages.append(m))
    win.qsettings.setValue("monthly_check", False)
    assert not win._monthly_due()                                   # off by default
    win.qsettings.setValue("monthly_check", True)
    win.qsettings.setValue("monthly/last", _time.time() - 5 * 86400)
    assert not win._monthly_due()                                   # not a month yet
    win.qsettings.setValue("monthly/last", 0.0)
    assert win._monthly_due() and calls == [1]
    ov = overview.Overview()
    ov.space = space.Breakdown("C:", 10 ** 12, 10 ** 11, sizes={space.CACHES: 2 * 1024 ** 3})
    win.home.analyzed.emit(ov)
    assert messages and "could be freed" in messages[0] and "nothing has been changed" in messages[0]
    assert _time.time() - win.qsettings.value("monthly/last", 0.0, float) < 60
    messages.clear()
    win._monthly_running = True
    win.home.analyzed.emit(overview.Overview())                     # little to free: no notification
    assert not messages
    win.qsettings.setValue("monthly_check", False)


def test_history_can_be_deleted_and_turned_off(win, tmp_path, no_dialogs):
    import time as _time
    from pathlib import Path

    from wick import journal
    from wick import log as applog
    from wick import pending

    def run(names):
        w = journal.RunWriter(tmp_path, {"tool": "compress"})
        for n in names:
            w.add(journal.FileRecord(str(tmp_path / n), str(tmp_path / (n + ".jxl")), 1000, 10, "jpeg", "x", False,
                                     _time.time()))
        w.close()
        return w.run.run_id

    journal.clear()
    a, b = run(["secret-a.jpg", "keep.jpg"]), run(["b.jpg"])
    h = win.history
    win.go_to(h)
    h.refresh(force=True)
    assert len(h.runs) == 2
    # One file's record (and name) goes; its run stays with the other file.
    journal.forget_files(a, {str(tmp_path / "secret-a.jpg")})
    assert "secret-a" not in (journal.runs_dir() / f"{a}.jsonl").read_text("utf-8")
    h.refresh(force=True)
    run_a = next(r for r in h.runs if r.run_id == a)
    assert [Path(f.original).name for f in run_a.files] == ["keep.jpg"]
    # Selected runs are deleted from the page; nothing selected deletes everything (after asking).
    h.tree.clearSelection()
    next(h.tree.topLevelItem(i) for i in range(h.tree.topLevelItemCount())
         if h.tree.topLevelItem(i).data(0, 256)[1].run_id == b).setSelected(True)
    h.delete_selected()
    assert [r.run_id for r in h.runs] == [a]
    h.tree.clearSelection()
    h.delete_selected()                                # nothing selected: nothing deleted
    assert [r.run_id for r in h.runs] == [a]
    applog.log.info("a line naming secret-a.jpg")
    h.delete_everything()                              # every run and the log (after asking)
    assert not h.runs and not list(journal.runs_dir().glob("*.jsonl"))
    assert not applog.log_file().exists() or "secret-a" not in applog.log_file().read_text("utf-8")
    h.reload()                                         # Refresh: reads again, rechecks the bin

    # Off: nothing about the files is written (runs, resume lists, log), and what's there can be deleted.
    run(["old.jpg"])
    page = win.app_settings
    try:
        page.history_on.setChecked(False)
        assert not journal.ENABLED and not list(journal.runs_dir().glob("*.jsonl"))    # (asked: deleted)
        assert not applog.log_file().exists()
        run(["private.jpg"])
        assert not list(journal.runs_dir().glob("*.jsonl"))
        pending.start("compress", {}, str(tmp_path), [str(tmp_path / "private.jpg")])
        assert pending.load("compress") is None
        applog.log.info("private.jpg")
        assert not applog.log_file().exists()
        h.refresh(force=True)
        win.go_to(h)
        assert "History is off" in h.placeholder.message
        assert not page.history_keep_row.isEnabled()
    finally:
        page.history_on.setChecked(True)
    assert journal.ENABLED
    run(["new.jpg"])
    assert list(journal.runs_dir().glob("*.jsonl"))


def test_cleanup_hides_what_isnt_suggested(win, tmp_path):
    from wick.cleanup.common import Candidate, ScanResult

    win.go_to(win.cleanup)
    sec = win.cleanup.sections[0]
    ok = Candidate(tmp_path / "a.zip", 1000)
    risky = Candidate(tmp_path / "b.zip", 2000, warning="still in use", selected=False)
    sec.hide_unsuggested.setChecked(True)
    sec._scanned(ScanResult(sec.tool.key, [ok, risky]))
    shown = [c.path.name for _, c in sec._items()]
    assert shown == ["a.zip"] and not sec.hide_unsuggested.isHidden()
    assert "1 not suggested, hidden" in sec.summary.text()
    sec.hide_unsuggested.setChecked(False)
    assert sorted(c.path.name for _, c in sec._items()) == ["a.zip", "b.zip"]
    sec.hide_unsuggested.setChecked(True)


def test_clicking_a_row_ticks_it_and_type_selection(win, tmp_path, no_dialogs):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from wick.gui.file_model import C_CHECK, C_NAME
    from wick.settings import Settings

    for i in range(2):
        Image.effect_noise((200, 150), 30).convert("RGB").save(tmp_path / f"p{i}.png")
    Image.effect_noise((200, 150), 30).convert("RGB").save(tmp_path / "j.jpg", quality=95)
    c = win.compress
    c.panel.load(Settings(image_format="webp", png_mode="lossy", jpeg_mode="lossy", min_image_kb=0, video_codec="none",
                          require_savings=False))
    c.clear()
    c.add_paths([str(tmp_path)])
    assert _wait(lambda: not c.busy() and len(c.model.rows) == 3, 60)
    win.go_to(c)
    _wait(lambda: c.table.isVisible(), 5)
    row0 = c.proxy.index(0, C_NAME)
    before = c.proxy.index(0, C_CHECK).data(Qt.CheckStateRole)
    rect = c.table.visualRect(row0)
    QTest.mouseClick(c.table.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())   # on the name, not the box
    after = c.proxy.index(0, C_CHECK).data(Qt.CheckStateRole)
    assert Qt.CheckState(after) != Qt.CheckState(before)
    QTest.mouseClick(c.table.viewport(), Qt.LeftButton, Qt.ControlModifier, rect.center())   # Ctrl: select only
    assert Qt.CheckState(c.proxy.index(0, C_CHECK).data(Qt.CheckStateRole)) == Qt.CheckState(after)
    # Right-click "Deselect All PNG Files": every PNG, and only PNGs.
    from wick.formats import type_label

    for r in c.model.rows:
        r.checked = True
    pngs = [r for r in c.model.rows if type_label(r.entry.path, r.entry.kind) == "PNG"]
    for r in pngs:
        c.model.setData(c.model.index(c.model.rows.index(r), C_CHECK), Qt.Unchecked, Qt.CheckStateRole)
    assert [r.entry.path.suffix for r in c.model.rows if r.checked] == [".jpg"]


def test_date_columns_can_be_shown_and_are_remembered(win, tmp_path):
    from wick.gui.file_model import C_CREATED, C_MODIFIED
    from wick.gui.main_window import open_qsettings

    c = win.compress
    h = c.table.horizontalHeader()
    assert c.table.isColumnHidden(C_CREATED) and c.table.isColumnHidden(C_MODIFIED)   # off until wanted
    h.set_column_shown(C_MODIFIED, True)
    assert C_MODIFIED not in h.user_hidden
    assert str(C_MODIFIED) not in [str(x) for x in (open_qsettings().value("columns/compress/hidden") or [])]
    Image.effect_noise((64, 48), 30).convert("RGB").save(tmp_path / "d.png")
    c.clear()
    c.add_paths([str(tmp_path / "d.png")])
    assert _wait(lambda: not c.busy() and c.model.rows, 60)
    text = c.model.data(c.model.index(0, C_MODIFIED))
    assert len(text) == 16 and text[4] == "-" and text[7] == "-"        # 2026-11-06 14:05
    h.set_column_shown(C_MODIFIED, False)
    assert C_MODIFIED in h.user_hidden


def test_folder_sizes_open_level_by_level(win, tmp_path, no_dialogs, monkeypatch):
    from PySide6.QtCore import Qt

    from wick.cleanup.folders import find_folder_sizes
    from wick.gui import cleanup_section
    from wick.gui.cleanup_section import ROLE_ITEM

    monkeypatch.setattr(cleanup_section, "choose", lambda *a, **k: False)

    (tmp_path / "Big" / "Deep").mkdir(parents=True)
    (tmp_path / "Small").mkdir()
    (tmp_path / "AppData").mkdir()
    (tmp_path / "Big" / "Deep" / "movie.bin").write_bytes(b"x" * 300_000)
    (tmp_path / "Big" / "notes.txt").write_bytes(b"x" * 1000)
    (tmp_path / "Small" / "a.txt").write_bytes(b"x" * 5000)
    (tmp_path / "AppData" / "cache.bin").write_bytes(b"x" * 50_000)
    win.go_to(win.cleanup)
    sec = next(s for s in win.cleanup.sections if s.tool.key == "folders")
    sec.folders = [tmp_path]
    sec._scanned(find_folder_sizes([tmp_path]))
    root = sec.tree.topLevelItem(0)
    assert root.isExpanded()
    names = [root.child(i).text(0) for i in range(root.childCount())]
    assert names == ["Big", "AppData", "Small"]                         # biggest first
    appdata = root.child(1)
    assert not appdata.flags() & Qt.ItemIsUserCheckable                 # apps need it: can't be selected
    big = root.child(0)
    assert big.childCount() == 0
    big.setExpanded(True)                                               # rows made when it opens
    assert [big.child(i).text(0) for i in range(big.childCount())] == ["Deep", "notes.txt"]
    big.setCheckState(0, Qt.Checked)
    big.child(0).setCheckState(0, Qt.Checked)                          # inside a chosen folder: not counted twice
    assert [c.path.name for _, c in sec._items(True)] == ["Big"]
    assert "1 selected" in sec.summary.text()
    big.child(0).setCheckState(0, Qt.Unchecked)
    big.setCheckState(0, Qt.Unchecked)
    big.child(0).setCheckState(0, Qt.Checked)                          # remove Big/Deep
    sec.run_action("trash")
    assert _wait(lambda: not sec.busy(), 30)
    assert not (tmp_path / "Big" / "Deep").exists()
    root = sec.tree.topLevelItem(0)
    big = next(root.child(i) for i in range(root.childCount()) if root.child(i).text(0) == "Big")
    assert big.isExpanded() and [big.child(i).text(0) for i in range(big.childCount())] == ["notes.txt"]
    assert big.data(0, ROLE_ITEM).size == 1000
    assert root.child(0).text(0) == "AppData"                           # sizes went down: order follows


def test_convert_files_can_have_their_own_format(win, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt

    from wick.gui import convert_settings

    for name in ("a.png", "b.png"):
        Image.new("RGB", (64, 48), "teal").save(tmp_path / name)
    conv = win.converter
    conv.clear()
    conv.image_target.setCurrentIndex(conv.image_target.findData("jpeg"))
    conv.add_paths([str(tmp_path)])
    assert _wait(lambda: not conv.busy() and conv.tree.topLevelItemCount() == 2, 30)
    entries = {e.path.name: e for e in conv.entries}
    menu = dict((x[0], x[1]) for x in conv.menu_entries([entries["b.png"]]) if x)
    webp = dict(menu["Convert To"])["WebP"]
    conv.tree.topLevelItem(0).setCheckState(0, Qt.Unchecked)       # (a left-out row stays left out)
    webp()
    rows = {conv.tree.topLevelItem(i).text(0): conv.tree.topLevelItem(i) for i in range(2)}
    assert rows["b.png"].text(4).startswith("WebP") and "own settings" in rows["b.png"].text(4)
    assert rows["a.png"].text(4).startswith("JPEG")
    assert sum(conv.tree.topLevelItem(i).checkState(0) == Qt.Checked for i in range(2)) == 1

    # The settings dialog changes only what was chosen for these files.
    def fake_exec(dlg):
        dlg.limit.setChecked(True)
        dlg.side.setValue(32)
        return dlg.DialogCode.Accepted
    monkeypatch.setattr(convert_settings.ConvertSettingsDialog, "exec", fake_exec)
    menu = dict((x[0], x[1]) for x in conv.menu_entries([entries["b.png"]]) if x)
    menu["Format and Settings…"]()
    assert conv.opts_for(entries["b.png"]).max_side == 32 and conv.opts_for(entries["b.png"]).image_target == "webp"
    assert conv.opts_for(entries["a.png"]).max_side == 0
    menu = dict((x[0], x[1]) for x in conv.menu_entries([entries["b.png"]]) if x)
    menu["Use the Page's Settings"]()
    assert conv.opts_for(entries["b.png"]).image_target == "jpeg"
    conv.clear()


def test_convert_lists_documents_and_offers_pandoc(win, tmp_path, monkeypatch):
    from wick import tools

    (tmp_path / "notes.md").write_text("# Notes\n")
    (tmp_path / "sheet.xlsx").write_bytes(b"PK")                      # (only an office app reads it: to PDF)
    real = tools.find_tool
    monkeypatch.setattr(tools, "find_tool", lambda n: None if n == "pandoc" else real(n))
    conv = win.converter
    conv.clear()
    conv.add_paths([str(tmp_path)])
    assert _wait(lambda: not conv.busy() and conv.tree.topLevelItemCount() == 2, 30)
    rows = {conv.tree.topLevelItem(i).text(0): conv.tree.topLevelItem(i) for i in range(2)}
    row = rows["notes.md"]
    assert row.text(conv.STATUS_COL) == "Needs Pandoc (see above)"
    assert rows["sheet.xlsx"].text(conv.STATUS_COL) == "Only Office or LibreOffice reads this: choose PDF"
    assert not conv.pandoc_row.isHidden() and "Pandoc" in conv.pandoc_note.text()
    monkeypatch.setattr(tools, "find_tool", real)
    conv.document_target.setCurrentIndex(conv.document_target.findData("none"))
    assert conv.pandoc_row.isHidden()                                  # (documents aren't being converted)
    conv.document_target.setCurrentIndex(conv.document_target.findData("docx"))
    conv.clear()


def test_icon_sizes_and_website_icons_from_convert(win, tmp_path, monkeypatch):
    from wick import convert, icons
    from wick.gui.convert_settings import IMAGE, ConvertSettingsDialog

    dlg = ConvertSettingsDialog(convert.ConvertOptions(image_target="ico"), {IMAGE}, "Settings", parent=win)
    assert dlg.ico_preset.currentData() == "web" and dlg.ico_preset.isVisibleTo(dlg)
    dlg.ico_preset.setCurrentIndex(dlg.ico_preset.findData("mediawiki"))
    assert dlg._ico_sizes() == (16, 32, 48, 64)
    dlg.ico_boxes[256].setChecked(True)                                # a size of your own: "Choose sizes"
    assert dlg.ico_preset.currentData() == "custom"
    assert dlg.result_options().ico_sizes == (16, 32, 48, 64, 256)
    dlg.image_target.setCurrentIndex(dlg.image_target.findData("png"))
    assert not dlg.ico_preset.isVisibleTo(dlg)                         # (only for icons)
    dlg.deleteLater()

    Image.new("RGB", (64, 64), "teal").save(tmp_path / "logo.png")
    conv = win.converter
    conv.clear()
    conv.add_paths([str(tmp_path)])
    assert _wait(lambda: not conv.busy() and conv.tree.topLevelItemCount() == 1, 30)
    shown = []
    from wick.gui import dialogs
    monkeypatch.setattr(dialogs, "choose", lambda _p, text, info, *a: shown.append((text, info)) or False)
    menu = dict((x[0], x[1]) for x in conv.menu_entries(conv.entries) if x)
    menu["Make Website Icons…"]()
    out = tmp_path / "logo website icons"
    assert sorted(p.name for p in out.iterdir()) == sorted(n for n in icons.WEB_FILES if n != "icon.svg")
    assert shown and "<link rel=\"icon\"" in shown[0][1]
    from PySide6.QtWidgets import QApplication

    assert 'rel="manifest"' in QApplication.clipboard().text()
    conv.clear()


def test_convert_from_the_context_menu(win, tmp_path):
    """Explorer's Convert with Wick ▸ JPEG: one launch per selected file, collected, converted next to
    the originals and started without asking; files already listed are left as they were."""
    from wick.gui import integration

    for name in ("a.png", "b.png", "keep.png"):
        Image.new("RGB", (64, 48), "teal").save(tmp_path / name)
    conv = win.converter
    conv.clear()
    smaller = conv.smaller.isChecked()
    conv.smaller.setChecked(False)         # this test deliberately exports tiny PNGs as larger JPEGs
    conv.output.setCurrentIndex(conv.output.findData("suffix"))       # (the page's choice doesn't apply here)
    conv.add_paths([str(tmp_path / "keep.png")])
    assert _wait(lambda: not conv.busy() and conv.tree.topLevelItemCount() == 1, 30)
    win.receive_paths([str(tmp_path / "a.png")], show=False, convert="jpeg")
    win.receive_paths([str(tmp_path / "b.png")], show=False, convert="jpeg")
    assert win.pages.currentWidget() is conv
    assert _wait(lambda: (tmp_path / "a.jpg").exists() and (tmp_path / "b.jpg").exists() and not conv.busy(), 60)
    assert not (tmp_path / "keep.jpg").exists() and (tmp_path / "a.png").exists()   # originals kept
    assert integration.CONVERT_MENUS["image"][0] == ("JPEG", "jpeg")
    labels = [label for label, _ in integration._convert_items("video")]
    assert labels[-1] == "Other Format…" and "GIF" in labels
    conv.output.setCurrentIndex(conv.output.findData("beside"))
    conv.smaller.setChecked(smaller)
    conv.clear()


def test_rows_that_become_convertible_are_ticked(win, tmp_path):
    from PySide6.QtCore import Qt

    Image.new("RGB", (64, 48), "teal").save(tmp_path / "a.png")
    conv = win.converter
    conv.clear()
    conv.image_target.setCurrentIndex(conv.image_target.findData("png"))
    conv.add_paths([str(tmp_path)])
    assert _wait(lambda: not conv.busy() and conv.tree.topLevelItemCount() == 1, 30)
    assert conv.tree.topLevelItem(0).text(conv.STATUS_COL).startswith("Already")
    conv.image_target.setCurrentIndex(conv.image_target.findData("webp"))
    assert conv.tree.topLevelItem(0).checkState(0) == Qt.Checked        # (it wasn't left out by you)
    conv.clear()


def test_convert_pdf_actions_and_combining(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from wick import pdftools
    from wick.gui import convert_tab, dialogs

    Image.new("RGB", (64, 48), "teal").save(tmp_path / "a.png")
    Image.new("RGB", (48, 64), "red").save(tmp_path / "b.jpg", quality=90)
    pdftools.combine([tmp_path / "a.png"], tmp_path / "doc.pdf")
    conv = win.converter
    conv.clear()
    conv.add_paths([str(tmp_path)])
    assert _wait(lambda: not conv.busy() and conv.tree.topLevelItemCount() == 3, 30)
    entries = {e.path.name: e for e in conv.entries}
    assert entries["doc.pdf"].kind == "pdf"
    menu = dict(x for x in conv.menu_entries([entries["doc.pdf"]]) if x)
    pdf_menu = dict(x for x in menu["PDF"] if x)
    monkeypatch.setattr(convert_tab.ConvertTab, "_ask_password", lambda self, *a, **k: "s3cret")
    pdf_menu["Add a Password…"]()
    o = conv.opts_for(entries["doc.pdf"])
    assert o.pdf_target == "protect" and o.pdf_password == "s3cret"
    row = next(conv.tree.topLevelItem(i) for i in range(3) if conv.tree.topLevelItem(i).text(0) == "doc.pdf")
    assert row.text(4).startswith("With a password")

    dest = tmp_path / "both.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(dest), ""))
    monkeypatch.setattr(dialogs, "choose", lambda *a, **k: False)
    menu = dict(x for x in conv.menu_entries([entries["b.jpg"], entries["a.png"]]) if x)
    menu["Combine Into One PDF…"]()
    assert _wait(lambda: dest.exists() and not conv.busy(), 30)
    assert pdftools.page_count(dest) == 2
    conv.clear()


def test_turning_from_the_convert_menu_adds_up(win, tmp_path):
    Image.new("RGB", (64, 48), "teal").save(tmp_path / "a.jpg")
    conv = win.converter
    conv.clear()
    conv.add_paths([str(tmp_path)])
    assert _wait(lambda: not conv.busy() and conv.tree.topLevelItemCount() == 1, 30)
    e = conv.entries[0]

    def do(label):
        menu = dict(x for x in conv.menu_entries([e]) if x)
        dict(x for x in menu["Turn"] if x)[label]()

    do("Right")
    assert (conv.opts_for(e).turn, conv.opts_for(e).image_target) == (90, "keep")
    do("Right")
    assert conv.opts_for(e).turn == 180
    do("Mirror")                                   # after an upside-down turn, a mirror is still left to right
    assert conv.opts_for(e).flip == "h"
    do("Flip Upside Down")                         # both mirrors: turned upside down again, no mirror
    assert (conv.opts_for(e).turn, conv.opts_for(e).flip) == (0, "")
    assert "Nothing to change" in conv.tree.topLevelItem(0).text(conv.STATUS_COL)
    conv.clear()


def test_trim_from_convert(win, tmp_path, monkeypatch):
    from wick.gui import dialogs
    from wick.gui.trim_dialog import TrimDialog, clock, parse_clock

    from .conftest import ffmpeg, have

    if not have("ffmpeg", "ffprobe"):
        pytest.skip("ffmpeg not installed")
    assert parse_clock("1:23.4") == 83.4 and parse_clock("0:01:00") == 60 and parse_clock("x") is None
    assert clock(83.44) == "1:23.4"
    ffmpeg("-f", "lavfi", "-i", "sine=d=4", str(tmp_path / "song.mp3"))
    conv = win.converter
    conv.clear()
    conv.add_paths([str(tmp_path)])
    assert _wait(lambda: not conv.busy() and conv.tree.topLevelItemCount() == 1, 30)

    def fake_exec(dlg):
        assert not dlg.save.isEnabled()                       # (nothing cut off yet)
        dlg.fields[0].setText("1")
        dlg._typed(0)
        dlg.sliders[1].setValue(30)
        assert dlg.times() == (1.0, 3.0) and dlg.save.isEnabled()
        return dlg.DialogCode.Accepted
    monkeypatch.setattr(TrimDialog, "exec", fake_exec)
    monkeypatch.setattr(dialogs, "choose", lambda *a, **k: False)
    menu = dict(x for x in conv.menu_entries(conv.entries) if x)
    menu["Trim…"]()
    out = tmp_path / "song (trimmed).mp3"
    assert _wait(lambda: out.exists() and not conv.busy(), 30)
    from wick.cutjoin import duration_of

    assert abs(duration_of(out) - 2.0) < 0.1
    conv.clear()


def test_convert_presets_from_the_page_and_the_context_menu(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from wick import engine, journal
    from wick.gui import convert_presets, integration

    conv = win.converter
    conv.clear()
    conv.image_target.setCurrentIndex(conv.image_target.findData("webp"))
    conv.output.setCurrentIndex(conv.output.findData("beside"))
    conv.originals.setCurrentIndex(conv.originals.findData("move"))
    conv.originals_dir.setText(str(tmp_path / "done"))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Web pictures", True))
    conv._save_preset()
    presets = convert_presets.load(conv.qs)
    assert "image" in convert_presets.kinds(presets["Web pictures"])     # (and whatever else the page converts)
    assert "originals moved to a folder" in convert_presets.describe(presets["Web pictures"])
    labels = [label for label, _ in integration._convert_items("image", presets)]
    assert "Web pictures" in labels and labels[-1] == "Other Format…"

    # Change the page, then run the preset from Explorer: its own choices apply, not the page's.
    conv.image_target.setCurrentIndex(conv.image_target.findData("png"))
    conv.originals.setCurrentIndex(conv.originals.findData("keep"))
    Image.new("RGB", (64, 48), "teal").save(tmp_path / "a.jpg")
    win.receive_paths([str(tmp_path / "a.jpg")], show=False, convert="preset:Web pictures")
    assert _wait(lambda: (tmp_path / "a.webp").exists() and not conv.busy(), 60)
    assert not (tmp_path / "a.jpg").exists() and (tmp_path / "done" / "a.jpg").exists()   # the original moved
    run = journal.list_runs()[0]
    engine.undo_run(run.run_id)
    assert (tmp_path / "a.jpg").exists() and not (tmp_path / "a.webp").exists()          # and came back

    assert conv.apply_preset("Web pictures")
    assert conv.image_target.currentData() == "webp" and conv.originals.currentData() == "move"
    conv._delete_preset("Web pictures")
    assert "Web pictures" not in convert_presets.load(conv.qs)
    conv.originals.setCurrentIndex(conv.originals.findData("keep"))
    conv.clear()


def test_icons_drawn_at_display_scale(win):
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QIcon

    from wick.gui import style

    ic = style.icon("home")
    for scale in (1.0, 1.25, 2.5):
        pm = ic.pixmap(QSize(20, 20), scale)
        assert pm.width() == round(20 * scale) and pm.devicePixelRatio() == scale
    assert not ic.pixmap(QSize(16, 16), 1.0, QIcon.Disabled).isNull()
    assert style.icon_pixmap("home", "#fff", 16, 1.75).width() == 28


def test_developer_settings_hidden_until_switched_on(win, monkeypatch):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from wick.gui import style
    from wick.gui.about_dialog import AboutDialog
    from wick.gui.devmode import CLICKS

    monkeypatch.delenv("WICK_DEV", raising=False)
    win.qsettings.remove("dev/enabled")
    page = win.app_settings
    page.update_dev_section()
    assert page.dev_section.isHidden()
    d = AboutDialog(win)
    for _ in range(CLICKS):
        ev = QMouseEvent(QEvent.MouseButtonRelease, QPointF(2, 2), QPointF(2, 2), Qt.LeftButton, Qt.NoButton,
                         Qt.NoModifier)
        QApplication.sendEvent(d._version, ev)
    assert not page.dev_section.isHidden()
    page.fluent_icons.setChecked(True)      # (no icon font here: the app's own icons are still drawn)
    assert not style.icon("home").pixmap(20, 20).isNull()
    page.fluent_icons.setChecked(False)
    d._toggle_dev()
    assert page.dev_section.isHidden()
    d.deleteLater()


def test_every_control_has_a_name_for_screen_readers(win):
    """Everything Tab can reach on every page (and in About) has a name Narrator reads out, a dropdown's list and a
    number field's text box included; symbol-only buttons ("…") say what they do, and dropdowns are named rather
    than read out as just their current value."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QAccessible, QFocusEvent
    from PySide6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QApplication, QComboBox, QScrollArea, QScrollBar,
                                   QWidget)

    from wick.gui.about_dialog import AboutDialog
    from wick.gui.main_window import LAZY_PAGES

    for name in LAZY_PAGES:
        getattr(win, name)
    assert win.cleanup.sections                 # (builds every Clean Up tool)
    about = AboutDialog(win)
    problems = []
    for root in (win, about):
        for w in root.findChildren(QWidget):
            w.ensurePolished()
        for w in root.findChildren(QWidget):          # (what Narrator sees when a list opens or a field is focused)
            if isinstance(w, QComboBox):
                QApplication.sendEvent(w.view(), QEvent(QEvent.Show))
            elif isinstance(w, QAbstractSpinBox) and w.lineEdit() is not None:
                QApplication.sendEvent(w.lineEdit(), QFocusEvent(QEvent.FocusIn))
        for w in root.findChildren(QWidget):
            if not (w.focusPolicy() & Qt.TabFocus) or isinstance(w, (QScrollBar, QScrollArea)):
                continue
            if w.objectName() == "updateLink":        # (named when it appears, with the version)
                continue
            iface = QAccessible.queryAccessibleInterface(w)
            label = (iface.text(QAccessible.Name) if iface else "") or ""
            what = f"{type(w).__name__} {w.objectName()!r} tip={w.toolTip()[:40]!r}"
            if not label.strip():
                problems.append("no name: " + what)
            elif isinstance(w, QComboBox) and not w.accessibleName():
                problems.append(f"named only by its value {label!r}: " + what)
            elif isinstance(w, QAbstractButton) and not any(ch.isalnum() for ch in label):
                problems.append(f"symbol only {label!r}: " + what)
    about.deleteLater()
    assert not problems, "\n".join(sorted(set(problems)))


def test_shift_click_ticks_a_range(win, monkeypatch):
    """Click one row, Shift-click another: both and every row between get the first row's state."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem

    from wick.gui.filelist import click_to_check

    tree = QTreeWidget()
    items = [QTreeWidgetItem(tree, [f"f{i}"]) for i in range(6)]
    for it in items:
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(0, Qt.Unchecked)
    click_to_check(tree)
    tree.show()
    c = tree._click_to_check
    c._tree_pressed(items[1], 0)
    c._tree_clicked(items[1], 0)                              # a plain click ticks row 1
    monkeypatch.setattr(QApplication, "keyboardModifiers", staticmethod(lambda: Qt.ShiftModifier))
    c._tree_clicked(items[4], 0)                              # Shift-click row 4: rows 1-4 ticked
    assert [it.checkState(0) == Qt.Checked for it in items] == [False, True, True, True, True, False]


def test_pausing_freezes_loading_bar_and_resuming_restarts_it(win):
    from PySide6.QtWidgets import QApplication

    from wick.gui.jobstatus import JobStatus

    status = JobStatus(parent=win)
    status.start(1, 100)
    QApplication.processEvents()
    assert status.bar._tick.isActive()
    status.set_paused("Paused by you")
    assert not status.bar._tick.isActive() and status.bar._paused_at is not None
    status.set_paused("")
    assert status.bar._tick.isActive() and status.bar._paused_at is None
    status._tick.stop()
    status.close()
    status.deleteLater()


def test_progress_keeps_stage_percentage_and_estimates(win, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from wick.gui import jobstatus

    clock = [100.0]
    monkeypatch.setattr(jobstatus.time, "monotonic", lambda: clock[0])
    status = jobstatus.JobStatus(parent=win)
    entry = SimpleNamespace(path=tmp_path / "clip.mp4", size=100)
    status.start(1, 100)
    status.file_started(entry)
    status.file_step(entry, "Encoding AV1")
    clock[0] = 108
    status.file_progress(entry, 0.5)
    assert "Encoding AV1" in status.detail.text() and "50%" in status.detail.text()
    assert "left in this stage" in status.detail.text()
    status.file_step(entry, "Checking the result")
    assert "50%" not in status.detail.text() and status.bar.maximum() == 0
    clock[0] = 125
    status._render()
    assert "no progress update" in status.detail.text()
    status._tick.stop()
    status.close()
    status.deleteLater()


@pytest.mark.parametrize("extension", ["gif", "mp4"])
@pytest.mark.parametrize("action", ["close", "reject"])
def test_close_busy_preview_never_waits_on_ui_thread(win, tmp_path, monkeypatch, extension, action):
    import threading

    from wick.formats import ImageInfo, VIDEO
    from wick.gui.preview import PreviewDialog, PreviewThread
    from wick.probe import VideoInfo
    from wick.scanner import FileEntry
    from wick.settings import Settings

    path = tmp_path / f"preview.{extension}"
    if extension == "gif":
        Image.new("RGB", (32, 24), "red").save(path)
    else:
        path.write_bytes(b"test video header")
    entry = FileEntry(path, path.stat().st_size, VIDEO if extension == "mp4" else "gif", 0,
                      image=ImageInfo(32, 24),
                      video=VideoInfo("h264", 32, 24, "yuv420p", 24, 2, 1000, 0), picked=True)
    release, finished = threading.Event(), threading.Event()
    monkeypatch.setattr(PreviewThread, "run", lambda self: release.wait(2))
    dialog = PreviewDialog(entry, Settings(video_encoder="cpu"), win)
    dialog.show()
    worker = dialog.worker
    worker.finished.connect(finished.set)
    try:
        assert _wait(worker.isRunning, 1)
        started = time.monotonic()
        getattr(dialog, action)()
        assert time.monotonic() - started < 0.3
        assert not dialog.isVisible() and worker.cancel.is_set()
    finally:
        release.set()
        assert _wait(finished.is_set, 3)
        dialog.deleteLater()
