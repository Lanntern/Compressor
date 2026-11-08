"""Format conversion, metadata cleanup and the Home overview."""

import os
from pathlib import Path

import pytest
from PIL import Image

from compressor import convert, metaclean, overview
from compressor.engine import DONE, process_convert, process_metadata, run_job, undo_run
from compressor.plan import Plan
from compressor.scanner import scan
from compressor.settings import Settings

from .conftest import ffmpeg, needs_ffmpeg, needs_image_tools, needs_pandoc


def _gps_jpeg(path: Path) -> Path:
    ex = Image.Exif()
    ex[0x010F] = "Cam"
    ex[0x0110] = "Model 1"
    ex[0x013B] = "Jane"
    ex.get_ifd(0x8769)[0x9003] = "2024:05:01 10:00:00"
    gps = ex.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (59.0, 19.0, 45.0), "E", (18.0, 4.0, 7.0)
    Image.radial_gradient("L").convert("RGB").resize((320, 240)).save(path, quality=90, exif=ex.tobytes())
    return path


def _convert_all(d: Path, opts: convert.ConvertOptions, s: Settings):
    result = scan(d, s)
    items = []
    for e in result.entries:
        t = opts.target_for(e.kind)
        if t:
            items.append((e, Plan(True, convert.describe(e.kind, opts), t[1], route=t[0])))
    return run_job(items, s, d, processor=lambda *a: process_convert(*a, opts=opts), tool="convert")


@needs_image_tools
@pytest.mark.parametrize("target", [t for t in convert.IMAGE_TARGETS if t not in ("mp4", "svg")])   # (see below)
def test_convert_images(media_dir, target):
    """Every image format (MP4 takes only animations and SVG only drawings: tested on their own)."""
    _gps_jpeg(media_dir / "photo.jpg")
    rgba = Image.new("RGBA", (64, 48), (255, 0, 0, 128))
    rgba.save(media_dir / "alpha.png")
    frames = [Image.new("RGB", (40, 30), c) for c in ("red", "green", "blue")]
    frames[0].save(media_dir / "anim.gif", save_all=True, append_images=frames[1:], duration=100, loop=0)
    before = sorted(p.name for p in media_dir.iterdir())
    s = Settings(output_mode="beside", min_image_kb=0)
    results, run_id = _convert_all(media_dir, convert.ConvertOptions(image_target=target), s)
    bad = {r.entry.path.name: r.message for r in results if r.status != DONE}
    assert not bad
    ext = convert.IMAGE_TARGETS[target][1]
    for r in results:
        assert r.output.suffix == ext and r.output.exists()
        assert r.entry.path.exists()                       # originals kept in "beside" mode
    if target in ("gif", "webp", "png", "avif"):
        with Image.open(next(r.output for r in results if r.entry.path.name == "anim.gif")) as im:
            assert getattr(im, "n_frames", 1) == 3         # animation kept
    undo_run(run_id)
    assert sorted(p.name for p in media_dir.iterdir()) == before


@needs_ffmpeg
def test_convert_video_to_h264_and_vp9(media_dir):
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2", "-f", "lavfi", "-i", "sine=f=440:d=2",
           "-c:v", "mpeg4", "-c:a", "pcm_s16le", "clip.avi", cwd=media_dir)
    for target, codec in (("mp4_h264", "h264"), ("webm_vp9", "vp9")):
        s = Settings(output_mode="beside", min_video_mb=0, video_encoder="cpu")
        results, _ = _convert_all(media_dir, convert.ConvertOptions(image_target="none", video_target=target), s)
        r = next(r for r in results if r.entry.path.name == "clip.avi")
        assert r.status == DONE, r.message
        from compressor.probe import probe_video

        assert probe_video(r.output).codec == codec


