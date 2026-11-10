"""Scene-by-scene encoding, carrying on after a stop, film-grain detection and the extra quality measures."""

import subprocess
import threading
from pathlib import Path

import pytest

from compressor import chunked, quality, tools, video
from compressor.plan import Plan
from compressor.probe import probe_video
from compressor.settings import Settings
from tests.conftest import have

pytestmark = pytest.mark.skipif(not have("ffmpeg", "ffprobe"), reason="needs ffmpeg")


def _frames(path: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                          "stream=nb_read_frames", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return int(out.stdout.strip())


@pytest.fixture
def scenes(tmp_path: Path) -> Path:
    """12 s, 25 fps, 320x180: four 3-second scenes that look nothing alike, with sound."""
    src = tmp_path / "scenes.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=3",
                    "-f", "lavfi", "-i", "mandelbrot=s=320x180:r=25",
                    "-f", "lavfi", "-i", "smptebars=s=320x180:r=25:d=3",
                    "-f", "lavfi", "-i", "life=s=320x180:r=25:mold=10:ratio=0.1",
                    "-f", "lavfi", "-i", "sine=f=440:d=12",
                    "-filter_complex", "[1:v]trim=duration=3,setpts=PTS-STARTPTS[m];"
                    "[3:v]trim=duration=3,setpts=PTS-STARTPTS,format=yuv420p[l];"
                    "[0:v][m][2:v][l]concat=n=4:v=1:a=0,format=yuv420p[v]",
                    "-map", "[v]", "-map", "4:a", "-c:v", "libx264", "-crf", "16", "-c:a", "aac", str(src)],
                   check=True)
    return src


@pytest.fixture
def small_parts(monkeypatch):
    monkeypatch.setattr(chunked, "MIN_VIDEO_SECONDS", 5)
    monkeypatch.setattr(chunked, "MIN_PART_SECONDS", 1.5)
    monkeypatch.setattr(chunked, "MAX_PART_SECONDS", 4)


def test_parts_split_at_scene_changes_halfway_between_frames():
    parts = chunked.plan_parts(120.0, 25.0, [15.0, 15.4, 40.0, 119.0])
    starts = [a for a, _ in parts]
    rounded = [round(a, 2) for a in starts]
    assert starts[0] == 0.0 and parts[-1][1] == 0.0
    assert all(abs(a * 25 - round(a * 25 - 0.5) - 0.5) < 1e-6 for a in starts[1:])   # mid-frame
    assert 14.98 in rounded and 39.98 in rounded
    assert 15.38 not in rounded and 118.98 not in rounded          # too close to a cut / the end
    assert len(parts) == 4                                          # the 80 s scene split in two
    assert all(b - a <= chunked.MAX_PART_SECONDS + 0.05 for a, b in parts[:-1])
    assert all(a < b for (a, b) in parts[:-1]) and all(parts[i][1] == parts[i + 1][0] for i in range(len(parts) - 1))


@pytest.mark.skipif(not have("ffmpeg"), reason="needs ffmpeg")
def test_scene_by_scene_is_frame_exact_and_carries_on(scenes, small_parts, monkeypatch):
    if not video.encoder_works("libsvtav1"):
        pytest.skip("no SVT-AV1")
    info = probe_video(scenes)
    s = Settings(video_codec="av1", video_encoder="cpu", video_quality="custom", video_crf=45)
    s.validate()
    assert chunked.applies(info, s, "libsvtav1")
    assert [round(t) for t in chunked.scene_cuts(scenes, info)] == [3, 6, 9]
    # Stop while the third part is being encoded: the first two are kept...
    real = tools.run_ffmpeg_progress
    started: list[str] = []
    cancel = threading.Event()

    def counting(args, duration, on_progress=None, c=None):
        if "part-" in str(args[-1]):
            started.append(str(args[-1]))
            if len(started) == 3:
                cancel.set()
        return real(args, duration, on_progress, c)

    monkeypatch.setattr(tools, "run_ffmpeg_progress", counting)
    with pytest.raises(tools.Cancelled):
        video.convert_video(scenes, info, Plan(True, target_ext=".mkv"), s, cancel=cancel)
    folder = next(scenes.parent.glob(chunked.PARTS_PREFIX + "*"))
    assert len(list(folder.glob("part-*.ok"))) == 2
    # ...and the next run encodes only the rest. Every frame is there, in step with the original.
    started.clear()
    plan = Plan(True, target_ext=".mkv")
    out, _ = video.convert_video(scenes, info, plan, s)
    assert len(started) == 2 and "2 carried over" in plan.outcome
    assert _frames(out) == _frames(scenes) == 300
    assert len(probe_video(out).audio_streams()) == 1
    stamps = sorted(float(t) for t in subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0",
         str(out)], capture_output=True, text=True).stdout.split())
    assert {round(b - a, 3) for a, b in zip(stamps, stamps[1:], strict=False)} == {0.04}   # no gap or overlap
    assert not folder.exists()                                     # parts removed once the video is done


def test_parts_folders_are_never_scanned(tmp_path):
    from compressor.scanner import scan

    folder = tmp_path / (chunked.PARTS_PREFIX + "abc")
    folder.mkdir()
    (folder / "part-0000.mkv").write_bytes(b"x" * 20_000_000)
    s = Settings(include_hidden=True, min_video_mb=0)
    assert not [e for e in scan([tmp_path], s, probe=False).entries if chunked.PARTS_PREFIX in str(e.path)]


def test_old_parts_are_pruned(tmp_path):
    folder = tmp_path / (chunked.PARTS_PREFIX + "old")
    folder.mkdir()
    chunked._note_folder(folder)
    assert chunked.prune(max_age_days=1) == 0 and folder.exists()
    assert chunked.prune(max_age_days=-1) >= 1 and not folder.exists()


