"""Repacking archives: only when chosen, checked, and only kept when smaller."""

import tarfile
import zipfile

import py7zr
import pytest

from compressor import archives, convert
from compressor.engine import DONE, SKIPPED, process_convert
from compressor.plan import Plan
from compressor.scanner import scan
from compressor.settings import Settings


def _stored_zip(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for name, data in files.items():
            z.writestr(name, data)


def test_repacking_is_checked_and_kept_only_when_smaller(tmp_path):
    text = ("All work and no play makes Jack a dull boy.\n" * 4000).encode()
    _stored_zip(tmp_path / "notes.zip", {"a.txt": text, "sub/b.txt": text[::-1]})
    with tarfile.open(tmp_path / "logs.tar.gz", "w:gz", compresslevel=1) as t:
        info = tarfile.TarInfo("log.txt")
        info.size = len(text)
        t.addfile(info, __import__("io").BytesIO(text))
    _stored_zip(tmp_path / "random.zip", {"r.bin": __import__("os").urandom(20000)})
    s = Settings(output_mode="beside", convert_inputs=True)
    entries = {e.path.name: e for e in scan(tmp_path, s).entries}
    assert {e.kind for e in entries.values()} == {"archive"}
    assert convert.ConvertOptions().target_for("archive") is None              # never by default
    opts = convert.ConvertOptions(archive_target="7z")
    r = process_convert(entries["notes.zip"], Plan(True, "", ".7z", route="7z"), s, None, opts=opts)
    assert r.status == DONE and r.output == tmp_path / "notes.7z" and r.new_size < entries["notes.zip"].size
    with py7zr.SevenZipFile(r.output) as z:
        assert sorted(z.getnames()) == ["a.txt", "sub", "sub/b.txt"] or sorted(
            n for n in z.getnames() if n.endswith(".txt")) == ["a.txt", "sub/b.txt"]
    r = process_convert(entries["logs.tar.gz"], Plan(True, "", ".7z", route="7z"), s, None, opts=opts)
    assert r.status == DONE and r.output == tmp_path / "logs.7z"                 # (not logs.tar.7z)
    r = process_convert(entries["random.zip"], Plan(True, "", ".zip", route="zip"), s, None,
                        opts=convert.ConvertOptions(archive_target="zip"))
    assert r.status == SKIPPED and not (tmp_path / "random (2).zip").exists()   # not smaller: thrown away


def test_repacking_refuses_unsafe_names_and_passwords(tmp_path):
    _stored_zip(tmp_path / "evil.zip", {"../outside.txt": b"x"})
    with pytest.raises(ValueError, match="outside"):
        archives.repack(tmp_path / "evil.zip", tmp_path / "out.7z", "7z")
    assert not (tmp_path.parent / "outside.txt").exists()
    with py7zr.SevenZipFile(tmp_path / "locked.7z", "w", password="pw") as z:
        z.writestr(b"secret", "s.txt")
    with pytest.raises(ValueError, match="password"):
        archives.repack(tmp_path / "locked.7z", tmp_path / "out.zip", "zip")
