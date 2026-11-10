"""Music and other audio: FLAC (verified sample for sample), mono, CD quality, lossy formats, tags and art."""

from pathlib import Path

import pytest

from wick.engine import DONE, run_job, undo_record
from wick.plan import plan_entry
from wick.scanner import scan
from wick.settings import Settings

from .conftest import ffmpeg, needs_ffmpeg

pytestmark = needs_ffmpeg


def _settings(**kw) -> Settings:
    base = dict(output_mode="suffix", min_audio_kb=0, image_format="none", video_codec="none")
    return Settings(**{**base, **kw})


def _run(folder: Path, s: Settings):
    entries = scan([folder], s).entries
    items = [(e, plan_entry(e, s)) for e in entries]
    results, run_id = run_job([(e, p) for e, p in items if p.convert], s, folder)
    return items, {r.entry.path.name: r for r in results}, run_id


def _probe(path: Path):
    from wick.probe import probe_audio

    info = probe_audio(path)
    assert info is not None
    return info


def test_wav_to_flac_is_lossless_and_identical_stereo_becomes_mono(media_dir):
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=6:c=pink:a=0.3", "-f", "lavfi", "-i", "sine=f=330:d=6",
           "-filter_complex", "[0][1]amerge=inputs=2,lowpass=8000", "-c:a", "pcm_s16le", "Låt.wav", cwd=media_dir)
    ffmpeg("-f", "lavfi", "-i", "sine=f=440:d=6", "-ac", "2", "-c:a", "pcm_s16le", "voice.wav", cwd=media_dir)
    items, results, run_id = _run(media_dir, _settings())
    plans = {e.path.name: p for e, p in items}
    assert plans["Låt.wav"].reason == "WAV → FLAC (lossless)" and plans["Låt.wav"].lossless

    stereo, voice = results["Låt.wav"], results["voice.wav"]
    assert stereo.status == DONE and voice.status == DONE
    assert stereo.output.suffix == ".flac" and stereo.new_size < stereo.entry.size * 0.8
    assert _probe(stereo.output).channels == 2
    assert _probe(voice.output).channels == 1 and "mono" in voice.message

    from wick.audio import pcm_md5

    assert pcm_md5(media_dir / "Låt.wav", "0:a:0") == pcm_md5(stereo.output, "0:a:0")


def test_float_wav_is_not_called_lossless_flac(media_dir):
    ffmpeg("-f", "lavfi", "-i", "sine=f=440:d=3", "-c:a", "pcm_f32le", "float.wav", cwd=media_dir)
    entry = scan([media_dir], _settings()).entries[0]
    p = plan_entry(entry, _settings())
    assert not p.convert and "Floating-point" in p.reason


def test_hires_to_cd_quality(media_dir):
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=4:c=pink:a=0.2", "-ar", "96000", "-c:a", "flac", "-sample_fmt", "s32",
           "hires.flac", cwd=media_dir)
    s = _settings(audio_cd_quality=True)
    items, results, _ = _run(media_dir, s)
    assert "48 kHz/16-bit" in items[0][1].reason
    r = results["hires.flac"]
    assert r.status == DONE
    info = _probe(r.output)
    assert (info.sample_rate, info.bits) == (48000, 16)


def test_lossy_reencode_keeps_tags_and_album_art(media_dir):
    from PIL import Image

    Image.new("RGB", (64, 64), "teal").save(media_dir / "cover.jpg")
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=6:c=pink:a=0.3", "-ac", "2", "-i", "cover.jpg", "-map", "0", "-map", "1",
           "-c:v", "copy", "-disposition:v", "attached_pic", "-metadata", "title=Sång", "-metadata", "artist=Band",
           "-c:a", "libmp3lame", "-b:a", "320k", "high.mp3", cwd=media_dir)
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=6:c=pink:a=0.3", "-c:a", "libmp3lame", "-b:a", "96k", "low.mp3",
           cwd=media_dir)
    (media_dir / "cover.jpg").unlink()
    s = _settings(audio_lossy="reencode", audio_format="opus")
    items, results, _ = _run(media_dir, s)
    plans = {e.path.name: p for e, p in items}
    assert not plans["low.mp3"].convert and "Already efficient" in plans["low.mp3"].reason

    out = results["high.mp3"].output
    assert out.suffix == ".opus" and results["high.mp3"].new_size < results["high.mp3"].entry.size
    info = _probe(out)
    assert info.codec == "opus" and info.cover_index >= 0           # album art survived
    assert info.tags.get("title") == "Sång" and info.tags.get("artist") == "Band"


@pytest.mark.parametrize("fmt,codec", [("aac", "aac"), ("mp3", "mp3")])
def test_lossless_to_lossy_formats(media_dir, fmt, codec):
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=4:c=pink:a=0.3", "-ac", "2", "-c:a", "flac", "a.flac", cwd=media_dir)
    _, results, _ = _run(media_dir, _settings(audio_lossless="lossy", audio_format=fmt))
    r = results["a.flac"]
    assert r.status == DONE and _probe(r.output).codec == codec


