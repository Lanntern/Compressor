"""Speed test: how fast this computer compresses, old vs new parallelism.

Generates sample photos and videos in a temporary folder, compresses them into another temporary folder
(nothing of yours is touched), and compares
* images: 4 at once (the old fixed limit) vs the automatic number for this CPU;
* videos: one at a time vs two at once (only with an NVIDIA GPU, which has two encoders).
Nothing is added to History or to the learned size estimates; all temporary files are removed.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import calibration, journal, tools
from .engine import DONE, run_job
from .plan import plan_entry
from .scanner import scan
from .settings import Settings

Progress = Callable[[str], None]


def _photos(folder: Path, n: int) -> None:
    from PIL import Image, ImageChops, ImageFilter

    base = Image.effect_mandelbrot((3000, 2000), (-2.2, -1.25, 0.8, 1.25), 120).convert("RGB")
    for i in range(n):
        noise = Image.effect_noise((3000, 2000), 18 + i % 7).convert("RGB")
        img = ImageChops.add(base.filter(ImageFilter.GaussianBlur(i % 3)), noise, scale=1.6, offset=-40)
        img.save(folder / f"photo_{i:02d}.jpg", quality=92)


def _videos(folder: Path, n: int, seconds: int = 12) -> None:
    ffmpeg = tools.require("ffmpeg")
    for i in range(n):
        tools.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                   f"testsrc2=s=1920x1080:r=30:d={seconds},noise=alls={8 + i * 3}:allf=t", "-c:v", "libx264",
                   "-preset", "veryfast", "-crf", "18", str(folder / f"clip_{i}.mp4")], timeout=600)


def _timed(src: Path, out: Path, s: Settings, kind: str) -> tuple[float, int]:
    s = dataclasses.replace(s, output_mode="folder", output_dir=str(out), require_savings=False,
                            min_image_kb=0, min_video_mb=0)
    entries = [e for e in scan([src], s).entries if (e.kind == "video") == (kind == "video")]
    items = [(e, p) for e in entries if (p := plan_entry(e, s)).convert]
    t = time.perf_counter()
    results, run_id = run_job(items, s, src)
    elapsed = time.perf_counter() - t
    if run_id:
        try:
            (journal.runs_dir() / f"{run_id}.jsonl").unlink()   # not a real job: keep History clean
        except OSError:
            pass
    shutil.rmtree(out, ignore_errors=True)
    return elapsed, sum(r.status == DONE for r in results)


def run(progress: Progress | None = None, photos: int = 16, videos: int = 4,
        cancel: threading.Event | None = None) -> str:
    """Run the comparison and return a short plain-text report."""
    say = progress or (lambda m: None)
    from .video import choose_encoder, parallel_videos

    base = Settings(image_format="jxl", jpeg_mode="lossy", image_quality=85, video_codec="hevc",
                    video_quality="balanced")
    lines = [f"CPU threads: {os.cpu_count()} · memory: {tools.total_memory_gb():.0f} GB"]
    calibration.set_enabled(False)
    work = Path(tempfile.mkdtemp(prefix="wick-speedtest-"))
    try:
        pics, clips = work / "photos", work / "videos"
        pics.mkdir()
        clips.mkdir()
        say("Making sample photos…")
        _photos(pics, photos)
        auto = base.image_workers()
        old = max(1, min(4, (os.cpu_count() or 2) // 2))          # the previous fixed rule
        configs = ((f"before ({old} at once)", dataclasses.replace(base, workers=old, encoder_threads=-1)),
                   (f"now ({auto} at once)", base))
        for label, cfg in configs:
            if cancel is not None and cancel.is_set():
                break
            say(f"Photos, {label}…")
            secs, done = _timed(pics, work / f"out-img-{label[:3]}", cfg, "image")
            lines.append(f"{photos} photos (6 MP JPEG → JPEG XL), {label}: {secs:.1f} s"
                         + ("" if done == photos else f" ({done} done)"))
        encoder = choose_encoder(base.video_codec, base.video_encoder) if tools.find_tool("ffmpeg") else ""
        parallel = parallel_videos(base) if encoder else 1
        if parallel > 1 and not (cancel is not None and cancel.is_set()):   # only worth timing with two encoders
            say("Making sample videos…")
            _videos(clips, videos)
            for label, n in (("one at a time", 1), ("two at once", 2)):
                if cancel is not None and cancel.is_set():
                    break
                say(f"Videos, {label}…")
                secs, done = _timed(clips, work / f"out-vid-{n}", dataclasses.replace(base, video_jobs=n), "video")
                lines.append(f"{videos} videos (1080p, 12 s, H.264 → HEVC on {encoder}), {label}: {secs:.1f} s")
        elif encoder:
            lines.append(f"Videos: one at a time here ({encoder} already uses every CPU core), so not timed.")
    finally:
        calibration.set_enabled(True)
        shutil.rmtree(work, ignore_errors=True)
    return "\n".join(lines)


__all__ = ["run"]
