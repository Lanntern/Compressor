"""Motion Photos, Live Photos, HDR, interlaced video, learned estimates and similar photos."""

from pathlib import Path

from PIL import Image, ImageChops

from wick.plan import plan_entry
from wick.scanner import scan
from wick.settings import Settings

from .conftest import ffmpeg, needs_ffmpeg


def _jpeg_with_video_inside(path: Path) -> None:
    Image.new("RGB", (400, 300), "teal").save(path, quality=90)
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description GCamera:MotionPhoto="1" '
           b'GCamera:MotionPhotoVersion="1"/></rdf:RDF></x:xmpmeta>')
    data = path.read_bytes()
    seg = b"\xff\xe1" + (len(xmp) + 31).to_bytes(2, "big") + b"http://ns.adobe.com/xap/1.0/\x00" + xmp
    path.write_bytes(data[:2] + seg + data[2:] + b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000)


def test_motion_photos_and_live_photos_are_left_alone(tmp_path):
    _jpeg_with_video_inside(tmp_path / "PXL_20260101_motion.jpg")
    Image.new("RGB", (400, 300), "navy").save(tmp_path / "IMG_0001.JPG", quality=90)
    (tmp_path / "IMG_0001.MOV").write_bytes(b"\x00" * 100)        # its Live Photo video
    Image.new("RGB", (400, 300), "olive").save(tmp_path / "plain.jpg", quality=90)
    s = Settings(min_image_kb=0, image_format="jxl", jpeg_mode="lossy")
    plans = {e.path.name: plan_entry(e, s) for e in scan([tmp_path], s).entries}
    assert not plans["PXL_20260101_motion.jpg"].convert and "Motion Photo" in plans["PXL_20260101_motion.jpg"].reason
    assert not plans["IMG_0001.JPG"].convert and "Live Photo" in plans["IMG_0001.JPG"].reason
    assert plans["plain.jpg"].convert


@needs_ffmpeg
def test_hdr10_metadata_and_interlacing(tmp_path):
    from wick.engine import DONE, run_job
    from wick.probe import _mastering, probe_video

    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=2", "-pix_fmt", "yuv420p10le", "-c:v", "libx265",
           "-x265-params", "log-level=error:crf=4:hdr10=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:"
           "colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)"
           "L(10000000,1):max-cll=1000,400", "-color_primaries", "bt2020", "-color_trc", "smpte2084",
           "-colorspace", "bt2020nc", "hdr.mp4", cwd=tmp_path)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2", "-vf", "setfield=tff", "-c:v", "libx264",
           "-flags", "+ilme+ildct", "-x264-params", "tff=1", "tv.mp4", cwd=tmp_path)
    s = Settings(video_codec="hevc", video_encoder="cpu", min_video_mb=0, output_mode="suffix",
                 image_format="none", require_savings=False, video_max_height=160)
    entries = {e.path.name: e for e in scan([tmp_path], s).entries}
    hdr, tv = entries["hdr.mp4"], entries["tv.mp4"]
    assert hdr.video.hdr == "HDR10" and hdr.video.mastering.get("max_luminance") == 1000
    assert tv.video.interlaced and "deinterlaced" in plan_entry(tv, s).reason
    results, _ = run_job([(e, plan_entry(e, s)) for e in (hdr, tv)], s, tmp_path)
    assert all(r.status == DONE for r in results), [r.message for r in results]
    out = {r.entry.path.name: probe_video(r.output) for r in results}
    assert out["hdr.mp4"].hdr == "HDR10" and out["hdr.mp4"].is_10bit
    assert _mastering(next(r.output for r in results if r.entry is hdr), 0).get("max_content") == 1000
    assert not out["tv.mp4"].interlaced


def test_estimates_learn_from_results(tmp_path):
    from wick import calibration
    from wick.plan import Plan
    from wick.scanner import FileEntry

    calibration.reset()
    e = FileEntry(tmp_path / "x.png", 1000, "png", 0)
    p = Plan(True, "", ".jxl", 600, True, False, "jxl")
    assert calibration.factor(e, p) == 1.0
    for _ in range(5):
        calibration.record(e, p, 300, 600)          # always half the estimate
    assert abs(calibration.factor(e, p) - 0.5) < 0.01
    calibration.save()
    calibration._data = None                         # reload from disk
    assert abs(calibration.factor(e, p) - 0.5) < 0.01
    calibration.reset()


