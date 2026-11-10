"""Fast tests that need no external tools."""

import os
import struct
import zlib
from pathlib import Path

import pytest

from wick import trash_restore
from wick.fileops import final_path
from wick.formats import GIF, JPEG, PNG, VIDEO, ImageInfo, classify, read_png_info, video_output_ext
from wick.plan import plan_entry
from wick.probe import StreamInfo, VideoInfo
from wick.scanner import FileEntry, scan
from wick.settings import Settings


def _png(path: Path, depth: int = 8, animated: bool = False, w: int = 4, h: int = 4) -> Path:
    def chunk(t: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data))

    raw = b"".join(b"\x00" + b"\x00" * (w * 3 * depth // 8) for _ in range(h))
    body = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, 2, 0, 0, 0))
    if animated:
        body += chunk(b"acTL", struct.pack(">II", 1, 0))
    body += chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + body)
    return path


def test_classify():
    assert classify(Path("a.JPG")) == JPEG
    assert classify(Path("a.png")) == PNG
    assert classify(Path("a.MKV")) == VIDEO
    assert classify(Path("a.heic")) == "heic"
    assert classify(Path("a.GIF")) == GIF
    assert classify(Path("a.jxl")) is None
    assert classify(Path("a.txt")) is None


def test_video_output_ext():
    assert video_output_ext(".mov", "av1") == ".mp4"
    assert video_output_ext(".mp4", "hevc") == ".mp4"
    assert video_output_ext(".webm", "av1") == ".webm"
    assert video_output_ext(".webm", "hevc") == ".mkv"
    assert video_output_ext(".avi", "av1") == ".mkv"


def test_png_info(tmp_path):
    info = read_png_info(_png(tmp_path / "a.png", depth=16, w=7, h=5))
    assert (info.width, info.height, info.bit_depth, info.animated) == (7, 5, 16, False)
    assert read_png_info(_png(tmp_path / "b.png", animated=True)).animated
    (tmp_path / "c.png").write_bytes(b"not a png")
    assert read_png_info(tmp_path / "c.png") is None


def test_settings_from_dict_is_forgiving():
    s = Settings.from_dict({"image_format": "bogus", "image_quality": "500", "skip_program_folders": "false",
                            "include_hidden": "true", "unknown": 1})
    assert s.image_format == "jxl"
    assert s.image_quality == 100
    assert s.skip_program_folders is False
    assert s.include_hidden is True


def _entry(tmp_path, name, size, kind, **kw):
    return FileEntry(tmp_path / name, size, kind, 0, **kw)


def _img(w=1000, h=800, frames=1, depth=8, lossless=True):
    return ImageInfo(w, h, frames, depth, 1, lossless)


def test_plan_images(tmp_path):
    s = Settings()
    big = 100_000
    jpg = lambda: _entry(tmp_path, "a.jpg", big, JPEG, image=_img(lossless=False))  # noqa: E731
    p = plan_entry(jpg(), s)
    assert p.convert and p.reversible and p.target_ext == ".jxl" and p.route == "jxl_transcode"
    assert not plan_entry(_entry(tmp_path, "a.jpg", 100, JPEG, image=_img()), s).convert  # too small

    webp = Settings(image_format="webp")
    assert not plan_entry(jpg(), webp).convert  # no lossless JPEG->WebP
    deep = _entry(tmp_path, "d.png", big, PNG, image=_img(depth=16))
    assert not plan_entry(deep, webp).convert
    assert plan_entry(deep, s).convert  # JXL keeps 16-bit
    anim = _entry(tmp_path, "e.gif", big, GIF, image=_img(frames=10))
    assert plan_entry(anim, s).route == "jxl"
    assert plan_entry(anim, webp).route == "webp"
    assert not plan_entry(jpg(), Settings(image_format="none")).convert

    keep = Settings(image_format="keep")
    assert plan_entry(jpg(), keep).route == "jpeg_opt"
    assert plan_entry(jpg(), Settings(image_format="keep", jpeg_mode="lossy")).route == "jpeg"
    bmp = plan_entry(_entry(tmp_path, "b.bmp", big, "bmp", image=_img()), keep)
    assert bmp.route == "png_opt" and bmp.target_ext == ".png"

    r = plan_entry(jpg(), Settings(resize_mode="long_edge", resize_long_edge=500))
    assert r.resize == (500, 400) and r.route == "jxl"  # resizing can't be reversible
    assert plan_entry(jpg(), Settings(metadata="strip_gps")).reversible is False
    assert not plan_entry(jpg(), Settings(output_mode="folder")).convert  # needs a folder


