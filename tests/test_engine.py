"""End-to-end tests using the real encoders and the real Recycle Bin / Trash."""

import os
from pathlib import Path

import pytest

from wick import journal
from wick.engine import DONE, SKIPPED, run_job, undo_run
from wick.plan import plan_entry
from wick.scanner import scan
from wick.settings import Settings

from .conftest import ffmpeg, needs_ffmpeg, needs_gifsicle, needs_image_tools


def _make_images(d: Path) -> dict[str, bytes]:
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x480", "-frames:v", "1", "-q:v", "3", "photo.jpg", cwd=d)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240,format=rgba", "-frames:v", "1", "shot.png", cwd=d)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240,format=gray", "-frames:v", "1", "gray.png", cwd=d)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240,format=rgb48be", "-frames:v", "1", "deep.png", cwd=d)
    return {p.name: p.read_bytes() for p in d.iterdir()}


def _make_more(d: Path) -> None:
    """Every other supported source format, incl. animations and a rotated photo with GPS."""
    from PIL import Image, ImageSequence

    import pillow_heif

    pillow_heif.register_heif_opener()
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=160x120:r=10:d=1", "anim.gif", cwd=d)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=160x120:r=10:d=1", "-plays", "0", "-f", "apng", "anim.png", cwd=d)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240", "-frames:v", "1", "-pix_fmt", "bgr24", "pic.bmp", cwd=d)
    base = Image.open(d / "photo.jpg")
    ex = Image.Exif()
    ex[0x010F] = "Cam"
    ex[0x0112] = 6
    ex.get_ifd(0x8825)[1] = "N"
    base.save(d / "gps.jpg", quality=92, exif=ex.tobytes())
    base.save(d / "scan.tif", compression=None)
    base.save(d / "phone.heic", quality=90)
    base.save(d / "lossy.webp", quality=95)
    frames = [f.convert("RGBA") for f in ImageSequence.Iterator(Image.open(d / "anim.gif"))]
    frames[0].save(d / "anim.webp", save_all=True, append_images=frames[1:], duration=100, lossless=True)


def _run(d: Path, s: Settings):
    result = scan(d, s)
    items = [(e, plan_entry(e, s)) for e in result.entries]
    return run_job([(e, p) for e, p in items if p.convert], s, d)


@needs_image_tools
@pytest.mark.parametrize("fmt,mode", [("jxl", "lossless"), ("jxl", "lossy"), ("webp", "lossless"), ("webp", "lossy")])
def test_images_roundtrip(media_dir, fmt, mode):
    originals = _make_images(media_dir)
    s = Settings(image_format=fmt, jpeg_mode=mode, png_mode=mode, min_image_kb=0, min_savings_pct=0)
    results, run_id = _run(media_dir, s)
    by_name = {r.entry.path.name: r for r in results}

    failures = {n: r.message for n, r in by_name.items() if r.status not in (DONE, SKIPPED)}
    assert not failures
    ext = "." + fmt
    for name, r in by_name.items():
        if r.status == DONE:
            assert r.output.suffix == ext and r.output.exists()
            assert not (media_dir / name).exists()  # original went to the bin
            assert r.output.stat().st_mtime == pytest.approx(r.entry.mtime, abs=2)
    assert not [p for p in media_dir.iterdir() if p.name.startswith(".cmp-")]

    if run_id:
        ok, failed = undo_run(run_id)
        assert failed == 0
    for name, data in originals.items():
        assert (media_dir / name).read_bytes() == data, name
    assert sorted(p.name for p in media_dir.iterdir()) == sorted(originals)


@needs_image_tools
def test_min_savings_keeps_original(media_dir):
    _make_images(media_dir)
    s = Settings(min_image_kb=0, min_savings_pct=90)
    results, run_id = _run(media_dir, s)
    assert results and all(r.status == SKIPPED for r in results)
    assert run_id is None
    assert not list(media_dir.glob("*.jxl"))


@needs_image_tools
def test_jpeg_rebuilt_when_bin_emptied(media_dir):
    _make_images(media_dir)
    original = (media_dir / "photo.jpg").read_bytes()
    s = Settings(min_image_kb=0, min_savings_pct=0, png_mode="lossless")
    results, run_id = _run(media_dir, s)
    run = journal.get_run(run_id)
    rec = next(f for f in run.files if f.original.endswith("photo.jpg"))
    assert rec.reversible and rec.original_sha256

    # Simulate an emptied Recycle Bin for the JPEG.
    from wick import trash_restore
    item = trash_restore.find_trashed(Path(rec.original), rec.trashed_at)
    assert item is not None, "original should be findable in the Recycle Bin/Trash"
    os.remove(item.data_path)
    os.remove(item.meta_path)

    ok, failed = undo_run(run_id)
    assert failed == 0
    assert (media_dir / "photo.jpg").read_bytes() == original


