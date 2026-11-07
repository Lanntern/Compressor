"""Keeping the computer awake, telling battery from mains, and pausing a job."""

import threading
import time
from pathlib import Path

from PIL import Image

from compressor import power


def _supply(root: Path, name: str, **values) -> None:
    d = root / name
    d.mkdir(parents=True)
    for k, v in values.items():
        (d / k).write_text(v + "\n")


def test_battery_or_mains_from_sysfs(tmp_path):
    assert power.linux_on_battery(tmp_path / "missing") is None
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    _supply(desktop, "AC", type="Mains", online="1")
    assert power.linux_on_battery(desktop) is None                 # no battery: never "on battery"
    laptop = tmp_path / "laptop"
    _supply(laptop, "AC", type="Mains", online="1")
    _supply(laptop, "BAT0", type="Battery", status="Charging")
    assert power.linux_on_battery(laptop) is False
    (laptop / "AC" / "online").write_text("0\n")
    assert power.linux_on_battery(laptop) is True


def test_awake_nests_and_can_be_turned_off(monkeypatch):
    calls = []
    monkeypatch.setattr(power, "_hold_other", lambda: calls.append("hold"))
    monkeypatch.setattr(power, "_release_other", lambda: calls.append("release"))
    if power.os.name == "nt":
        return       # (Windows holds it per thread through the system call instead)
    with power.awake():
        with power.awake():
            pass
        assert calls == ["hold"]
    assert calls == ["hold", "release"]
    monkeypatch.setattr(power, "ENABLED", False)
    with power.awake():
        pass
    assert calls == ["hold", "release"]


def test_a_paused_job_starts_nothing_new(tmp_path):
    from compressor.engine import DONE, run_job
    from compressor.plan import plan_entry
    from compressor.scanner import scan
    from compressor.settings import Settings

    for i in range(3):
        Image.effect_noise((64, 48), 40).convert("RGB").save(tmp_path / f"{i}.png")
    s = Settings(image_format="webp", png_mode="lossy", min_image_kb=0, require_savings=False, output_mode="suffix")
    items = [(e, plan_entry(e, s)) for e in scan([tmp_path], s).entries]
    pause, started, results = threading.Event(), [], {}
    pause.set()

    class Events:
        def file_started(self, e):
            started.append(e)

        def file_progress(self, *a): ...
        def file_finished(self, r): ...
        def file_step(self, *a): ...

    t = threading.Thread(target=lambda: results.update(r=run_job(items, s, tmp_path, Events(), pause=pause)))
    t.start()
    time.sleep(1.0)
    assert started == []                     # paused before anything began
    pause.clear()
    t.join(60)
    assert len(started) == 3 and all(r.status == DONE for r in results["r"][0])


def test_background_and_efficiency_modes_are_safe_everywhere():
    """Windows-only effects (background thread mode, EcoQoS); elsewhere they must simply do nothing."""
    import threading

    from compressor import power

    ran = []
    with power.background():
        ran.append(threading.current_thread().name)
    assert ran
    power.set_efficiency_mode(True)
    power.set_efficiency_mode(False)