def test_resize_target():
    s = Settings(resize_mode="fit", resize_width=1920, resize_height=1080)
    assert s.resize_target(3840, 2160) == (1920, 1080)
    assert s.resize_target(1000, 3000) == (360, 1080)
    assert s.resize_target(800, 600) is None  # never upscale
    assert Settings(resize_mode="percent", resize_percent=50).resize_target(101, 51) == (50, 26)


def test_output_paths(tmp_path):
    from wick.engine import output_path
    from wick.plan import Plan

    root = tmp_path / "Photos"
    (root / "2024").mkdir(parents=True)
    e = FileEntry(root / "2024" / "a.png", 1, PNG, 0, root)
    plan = Plan(True, target_ext=".jxl")
    assert output_path(e, plan, Settings()) == root / "2024" / "a.jxl"
    assert output_path(e, plan, Settings(output_mode="suffix")) == root / "2024" / "a_compressed.jxl"
    out = tmp_path / "out"
    assert output_path(e, plan, Settings(output_mode="folder", output_dir=str(out))) == out / "Photos" / "2024" / "a.jxl"
    assert output_path(e, plan, Settings(output_mode="folder", output_dir=str(out), keep_structure=False)) == out / "a.jxl"


def _video(codec="h264"):
    return VideoInfo(codec, 1920, 1080, "yuv420p", 30.0, 60.0, 8_000_000, 0,
                     [StreamInfo(0, "video", codec), StreamInfo(1, "audio", "aac")])


def test_plan_video(tmp_path):
    s = Settings()
    size = 60 * 1024 * 1024
    p = plan_entry(_entry(tmp_path, "a.mov", size, VIDEO, video=_video()), s)
    assert p.convert and p.target_ext == ".mp4" and 0 < p.est_size < size
    assert not plan_entry(_entry(tmp_path, "a.mp4", size, VIDEO, video=_video("hevc")), s).convert
    assert not plan_entry(_entry(tmp_path, "a.mp4", size, VIDEO, video=None), s).convert
    assert not plan_entry(_entry(tmp_path, "a.mp4", 1000, VIDEO, video=_video()), s).convert


def test_final_path(tmp_path):
    src = tmp_path / "a.jpg"
    assert final_path(src, ".jxl") == tmp_path / "a.jxl"
    (tmp_path / "a.jxl").write_bytes(b"x")
    assert final_path(src, ".jxl") == tmp_path / "a (1).jxl"
    assert final_path(tmp_path / "v.mp4", ".mp4") == tmp_path / "v.mp4"


