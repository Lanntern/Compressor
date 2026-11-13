"""Context-menu compatibility and cancellation regressions without GUI dependencies."""

import subprocess
import tempfile
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wick import convert, quality, steps, tools, video
from wick.formats import ImageInfo
from wick.probe import StreamInfo, VideoInfo
from wick.settings import Settings


def image(name, frames=1, size=(1920, 1080)):
    return SimpleNamespace(path=Path(name), image=ImageInfo(*size, frames=frames))


class FormatSuggestionsTests(unittest.TestCase):
    def test_normal_picture_offers_common_formats(self):
        targets = convert.suggested_image_targets([image("photo.png")])
        self.assertIn("jpeg", targets)
        self.assertIn("webp", targets)
        for key in ("png", "cur", "icns", "ico", "gif", "svg", "mp4", "keep"):
            self.assertNotIn(key, targets)

    def test_icon_sized_picture_can_become_icon_but_not_cursor(self):
        targets = convert.suggested_image_targets([image("logo.png", size=(64, 64))])
        self.assertIn("ico", targets)
        self.assertNotIn("cur", targets)
        targets = convert.suggested_image_targets([image("icon.ico", size=(64, 64))])
        self.assertIn("cur", targets)
        self.assertNotIn("ico", targets)

    def test_animation_menu_preserves_animation(self):
        targets = convert.suggested_image_targets([image("clip.gif", frames=8)])
        self.assertIn("mp4", targets)
        self.assertIn("webp", targets)
        self.assertNotIn("gif", targets)
        self.assertTrue(all(spec[2] for spec in targets.values()))
        targets = convert.suggested_image_targets([image("clip.gif", frames=8), image("still.png")])
        self.assertNotIn("mp4", targets)
        self.assertNotIn("jpeg", targets)

    def test_svg_optimizer_is_only_suggested_for_svg(self):
        self.assertIn("svg", convert.suggested_image_targets([image("drawing.svg")]))
        self.assertNotIn("svg", convert.suggested_image_targets([image("drawing.jpg")]))
        self.assertEqual(convert.suggested_image_targets([]), {})


class CancellationTests(unittest.TestCase):
    def test_cancelled_context_never_starts_another_tool(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(subprocess, "Popen") as start, tools.cancelling(cancel):
            with self.assertRaises(tools.Cancelled):
                tools.run([sys.executable, "-c", "pass"])
            with self.assertRaises(tools.Cancelled):
                tools.run_ffmpeg_progress([sys.executable, "-c", "pass"], 1)
            start.assert_not_called()

    def test_cancel_interrupts_silent_image_tool(self):
        cancel = threading.Event()
        timer = threading.Timer(0.3, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with tools.cancelling(cancel), self.assertRaises(tools.Cancelled):
                tools.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=5)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)

    def test_cancel_interrupts_encoder_without_stdout_updates(self):
        cancel = threading.Event()
        timer = threading.Timer(0.3, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(tools.Cancelled):
                tools.run_ffmpeg_progress([sys.executable, "-c", "import time; time.sleep(30)"], 1, cancel=cancel)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)

    def test_context_restores_parent_cancellation_and_regular_output(self):
        parent, child = threading.Event(), threading.Event()
        child.set()
        with tools.cancelling(parent):
            with tools.cancelling(child), self.assertRaises(tools.Cancelled):
                tools.run([sys.executable, "-c", "pass"])
            result = tools.run([sys.executable, "-c", "print('done')"])
            self.assertEqual(result.stdout.strip(), "done")
        with self.assertRaises(subprocess.TimeoutExpired):
            with tools.cancelling(parent):
                tools.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)

    def test_progress_callback_failure_is_logged(self):
        def broken(_message):
            raise ValueError("UI callback failed")

        with self.assertLogs("wick", level="ERROR") as captured, steps.reporting(broken):
            steps.report("Checking the result")
        self.assertIn("UI callback failed", "\n".join(captured.output))

    def test_cancelled_preview_removes_partial_frame_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sample, frames = root / "sample.mkv", root / "frames"
            frames.mkdir()
            (frames / "partial.png").write_bytes(b"partial frame")
            info = VideoInfo("h264", 32, 24, "yuv420p", 24, 2, 1000, 0,
                             streams=[StreamInfo(0, "video", "h264")])
            with patch.object(video, "choose_encoder", return_value="libx265"), \
                    patch.object(video, "temp_name", return_value=sample), \
                    patch.object(video, "build_args", return_value=["ffmpeg"]), \
                    patch.object(tools, "run", side_effect=lambda *a, **k: sample.write_bytes(b"sample")), \
                    patch.object(quality, "score_video_segment", return_value=quality.Score("VMAF", 96)), \
                    patch.object(video, "_frame_png", return_value=b"png"), \
                    patch.object(video, "_frame_sequence", side_effect=tools.Cancelled), \
                    patch.object(tempfile, "mkdtemp", return_value=str(frames)):
                with self.assertRaises(tools.Cancelled):
                    video.preview_video(root / "input.mp4", info, Settings(video_quality="custom"))
            self.assertFalse(sample.exists())
            self.assertFalse(frames.exists())


if __name__ == "__main__":
    unittest.main()