@pytest.mark.parametrize("fmt", ["jpg", "png", "webp"])
def test_metadata_cleanup_keeps_image_data(media_dir, fmt):
    src = _gps_jpeg(media_dir / "photo.jpg")
    if fmt != "jpg":
        with Image.open(src) as im:
            exif = im.info["exif"]
            p = media_dir / f"photo.{fmt}"
            im.save(p, exif=exif)
        src.unlink()
        src = p
    summary = metaclean.summarise(src)
    assert summary.gps.startswith("59.") and "Cam" in summary.camera and summary.taken.startswith("2024")
    original_payload = metaclean._payload(src.read_bytes(), metaclean.kind_of(src))
    s = Settings(min_image_kb=0)
    result = scan(media_dir, s)
    items = [(e, Plan(True, "metadata", e.path.suffix.lower(), route="metadata")) for e in result.entries]
    results, run_id = run_job(items, s, media_dir,
                              processor=lambda *a: process_metadata(*a, policy="strip_all_keep_date"),
                              tool="metadata")
    assert results[0].status == DONE, results[0].message
    after = metaclean.summarise(src)
    assert not after.gps and not after.camera and after.taken.startswith("2024")   # date taken kept
    assert metaclean._payload(src.read_bytes(), metaclean.kind_of(src)) == original_payload
    undo_run(run_id)
    assert metaclean.summarise(src).gps.startswith("59.")


@needs_ffmpeg
def test_metadata_cleanup_video(media_dir):
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=1", "-metadata", "location=+59.3293+018.0686/",
           "-metadata", "creation_time=2024-05-01T10:00:00Z", "-c:v", "libx264", "v.mp4", cwd=media_dir)
    v = media_dir / "v.mp4"
    assert metaclean.summarise(v).gps
    out = metaclean.clean(v, "strip_gps")
    try:
        os.replace(out, v)
        s = metaclean.summarise(v)
        assert not s.gps and s.taken == "2024-05-01 10:00"
    finally:
        if out.exists():
            out.unlink()


def test_overview_ranks_by_confidence(tmp_path):
    from compressor.formats import ImageInfo
    from compressor.probe import StreamInfo, VideoInfo
    from compressor.scanner import FileEntry

    s = Settings()
    big_jpeg = FileEntry(tmp_path / "a.jpg", 10_000_000, "jpeg", 0, image=ImageInfo(4000, 3000, 1, 8, 1, False))
    heic = FileEntry(tmp_path / "b.heic", 12_000_000, "heic", 0, image=ImageInfo(4000, 3000, 1, 8, 1, False))
    p1, p2 = __import__("compressor.plan", fromlist=["plan_entry"]).plan_entry(big_jpeg, s), None
    assert overview.confidence(big_jpeg, p1)[0] == overview.HIGH
    p2 = Plan(True, "x", ".jxl", 6_000_000, route="jxl")
    assert overview.confidence(heic, p2)[0] == overview.LOW
    hevc = FileEntry(tmp_path / "c.mp4", 50_000_000, "video", 0,
                     video=VideoInfo("hevc", 1920, 1080, "yuv420p", 30, 60, 6_000_000, 0, [StreamInfo(0, "video", "hevc")]))
    assert overview._efficient_bucket(hevc, Plan(False))[0].startswith("Modern videos")