def test_scan_skips_program_and_system_folders(tmp_path):
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "a.jpg").write_bytes(b"x" * 10)
    (tmp_path / "game").mkdir()
    (tmp_path / "game" / "engine.dll").write_bytes(b"")
    (tmp_path / "game" / "tex.jpg").write_bytes(b"x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "b.png").write_bytes(b"x")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "c.jpg").write_bytes(b"x")
    (tmp_path / "photos" / ".cmp-abc.jxl").write_bytes(b"x")
    r = scan(tmp_path, Settings(), probe=False)
    assert [e.path.name for e in r.entries] == ["a.jpg"]
    assert r.skipped_program_dirs == [tmp_path / "game"]
    r = scan(tmp_path, Settings(skip_program_folders=False, include_hidden=True), probe=False)
    assert sorted(e.path.name for e in r.entries) == ["a.jpg", "c.jpg", "tex.jpg"]
    # individual files are always included
    r = scan([tmp_path / "game" / "tex.jpg"], Settings(), probe=False)
    assert [e.path.name for e in r.entries] == ["tex.jpg"] and r.entries[0].root is None


def test_parse_windows_recycle_info():
    path = "C:\\Users\\Åsa\\Bilder\\foto.jpg"
    ft = int((1_700_000_000 * 1e7) + 116444736000000000)
    v2 = struct.pack("<qqqI", 2, 1234, ft, len(path) + 1) + (path + "\x00").encode("utf-16-le")
    assert trash_restore.parse_windows_info(v2) == (path, 1_700_000_000.0)
    v1 = struct.pack("<qqq", 1, 1234, ft) + (path + "\x00").encode("utf-16-le").ljust(520, b"\x00")
    assert trash_restore.parse_windows_info(v1) == (path, 1_700_000_000.0)
    assert trash_restore.parse_windows_info(b"junk") is None


@pytest.mark.skipif(os.name == "nt", reason="freedesktop Trash layout is Linux-only")
def test_freedesktop_restore(tmp_path):
    trash = tmp_path / "Trash"
    (trash / "files").mkdir(parents=True)
    (trash / "info").mkdir()
    original = tmp_path / "home" / "my photo.jpg"
    original.parent.mkdir()
    (trash / "files" / "my photo.jpg").write_bytes(b"data")
    (trash / "info" / "my photo.jpg.trashinfo").write_text(
        f"[Trash Info]\nPath={str(original).replace(' ', '%20')}\nDeletionDate=2030-01-01T10:00:00\n")
    item = trash_restore.find_trashed(original, 0, bins=[(trash, None)])
    trash_restore.restore(item)
    assert original.read_bytes() == b"data"
    assert not (trash / "info" / "my photo.jpg.trashinfo").exists()


def test_jpeg_metadata_rewrite_is_lossless():
    from io import BytesIO

    from PIL import Image

    from wick import metadata as md

    ex = Image.Exif()
    ex[0x010F] = "Cam"
    ex[0x0112] = 6
    ex.get_ifd(0x8825)[1] = "N"
    buf = BytesIO()
    Image.new("RGB", (64, 48), "red").save(buf, "JPEG", exif=ex.tobytes())
    data = buf.getvalue()
    sos = data.index(b"\xff\xda")
    for policy, make, gps in (("strip_gps", "Cam", False), ("strip_all", None, False)):
        meta = md.filter_meta(md.jpeg_read_meta(data), policy)
        new = md.jpeg_write_meta(data, meta, policy == "strip_all")
        assert new[new.index(b"\xff\xda"):] == data[sos:]  # image data untouched
        got = Image.open(BytesIO(new)).getexif()
        assert got.get(0x010F) == make and bool(got.get_ifd(0x8825)) == gps
        assert got.get(0x0112) == 6  # orientation always kept


def test_jxl_meta_boxes():
    from wick import metadata as md

    bare = b"\xff\x0a" + b"codestream"
    out = md.jxl_write_meta(bare, md.Meta(exif=b"II*\x00tiff", xmp=b"<x/>"))
    assert out.startswith(b"\x00\x00\x00\x0cJXL ") and b"Exif" in out and b"xml " in out
    assert out.endswith(b"jxlc" + bare)
    again = md.jxl_write_meta(out, md.Meta())
    assert b"Exif" not in again and again.endswith(bare)


def test_watcher_waits_for_stable_new_files(tmp_path):
    from wick.gui import watcher as w

    (tmp_path / "old.jpg").write_bytes(b"x" * 10)
    fw = w.FolderWatcher.__new__(w.FolderWatcher)  # no Qt needed for the polling logic
    import threading

    fw.folders, fw.recursive = [tmp_path], True
    fw._pending, fw._ignored, fw._lock = {}, {}, threading.Lock()
    fw._seen = fw._snapshot()
    assert fw.poll_once() == []
    new = tmp_path / "new.png"
    new.write_bytes(b"y" * 10)
    assert fw.poll_once() == []          # first sighting
    new.write_bytes(b"y" * 20)           # still being written
    assert fw.poll_once() == []
    assert fw.poll_once() == []          # stable once
    assert fw.poll_once() == [str(new)]  # stable twice -> ready
    assert fw.poll_once() == []          # not reported again
    (tmp_path / "notes.txt").write_text("ignored")
    out = tmp_path / "new.jxl"
    out.write_bytes(b"z")
    fw._ignored[__import__("os").path.normcase(str(out))] = (out.stat().st_size, out.stat().st_mtime_ns)
    for _ in range(4):
        assert fw.poll_once() == []      # our own output is ignored


@pytest.mark.skipif(os.name == "nt", reason="Linux desktop-entry variant")
def test_desktop_integration_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from wick.gui import integration as it

    assert not it.context_menu_installed()
    it.install_context_menu()
    assert it.context_menu_installed()
    text = (tmp_path / "data" / "applications" / "wick.desktop").read_text()
    assert "Exec=" in text and "%F" in text and "image/heic" in text
    it.uninstall_context_menu()
    assert not it.context_menu_installed()
    it.set_autostart(True)
    assert it.autostart_enabled() and "--background" in (tmp_path / "config" / "autostart" / "wick.desktop").read_text()
    it.set_autostart(False)
    assert not it.autostart_enabled()


@pytest.mark.skipif(os.name != "nt", reason="Explorer's menus are in the Windows registry")
def test_explorer_menus_compress_and_convert():
    import winreg

    from wick.gui import integration as it

    had = it.context_menu_installed()
    try:
        it.install_context_menu()
        key = it._CONVERT_KEY.format(kind="video") + r"\shell\00\command"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            command = winreg.QueryValueEx(k, None)[0]
        assert "--convert" in command and "mp4_h264" in command and command.endswith('"%1"')
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, it._CONVERT_KEY.format(kind="image")) as k:
            assert winreg.QueryValueEx(k, "MUIVerb")[0] == "Convert with Wick"
        it.uninstall_context_menu()
        for kind in it.CONVERT_MENUS:
            with pytest.raises(OSError):
                winreg.OpenKey(winreg.HKEY_CURRENT_USER, it._CONVERT_KEY.format(kind=kind))
    finally:
        (it.install_context_menu if had else it.uninstall_context_menu)()


