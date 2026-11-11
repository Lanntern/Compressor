"""File index, presets, history retention, logging, video options and previews, Smart quality."""

import os
import time
from pathlib import Path

import pytest
from PIL import Image

from wick import index, journal, presets
from wick.settings import Settings

from .conftest import ffmpeg, needs_ffmpeg


def test_index_round_trip_and_invalidation(tmp_path):
    from wick.scanner import scan

    Image.new("RGB", (64, 48), "teal").save(tmp_path / "a.png")
    s = Settings(min_image_kb=0)
    first = scan([tmp_path], s).entries[0]
    st = (tmp_path / "a.png").stat()
    cached = index.lookup(tmp_path / "a.png", st.st_size, st.st_mtime, first.kind)
    assert cached is not None and (cached.width, cached.height) == (64, 48)
    # A changed file is read again.
    assert index.lookup(tmp_path / "a.png", st.st_size + 1, st.st_mtime, first.kind) is None
    index.set_enabled(False)
    try:
        assert index.lookup(tmp_path / "a.png", st.st_size, st.st_mtime, first.kind) is None
    finally:
        index.set_enabled(True)
    assert index.prune(max_age_days=-1) >= 1   # everything is "old" with a negative age
    assert index.lookup(tmp_path / "a.png", st.st_size, st.st_mtime, first.kind) is None


def test_presets_save_apply_match_delete():
    base = Settings(output_mode="folder", output_dir="/somewhere")
    assert set(presets.BUILTIN) <= set(presets.names())
    applied = presets.apply("Maximum savings", base)
    assert applied.smart_quality and applied.smart_target == "good"
    assert applied.output_dir == "/somewhere"  # personal options are left alone
    assert presets.matching(applied) == "Maximum savings"

    with pytest.raises(ValueError):
        presets.save("Photo archive", base)
    mine = Settings(image_format="avif", image_quality=61)
    presets.save("  Mine ", mine)
    assert "Mine" in presets.names() and not presets.is_builtin("Mine")
    assert presets.apply("Mine", base).image_quality == 61
    presets.delete("Mine")
    assert "Mine" not in presets.names()
    with pytest.raises(KeyError):
        presets.apply("Mine", base)


def test_history_retention():
    w = journal.RunWriter(Path("."), {})
    old = time.time() - 40 * 86400
    os.utime(w.path, (old, old))
    assert journal.prune(0) == 0          # 0 = keep forever
    assert w.path.exists()
    assert journal.prune(30) >= 1
    assert not w.path.exists()


def test_log_and_diagnostics():
    from wick import log

    log.setup()
    log.log.warning("test line %d", 42)
    for h in log.log.handlers:
        h.flush()
    assert any("test line 42" in line for line in log.tail())
    text = log.diagnostics()
    assert text.startswith("Wick ") and "ffmpeg" in text


def test_gui_command_line_parsing():
    from wick.gui.app import parse_args

    bg, preset, convert_to, paths = parse_args(["--preset", "For sharing", "x.jpg", "--background"])
    assert bg and preset == "For sharing" and not convert_to and paths == [os.path.abspath("x.jpg")]
    assert parse_args(["--preset=Screenshots"])[1] == "Screenshots"
    assert parse_args(["--convert", "jpeg", "a.png", "b.png"])[2:] == (
        "jpeg", [os.path.abspath("a.png"), os.path.abspath("b.png")])
    _bg, _p, convert_to, paths = parse_args(["--convert=mp4_h264", "clip.mov"])
    assert convert_to == "mp4_h264" and paths == [os.path.abspath("clip.mov")]

    from wick.gui.app import open_target

    assert open_target(["--open=history"]) == "history" and open_target(["--open", "cleanup"]) == "cleanup"
    assert open_target(["a.jpg"]) == "" and parse_args(["--open", "cleanup"])[3] == []   # not taken for a path