@needs_image_tools
def test_jpeg_xl_is_an_input_for_convert(media_dir):
    """JPEG XL files are listed (by Convert only), read from their headers, decoded upright, and a JPEG XL made
    losslessly from a JPEG turns back into that exact JPEG."""
    import subprocess

    from compressor import tools
    from compressor.formats import JXL, read_jxl_info

    cjxl = tools.require("cjxl")
    src = _gps_jpeg(media_dir / "orig.jpg")
    with Image.open(src) as im:                       # the same photo, stored sideways (EXIF orientation 6)
        ex = im.getexif()
        ex[0x0112] = 6
        im.save(media_dir / "turned.jpg", quality=90, exif=ex.tobytes())
    subprocess.run([cjxl, str(src), str(media_dir / "exact.jxl"), "--quiet"], check=True)
    subprocess.run([cjxl, str(media_dir / "turned.jpg"), str(media_dir / "turned.jxl"), "--lossless_jpeg=0", "-q", "85",
                    "--quiet"], check=True)
    (media_dir / "turned.jpg").unlink()
    from compressor import metadata as md

    turned_path = media_dir / "turned.jxl"         # (cjxl drops EXIF when encoding lossily: added the way we write it)
    turned_path.write_bytes(md.jxl_write_meta(turned_path.read_bytes(), md.Meta(ex.tobytes()[6:], None, None)))
    keep = src.read_bytes()
    src.unlink()

    info = read_jxl_info(media_dir / "exact.jxl")
    assert info is not None and (info.width, info.height) == (320, 240) and info.reconstructs_jpeg
    turned = read_jxl_info(media_dir / "turned.jxl")
    assert turned is not None and (turned.width, turned.height) == (240, 320)     # as displayed

    assert not scan(media_dir, Settings(min_image_kb=0)).entries                  # Compress: already efficient
    s = Settings(output_mode="beside", min_image_kb=0, scan_jxl=True)
    entries = scan(media_dir, s).entries
    assert sorted(e.path.name for e in entries) == ["exact.jxl", "turned.jxl"] and {e.kind for e in entries} == {JXL}

    results, _ = _convert_all(media_dir, convert.ConvertOptions(image_target="jpeg"), s)
    assert all(r.status == DONE for r in results), [r.message for r in results]
    assert (media_dir / "exact.jpg").read_bytes() == keep                            # byte for byte
    with Image.open(media_dir / "turned.jpg") as im:
        assert im.size == (240, 320) and im.getexif().get(0x0112, 1) == 1          # upright, not turned twice
        assert im.getexif().get(0x0110) == "Model 1"                               # metadata carried over

    for p in media_dir.glob("*.jpg"):
        p.unlink()
    results, _ = _convert_all(media_dir, convert.ConvertOptions(image_target="png"), s)
    assert all(r.status == DONE for r in results), [r.message for r in results]
    with Image.open(media_dir / "turned.png") as im:
        assert im.size == (240, 320) and im.getexif().get(0x0112, 1) == 1


def test_convert_with_a_size_limit(media_dir):
    Image.radial_gradient("L").convert("RGB").resize((800, 400)).save(media_dir / "wide.png")
    frames = [Image.new("RGB", (400, 200), c) for c in ("red", "green")]
    frames[0].save(media_dir / "anim.gif", save_all=True, append_images=frames[1:], duration=100, loop=0)
    s = Settings(output_mode="beside", min_image_kb=0)
    results, _ = _convert_all(media_dir, convert.ConvertOptions(image_target="webp", max_side=200), s)
    sizes = {}
    for r in results:
        assert r.status == DONE, r.message
        with Image.open(r.output) as im:
            sizes[r.entry.path.name] = (im.size, getattr(im, "n_frames", 1))
    assert sizes == {"wide.png": ((200, 100), 1), "anim.gif": ((200, 100), 2)}
    assert convert.describe("png", convert.ConvertOptions(image_target="webp", max_side=200)) == \
        "WebP, quality 90, up to 200 px"


@needs_ffmpeg
def test_convert_video_resolution_and_frame_rate(media_dir):
    from compressor.probe import probe_video

    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=50:d=2", "-c:v", "mpeg4", "clip.avi", cwd=media_dir)
    opts = convert.ConvertOptions(image_target="none", video_target="mp4_h264", video_height=180, video_fps=25)
    results, _ = _convert_all(media_dir, opts, Settings(output_mode="beside", min_video_mb=0, video_encoder="cpu"))
    r = results[0]
    assert r.status == DONE, r.message
    info = probe_video(r.output)
    assert (info.width, info.height) == (320, 180) and abs(info.fps - 25) < 0.5
    assert convert.describe("video", opts) == "MP4, 180p, 25 fps"


