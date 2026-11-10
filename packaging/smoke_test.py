"""Run the built app's command line on real files to prove the bundle is complete.

Usage: python packaging/smoke_test.py dist/Wick
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image


def gui_start(exe: Path, work: Path) -> bool:
    """Open the real window (twice: first and repeat start) and time it until it's up."""
    import time

    ok = True
    for attempt in ("first", "repeat"):
        probe = work / f"started-{attempt}.txt"
        env = dict(os.environ, WICK_STARTUP_PROBE=str(probe), WICK_SETTINGS_FILE=str(work / "s.ini"))
        t = time.perf_counter()
        proc = subprocess.Popen([str(exe)], env=env)
        while not probe.exists() and proc.poll() is None and time.perf_counter() - t < 60:
            time.sleep(0.02)
        shown = time.perf_counter() - t
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
        if probe.exists():
            print(f"window start-up ({attempt}): {shown:.2f} s")
        else:
            print(f"FAILED: the window didn't open ({attempt} start, exit {proc.poll()})")
            ok = False
    return ok


def main() -> int:
    app = Path(sys.argv[1])
    exe = next(p for p in (app / "Wick.exe", app / "Wick") if p.exists())
    work = Path(tempfile.mkdtemp(prefix="smoke-åäö-"))
    src = work / "in"
    src.mkdir()
    img = Image.radial_gradient("L").resize((600, 400)).convert("RGB")
    img.save(src / "photo.jpg", quality=95)
    img.save(src / "shot.png")
    # A scanned-document PDF (JPEG pages): optimised by pikepdf, which the build must bundle.
    Image.effect_mandelbrot((1600, 1200), (-2, -1.2, 1, 1.2), 60).convert("RGB").save(src / "scan.pdf", quality=95)
    frames = [img.rotate(a).convert("P") for a in range(0, 60, 15)]
    frames[0].save(src / "anim.gif", save_all=True, append_images=frames[1:], duration=80, loop=0)
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        img.save(src / "phone.heic", quality=90)
    except Exception as e:  # noqa: BLE001
        print("no HEIC sample:", e)

    trace = work / "trace.txt"
    env = dict(os.environ, WICK_TRACE=str(trace))

    def run(args, timeout):
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            out, err = proc.communicate(timeout=timeout)
            return subprocess.CompletedProcess(args, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            print(f"TIMEOUT: {args[1:3]}")
            for out in work.glob("out-*"):
                print(f"  outputs so far in {out.name}: {sorted(p.name for p in out.rglob('*') if p.is_file())}")
            if os.name == "nt":
                ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                                     "Get-CimInstance Win32_Process | Where-Object { $_.Name -match "
                                     "'Wick|cjxl|djxl|ffmpeg|ffprobe|gifsicle|python' } | "
                                     "Select-Object ProcessId,ParentProcessId,Name,CommandLine | Format-List"],
                                    capture_output=True, text=True)
                print("PROCESSES:\n" + ps.stdout[-6000:])
            if trace.exists():
                text = trace.read_text(encoding="utf-8", errors="replace")
                print("TRACE (first 6000 chars):\n" + text[:6000])
            proc.kill()
            raise

    import time

    t = time.perf_counter()
    proc = run([str(exe), "--version"], 120)
    print(f"start-up of the built app (--version): {time.perf_counter() - t:.2f} s")
    proc = run([str(exe), "tools"], 120)
    print("tools: exit", proc.returncode)
    gui_ok = gui_start(exe, work)

    expectations = {
        "jxl": {"photo.jxl", "shot.jxl", "anim.jxl"},
        "keep": {"photo.jpg", "shot.png", "scan.pdf"},   # PNG via oxipng in a helper process; PDF via pikepdf
        "keep-lossy": {"shot.png"},                    # PNG via libimagequant in a helper process
        "webp": {"shot.webp", "anim.webp"},
    }
    failed = False
    for fmt, expected in expectations.items():
        out = work / f"out-{fmt}"
        args = [str(exe), "run", str(src), "-y", "--images", fmt.split("-")[0], "--output", "folder",
                "--output-dir", str(out), "--flat", "--min-image-kb", "0", "--min-savings", "0"]
        if fmt == "webp":
            args += ["--jpeg", "lossy"]
        if fmt == "keep-lossy":
            args += ["--png", "lossy"]
        proc = run(args, 300)
        got = {p.name for p in out.rglob("*") if p.is_file()} if out.exists() else set()
        ok = expected <= got and proc.returncode == 0
        failed |= not ok
        print(f"{fmt}: exit {proc.returncode}, outputs {sorted(got)} {'OK' if ok else 'MISSING ' + str(expected - got)}")
    failed |= not gui_ok
    applog = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Wick" / "logs" / "wick.log"
    if applog.exists() and "Helper processes unavailable" in applog.read_text(encoding="utf-8", errors="replace"):
        print("FAILED: helper processes didn't start in the built app (see multiprocessing.freeze_support)")
        failed = True
    if failed:
        log = Path.home() / "AppData" / "Local" / "Wick" / "last-error.txt"
        if log.exists():
            print(log.read_text())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