def test_video_target_size_rules():
    s = Settings(video_max_height=720)
    assert s.video_target_size(1920, 1080) == (1280, 720)
    assert s.video_target_size(1080, 1920) == (720, 1280)   # portrait: limit applies to the short side
    assert s.video_target_size(640, 360) == (640, 360)      # never upscales
    assert Settings().video_target_size(1921, 1081) == (1920, 1080)  # even dimensions


@needs_ffmpeg
def test_video_preview_and_options(tmp_path):
    from wick.plan import plan_entry
    from wick.scanner import scan
    from wick.video import preview_video

    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3",
           "-c:v", "libx264", "-crf", "16", "-c:a", "aac", "clip.mp4", cwd=tmp_path)
    s = Settings(video_codec="hevc", video_max_height=240, video_max_fps=24, audio_bitrate=64, min_video_mb=0)
    entry = scan([tmp_path], s).entries[0]
    plan = plan_entry(entry, s)
    assert plan.convert and "240p" in plan.reason and "24 fps" in plan.reason
    r = preview_video(entry.path, entry.video, s)
    assert 0 < r.est_size < entry.size
    with Image.open(__import__("io").BytesIO(r.after)) as im:
        assert im.size == (426, 240)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clip.mp4"]  # no temp files left


def test_smart_quality_search_and_thresholds():
    from wick import quality

    # Values ordered from best quality to most compression; passes while v <= 70.
    chosen = quality.search([90, 80, 70, 60, 50], lambda v: (v >= 70, f"q{v}"))
    assert chosen == (70, "q70")
    assert quality.search([90, 80], lambda v: (False, None)) is None
    assert quality.Score("SSIMULACRA2", 86).meets("identical")
    assert not quality.Score("SSIMULACRA2", 75).meets("high")
    assert str(quality.Score("SSIM", 0.98123)) == "SSIM 0.981"


def test_smart_quality_image_reports_choice(tmp_path):
    from wick.engine import preview_file
    from wick.fileops import remove_quietly
    from wick.plan import plan_entry
    from wick.scanner import scan

    Image.radial_gradient("L").resize((300, 300)).convert("RGB").save(tmp_path / "g.png")
    s = Settings(image_format="webp", png_mode="lossy", smart_quality=True, smart_target="good", min_image_kb=0)
    entry = scan([tmp_path], s).entries[0]
    plan = plan_entry(entry, s)
    out = preview_file(entry, plan, s)
    try:
        assert out.exists() and plan.outcome.startswith("quality ")
    finally:
        remove_quietly(out)


def test_smart_quality_unreached_target_keeps_the_best_quality_attempt(tmp_path, monkeypatch):
    """When no quality meets the target, the result is the one made at the best quality (it used to be
    whichever attempt came first, labeled as the best)."""
    import dataclasses

    from wick import quality
    from wick.engine import preview_file
    from wick.fileops import remove_quietly
    from wick.images import SMART_QUALITIES
    from wick.plan import plan_entry
    from wick.scanner import scan

    Image.effect_noise((240, 180), 50).convert("RGB").save(tmp_path / "n.png")
    monkeypatch.setattr(quality, "score_images", lambda *a, **k: quality.Score("SSIMULACRA2", 1.0))
    s = Settings(image_format="webp", png_mode="lossy", smart_quality=True, min_image_kb=0)
    entry = scan([tmp_path], s).entries[0]
    plan = plan_entry(entry, s)
    assert "Smart quality (visually identical)" in plan.reason
    smart = preview_file(entry, plan, s)
    best = dataclasses.replace(s, smart_quality=False, image_quality=SMART_QUALITIES[0])
    fixed = preview_file(entry, plan_entry(entry, best), best)
    try:
        assert plan.outcome.startswith(f"quality {SMART_QUALITIES[0]} ")
        assert smart.stat().st_size == fixed.stat().st_size
    finally:
        remove_quietly(smart, fixed)