@needs_ffmpeg
def test_convert_audio_bitrate_rate_and_channels(media_dir):
    from compressor.probe import probe_audio

    ffmpeg("-f", "lavfi", "-i", "sine=f=440:d=3", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", "tone.wav",
           cwd=media_dir)
    opts = convert.ConvertOptions(image_target="none", audio_target="mp3", audio_kbps=96, sample_rate=22050,
                                  channels=1)
    results, _ = _convert_all(media_dir, opts, Settings(output_mode="beside", min_audio_kb=0))
    r = results[0]
    assert r.status == DONE, r.message
    info = probe_audio(r.output)
    assert info.channels == 1 and info.sample_rate == 22050 and abs(info.bit_rate - 96000) < 8000
    assert convert.describe("audio", opts) == "MP3, 96 kbps, 22.05 kHz, mono"


def _psd(path: Path, w: int = 4, h: int = 2) -> None:
    """A minimal Photoshop file: RGB, 8 bits, no layers, raw planar pixels (red)."""
    import struct

    head = b"8BPS" + struct.pack(">H6xHIIHH", 1, 3, h, w, 8, 3)
    planes = bytes([255]) * (w * h) + bytes(w * h) * 2
    path.write_bytes(head + struct.pack(">III", 0, 0, 0) + struct.pack(">H", 0) + planes)


def test_convert_reads_raw_psd_svg_and_icons(media_dir, monkeypatch):
    import sys
    import types

    import numpy as np

    _psd(media_dir / "art.psd")
    Image.new("RGB", (48, 48), "blue").save(media_dir / "app.ico", sizes=[(16, 16), (48, 48)])
    (media_dir / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 32">'
                                        '<rect width="64" height="32" fill="#0a84ff"/></svg>')
    # A stand-in for LibRaw (no RAW sample in the repository): a portrait photo, 30×20 pixels.
    ex = Image.Exif()
    ex[0x010F] = "Cam"
    Image.new("RGB", (8, 8)).save(media_dir / "photo.nef", "TIFF", exif=ex.tobytes())

    class Raw:
        sizes = types.SimpleNamespace(width=30, height=20, flip=6)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def postprocess(self, **_):
            return np.full((30, 20, 3), 128, dtype=np.uint8)

    monkeypatch.setitem(sys.modules, "rawpy", types.SimpleNamespace(imread=lambda _p: Raw()))
    s = Settings(output_mode="beside", min_image_kb=0, convert_inputs=True)
    assert {e.path.name: e.kind for e in scan(media_dir, s).entries} == {
        "art.psd": "psd", "app.ico": "ico", "logo.svg": "svg", "photo.nef": "raw"}
    results, _ = _convert_all(media_dir, convert.ConvertOptions(image_target="png"), s)
    got = {}
    for r in results:
        assert r.status == DONE, (r.entry.path.name, r.message)
        with Image.open(r.output) as im:
            got[r.entry.path.name] = (im.size, im.convert("RGB").getpixel((0, 0)))
            if r.entry.path.name == "photo.nef":
                assert im.getexif().get(0x010F) == "Cam"                 # its EXIF came along
    assert got["art.psd"] == ((4, 2), (255, 0, 0))
    assert got["app.ico"][0] == (48, 48)                                  # the largest picture in the icon
    assert got["photo.nef"][0] == (20, 30)
    assert got["logo.svg"][0] == (64, 32)
    for r in results:
        r.output.unlink()
    # A drawing is drawn at the size asked for, larger too.
    big, _ = _convert_all(media_dir, convert.ConvertOptions(image_target="png", max_side=256), s)
    with Image.open(next(r.output for r in big if r.entry.path.name == "logo.svg")) as im:
        assert im.size == (256, 128)