def test_audio_replace_and_undo(media_dir):
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=4:c=pink:a=0.3", "-c:a", "pcm_s16le", "take.wav", cwd=media_dir)
    original = (media_dir / "take.wav").read_bytes()
    _, results, run_id = _run(media_dir, _settings(output_mode="replace"))
    r = results["take.wav"]
    assert r.status == DONE and not (media_dir / "take.wav").exists() and (media_dir / "take.flac").exists()

    from wick import journal

    for rec in journal.get_run(run_id).files:
        ok, msg = undo_record(run_id, rec)
        assert ok, msg
    assert (media_dir / "take.wav").read_bytes() == original and not (media_dir / "take.flac").exists()


def test_convert_page_audio_targets(media_dir):
    from wick import convert
    from wick.engine import process_convert

    ffmpeg("-f", "lavfi", "-i", "sine=f=440:d=3", "-c:a", "libmp3lame", "-b:a", "192k", "tune.mp3", cwd=media_dir)
    entry = scan([media_dir], _settings()).entries[0]
    opts = convert.ConvertOptions(audio_target="wav")
    t = opts.target_for(entry.kind)
    assert t == ("wav", ".wav")
    from wick.plan import Plan

    s = _settings(output_mode="beside")
    r = process_convert(entry, Plan(True, convert.describe(entry.kind, opts), t[1], route=t[0]), s, None, opts=opts)
    assert r.status == DONE and r.output.suffix == ".wav" and _probe(r.output).codec.startswith("pcm_")


def test_home_ranks_uncompressed_audio_high(media_dir):
    from wick import overview

    ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=8:c=pink:a=0.3", "-ac", "2", "-c:a", "pcm_s16le", "big.wav",
           cwd=media_dir)
    ov = overview.analyze([media_dir], _settings())
    assert ov.opportunities and ov.opportunities[0].confidence == overview.HIGH


@needs_ffmpeg
def test_music_copy_stays_in_sync(tmp_path):
    """Lossless songs become Opus with tags and album art, MP3s are copied, only changes are redone, removed
    songs leave the copy, and nothing the app didn't make is touched."""
    import json as _json

    from PIL import Image as _Image

    from wick import mirror
    from wick.probe import probe_audio, probe_raw

    lib, phone = tmp_path / "Music", tmp_path / "Phone"
    album = lib / "Artist" / "Album"
    album.mkdir(parents=True)
    _Image.new("RGB", (64, 64), (200, 40, 40)).save(tmp_path / "art.jpg")
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-i", str(tmp_path / "art.jpg"),
           "-map", "0", "-map", "1", "-c:a", "flac", "-c:v", "copy", "-disposition:v", "attached_pic",
           "-metadata", "title=First Song", "-metadata", "artist=The Band", str(album / "01 First.flac"))
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=660:duration=3", "-c:a", "libmp3lame", "-b:a", "128k",
           str(album / "02 Second.mp3"))
    _Image.new("RGB", (32, 32)).save(album / "cover.jpg")
    phone.mkdir()
    (phone / "my notes.txt").write_text("not the app's")

    r = mirror.sync(lib, phone, "opus", "balanced")
    assert (r.encoded, r.copied, r.failed) == (1, 2, [])
    song = phone / "Artist" / "Album" / "01 First.opus"
    tags = {k.lower(): v for k, v in probe_raw(song)["format"].get("tags", {}).items()}
    tags.update({k.lower(): v for st in probe_raw(song)["streams"] for k, v in st.get("tags", {}).items()})
    assert tags.get("title") == "First Song" and tags.get("artist") == "The Band"
    assert probe_audio(song).cover_index >= 0                                # album art came along
    assert (phone / "Artist" / "Album" / "02 Second.mp3").read_bytes() == (album / "02 Second.mp3").read_bytes()
    assert (phone / "Artist" / "Album" / "cover.jpg").exists()

    again = mirror.sync(lib, phone, "opus", "balanced")
    assert (again.encoded, again.copied, again.unchanged) == (0, 0, 3)

    (album / "02 Second.mp3").unlink()
    aac = mirror.sync(lib, phone, "aac", "balanced")
    assert aac.encoded == 1 and aac.removed >= 2                            # the .opus and the gone MP3
    assert (phone / "Artist" / "Album" / "01 First.m4a").exists() and not song.exists()
    assert not (phone / "Artist" / "Album" / "02 Second.mp3").exists()
    assert (phone / "my notes.txt").read_text() == "not the app's"
    assert _json.loads((phone / mirror.MANIFEST).read_text())["format"] == "aac"
    with pytest.raises(ValueError):
        mirror.sync(lib, lib / "inside")