def test_custom_video_quality_value():
    from wick.video import encoder_quality, measures_quality

    s = Settings(video_quality="custom", video_crf=30, smart_quality=True)
    assert encoder_quality(s, "libx265") == 30 and not measures_quality(s)
    assert encoder_quality(Settings(video_quality="custom", video_crf=60), "libx265") == 51   # x265's range
    assert encoder_quality(Settings(video_quality="custom", video_crf=60), "libsvtav1") == 60
    assert measures_quality(Settings(video_quality="identical"))
    assert Settings.from_dict({"video_quality": "custom", "video_crf": 999}).video_crf == 63


@needs_ffmpeg
def test_repeated_frames_detected_and_removed(tmp_path):
    from wick import mediainfo
    from wick.engine import DONE, run_job
    from wick.plan import plan_entry
    from wick.probe import detect_cadence, probe_video
    from wick.scanner import scan

    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=24:d=8", "-vf", "fps=30", "-c:v", "libx264", "-crf", "20",
           "padded.mp4", cwd=tmp_path)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=8", "-c:v", "libx264", "-crf", "20", "real.mp4",
           cwd=tmp_path)
    assert detect_cadence(tmp_path / "real.mp4", probe_video(tmp_path / "real.mp4")) == 0

    s = Settings(video_codec="hevc", min_video_mb=0, output_mode="suffix", image_format="none")
    entry = next(e for e in scan([tmp_path], s).entries if e.path.name == "padded.mp4")
    assert entry.video.cadence == 5 and round(entry.video.content_fps) == 24
    assert "30→24 fps" in mediainfo.describe(entry) and "padded" in mediainfo.cadence_note(entry.video)
    plan = plan_entry(entry, s)
    assert "repeated frames removed" in plan.reason
    results, _ = run_job([(entry, plan)], s, tmp_path)
    assert results[0].status == DONE
    out = probe_video(results[0].output)
    assert round(out.fps) == 24 and abs(out.duration - entry.video.duration) < 0.5


def test_media_details_text():
    from wick import mediainfo
    from wick.formats import AUDIO, VIDEO
    from wick.probe import AudioInfo, VideoInfo
    from wick.scanner import FileEntry

    v = FileEntry(Path("a.mp4"), 1, VIDEO, 0, video=VideoInfo("h264", 1920, 1080, "yuv420p", 29.97, 201.0,
                                                               8_200_000, 0))
    assert mediainfo.describe(v) == "1920×1080 · 29.97 fps · H.264 · 8.2 Mb/s · 3:21"
    a = FileEntry(Path("a.flac"), 1, AUDIO, 0, audio=AudioInfo("flac", 96000, 2, 24, "s32", 65.0, 2_000_000, 0))
    assert mediainfo.describe(a) == "FLAC · 96 kHz · 24-bit · stereo · 2.0 Mb/s · 1:05"


def test_parallelism_rules(monkeypatch):
    from wick import tools, video

    monkeypatch.setattr("os.cpu_count", lambda: 16)
    tools.total_memory_gb.cache_clear()
    monkeypatch.setattr(tools, "total_memory_gb", lambda: 32.0)
    assert Settings().image_workers() == 16 and Settings().threads_per_job() == 1
    monkeypatch.setattr(tools, "total_memory_gb", lambda: 6.0)        # little memory: fewer at once
    assert Settings().image_workers() == 6
    assert Settings(workers=3).image_workers() == 3
    monkeypatch.setattr(video, "choose_encoder", lambda codec, pref: "hevc_nvenc")
    assert video.parallel_videos(Settings()) == 2
    monkeypatch.setattr(video, "choose_encoder", lambda codec, pref: "libx265")
    assert video.parallel_videos(Settings()) == 1
    assert video.parallel_videos(Settings(video_jobs=2)) == 2