def _files(folder, n):
    folder.mkdir()
    paths = []
    for i in range(n):
        p = folder / f"f{i}.bin"
        p.write_bytes(b"x" * 1000)
        paths.append(p)
    return paths


def test_recycle_bin_speed_one_by_one(tmp_path):
    """Timing reference (see CI's --durations list): 40 files sent to the Recycle Bin/Trash one at a time."""
    from wick.fileops import move_to_trash

    for p in _files(tmp_path / "one", 40):
        move_to_trash(p)
    assert not any((tmp_path / "one").iterdir())


def test_recycle_bin_speed_batched(tmp_path):
    """Timing reference: the same 40 files in a single call."""
    from send2trash import send2trash

    send2trash([str(p) for p in _files(tmp_path / "batch", 40)])
    assert not any((tmp_path / "batch").iterdir())


def test_installer_choices_are_applied_once(tmp_path, monkeypatch):
    """The installer passes what was ticked: sign-in is added, and the update check is only set on a first
    install (a later choice in Settings is kept on upgrades)."""
    from PySide6.QtCore import QSettings

    from wick import cli
    from wick.gui import integration

    monkeypatch.setenv("WICK_SETTINGS_FILE", str(tmp_path / "s.ini"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if integration.IS_WINDOWS:
        pytest.skip("touches the real registry")
    from wick import updates

    assert cli.main(["--installer-setup", "context=0", "autostart=1", "updates=monthly", "upgrade=0"]) == 0
    assert integration.autostart_enabled()
    qs = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    assert updates.frequency(qs) == "monthly"
    qs.setValue("updates/frequency", "launch")         # changed in Settings later
    qs.sync()
    cli.main(["--installer-setup", "updates=never", "upgrade=1"])
    assert updates.frequency(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)) == "launch"
    cli.main(["--installer-setup", "updates=never", "upgrade=0"])     # (a reinstall over kept settings, too)
    assert updates.frequency(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)) == "launch"


def test_update_check_frequency(tmp_path):
    """Settings from before the frequency choice (an on/off switch) still mean the same; each choice is due when
    it should be."""
    from PySide6.QtCore import QSettings

    from wick import updates

    qs = QSettings(str(tmp_path / "u.ini"), QSettings.IniFormat)
    assert updates.frequency(qs) == "weekly"                         # nothing chosen yet: the default
    qs.setValue("updates/auto", False)
    assert updates.frequency(qs) == "never"                          # switched off in an older version
    qs.setValue("updates/auto", True)
    assert updates.frequency(qs) == "weekly"
    qs.setValue("updates/frequency", "monthly")
    assert updates.frequency(qs) == "monthly"                        # the new choice wins
    qs.setValue("updates/frequency", "nonsense")
    assert updates.frequency(qs) == "weekly"
    day, now = 86400, 1_000_000_000.0
    assert updates.due("launch", now - 60, now)
    assert updates.due("weekly", now - 8 * day, now) and not updates.due("weekly", now - 6 * day, now)
    assert updates.due("monthly", now - 31 * day, now) and not updates.due("monthly", now - 8 * day, now)
    assert not updates.due("never", 0, now)
    assert updates.due("weekly", 0, now)                             # never checked yet