@needs_ffmpeg
def test_videos_become_animations_and_back(media_dir):
    from compressor.probe import probe_video

    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=2", "-c:v", "libx264", "clip.mp4", cwd=media_dir)
    s = Settings(output_mode="beside", min_video_mb=0, min_image_kb=0)
    for target, frames in (("gif", 30), ("webp_anim", 48)):
        results, _ = _convert_all(media_dir, convert.ConvertOptions(image_target="none", video_target=target), s)
        r = next(r for r in results if r.entry.path.name == "clip.mp4")
        assert r.status == DONE, r.message
        with Image.open(r.output) as im:
            assert im.size == (640, 360) and im.n_frames == frames         # (under 480p: kept; 15 / 24 fps)
        r.output.rename(media_dir / f"anim-{target}{r.output.suffix}")
    results, _ = _convert_all(media_dir, convert.ConvertOptions(image_target="mp4", video_target="none"), s)
    made = {r.entry.path.name: r for r in results}
    for name in ("anim-gif.gif", "anim-webp_anim.webp"):
        assert made[name].status == DONE, made[name].message
        info = probe_video(made[name].output)
        assert info.codec == "h264" and (info.width, info.height) == (640, 360) and abs(info.duration - 2) < 0.3


@needs_pandoc
def test_documents_convert_with_pandoc(media_dir):
    import zipfile

    (media_dir / "notes.md").write_text("# Notes\n\nSome *text*.\n\n![remote](https://example.com/x.png)\n")
    s = Settings(output_mode="beside", convert_inputs=True)
    from compressor import office

    for target in convert.DOCUMENT_TARGETS:
        if target == "md" or (target == "pdf" and not office.converter_for(".md")):
            continue          # (PDF needs Microsoft Word or LibreOffice: test_office_documents_to_pdf covers it)
        results, _ = _convert_all(media_dir, convert.ConvertOptions(document_target=target), s)
        r = next(r for r in results if r.entry.path.name == "notes.md")
        assert r.status == DONE, (target, r.message)
        if target == "docx":
            with zipfile.ZipFile(r.output) as z:
                assert "Notes" in z.read("word/document.xml").decode()
                assert not any(n.startswith("word/media/") for n in z.namelist())   # nothing fetched online
        r.output.unlink()


def test_addons_are_checked_before_use(tmp_path, monkeypatch):
    import hashlib
    import zipfile

    from compressor import addons

    monkeypatch.setenv("COMPRESSOR_DATA_DIR", str(tmp_path / "data"))
    archive = tmp_path / "tool.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("tool-1.0/tool.exe", b"MZ fake program")
        z.writestr("tool-1.0/COPYRIGHT.txt", b"licence")
    good = addons.Addon("tool", "1.0", archive.as_uri(), hashlib.sha256(archive.read_bytes()).hexdigest(),
                        archive.stat().st_size, "tool-1.0/tool.exe", "tool-1.0/COPYRIGHT.txt", "")
    exe = addons.download(good)
    assert exe.read_bytes() == b"MZ fake program" and (exe.parent / "LICENSE-tool.txt").exists()
    exe.unlink()
    bad = addons.Addon(**{**good.__dict__, "sha256": "0" * 64})
    with pytest.raises(ValueError, match="fingerprint"):
        addons.download(bad)
    assert not exe.exists() and not list(exe.parent.glob(".download-*"))


@needs_ffmpeg
def test_the_sound_of_a_video(media_dir):
    from compressor.probe import probe_audio

    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3",
           "-c:v", "libx264", "-c:a", "aac", "-shortest", "clip.mp4", cwd=media_dir)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=2", "-c:v", "libx264", "silent.mp4", cwd=media_dir)
    s = Settings(output_mode="beside", min_video_mb=0)
    results, _ = _convert_all(media_dir, convert.ConvertOptions(image_target="none", video_target="audio_aac"), s)
    got = {r.entry.path.name: r for r in results}
    r = got["clip.mp4"]
    assert r.status == DONE and r.output.suffix == ".m4a" and "copied" in r.message      # (already AAC: copied)
    assert probe_audio(r.output).codec == "aac"
    assert got["silent.mp4"].status != DONE and "no sound" in got["silent.mp4"].message
    r.output.unlink()
    results, _ = _convert_all(media_dir, convert.ConvertOptions(image_target="none", video_target="audio_mp3"), s)
    r = next(r for r in results if r.entry.path.name == "clip.mp4")
    assert r.status == DONE and probe_audio(r.output).codec == "mp3"


