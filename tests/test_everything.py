"""Everything (voidtools) answers: encoding the question, reading the answer, and turning it into the scanner's
listing. (Talking to a running Everything is Windows-only: tested in CI on Windows.)"""

import struct
from pathlib import Path, PureWindowsPath

from compressor import everything


def _reply(items):
    """An EVERYTHING_IPC_LIST2 as Everything sends it (full path, size, modified, attributes per item)."""
    flags = 0x4 | 0x10 | 0x40 | 0x100
    head = struct.pack("<5I", len(items), len(items), 0, flags, 1)
    table, data = b"", b""
    base = 20 + 8 * len(items)
    for name, is_dir, size, mtime, attrs in items:
        ft = int(mtime * 10_000_000) + 116444736000000000
        blob = struct.pack("<I", len(name)) + (name + "\0").encode("utf-16-le") + struct.pack("<qQI", size, ft, attrs)
        table += struct.pack("<2I", 1 if is_dir else 0, base + len(data))
        data += blob
    return head + table + data


def test_question_and_answer_round_trip():
    q = everything.build_query(0x1234, '"C:\\Users\\me\\" <folder:|ext:jpg;png>')
    reply_hwnd, reply_id, _flags, _offset, max_results, requested, sort = struct.unpack_from("<7I", q)
    assert reply_hwnd == 0x1234 and max_results == 0xFFFFFFFF and requested == 0x4 | 0x10 | 0x40 | 0x100 and sort == 1
    assert q[28:].decode("utf-16-le") == '"C:\\Users\\me\\" <folder:|ext:jpg;png>\0'
    items = [("C:\\Users\\me\\Pictures", True, 0, 1_700_000_000.0, 0x10),
             ("C:\\Users\\me\\Pictures\\Ålesund.jpg", False, 123_456, 1_700_000_100.0, 0x20)]
    got = everything.parse_reply(_reply(items))
    assert [(n, d, s, round(m), a) for n, d, s, m, a in got] == [(n, d, s, round(m), a) for n, d, s, m, a in items]


def test_answer_becomes_the_scanners_listing(monkeypatch):
    monkeypatch.setattr("os.path", __import__("ntpath"))         # Windows paths, whatever runs the test
    root = PureWindowsPath("C:\\Users\\me")
    items = [("C:\\Users\\me\\Pictures", True, 0, 0.0, 0x10),
             ("C:\\Users\\me\\Pictures\\Trip", True, 0, 0.0, 0x10),
             ("C:\\Users\\me\\AppData", True, 0, 0.0, 0x12),                 # hidden: the scanner skips it
             ("C:\\Users\\me\\Pictures\\a.jpg", False, 10, 5.0, 0x20),
             ("C:\\Users\\me\\Pictures\\Trip\\b.png", False, 20, 6.0, 0x20),
             ("C:\\Users\\me\\Games\\Game", True, 0, 0.0, 0x10),               # parent not listed: left out
             ("C:\\Users\\me\\Pictures\\Trip\\x.dll", False, 1, 1.0, 0x20)]    # marks a program folder
    listing = everything.to_listing(root, items)
    names = {rec: d[3] for rec, d in listing.dirs.items()}
    pictures = next(r for r, n in names.items() if n == "Pictures")
    trip = next(r for r, n in names.items() if n == "Trip")
    assert [f[3] for f in listing.files[pictures]] == ["a.jpg"] and [f[3] for f in listing.files[trip]] == ["b.png"]
    assert listing.dirs[trip][2] is True and listing.dirs[pictures][2] is False   # the library marks Trip
    assert "Game" not in names.values()
    assert listing.dirs[next(r for r, n in names.items() if n == "AppData")][1] & 0x2   # hidden attribute kept


def test_search_asks_for_folders_and_the_listed_types():
    s = everything.search_for(Path("C:/Users/me"), {".JPG", "png"})
    assert s.endswith('<folder:|ext:jpg;png>') and s.startswith('"')


def test_a_running_everything_lists_a_folder(tmp_path):
    """Windows CI installs and starts Everything (COMPRESSOR_REQUIRE_EVERYTHING=1); elsewhere this is skipped."""
    import os
    import time

    import pytest

    required = os.environ.get("COMPRESSOR_REQUIRE_EVERYTHING") == "1"
    if os.name != "nt" or not (required or everything.available()):
        pytest.skip("Everything isn't running here")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "photo.jpg").write_bytes(b"x" * 1234)
    (tmp_path / "notes.txt").write_text("not asked for")
    for _ in range(30):
        if everything.available():
            break
        time.sleep(1)
    assert everything.available(), "Everything isn't running or hasn't loaded its index"
    assert everything.query('"' + str(everything.long_path(tmp_path)) + '\\"') is not None, "no reply to a query"
    listing = None
    for _ in range(60):                       # its index follows the drive's change journal within moments
        listing = everything.listing_for(tmp_path, {".jpg"})
        if listing is not None and any(f[3] == "photo.jpg" for files in listing.files.values() for f in files):
            break
        time.sleep(1)
    assert listing is not None, "no answer from Everything"
    files = [f for files in listing.files.values() for f in files]
    assert [(f[3], f[0]) for f in files] == [("photo.jpg", 1234)]