@needs_ffmpeg
@pytest.mark.parametrize("codec", ["av1", "hevc"])
def test_video_roundtrip(media_dir, codec):
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3",
           "-c:v", "libx264", "-crf", "12", "-c:a", "aac", "clip.mp4", cwd=media_dir)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=321x241:r=25:d=3", "-f", "lavfi", "-i", "sine=f=300:d=3",
           "-c:v", "mpeg4", "-q:v", "2", "-c:a", "pcm_s16le", "odd.avi", cwd=media_dir)
    originals = {p.name: p.read_bytes() for p in media_dir.iterdir()}
    s = Settings(video_codec=codec, video_encoder="cpu", video_quality="small", min_video_mb=0, min_savings_pct=0)
    results, run_id = _run(media_dir, s)
    assert {r.entry.path.name: r.status for r in results} == {"clip.mp4": DONE, "odd.avi": DONE}, \
        [r.message for r in results]
    assert (media_dir / "clip.mp4").exists() and (media_dir / "odd.mkv").exists()

    ok, failed = undo_run(run_id)
    assert failed == 0
    for name, data in originals.items():
        assert (media_dir / name).read_bytes() == data, name
    assert sorted(p.name for p in media_dir.iterdir()) == sorted(originals)


@needs_image_tools
@pytest.mark.parametrize("fmt", ["keep", "jxl", "webp", "avif"])
@pytest.mark.parametrize("mode", ["lossless", "lossy"])
def test_all_formats_roundtrip(media_dir, fmt, mode):
    _make_images(media_dir)
    _make_more(media_dir)
    originals = {p.name: p.read_bytes() for p in media_dir.iterdir()}
    s = Settings(image_format=fmt, jpeg_mode=mode, png_mode=mode, min_image_kb=0, min_savings_pct=0)
    results, run_id = _run(media_dir, s)
    failures = {r.entry.path.name: r.message for r in results if r.status not in (DONE, SKIPPED)}
    # libjxl < 0.9 can't decode animated JXL made from APNG; verification catches it.
    failures.pop("anim.png", None) if fmt == "jxl" else None
    assert not failures
    assert any(r.status == DONE for r in results)
    assert not [p for p in media_dir.iterdir() if p.name.startswith(".cmp-")]
    if run_id:
        problems = []
        undo_run(run_id, lambda rec, ok, msg: problems.append(f"{rec.original}: {msg}") if not ok else None)
        assert not problems
    for name, data in originals.items():
        assert (media_dir / name).read_bytes() == data, name
    assert sorted(p.name for p in media_dir.iterdir()) == sorted(originals)


@needs_image_tools
def test_output_folder_keeps_originals(media_dir, tmp_path):
    _make_images(media_dir)
    (media_dir / "sub").mkdir()
    (media_dir / "photo.jpg").rename(media_dir / "sub" / "photo.jpg")
    originals = {p: p.read_bytes() for p in media_dir.rglob("*") if p.is_file()}
    out = tmp_path / "out"
    s = Settings(min_image_kb=0, min_savings_pct=0, output_mode="folder", output_dir=str(out))
    results, run_id = _run(media_dir, s)
    assert all(r.status in (DONE, SKIPPED) for r in results)
    assert (out / media_dir.name / "sub" / "photo.jxl").exists()
    for p, data in originals.items():
        assert p.read_bytes() == data  # untouched
    undo_run(run_id)
    assert not [p for p in out.rglob("*") if p.is_file()]


@needs_image_tools
def test_strip_gps_and_resize(media_dir):
    from PIL import Image

    _make_images(media_dir)
    _make_more(media_dir)
    s = Settings(image_format="keep", jpeg_mode="lossy", min_image_kb=0, min_savings_pct=0,
                 metadata="strip_gps", resize_mode="long_edge", resize_long_edge=400)
    results, _ = _run(media_dir, s)
    r = next(r for r in results if r.entry.path.name == "gps.jpg")
    assert r.status == DONE, r.message
    with Image.open(r.output) as im:
        ex = im.getexif()
        assert ex.get(0x010F) == "Cam" and not ex.get_ifd(0x8825)
        assert max(im.size) == 400
        assert im.size == (300, 400)  # rotation baked into the pixels
        assert ex.get(0x0112, 1) == 1


@needs_gifsicle
@needs_image_tools
def test_gif_keep_format(media_dir):
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=160x120:r=10:d=1", "anim.gif", cwd=media_dir)
    for mode in ("lossless", "lossy"):
        s = Settings(image_format="keep", png_mode=mode, min_image_kb=0, min_savings_pct=0,
                     output_mode="suffix", output_suffix=f"_{mode}")
        results, _ = _run(media_dir, s)
        assert results[0].status in (DONE, SKIPPED), results[0].message


@needs_image_tools
def test_images_are_made_to_fit_a_size_limit(media_dir):
    """The best quality that fits; smaller dimensions only when even low quality is too big."""
    from PIL import Image as _Image

    _Image.effect_noise((1600, 1200), 60).convert("RGB").save(media_dir / "big.jpg", quality=97)
    _Image.effect_noise((300, 200), 40).convert("RGB").save(media_dir / "small.jpg", quality=90)
    small_before = (media_dir / "small.jpg").stat().st_size
    s = Settings(image_format="keep", jpeg_mode="lossless", fit_under_kb=200, min_image_kb=0, output_mode="suffix")
    results, _ = _run(media_dir, s)
    by = {r.entry.path.name: r for r in results}
    assert by["big.jpg"].status == DONE, by["big.jpg"].message
    out = by["big.jpg"].output
    assert out.stat().st_size <= 200 * 1024
    with _Image.open(out) as im:
        assert im.size[0] > 0
    if small_before <= 200 * 1024:
        assert by["small.jpg"].output is None or by["small.jpg"].output.stat().st_size <= small_before


