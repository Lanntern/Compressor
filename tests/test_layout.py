"""Nothing squeezed, cut off or overlapping on any page or dialog, at the system text sizes people use
(100%, 125%, 150%), in the smallest, a medium and a large window, light and dark (see layout_check.py)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
SCRIPT = Path(__file__).with_name("layout_check.py")


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5])
def test_nothing_squeezed_cut_off_or_overlapping(scale):
    from PySide6.QtWidgets import QApplication

    base = 9.0 if os.name == "nt" or QApplication.instance() is None else QApplication.font().pointSizeF()
    env = {**os.environ, "PT": str(round(base * scale, 2))}
    proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=900, env=env)
    out = proc.stdout.strip()
    assert proc.returncode == 0 and out.endswith("issues: 0"), f"{out}\n{proc.stderr[-2000:]}"