def test_stored_data_survives_damage(tmp_path, monkeypatch):
    """Presets and learned estimates are replaced in one step (never half-written); a damaged presets file is
    kept aside rather than written over; a damaged index rebuilds itself."""
    from wick import index, presets
    from wick.fileops import atomic_write_text
    from wick.settings import Settings

    monkeypatch.setenv("WICK_DATA_DIR", str(tmp_path))
    f = tmp_path / "x.json"
    atomic_write_text(f, '{"a": 1}')
    assert f.read_text("utf-8") == '{"a": 1}' and [p.name for p in tmp_path.iterdir()] == ["x.json"]

    presets.save("Mine", Settings())
    (tmp_path / "presets.json").write_text('{"Mine": {"image_qual', "utf-8")     # cut off mid-write
    assert presets.user_presets() == {}
    kept = [p.name for p in tmp_path.iterdir() if p.name.startswith("presets.json.damaged-")]
    assert kept, "the damaged file should be kept"
    presets.save("Again", Settings())
    assert "Again" in presets.user_presets()

    index._rebuild()
    monkeypatch.setattr(index, "path", lambda: tmp_path / "index.sqlite")
    (tmp_path / "index.sqlite").write_bytes(b"this is not a database" * 100)
    index.store([(tmp_path / "a.jpg", 1, 1.0, "jpeg", None)])                 # rebuilt, then stored
    assert index._db().execute("SELECT count(*) FROM files").fetchone()[0] in (0, 1)
    index._rebuild()


def test_output_names_from_a_pattern(tmp_path):
    import time

    from wick.engine import output_name

    src = tmp_path / "Holiday.jpg"
    src.write_bytes(b"x")
    t = time.mktime((2026, 11, 6, 14, 5, 9, 0, 0, -1))
    assert output_name(src, "_compressed") == "Holiday_compressed"            # plain text: a suffix, as before
    assert output_name(src, "%name% %date%", t) == "Holiday 2026-11-06"
    assert output_name(src, "%name%-%time%.%ext%", t) == "Holiday-14-05-09.jpg"
    assert output_name(src, "%name%: copy?", t) == "Holiday_ copy_"           # never characters a name can't hold
    assert output_name(src, "%unknown%", t) == "%unknown%"


def test_jump_list_tasks():
    """The taskbar / Start / Search tasks: each opens a page the app knows; on Windows the list is really built."""
    import sys

    from wick.gui import jumplist
    from wick.gui.main_window import MainWindow

    for title, target, description in jumplist.TASKS:
        page = target.removesuffix("-files")
        assert title and description and page in MainWindow.OPEN_PAGES, target
    if not jumplist.IS_WINDOWS:
        assert jumplist.update(exe="unused") is False
        return
    jumplist.set_process_app_id()
    assert jumplist.update(exe=sys.executable, args_prefix="-c pass ")     # COM calls all succeeded


def test_support_prompt_rules(tmp_path):
    """Asked once: only after a day and 10 GB saved; the most ever saved counts (History can be pruned later)."""
    from PySide6.QtCore import QSettings

    from wick.gui import support

    qs = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    day, gb = 86400, 1024 ** 3
    support.note_start(qs, now=1_000_000.0)
    support.note_start(qs, now=9_000_000.0)                  # only the first start counts
    assert support.note_saved(qs, 12 * gb) == 12 * gb
    assert not support.due(qs, now=1_000_000.0 + day - 60)   # not a day yet
    assert support.due(qs, now=1_000_000.0 + day + 60)
    assert support.note_saved(qs, 2 * gb) == 12 * gb         # pruned or undone later: still counts
    qs.setValue(support.ASKED, True)
    assert not support.due(qs, now=1_000_000.0 + 30 * day)   # never twice

    fresh = QSettings(str(tmp_path / "t.ini"), QSettings.IniFormat)
    support.note_start(fresh, now=0.0)
    support.note_saved(fresh, 9 * gb)
    assert not support.due(fresh, now=10 * day)              # under 10 GB


def test_downloaded_mark_is_kept(tmp_path):
    """Windows: a converted download keeps the "from the internet" mark (Zone.Identifier) of its original."""
    from wick import fileops

    a, b = tmp_path / "a.png", tmp_path / "b.webp"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    if os.name != "nt":
        assert fileops.read_zone_mark(a) is None
        fileops.write_zone_mark(b, b"[ZoneTransfer]\r\nZoneId=3\r\n")       # does nothing, raises nothing
        return
    assert fileops.read_zone_mark(a) is None
    (tmp_path / "a.png:Zone.Identifier").write_bytes(b"[ZoneTransfer]\r\nZoneId=3\r\n")
    mark = fileops.read_zone_mark(a)
    assert mark and b"ZoneId=3" in mark
    fileops.write_zone_mark(b, mark)
    assert fileops.read_zone_mark(b) == mark
    fileops.notify_shell(created=[b], updated=[a])                          # tells Explorer; never raises