def test_grain_is_measured(tmp_path):
    """The noise estimate follows the real noise; clean video gets no grain synthesis."""
    s = Settings()
    levels = []
    for strength in (0, 5, 12):
        p = tmp_path / f"g{strength}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=0x707070:s=640x360:d=2,drawbox=x=100:y=80:w=200:h=120:color=0xc0a080:t=fill,"
                        f"noise=alls={strength}:allf=t", "-c:v", "libx264", "-crf", "2", "-pix_fmt", "yuv420p",
                        str(p)], check=True)
        info = probe_video(p)
        levels.append(video.grain_level(p, info, s, "libsvtav1"))
    assert levels[0] == 0 and 0 < levels[1] < levels[2]
    assert video.grain_level(p, info, s, "hevc_nvenc") == 0                # only SVT-AV1 re-creates grain
    assert video.grain_level(p, info, Settings(video_grain="off"), "libsvtav1") == 0
    args = video.build_args(p, tmp_path / "o.mkv", info, s, "libsvtav1", 30, grain=10, threads=4)
    svt = args[args.index("-svtav1-params") + 1]
    assert svt.endswith("film-grain=10:film-grain-denoise=1:lp=4") and svt.startswith(video.svt_tuning())


def test_quality_measures():
    assert quality.Score("Butteraugli", 0.8).meets("identical") and not quality.Score("Butteraugli", 2.5).meets("good")
    assert quality.worst([quality.Score("Butteraugli", 0.8), quality.Score("Butteraugli", 1.9)]).value == 1.9
    assert quality.worst([quality.Score("VMAF", 97), quality.Score("VMAF", 91)]).value == 91
    assert str(quality.Score("XPSNR", 41.234)) == "XPSNR 41.2 dB"
    assert quality.video_metric("nonsense") == quality.video_metric("auto")
    if not quality._metric_works("XPSNR"):                     # (ffmpeg before 7.1)
        assert quality.video_metric("xpsnr") == quality.video_metric("auto")


@pytest.mark.skipif(not have("butteraugli_main"), reason="needs butteraugli_main")
def test_frame_measures_compare_the_same_moments(scenes):
    """Butteraugli and SSIMULACRA2 score frames taken at the same moments: the video against itself is perfect,
    against a blurred copy it isn't."""
    info = probe_video(scenes)
    size = (info.width, info.height)
    same = quality.score_video_output(scenes, scenes, 1.0, 2.0, size, metric="Butteraugli")
    assert same.value < 0.05
    blurred = scenes.with_name("blurred.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(scenes), "-vf", "gblur=sigma=3", "-c:a", "copy",
                    str(blurred)], check=True)
    assert quality.score_video_output(scenes, blurred, 1.0, 2.0, size, metric="Butteraugli").value > 2
    if quality.image_metric() == "SSIMULACRA2":
        assert quality.score_video_output(scenes, scenes, 1.0, 2.0, size, metric="SSIMULACRA2-video").value > 90
    # A result whose picture starts a little later in its file (room made for an audio encoder's lead-in) is
    # still compared frame for frame, not with the neighbouring frame.
    late = scenes.with_name("late.mkv")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-itsoffset", "0.023", "-i", str(scenes), "-i", str(scenes),
                    "-map", "0:v", "-map", "1:a", "-c", "copy", str(late)], check=True)
    assert quality.score_video_output(scenes, late, 1.0, 2.0, size, metric="Butteraugli").value < 0.05


def test_encoder_settings_are_checked_not_assumed(monkeypatch):
    """SVT-AV1 ignores settings it doesn't know (only warning), so each extra is tried and a warning counts as
    unsupported; the combinations used here must all be accepted."""
    ffmpeg = tools.find_tool("ffmpeg")
    if not video.encoder_works("libsvtav1"):
        pytest.skip("no SVT-AV1")
    assert not video._test_encode(ffmpeg, "libsvtav1", ("-svtav1-params", "no-such-setting=1"))
    tuning = video.svt_tuning()
    assert tuning.startswith("tune=0") or tuning == ""
    used = ":".join(p for p in (tuning, "film-grain=10:film-grain-denoise=1", "lp=2") if p)
    assert video._test_encode(ffmpeg, "libsvtav1", ("-svtav1-params", used))


def test_nvenc_uses_the_best_tuning_the_gpu_takes(monkeypatch):
    """Ultra high quality where the GPU takes it, else high quality at p7, else the basic settings; split-frame
    encoding only for 4K and up, and only where it works. The availability test always uses the basic ones."""
    accepted: set[str] = {"hq"}
    seen: list[tuple] = []

    def fake(ffmpeg, encoder, options=None, size="320x240"):
        seen.append((options, size))
        if options is None:
            return True
        return options[3] in accepted and ("-split_encode_mode" not in options or "split" in accepted)

    monkeypatch.setattr(video, "_test_encode", fake)
    for accepted_now, want in (({"uhq", "hq", "split"}, "uhq"), ({"hq"}, "hq")):
        video.nvenc_tuning.cache_clear()
        video.nvenc_split.cache_clear()
        accepted.clear()
        accepted.update(accepted_now)
        args = video._gpu_quality_args("av1_nvenc", 30, pixels=3840 * 2160)
        assert args[:4] == ["-preset", "p7", "-tune", want] and "-rc-lookahead" in args
        assert ("-split_encode_mode" in args) == ("split" in accepted_now)
        assert "-split_encode_mode" not in video._gpu_quality_args("av1_nvenc", 30, pixels=1920 * 1080)
    assert video._gpu_quality_args("av1_nvenc", 30, tuned=False)[:4] == list(video.NVENC_BASIC)
    video.nvenc_tuning.cache_clear()
    video.nvenc_split.cache_clear()
