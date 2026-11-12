"""Video quality and growth regressions; runnable with pytest or unittest without GUI dependencies."""

import dataclasses
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wick import chunked, convert, engine, index, probe, quality, tools, video
from wick.images import NotWorthIt
from wick.plan import Plan
from wick.settings import Settings


def clip():
    return probe.VideoInfo("h264", 1920, 1080, "yuv420p", 24, 15, 9_000_000, 0,
                           streams=[probe.StreamInfo(0, "video", "h264")],
                           video_bit_rate=5_500_000)


class VideoBudgetTests(unittest.TestCase):
    def test_budget_excludes_soundtrack_and_can_be_disabled(self):
        info = clip()
        s = Settings()
        self.assertEqual(video.source_rate_limit(info, s, "av1_nvenc"), 4702)
        info.bit_rate = 100_000_000
        self.assertEqual(video.source_rate_limit(info, s, "av1_nvenc"), 4702)
        self.assertEqual(video.source_rate_limit(info, dataclasses.replace(s, require_savings=False), "av1_nvenc"), 0)
        info.video_bit_rate = 0
        self.assertEqual(video.source_rate_limit(info, s, "av1_nvenc"), 0)

    def test_caps_use_each_encoders_supported_rate_control(self):
        with patch.object(tools, "require", return_value="ffmpeg"), \
                patch.object(video, "_nvenc_args", return_value=["-preset", "p7"]), \
                patch.object(video, "svt_tuning", return_value="tune=0"):
            for encoder in ("av1_nvenc", "hevc_nvenc", "libx265", "libsvtav1"):
                with self.subTest(encoder=encoder):
                    args = video.build_args(Path("in.mp4"), Path("out.mp4"), clip(), Settings(), encoder)
                    if encoder == "libsvtav1":
                        self.assertIn("mbr=4702", args[args.index("-svtav1-params") + 1])
                    else:
                        self.assertEqual(args[args.index("-maxrate") + 1], "4702k")
                    self.assertNotIn("hqdn3d", " ".join(args))
                    self.assertNotIn("nlmeans", " ".join(args))
            args = video.build_args(Path("in.mp4"), Path("out.mp4"), clip(), Settings(), "libx265",
                                    fit=(1000, 128, (1920, 1080)))
            self.assertEqual(args.count("-maxrate"), 1)
            self.assertEqual(args[args.index("-maxrate") + 1], "1500k")

    def test_opt_out_does_not_cap_constant_quality(self):
        with patch.object(tools, "require", return_value="ffmpeg"):
            args = video.build_args(Path("in.mp4"), Path("out.mp4"), clip(),
                                    Settings(require_savings=False), "libx265")
        self.assertNotIn("-maxrate", args)

    def test_unreachable_smart_target_keeps_original(self):
        with patch.object(quality, "video_metric", return_value="VMAF"), \
                patch.object(quality, "search", return_value=None):
            with self.assertRaises(NotWorthIt):
                video.smart_quality(Path("in.mp4"), clip(), Settings(smart_quality=True), "libx265")
            q, note = video.smart_quality(Path("in.mp4"), clip(), Settings(require_savings=False), "libx265")
        self.assertEqual(q, video.SMART_STEPS["libx265"][0])
        self.assertIn("target not reached", note)

    def test_finished_encode_must_meet_smart_target(self):
        score = quality.Score("VMAF", 80)
        with self.assertRaises(NotWorthIt):
            video.check_quality_budget(score, clip(), Settings(smart_quality=True), "libx265")
        video.check_quality_budget(score, clip(), Settings(smart_quality=True, require_savings=False), "libx265")
        video.check_quality_budget(score, clip(), Settings(video_quality="custom"), "libx265")

    def test_convert_uses_measured_quality_for_both_codecs(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "input.mp4"
            src.write_bytes(b"original")
            cancel = threading.Event()
            for target, codec in (("mkv_av1", "av1"), ("mp4_hevc", "hevc")):
                for s in (Settings(smart_quality=True), Settings(video_quality="identical")):
                    with self.subTest(target=target, quality=s.video_quality), \
                            patch.object(video, "choose_encoder", return_value="libx265"), \
                            patch.object(video, "smart_quality", return_value=(34, "CRF 34")) as choose, \
                            patch.object(video, "build_args", return_value=["ffmpeg"]) as build, \
                            patch.object(video, "verify_video"), patch.object(video, "measure", return_value=None), \
                            patch.object(tools, "run_ffmpeg_progress"):
                        convert.convert_video(src, clip(), target, s, cancel=cancel)
                        self.assertEqual(choose.call_args.args[2].video_codec, codec)
                        self.assertIs(choose.call_args.args[4], cancel)
                        self.assertEqual(build.call_args.args[5], 34)

    def test_convert_custom_quality_skips_smart_search(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(video, "choose_encoder", return_value="libx265"), \
                patch.object(video, "smart_quality") as choose, \
                patch.object(video, "build_args", return_value=["ffmpeg"]), \
                patch.object(video, "verify_video"), patch.object(tools, "run_ffmpeg_progress"):
            convert.convert_video(Path(d) / "input.mp4", clip(), "mp4_hevc",
                                  Settings(smart_quality=True, video_quality="custom"))
            choose.assert_not_called()

    def test_convert_cancellation_removes_temporary_output(self):
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d) / "input.mp4", Path(d) / "temp.mp4"
            src.write_bytes(b"original")
            out.write_bytes(b"partial")
            with patch.object(convert, "temp_name", return_value=out), \
                    patch.object(video, "choose_encoder", return_value="libx265"), \
                    patch.object(video, "smart_quality", side_effect=tools.Cancelled):
                with self.assertRaises(tools.Cancelled):
                    convert.convert_video(src, clip(), "mp4_hevc", Settings(smart_quality=True))
            self.assertFalse(out.exists())
            self.assertEqual(src.read_bytes(), b"original")

    def test_conversion_defaults_reject_larger_results_and_allow_explicit_opt_out(self):
        self.assertTrue(convert.ConvertOptions().only_if_smaller)
        with tempfile.TemporaryDirectory() as d:
            src, out, dest = Path(d) / "input.mp4", Path(d) / "temp.mp4", Path(d) / "output.mp4"
            src.write_bytes(b"original")
            entry = SimpleNamespace(path=src, kind="video", video=clip())
            plan = Plan(True, "HEVC", ".mp4", route="mp4_hevc")

            def encode(*args):
                out.write_bytes(b"larger than original")
                return out

            with patch.object(convert, "convert_video", side_effect=encode) as encode_mock, \
                    patch.object(engine, "_place", return_value=dest) as place:
                result = engine.process_convert(entry, plan, Settings(), None, opts=convert.ConvertOptions())
                self.assertEqual(result.status, engine.SKIPPED)
                place.assert_not_called()
                self.assertFalse(out.exists())
                self.assertTrue(encode_mock.call_args.args[3].require_savings)
                result = engine.process_convert(entry, plan, Settings(), None,
                                                opts=convert.ConvertOptions(only_if_smaller=False))
                self.assertEqual(result.status, engine.DONE)
                self.assertFalse(encode_mock.call_args.args[3].require_savings)
                self.assertEqual(src.read_bytes(), b"original")

    def test_compress_save_as_honors_savings_unless_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d) / "input.mp4", Path(d) / "temp.mp4"
            src.write_bytes(b"original")
            entry = SimpleNamespace(path=src, size=8, kind="video", video=clip())
            plan = Plan(True, "HEVC", ".mp4", dest=Path(d) / "saved.mp4")

            def encode(*args):
                out.write_bytes(b"larger than original")
                return out, "libx265"

            with patch.object(engine, "convert_video", side_effect=encode), \
                    patch("wick.calibration.record"), patch.object(engine, "_place", return_value=plan.dest) as place:
                result = engine.process_file(entry, plan, Settings(), None)
                self.assertEqual(result.status, engine.SKIPPED)
                place.assert_not_called()
                self.assertEqual(src.read_bytes(), b"original")
                result = engine.process_file(entry, plan, Settings(require_savings=False), None)
                self.assertEqual(result.status, engine.DONE)
                place.assert_called_once()

    def test_resume_cache_changes_with_budget_policy(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "input.mp4"
            src.write_bytes(b"original")
            limited = chunked.parts_folder(src, Settings(), "libx265", 0)
            unlimited = chunked.parts_folder(src, Settings(require_savings=False), "libx265", 0)
            self.assertNotEqual(limited, unlimited)

    def test_probe_reads_picture_bitrate_separately(self):
        raw = {"format": {"duration": "15", "bit_rate": "9000000"}, "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
             "bit_rate": "5500000", "avg_frame_rate": "24/1"}]}
        with patch.object(probe, "probe_raw", return_value=raw):
            self.assertEqual(probe.probe_video(Path("input.mp4")).video_bit_rate, 5_500_000)
            raw["streams"][0]["bit_rate"] = "N/A"
            self.assertEqual(probe.probe_video(Path("input.mp4")).video_bit_rate, 0)


    def test_old_cached_video_is_reprobed_for_picture_rate(self):
        data = dataclasses.asdict(clip())
        data.pop("video_bit_rate")
        with patch.object(index, "_db") as db, patch.object(index, "_enabled", True):
            db.return_value.execute.return_value.fetchone.return_value = (100, 1.0, "video", json.dumps(data))
            self.assertIsNone(index.lookup(Path("input.mp4"), 100, 1.0, "video"))
            data["video_bit_rate"] = 5_500_000
            db.return_value.execute.return_value.fetchone.return_value = (100, 1.0, "video", json.dumps(data))
            self.assertEqual(index.lookup(Path("input.mp4"), 100, 1.0, "video").video_bit_rate, 5_500_000)

    def test_gpu_fallback_remeasures_smart_quality_on_cpu(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "input.mp4"
            src.write_bytes(b"original")
            error = tools.ToolError(["ffmpeg"], 1, "GPU unavailable")
            s = Settings(video_codec="hevc", video_grain="off", video_chunked=False, smart_quality=True)
            with patch.object(video, "choose_encoder", return_value="hevc_nvenc"), \
                    patch.object(video, "smart_quality", side_effect=[(32, "CQ 32"), (34, "CRF 34")]) as choose, \
                    patch.object(video, "build_args", return_value=["ffmpeg"]) as build, \
                    patch.object(video, "verify_video"), patch.object(video, "measure", return_value=None), \
                    patch.object(tools, "run_ffmpeg_progress", side_effect=[error, error, None]):
                _out, used = video.convert_video(src, clip(), Plan(True, target_ext=".mp4"), s)
                self.assertEqual(used, "libx265")
                self.assertEqual([c.args[3] for c in choose.call_args_list], ["hevc_nvenc", "libx265"])
                self.assertEqual(build.call_args.args[5], 34)

    @unittest.skipUnless(tools.find_tool("ffmpeg") and tools.find_tool("ffprobe"), "ffmpeg required")
    def test_cpu_encoders_accept_bitrate_caps(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "input.mp4"
            tools.run([tools.require("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi",
                       "-i", "testsrc2=s=160x96:r=24:d=1", "-c:v", "libx264", "-preset", "veryfast", str(src)])
            info = probe.probe_video(src)
            self.assertGreater(info.video_bit_rate, 0)
            for encoder, codec in (("libsvtav1", "av1"), ("libx265", "hevc")):
                with self.subTest(encoder=encoder):
                    out = Path(d) / f"{codec}.mp4"
                    args = video.build_args(src, out, info, Settings(), encoder, 30, threads=2)
                    tools.run(args, timeout=120)
                    self.assertEqual(probe.probe_video(out).codec, codec)


if __name__ == "__main__":
    unittest.main()