def test_encoders_run_in_a_job_that_closes_with_the_app():
    """Windows: every encoder joins a kill-on-close job object (so a crash never leaves ffmpeg running), and
    tools.run behaves as before (output, exit code, ToolError, timeout)."""
    import subprocess
    import sys

    from wick import tools

    out = tools.run([sys.executable, "-c", "print('hi')"])
    assert out.returncode == 0 and out.stdout.strip() == "hi"
    with pytest.raises(tools.ToolError):
        tools.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    with pytest.raises(subprocess.TimeoutExpired):
        tools.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        tools._considerate(p)
        inside = wintypes.BOOL()
        ctypes.windll.kernel32.IsProcessInJob(wintypes.HANDLE(int(p._handle)), wintypes.HANDLE(tools._windows_job()),
                                              ctypes.byref(inside))
        assert inside.value
    finally:
        p.kill()
        p.wait()


def test_quiet_when_full_screen_and_restart_after_updates():
    """Off Windows both are harmless no-ops; on Windows they answer without raising."""
    from wick import power

    assert power.user_busy() in (True, False)
    assert power.set_restart_after_update(True) is (os.name == "nt")
    assert power.set_restart_after_update(False) is (os.name == "nt")


def test_windows_notifications():
    """Toast XML is escaped and carries the buttons; their wick: links reach the right page; on Windows the
    app registers its name and icon for notifications (and the uninstaller's clean-up removes it)."""
    import xml.dom.minidom

    from wick.gui import toasts
    from wick.gui.app import open_target, parse_args

    x = toasts.toast_xml("Wick", "Saved 3 GB <& more>", [("Show in History", "history")], launch="compress")
    doc = xml.dom.minidom.parseString(x)                      # well-formed
    texts = [t.firstChild.data for t in doc.getElementsByTagName("text")]
    assert texts == ["Wick", "Saved 3 GB <& more>"]
    action = doc.getElementsByTagName("action")[0]
    assert action.getAttribute("arguments") == "wick:open/history"
    assert doc.documentElement.getAttribute("launch") == "wick:open/compress"
    assert toasts.target_from_link('"wick:open/history/"') == "history" and toasts.target_from_link("http://x") == ""
    assert open_target(["--url", "wick:open/cleanup"]) == "cleanup"
    assert parse_args(["--url", "wick:open/cleanup"])[3] == []     # not taken for a file
    if os.name != "nt":
        assert toasts.show("t", "m") is False
        return
    import winreg

    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])               # (the icon file is drawn with Qt)
    assert toasts.register()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\AppUserModelId\Lanntern.Wick") as key:
        assert winreg.QueryValueEx(key, "DisplayName")[0] == "Wick"
    toasts.unregister()
    with pytest.raises(OSError):
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\AppUserModelId\Lanntern.Wick")
    toasts._registered = False


def test_windows11_menu_package_matches_the_dll():
    """The sparse package's manifest is valid XML, names the DLL's two command IDs and the package name the app
    registers, and has an unsigned-package publisher. (CI builds and packs both on Windows.)"""
    import re
    import sys
    import xml.dom.minidom

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "packaging"))
    import make_shell_package as pkg

    from wick.gui import integration

    doc = xml.dom.minidom.parseString(pkg.manifest("1.2.3"))
    identity = doc.getElementsByTagName("Identity")[0]
    assert identity.getAttribute("Name") == integration.MODERN_PACKAGE
    assert identity.getAttribute("Version") == "1.2.3.0"
    assert "OID.2.25.311729368913984317654407730594956997722=1" in identity.getAttribute("Publisher")
    cpp = (root / "packaging" / "shell" / "WickShell.cpp").read_text()
    ids = {m.lower() for m in re.findall(r"// \{([0-9a-f-]{36})\}", cpp)}
    assert ids == {pkg.CLSID_COMPRESS, pkg.CLSID_CONVERT}
    classes = {c.getAttribute("Id") for c in doc.getElementsByTagName("com:Class")}
    assert classes == ids
    assert integration._modern_menu_files() is None or os.name == "nt"     # (only a Windows build has it)


def test_default_apps_and_open_with():
    """Windows: Wick is listed in Default apps and Open with for its file types (never made the default),
    and unregistering removes it. Skipped where a real Wick is registered, so a real install isn't touched."""
    from wick.gui import integration

    if os.name != "nt":
        integration.register_file_types()                 # no-op elsewhere
        assert not integration.file_types_registered()
        return
    if integration.file_types_registered():
        pytest.skip("a real Wick is registered on this computer")
    import winreg

    exe = r"C:\WickTest\WickTest.exe"
    integration.register_file_types(exe)
    try:
        assert integration.file_types_registered()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\.mp4\OpenWithProgids") as k:
            winreg.QueryValueEx(k, "Wick.Video")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\Wick.Picture\shell\open\command") as k:
            assert winreg.QueryValueEx(k, "")[0] == f'"{exe}" "%1"'
    finally:
        integration.unregister_file_types("WickTest.exe")
    assert not integration.file_types_registered()


