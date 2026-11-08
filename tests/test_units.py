"""Fast tests that need no external tools."""

import os
import struct
import zlib
from pathlib import Path

import pytest

from compressor import trash_restore
from compressor.fileops import final_path
from compressor.formats import GIF, JPEG, PNG, VIDEO, ImageInfo, classify, read_png_info, video_output_ext
from compressor.plan import plan_entry
from compressor.probe import StreamInfo, VideoInfo
from compressor.scanner import FileEntry, scan
from compressor.settings import Settings


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
    from compressor.engine import output_path
    from compressor.plan import Plan

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

    from compressor import metadata as md

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
    from compressor import metadata as md

    bare = b"\xff\x0a" + b"codestream"
    out = md.jxl_write_meta(bare, md.Meta(exif=b"II*\x00tiff", xmp=b"<x/>"))
    assert out.startswith(b"\x00\x00\x00\x0cJXL ") and b"Exif" in out and b"xml " in out
    assert out.endswith(b"jxlc" + bare)
    again = md.jxl_write_meta(out, md.Meta())
    assert b"Exif" not in again and again.endswith(bare)


def test_watcher_waits_for_stable_new_files(tmp_path):
    from compressor.gui import watcher as w

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
    from compressor.gui import integration as it

    assert not it.context_menu_installed()
    it.install_context_menu()
    assert it.context_menu_installed()
    text = (tmp_path / "data" / "applications" / "compressor.desktop").read_text()
    assert "Exec=" in text and "%F" in text and "image/heic" in text
    it.uninstall_context_menu()
    assert not it.context_menu_installed()
    it.set_autostart(True)
    assert it.autostart_enabled() and "--background" in (tmp_path / "config" / "autostart" / "compressor.desktop").read_text()
    it.set_autostart(False)
    assert not it.autostart_enabled()


@pytest.mark.skipif(os.name != "nt", reason="Explorer's menus are in the Windows registry")
def test_explorer_menus_compress_and_convert():
    import winreg

    from compressor.gui import integration as it

    had = it.context_menu_installed()
    try:
        it.install_context_menu()
        key = it._CONVERT_KEY.format(kind="video") + r"\shell\00\command"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            command = winreg.QueryValueEx(k, None)[0]
        assert "--convert" in command and "mp4_h264" in command and command.endswith('"%1"')
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, it._CONVERT_KEY.format(kind="image")) as k:
            assert winreg.QueryValueEx(k, "MUIVerb")[0] == "Convert with Compressor"
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
    from compressor.fileops import move_to_trash

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

    from compressor import cli
    from compressor.gui import integration

    monkeypatch.setenv("COMPRESSOR_SETTINGS_FILE", str(tmp_path / "s.ini"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if integration.IS_WINDOWS:
        pytest.skip("touches the real registry")
    from compressor import updates

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

    from compressor import updates

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
    from compressor import index, presets
    from compressor.fileops import atomic_write_text
    from compressor.settings import Settings

    monkeypatch.setenv("COMPRESSOR_DATA_DIR", str(tmp_path))
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

    from compressor.engine import output_name

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

    from compressor.gui import jumplist
    from compressor.gui.main_window import MainWindow

    for title, target, description in jumplist.TASKS:
        page = target.removesuffix("-files")
        assert title and description and page in MainWindow.OPEN_PAGES, target
    if not jumplist.IS_WINDOWS:
        assert jumplist.update(exe="unused") is False
        return
    jumplist.set_process_app_id()
    assert jumplist.update(exe=sys.executable, args_prefix="-c pass ")     # COM calls all succeeded
