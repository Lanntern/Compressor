"""Update check, component versions, pinned tool downloads and the weekly upstream report."""

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

from wick import updates

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def test_version_comparison():
    assert updates.parse("v1.2.10") == (1, 2, 10) and updates.parse("0.3-beta") == (0, 3)
    assert updates.newer("0.1.1", "0.1.0") and updates.newer("1.0", "0.9.9") and updates.newer("v0.2", "0.1.9")
    assert not updates.newer("0.1.0", "0.1.0") and not updates.newer("0.1", "0.1.0") and not updates.newer("x", "0.1")


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_check_reads_github_release(monkeypatch):
    sent = {}

    def fake(req, timeout):
        sent["ua"] = req.headers.get("User-agent")
        return _Answer(json.dumps({"tag_name": "v99.0.0", "html_url": "https://example.invalid/r",
                                   "body": "Updates FFmpeg to fix CVE-2026-12345."}).encode())

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake)
    rel = updates.check()
    assert rel.version == "99.0.0" and rel.security and rel.url == "https://example.invalid/r"
    assert sent["ua"].startswith("Wick/")          # no other identifying information
    monkeypatch.setattr(updates.urllib.request, "urlopen",
                        lambda req, timeout: _Answer(json.dumps({"tag_name": "v0.0.1", "body": ""}).encode()))
    assert updates.check() is None                         # already newer than that


def test_check_failures_are_explained(monkeypatch):
    def missing(req, timeout):
        raise urllib.error.HTTPError(updates.API, 404, "Not Found", {}, None)

    monkeypatch.setattr(updates.urllib.request, "urlopen", missing)
    with pytest.raises(updates.CheckFailed, match="No published releases"):
        updates.check()

    def offline(req, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(updates.urllib.request, "urlopen", offline)
    with pytest.raises(updates.CheckFailed, match="No connection"):
        updates.check()


def test_component_versions():
    from wick import components

    v = components.versions()
    assert v["Pillow"].split(".")[0].isdigit() and "Qt" in v
    assert components.report().startswith("Wick ")


def test_tool_downloads_are_checked(monkeypatch):
    sys.path.insert(0, str(SCRIPTS))
    import fetch_tools

    good = fetch_tools.Download("demo", "1.0", "https://example.invalid/demo.zip",
                                __import__("hashlib").sha256(b"payload").hexdigest())
    fetch_tools.verify(good, b"payload", allow_unpinned=False)
    with pytest.raises(fetch_tools.ChecksumMismatch):
        fetch_tools.verify(good, b"tampered", allow_unpinned=False)
    unpinned = fetch_tools.Download("demo", "1.0", "https://example.invalid/demo.zip", None)
    with pytest.raises(fetch_tools.ChecksumMismatch, match="no recorded checksum"):
        fetch_tools.verify(unpinned, b"payload", allow_unpinned=False)
    for d in [*fetch_tools.WINDOWS, fetch_tools.LINUX_LIBJXL]:
        assert d.url.startswith("https://") and (d.sha256 is None or len(d.sha256) == 64)


def test_every_shipped_tool_comes_with_its_licence(monkeypatch, tmp_path):
    """A licence the archive doesn't carry (gifsicle's Windows zip) is shipped from packaging/licenses."""
    import hashlib
    import zipfile

    sys.path.insert(0, str(SCRIPTS))
    import fetch_tools

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("gifsicle-1.95/gifsicle.exe", b"exe")      # no COPYING inside
    data = buf.getvalue()
    gifsicle = next(d for d in fetch_tools.WINDOWS if d.tool == "gifsicle")
    d = fetch_tools.Download("gifsicle", "1.95", "https://example.invalid/g.zip", hashlib.sha256(data).hexdigest(),
                             gifsicle.files, required=False)
    monkeypatch.setattr(fetch_tools, "download", lambda url: data)
    assert fetch_tools.fetch(d, tmp_path, force=True)
    licence = (tmp_path / "LICENSE-gifsicle.txt").read_text()
    assert "GNU GENERAL PUBLIC LICENSE" in licence and "Version 2" in licence
    for d in fetch_tools.WINDOWS:          # every Windows tool brings a licence file, from its archive or ours
        assert any(out and out.lstrip("?").startswith("LICENSE") for out in d.files.values()) or d.tool == "jpegli"


def test_weekly_report(monkeypatch):
    sys.path.insert(0, str(SCRIPTS))
    import check_updates

    monkeypatch.setattr(check_updates, "latest", lambda repo, pattern: "99.0.0")
    monkeypatch.setattr(check_updates, "audit_findings", lambda: [])
    body, attention = check_updates.report()
    assert attention and "upstream's newest is **99.0.0**" in body and "held back on purpose" in body
    monkeypatch.setattr(check_updates, "latest", lambda repo, pattern: "0.0.1")
    monkeypatch.setattr(check_updates.fetch_tools, "WINDOWS",
                        [d for d in check_updates.fetch_tools.WINDOWS if d.sha256])
    body, attention = check_updates.report()
    assert not attention                                     # nothing newer, nothing vulnerable: no issue