def test_camera_originals_skip_repeat_detection():
    from wick.probe import VideoInfo, detect_cadence, is_camera_original

    v = VideoInfo("hevc", 1920, 1080, "yuv420p", 30.0, 10.0, 1, 0, tags={"com.apple.quicktime.make": "Apple"})
    assert is_camera_original(v) and detect_cadence(Path("missing.mov"), v) == 0


def test_history_reads_only_changed_files():
    from wick import journal

    w = journal.RunWriter(Path("."), {})
    w.add(journal.FileRecord("/a.jpg", "/a.jxl", 100, 80, "jpeg", "m", True, 0.0))
    sig = journal.signature()
    assert any(r.run_id == w.run.run_id for r in journal.list_runs())
    assert journal.signature() == sig            # nothing changed
    journal.mark_undone(w.run.run_id, "/a.jpg", "Undone")
    assert journal.signature() != sig
    run = next(r for r in journal.list_runs() if r.run_id == w.run.run_id)
    assert run.files[0].undone                  # the changed file was re-read


def test_languages_from_the_system(monkeypatch):
    from wick import languages

    assert languages.normalize("sv_SE.UTF-8") == "sv" and languages.normalize("ger") == "de"
    assert languages.normalize("deu") == "de" and languages.normalize("en-GB") == "en"
    assert languages.normalize("C") == "" and languages.normalize("xx") == ""
    assert {"ger", "deu", "de"} <= languages.track_codes(["de"])
    assert languages.parse("sv, en,xx,sv") == ["sv", "en"]
    if os.name != "nt":
        monkeypatch.setenv("LANGUAGE", "sv_SE:en_GB")
        monkeypatch.setenv("LANG", "sv_SE.UTF-8")
        assert languages.detect()[:2] == ["sv", "en"]
    s = Settings(languages="fr")
    from wick.settings import my_languages

    assert my_languages(s) == ["fr"]


def test_camera_videos_get_hevc_by_default():
    from wick.probe import VideoInfo
    from wick.video import codec_for

    phone = VideoInfo("h264", 1920, 1080, "yuv420p", 30, 10, 20_000_000, 0, tags={"com.apple.quicktime.make": "Apple"})
    download = VideoInfo("h264", 1920, 1080, "yuv420p", 30, 10, 5_000_000, 0)
    s = Settings()
    assert s.video_codec == "auto"
    assert codec_for(phone, s) == "hevc" and codec_for(download, s) == "av1"
    assert codec_for(phone, Settings(video_codec="av1")) == "av1"


@needs_ffmpeg
def test_only_my_languages_and_measured_quality(tmp_path):
    """Dubs in other languages are left out (the main soundtrack stays); an already-efficient video is only
    remuxed; every encoded video reports how close it is to the original."""
    from wick.engine import run_job
    from wick.plan import plan_entry
    from wick.probe import probe_video
    from wick.scanner import scan

    src = ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-f", "lavfi", "-i", "sine=frequency=440",
           "-f", "lavfi", "-i", "sine=frequency=660", "-f", "lavfi", "-i", "sine=frequency=880", "-t", "4",
           "-map", "0", "-map", "1", "-map", "2", "-map", "3", "-c:a", "ac3",
           "-metadata:s:a:0", "language=eng", "-disposition:a:0", "default",
           "-metadata:s:a:1", "language=ger", "-metadata:s:a:2", "language=swe"]
    ffmpeg(*src, "-c:v", "libx264", "-preset", "ultrafast", str(tmp_path / "film.mkv"))
    ffmpeg(*src, "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
           str(tmp_path / "already.mkv"))
    s = Settings(video_tracks="mine", languages="sv", min_video_mb=0, output_mode="suffix", video_encoder="cpu",
                 video_quality="identical", min_savings_pct=0, image_format="none")
    entries = scan(tmp_path, s).entries
    plans = {e.path.name: plan_entry(e, s) for e in entries}
    assert plans["already.mkv"].route == "video_remux" and plans["film.mkv"].route == "video"
    assert "other languages" in plans["film.mkv"].reason
    results, _ = run_job([(e, plans[e.path.name]) for e in entries], s, tmp_path)
    by = {r.entry.path.name: r for r in results}
    assert all(r.status == "done" for r in results), [r.message for r in results]
    assert "looks identical" in by["film.mkv"].message or "differences" in by["film.mkv"].message
    for name, codec in (("film_compressed.mkv", "av1"), ("already_compressed.mkv", "hevc")):
        v = probe_video(tmp_path / name)
        assert v.codec == codec
        assert [st.language for st in v.audio_streams()] == ["eng", "swe"]   # German dub left out
    # Off: nothing is left out.
    s2 = Settings(min_video_mb=0, image_format="none")
    assert all(p.route != "video_remux" for p in (plan_entry(e, s2) for e in entries))


