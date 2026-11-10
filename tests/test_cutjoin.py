"""Trimming and joining videos and music."""

import pytest

from wick import cutjoin

from .conftest import ffmpeg, needs_ffmpeg

pytestmark = needs_ffmpeg


def _clip(path, src="testsrc2", size="320x240", d=6, f=440):
    ffmpeg("-f", "lavfi", "-i", f"{src}=s={size}:r=25:d={d}", "-f", "lavfi", "-i", f"sine=f={f}:d={d}",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "50", "-c:a", "aac", "-shortest", str(path))


def test_trimming_copies_or_cuts_exactly(tmp_path):
    _clip(tmp_path / "a.mp4")
    got = cutjoin.trim(tmp_path / "a.mp4", 2.5, 4.5, tmp_path / "copy.mp4")
    assert 2.0 <= got <= 2.9                                    # starts at the keyframe at 2 s: nothing re-encoded
    assert abs(cutjoin.trim(tmp_path / "a.mp4", 2.5, 4.5, tmp_path / "exact.mp4", exact=True) - 2.0) < 0.1
    ffmpeg("-f", "lavfi", "-i", "sine=d=5", str(tmp_path / "s.mp3"))
    assert abs(cutjoin.trim(tmp_path / "s.mp3", 1, 3, tmp_path / "t.mp3") - 2.0) < 0.1
    with pytest.raises(ValueError):
        cutjoin.trim(tmp_path / "a.mp4", 3, 2, tmp_path / "x.mp4")


def test_joining(tmp_path):
    _clip(tmp_path / "a.mp4", d=3)
    _clip(tmp_path / "b.mp4", d=2, f=600)
    _clip(tmp_path / "c.mp4", size="640x360", d=2)
    assert cutjoin.join_problem([tmp_path / "a.mp4", tmp_path / "b.mp4"]) == ""
    assert abs(cutjoin.join([tmp_path / "a.mp4", tmp_path / "b.mp4"], tmp_path / "ab.mp4") - 5) < 0.3
    assert "differ" in cutjoin.join_problem([tmp_path / "a.mp4", tmp_path / "c.mp4"])
    assert abs(cutjoin.join([tmp_path / "a.mp4", tmp_path / "c.mp4"], tmp_path / "ac.mp4", reencode=True) - 5) < 0.3
    from wick.probe import probe_video

    v = probe_video(tmp_path / "ac.mp4")
    assert (v.width, v.height) == (320, 240)                      # fitted into the first one's picture
