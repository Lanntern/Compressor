import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# Isolate app data and (on Linux) the Trash before anything imports send2trash,
# which reads XDG_DATA_HOME at import time.
_SANDBOX = Path(tempfile.mkdtemp(prefix="compressor-tests-"))
os.environ["COMPRESSOR_DATA_DIR"] = str(_SANDBOX / "appdata")
os.environ["COMPRESSOR_NO_NETWORK"] = "1"      # no automatic update checks while testing
if os.name != "nt":
    os.environ["XDG_DATA_HOME"] = str(_SANDBOX / "xdg")

from compressor import tools  # noqa: E402


def have(*names: str) -> bool:
    return all(tools.find_tool(n) for n in names)


needs_image_tools = pytest.mark.skipif(
    not have("cjxl", "djxl", "ffmpeg", "ffprobe"), reason="image tools not installed")
needs_gifsicle = pytest.mark.skipif(not have("gifsicle"), reason="gifsicle not installed")
needs_ffmpeg = pytest.mark.skipif(not have("ffmpeg", "ffprobe"), reason="ffmpeg not installed")


def _pandoc_works() -> bool:
    """Pandoc that can convert with --sandbox (distribution builds keep data files it then can't read)."""
    exe = tools.find_tool("pandoc")
    if not exe:
        return False
    p = subprocess.run([exe, "-f", "markdown", "-t", "docx", "-o", os.devnull, "--sandbox"], input=b"# x",
                       capture_output=True)
    return p.returncode == 0


needs_pandoc = pytest.mark.skipif(not _pandoc_works(), reason="no Pandoc that works with --sandbox")


def ffmpeg(*args: str, cwd: Path | None = None) -> None:
    subprocess.run([tools.require("ffmpeg"), "-v", "error", "-y", *args], check=True, cwd=cwd)


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    # Non-ASCII folder name on purpose: encoders must cope with it.
    d = tmp_path / "Bilder åäö"
    d.mkdir()
    return d