def test_a_link_cannot_smuggle_in_files_or_options():
    """wick: links come from notifications but could come from any web page: they open a page, nothing more."""
    from wick.gui.app import open_target, parse_args

    crafted = ["--url", 'wick:open/history" "C:\\Users\\me\\Pictures', "--preset", "Maximum savings",
               "C:\\Users\\me\\Videos"]
    assert parse_args(crafted) == (False, "", "", [])
    assert open_target(crafted) == ""                     # not a well-formed page name: ignored
    assert open_target(["--url", "wick:open/compress-files"]) == "compress-files"


def test_gpu_encoders_nvidia_intel_amd(monkeypatch):
    """Auto tries NVIDIA, then Intel, then AMD (whichever passes its test encode), else the CPU; each gets its
    own quality settings, and AMD's AV1 scale (0-255) is four times the others'."""
    from wick import video
    from wick.settings import Settings

    for works, expect in (({"hevc_nvenc"}, "hevc_nvenc"), ({"hevc_qsv", "hevc_amf"}, "hevc_qsv"),
                          ({"hevc_amf"}, "hevc_amf"), (set(), "libx265")):
        monkeypatch.setattr(video, "encoder_works", lambda e, w=works: e in w)
        assert video.choose_encoder("hevc", "auto") == expect
    monkeypatch.setattr(video, "encoder_works", lambda e: False)
    with pytest.raises(RuntimeError, match="NVIDIA.*Intel.*AMD"):
        video.choose_encoder("av1", "gpu")
    assert "-global_quality" in video._gpu_quality_args("hevc_qsv", 26)
    assert video._gpu_quality_args("av1_amf", 124)[-4:] == ["-qp_i", "124", "-qp_p", "124"]
    s = Settings(video_quality="custom", video_crf=30)
    assert video.encoder_quality(s, "av1_amf") == 120 and video.encoder_quality(s, "hevc_amf") == 30
    assert [video.quality_label(e) for e in ("hevc_nvenc", "av1_qsv", "av1_amf", "libx265")] == ["CQ", "ICQ", "QP", "CRF"]
    for encoder in (*video.GPU_ENCODERS["av1"], *video.GPU_ENCODERS["hevc"]):
        assert encoder in video._QUALITY and encoder in video.SMART_STEPS and encoder in video.CRF_MAX


def test_hardware_decoding_is_asked_for_before_the_input():
    """Encodes ask ffmpeg to decode on the GPU (before -i, so it applies to the source); samples and the CPU retry
    don't. (The real round trips in test_engine run with it, which on a computer without a GPU decodes normally.)"""
    from wick import video
    from wick.probe import StreamInfo, VideoInfo
    from wick.settings import Settings

    info = VideoInfo("h264", 1920, 1080, "yuv420p", 30, 60, 6_000_000, 0, [StreamInfo(0, "video", "h264")])
    args = video.build_args(Path("a.mp4"), Path("b.mp4"), info, Settings(), "libx265", 24, hw_decode=True)
    assert args.index("-hwaccel") < args.index("-i")
    assert "-hwaccel" not in video.build_args(Path("a.mp4"), Path("b.mp4"), info, Settings(), "libx265", 24)