def test_similar_photos(tmp_path):
    from wick.cleanup.similar import find_similar

    base = Image.effect_mandelbrot((1200, 900), (-2, -1.2, 1, 1.2), 80).convert("RGB")
    base.save(tmp_path / "original.jpg", quality=95)
    base.save(tmp_path / "re-saved.jpg", quality=70)
    base.resize((600, 450)).save(tmp_path / "smaller.jpg", quality=90)
    from PIL import ImageFilter

    ImageChops.offset(base, 20, 0).filter(ImageFilter.GaussianBlur(1.5)).save(tmp_path / "burst.jpg", quality=95)
    Image.effect_mandelbrot((1200, 900), (-0.8, 0, -0.6, 0.2), 120).convert("RGB").save(tmp_path / "other.jpg")
    import os
    import time

    for age, name in enumerate(("original.jpg", "burst.jpg", "re-saved.jpg", "smaller.jpg")):
        t = time.time() - (10 - age) * 3600                      # the original is the oldest
        os.utime(tmp_path / name, (t, t))
    res = find_similar([tmp_path], strictness="very", min_size=0)
    by = {c.path.name: c for c in res.items}
    assert set(by) == {"re-saved.jpg", "smaller.jpg", "burst.jpg"}
    assert all(c.keeper.name == "original.jpg" for c in by.values())
    assert by["re-saved.jpg"].selected and by["smaller.jpg"].selected
    assert not by["burst.jpg"].selected                          # different shot: the user decides


def test_similar_groups_do_not_chain():
    """A~B and B~C without A~C: C must not be listed as similar to A (it doesn't look like it)."""
    import os
    from pathlib import Path

    from wick.cleanup.similar import _star_groups

    st = os.stat(__file__)
    files = {i: (Path(f"{i}.jpg"), st) for i in range(3)}
    prints = {0: (0b000, 400, 300), 1: (0b011, 400, 300), 2: (0b01111, 400, 300)}   # 0–1: 2 bits, 1–2: 2, 0–2: 4
    groups = _star_groups([0, 1, 2], prints, {0: 9.0, 1: 5.0, 2: 5.0}, files, limit=3)
    assert groups == [(0, [1])]                       # the sharpest stays; 2 is too far from it to be listed


def test_similar_photo_pairs_and_live_photos(tmp_path):
    """HEIC+JPG of one shot are labeled as such; removing a Live Photo's still takes its video along."""
    import os
    import time

    from wick.cleanup import actions
    from wick.cleanup.similar import find_similar
    from wick.engine import undo_run
    from wick.formats import register_heif

    register_heif()
    base = Image.effect_mandelbrot((1200, 900), (-2, -1.2, 1, 1.2), 80).convert("RGB")
    a = tmp_path / "import"
    a.mkdir()
    base.save(a / "IMG_0001.JPG", quality=95)
    base.save(a / "IMG_0001.HEIC", quality=60)
    b = tmp_path / "phone copy"
    b.mkdir()
    base.resize((600, 450)).save(b / "IMG_0002.JPG", quality=90)
    (b / "IMG_0002.MOV").write_bytes(os.urandom(50_000))
    t = time.time() - 3600
    os.utime(a / "IMG_0001.JPG", (t, t))
    res = find_similar([tmp_path], strictness="very", min_size=0)
    by = {c.path.name: c for c in res.items}
    assert set(by) == {"IMG_0001.HEIC", "IMG_0002.JPG"}
    assert all(c.keeper.name == "IMG_0001.JPG" for c in by.values())
    assert "same photo saved as HEIC and JPG" in by["IMG_0001.HEIC"].detail
    assert "companions" not in by["IMG_0001.HEIC"].extra              # no video next to it
    live = by["IMG_0002.JPG"]
    assert live.extra["companions"] == [str(b / "IMG_0002.MOV")] and "Live Photo" in live.detail
    assert live.size == (b / "IMG_0002.JPG").stat().st_size + 50_000
    outcomes, run_id = actions.apply([live], actions.TRASH, "similar", tmp_path)
    assert outcomes[0].ok and not (b / "IMG_0002.JPG").exists() and not (b / "IMG_0002.MOV").exists()
    assert undo_run(run_id) == (2, 0)
    assert (b / "IMG_0002.JPG").exists() and (b / "IMG_0002.MOV").exists()