@needs_ffmpeg
def test_videos_are_made_to_fit_a_size_limit(media_dir):
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6", "-f", "lavfi", "-i", "anoisesrc=d=6",
           "-c:v", "libx264", "-crf", "8", "-c:a", "aac", "-b:a", "256k", "clip.mp4", cwd=media_dir)
    limit_kb = 300
    assert (media_dir / "clip.mp4").stat().st_size > limit_kb * 1024
    s = Settings(video_codec="hevc", video_encoder="cpu", fit_under_kb=limit_kb, min_video_mb=0,
                 output_mode="suffix")
    results, _ = _run(media_dir, s)
    r = results[0]
    assert r.status == DONE, r.message
    assert r.output.stat().st_size <= limit_kb * 1024, r.output.stat().st_size


@needs_ffmpeg
def test_black_bars_are_found_and_removed(media_dir):
    """A 16:9 picture letterboxed into a 4:3 frame: the bars go, the picture stays whole."""
    from wick.probe import probe_video
    from wick.video import detect_bars

    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=4", "-vf", "pad=640:480:0:60:black",
           "-c:v", "libx264", "-crf", "16", "boxed.mp4", cwd=media_dir)
    info = probe_video(media_dir / "boxed.mp4")
    bars = detect_bars(media_dir / "boxed.mp4", info)
    assert bars is not None
    w, h, x, y = bars
    assert w == 640 and 356 <= h <= 364 and 56 <= y <= 64, bars
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x480:r=25:d=2", "-c:v", "libx264", "-crf", "16", "full.mp4",
           cwd=media_dir)
    assert detect_bars(media_dir / "full.mp4", probe_video(media_dir / "full.mp4")) is None   # nothing to remove

    (media_dir / "full.mp4").unlink()
    s = Settings(video_codec="hevc", video_encoder="cpu", video_quality="small", video_crop_bars=True,
                 min_video_mb=0, min_savings_pct=0, require_savings=False, output_mode="suffix")
    results, _ = _run(media_dir, s)
    assert results[0].status == DONE, results[0].message
    out = probe_video(results[0].output)
    assert (out.width, out.height) == (w, h) and "black bars removed" in results[0].message


@needs_ffmpeg
def test_a_file_you_pick_ignores_the_size_minimums(tmp_path):
    """Minimum sizes keep folder scans (and Watch Folders) off tiny files, but a video you drop or pick by itself
    is compressed whatever its size, instead of "1 file found, 0 can be compressed"."""
    from wick.plan import plan_entry
    from wick.scanner import scan

    clip = tmp_path / "clip.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25", "-c:v", "libx264", "-b:v", "2M",
           str(clip))
    s = Settings()                                       # the default minimum for videos is 10 MB
    picked = scan([clip], s, picked=True).entries[0]
    found = scan([tmp_path], s).entries[0]
    assert picked.picked and not found.picked
    assert plan_entry(picked, s).convert
    assert not plan_entry(found, s).convert and plan_entry(found, s).reason.startswith("Smaller than")
    watched = scan([clip], s).entries[0]                 # Watch Folders passes new files one by one: minimums apply
    assert not plan_entry(watched, s).convert


def test_same_named_results_finishing_together_get_their_own_names(tmp_path, monkeypatch):
    """photo.png and photo.gif both become photo.webp: when they finish at the same moment, each gets its own
    free name (choosing it and putting the file there is one step), and nothing is overwritten."""
    import threading
    import time
    from types import SimpleNamespace

    from wick import engine
    from wick.settings import Settings

    s = Settings(output_mode="beside")
    s.validate()
    real = engine.output_path

    def slow(entry, plan, settings):
        dest = real(entry, plan, settings)
        time.sleep(0.3)                     # the other file finishes meanwhile
        return dest

    monkeypatch.setattr(engine, "output_path", slow)
    results = []

    def finish(name: str, data: bytes) -> None:
        src = tmp_path / name
        src.write_bytes(b"original")
        tmp = tmp_path / f"{name}.tmp"
        tmp.write_bytes(data)
        entry = SimpleNamespace(path=src, kind="png", root=None, size=8)
        plan = SimpleNamespace(dest=None, target_ext=".webp")
        results.append(engine._place(entry, src.stat(), tmp, lambda: engine.output_path(entry, plan, s), s, None,
                                     "test"))

    threads = [threading.Thread(target=finish, args=(n, n.encode())) for n in ("photo.png", "photo.gif")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(p.name for p in results) == ["photo (1).webp", "photo.webp"]
    assert {p.read_bytes() for p in results} == {b"photo.png", b"photo.gif"}