def test_turning_pictures_without_losing_quality(media_dir):
    from compressor import metadata as md

    img = Image.radial_gradient("L").convert("RGB").resize((64, 32))
    img.save(media_dir / "photo.jpg", quality=90)
    img.save(media_dir / "shot.png")
    s = Settings(output_mode="beside", min_image_kb=0, output_suffix="_turned")
    opts = convert.ConvertOptions(image_target="keep", turn=90)
    assert convert.describe("jpeg", opts) == "Same format, turned right"
    entries = {e.path.name: e for e in scan(media_dir, s).entries}
    data = (media_dir / "photo.jpg").read_bytes()
    r = process_convert(entries["photo.jpg"], Plan(True, "", ".jpg", route="keep"), s, None, opts=opts)
    assert r.status == DONE, r.message
    out = r.output.read_bytes()
    assert out[md._jpeg_segments(out)[1]:] == data[md._jpeg_segments(data)[1]:]     # the very same picture data
    with Image.open(r.output) as im:
        assert im.getexif()[0x0112] == 6
    r = process_convert(entries["shot.png"], Plan(True, "", ".png", route="keep"), s, None,
                        opts=convert.ConvertOptions(image_target="keep", turn=270, flip="h"))
    assert r.status == DONE
    with Image.open(r.output) as im:
        expected = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT).transpose(Image.Transpose.ROTATE_90)
        assert im.size == (32, 64) and im.convert("RGB").tobytes() == expected.tobytes()   # lossless
    assert convert.orientation_after(6, convert.ConvertOptions(turn=90)) == 3


@needs_ffmpeg
def test_turning_a_video_without_re_encoding(media_dir):
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=160x96:r=25:d=2", "-c:v", "libx264", "clip.mp4", cwd=media_dir)
    s = Settings(output_mode="beside", min_video_mb=0, output_suffix="_turned")
    entry = scan(media_dir, s).entries[0]
    r = process_convert(entry, Plan(True, "", ".mp4", route="keep"), s, None,
                        opts=convert.ConvertOptions(video_target="keep", turn=90))
    assert r.status == DONE, r.message
    assert convert.video_rotation(r.output) == -90                     # (ffmpeg counts counter-clockwise)
    assert abs(r.output.stat().st_size - entry.size) < 4096            # copied, not re-encoded


@pytest.mark.skipif(not __import__("compressor.office", fromlist=["libreoffice"]).libreoffice(),
                    reason="LibreOffice not installed")
def test_office_documents_to_pdf(media_dir, tmp_path):
    import subprocess

    from compressor import office, pdftools

    (tmp_path / "letter.txt").write_text("Dear reader,\n\nHello.\n")
    soffice = office.libreoffice()
    subprocess.run([soffice, "--headless", f"-env:UserInstallation={(tmp_path / 'p').as_uri()}", "--convert-to",
                    "odt", "--outdir", str(media_dir), str(tmp_path / "letter.txt")], check=True,
                   capture_output=True, timeout=300)
    s = Settings(output_mode="beside", convert_inputs=True)
    results, _ = _convert_all(media_dir, convert.ConvertOptions(document_target="pdf"), s)
    r = next(r for r in results if r.entry.path.name == "letter.odt")
    assert r.status == DONE, r.message
    assert r.output.suffix == ".pdf" and pdftools.page_count(r.output) == 1