def test_recordings_and_watch_suggestions(tmp_path, monkeypatch):
    from wick import suggest
    from wick.overview import confidence, is_recording
    from wick.probe import VideoInfo
    from wick.scanner import FileEntry

    rec = FileEntry(tmp_path / "Videos" / "Captures" / "game.mp4", 1, "video", 0)
    rec.video = VideoInfo("h264", 1920, 1080, "yuv420p", 60, 60, 50_000_000, 0)     # ~0.4 bits per pixel
    normal = FileEntry(tmp_path / "Videos" / "Holiday" / "beach.mp4", 1, "video", 0)
    normal.video = VideoInfo("h264", 1920, 1080, "yuv420p", 60, 60, 50_000_000, 0)
    assert is_recording(rec) and not is_recording(normal)
    assert "recording" in confidence(rec, None)[1]
    (tmp_path / "Pictures" / "Screenshots").mkdir(parents=True)
    (tmp_path / "Videos" / "Captures").mkdir(parents=True, exist_ok=True)
    obs = tmp_path / "obs" / "basic" / "profiles" / "Untitled"
    obs.mkdir(parents=True)
    (obs / "basic.ini").write_text(f"[SimpleOutput]\nFilePath={tmp_path / 'OBS out'}\n")
    (tmp_path / "OBS out").mkdir()
    import wick.cleanup.common as common

    monkeypatch.setattr(common, "known_folder", lambda n: tmp_path / n)
    monkeypatch.setattr(suggest, "_obs_folders", lambda: [tmp_path / "OBS out"])
    found = suggest.watch_suggestions([tmp_path / "Videos" / "Captures"])        # already watched: not offered
    assert [(label, preset) for label, _p, preset in found] == [("Screenshots", "Screenshots"), ("OBS recordings", "")]


def test_undo_knows_when_the_original_left_the_bin(tmp_path):
    """Emptying the Recycle Bin/Trash makes undo impossible: say so in advance (except for lossless JPEG XL,
    which rebuilds the exact JPEG)."""
    import os
    import time as _time

    from wick import journal, trash_restore
    from wick.engine import cannot_undo
    from wick.fileops import move_to_trash

    if os.name == "nt":
        pytest.skip("the real Recycle Bin is covered by the Windows integration tests")
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"x" * 1000)
    t0 = _time.time()
    move_to_trash(f)
    out = tmp_path / "photo.jxl"
    out.write_bytes(b"y" * 500)
    rec = journal.FileRecord(str(f), str(out), 1000, 500, "jpeg", "x", False, t0, mode="replace")
    idx = trash_restore.index([f])
    assert cannot_undo(rec, idx) == ""
    item = trash_restore.find_trashed(f, t0)
    trash_restore.purge(item)                                  # as if the bin was emptied
    idx = trash_restore.index([f])
    assert "no longer in the" in cannot_undo(rec, idx)
    rec.reversible = True                                      # lossless JPEG XL: still possible
    assert cannot_undo(rec, idx) == ""
    copy = journal.FileRecord(str(f), str(out), 1000, 500, "jpeg", "x", False, t0, mode="copy")
    assert cannot_undo(copy, idx) == ""                        # the original was never touched