def test_thumbnails_from_windows(tmp_path):
    """Windows: its own thumbnail (made on request here, since the test file is new) comes back as a picture of the
    right size; elsewhere there's none and our own decoders are used."""
    from PIL import Image
    from PySide6.QtWidgets import QApplication

    from wick.formats import PNG
    from wick.gui import thumbs, winthumbs

    QApplication.instance() or QApplication([])
    src = tmp_path / "photo.png"
    Image.new("RGB", (800, 400), (200, 30, 30)).save(src)
    if os.name != "nt":
        assert winthumbs.thumbnail(src, 160) is None
    else:
        img = winthumbs.thumbnail(src, 160, cache_only=False)
        assert img is not None and 0 < max(img.width(), img.height()) <= 160
        assert img.pixelColor(img.width() // 2, img.height() // 2).red() > 150      # the red picture, not black
    data = thumbs.make_thumbnail(src, PNG, 160)
    assert data and data[:8] == b"\x89PNG\r\n\x1a\n"
    from PySide6.QtGui import QImage

    pic = QImage(20, 10, QImage.Format_RGB32)
    pic.fill(0xFF2030)
    assert thumbs._png(pic)[:8] == b"\x89PNG\r\n\x1a\n"           # (how Windows' thumbnails are passed on)


def test_limits_against_bombs(tmp_path, monkeypatch):
    """A comic book that would unpack to more than the limit is refused before anything is read; encoders get a
    per-process memory limit of 3/4 of the computer's memory (at least 2 GB), and on Windows the job carries it."""
    import zipfile

    from wick import convert, tools

    cbz = tmp_path / "bomb.cbz"
    with zipfile.ZipFile(cbz, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("001.png", b"\0" * (3 * 1024 * 1024))          # 3 MB of zeros: a few KB compressed
    monkeypatch.setattr(convert, "MAX_COMIC_BYTES", 1024 * 1024)
    with pytest.raises(ValueError, match="would unpack to"):
        convert.convert_comic(cbz, convert.ConvertOptions())
    monkeypatch.setattr(tools, "total_memory_gb", lambda: 16.0)
    assert tools.encoder_memory_limit() == 12 * 1024 ** 3
    monkeypatch.setattr(tools, "total_memory_gb", lambda: 1.0)
    assert tools.encoder_memory_limit() == 2 * 1024 ** 3


def test_winrt_notifications_and_accent():
    """Windows: a notification is built through WinRT itself (no PowerShell), and the accent colour comes from
    UISettings; elsewhere neither exists."""
    import re

    from wick.gui import toasts, winrt

    if os.name != "nt":
        assert winrt.accent_color() is None and toasts.show("t", "m") is False
        return
    assert re.fullmatch(r"#[0-9A-F]{6}", winrt.accent_color() or "")
    notifier, toast = toasts._build(toasts.toast_xml("Done", "Saved 1 GB", [("Show in History", "history")]))
    assert notifier.ptr and toast.ptr
    toast.release()
    notifier.release()
    try:
        toasts._build("<toast><not-closed>")
    except OSError as e:
        assert "HRESULT" in str(e)                    # bad XML is refused cleanly, not a crash
    else:
        raise AssertionError("malformed toast XML was accepted")


def test_installer_script_has_no_line_inno_would_read_as_a_section():
    """Inno Setup reads any line starting with "[" as a new section, even inside [Code] (an array literal on its
    own line broke the installer build). The installer is only compiled on main, so this is checked here."""
    lines = (Path(__file__).parent.parent / "packaging" / "installer.iss").read_text("utf-8-sig").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "[Code]")
    bad = [f"{start + n + 2}: {line}" for n, line in enumerate(lines[start + 1:])
           if line.lstrip().startswith("[") and not line.startswith("[")]
    assert not bad, "\n".join(bad)


def test_renamed_from_compressor_carries_history_and_settings_over(tmp_path, monkeypatch):
    """Renamed from Compressor: its data folder (History, log, presets) moves to Wick's once, and its settings are
    copied once; nothing that already exists under the new name is ever overwritten."""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from wick import journal
    from wick.gui import main_window

    QApplication.instance() or QApplication([])
    old, new = tmp_path / "Compressor", tmp_path / "Wick"
    (old / "runs").mkdir(parents=True)
    (old / "runs" / "r1.jsonl").write_text("{}", "utf-8")
    journal._carry_over(new, [old])
    assert (new / "runs" / "r1.jsonl").exists() and not old.exists()

    monkeypatch.delenv("WICK_SETTINGS_FILE", raising=False)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path / "settings"))   # (not the real ones)
    legacy = QSettings(QSettings.IniFormat, QSettings.UserScope, "Compressor", "Compressor")
    legacy.setValue("theme", "dark")
    legacy.setValue("updates/frequency", "monthly")
    legacy.sync()
    monkeypatch.setattr(main_window, "QSettings", lambda *a, **k: QSettings(QSettings.IniFormat,
                                                                          QSettings.UserScope, *a))
    assert main_window.carry_over_settings() == 2
    wick_settings = QSettings(QSettings.IniFormat, QSettings.UserScope, "Wick", "Wick")
    assert wick_settings.value("theme") == "dark" and wick_settings.value("updates/frequency") == "monthly"
    legacy.setValue("theme", "light")
    legacy.sync()
    assert main_window.carry_over_settings() == 0                # once only: Wick's own settings stay
    assert QSettings(QSettings.IniFormat, QSettings.UserScope, "Wick", "Wick").value("theme") == "dark"
