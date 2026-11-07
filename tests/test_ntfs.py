"""Fast drive scanning: reading NTFS's file table directly (compressor/ntfs.py).

tests/data/ntfs.img.gz is a small real NTFS volume (see make_ntfs_image.sh); on Windows the real C: drive is
read as well (CI runs as administrator).
"""

import gzip
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from compressor import ntfs
from compressor.scanner import ScanResult, walk_listing
from compressor.settings import Settings

IMAGE = Path(__file__).with_name("data") / "ntfs.img.gz"
EXPECTED = {   # path below the volume root -> size (as made by make_ntfs_image.sh)
    "Pictures/Holiday 2024/small.jpg": 3, "Pictures/b.png": 123456, "Pictures/Ünïcode – 日本/clip.mp4": 300000,
    "Program/App/x.dll": 0, "Program/App/y.jpg": 0, "$Recycle.Bin/S-1-5-21/$Rabc.jpg": 0,
    "node_modules/x/logo.png": 0, ".dotfile.jpg": 0, "Deep/a/b/c/deep.heic": 70000, "Deep/notes.txt": 2,
    "Deep/old.jxl": 9000,
}


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    out = tmp_path_factory.mktemp("ntfs") / "ntfs.img"
    with gzip.open(IMAGE, "rb") as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out


def _path(tree, i):
    parts = []
    while i != ntfs.ROOT:
        n = tree.nodes[i]
        parts.append(n.name)
        i = n.parent
    return "/".join(reversed(parts))


def test_run_lists():
    # 0x21: 1 length byte, 2 offset bytes; then a run that goes backwards; then a sparse run; end.
    data = bytes([0x21, 0x10, 0x00, 0x01, 0x21, 0x08, 0x00, 0xFF, 0x01, 0x05, 0x00])
    assert ntfs.runs(data, 0) == [(256, 16), (0, 8), (None, 5)]


def test_reads_every_file_with_its_size_and_date(image):
    with open(image, "rb") as f:
        tree = ntfs.read_tree(f)
    every = {_path(tree, i): n for i, n in tree.nodes.items() if not n.is_dir and i != ntfs.ROOT}
    files = {k: v for k, v in every.items() if not k.startswith(("$MFT", "$Log", "$Vol", "$Att", "$Bit", "$Boot",
                                                                 "$Bad", "$Sec", "$UpC", "$Extend"))}   # NTFS's own
    assert {k: v.size for k, v in files.items()} == EXPECTED
    assert files["Pictures/b.png"].mtime == pytest.approx(
        __import__("datetime").datetime(2020, 5, 1, 12, 34, 56).timestamp(), abs=1)
    assert tree.find(["pictures", "ÜNÏCODE – 日本"]) is not None          # case-insensitive, like Windows


def test_a_drive_scan_from_the_table_follows_the_usual_rules(image, tmp_path):
    keep = lambda n: Path(n).suffix.lower() in {".jpg", ".png", ".mp4", ".heic", ".jxl"}   # noqa: E731
    out = tmp_path / "dump.tsv.gz"
    with open(image, "rb") as f:
        ntfs.dump(f, [[]], keep, out)
    listing = ntfs.load(out)
    result = ScanResult(roots=[Path("X:/")])
    walk_listing(result, Path("X:/"), listing, 0, Settings(min_image_kb=0), set())
    got = sorted(e.path.relative_to("X:/").as_posix() for e in result.entries)
    # Not: the program folder (it holds a .dll), the Recycle Bin, node_modules, hidden (dot) files; .jxl counted.
    assert got == ["Deep/a/b/c/deep.heic", "Pictures/Holiday 2024/small.jpg", "Pictures/b.png",
                   "Pictures/Ünïcode – 日本/clip.mp4"]
    assert [p.as_posix() for p in result.skipped_program_dirs] == ["X:/Program/App"]
    assert (result.jxl_files, result.jxl_bytes) == (1, 9000)
    sizes = {e.path.name: e.size for e in result.entries}
    assert sizes["clip.mp4"] == 300000


def test_not_ntfs_is_refused(tmp_path):
    f = tmp_path / "zeros"
    f.write_bytes(b"\0" * 4096)
    with open(f, "rb") as fh, pytest.raises(ntfs.NotNTFS):
        ntfs.Volume(fh)


@pytest.mark.skipif(os.name != "nt" or not ntfs.is_admin(), reason="needs Windows, as administrator")
def test_the_real_system_drive():
    """CI's Windows runner is an administrator: read C:'s real file table and find a file made just now."""
    with tempfile.NamedTemporaryFile(dir=os.environ.get("TEMP"), suffix=".jpg", delete=False) as t:
        t.write(b"x" * 12345)
    try:
        with ntfs.open_volume(ntfs.drive_of(Path(t.name))) as f:
            tree = ntfs.read_tree(f)
        parts = list(Path(os.path.realpath(t.name)).parts[1:])
        found = tree.find(parts)
        assert found is not None and tree.nodes[found].size == 12345
        assert len(tree.nodes) > 10000
    finally:
        os.unlink(t.name)
