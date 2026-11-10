"""Command-line interface. ``wick`` with no arguments opens the GUI."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

from . import __version__, journal, tools
from .engine import DONE, FAILED, FileResult, JobEvents, run_job, undo_run
from .plan import plan_entry
from .scanner import scan
from .formats import human
from .settings import (IMAGE_FORMATS, IMAGE_MODES, JPEG_SUBSAMPLING, METADATA_MODES, OUTPUT_MODES, SMART_TARGETS,
                       AUDIO_FORMATS, AUDIO_LOSSLESS_MODES, AUDIO_LOSSY_MODES, AUDIO_QUALITIES,
                       VIDEO_CODECS, VIDEO_ENCODERS, VIDEO_QUALITIES, Settings)


def _add_settings_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("paths", type=Path, nargs="+", help="folders and/or files")
    g = p.add_argument_group("images")
    g.add_argument("--images", choices=IMAGE_FORMATS, default="jxl",
                   help="target format; 'keep' optimises within the same format")
    g.add_argument("--jpeg", choices=IMAGE_MODES, default="lossless")
    g.add_argument("--png", choices=IMAGE_MODES, default="lossless", help="PNG/GIF/BMP/TIFF/lossless WebP")
    g.add_argument("--quality", type=int, default=90, help="quality for lossy images (1-100)")
    g.add_argument("--effort", type=int, default=7, help="JPEG XL effort (1-9)")
    g.add_argument("--baseline", action="store_true", help="baseline (not progressive) JPEG output")
    g.add_argument("--subsampling", choices=JPEG_SUBSAMPLING, default="auto", help="JPEG chroma subsampling")
    r = g.add_mutually_exclusive_group()
    r.add_argument("--resize-long-edge", type=int, metavar="PX")
    r.add_argument("--resize-fit", metavar="WxH")
    r.add_argument("--resize-percent", type=int, metavar="P")
    g.add_argument("--metadata", choices=METADATA_MODES, default="keep")
    g = p.add_argument_group("videos")
    g.add_argument("--video", choices=VIDEO_CODECS, default="av1")
    g.add_argument("--encoder", choices=VIDEO_ENCODERS, default="auto")
    g.add_argument("--video-quality", choices=VIDEO_QUALITIES, default="balanced")
    g.add_argument("--max-height", type=int, default=0, metavar="PX",
                   help="limit video resolution (short side, e.g. 1080); 0 = keep")
    g.add_argument("--max-fps", type=int, default=0, help="cap the video frame rate; 0 = keep")
    g.add_argument("--audio-bitrate", type=int, default=0, metavar="KBPS", help="re-encode audio; 0 = copy")
    g.add_argument("--no-film-grain", action="store_true",
                   help="AV1: don't store grainy videos without their grain (re-created on playback)")
    g.add_argument("--whole", action="store_true",
                   help="encode videos as a whole, not scene by scene (no parts side by side, no carrying on)")
    g.add_argument("--video-metric", choices=("auto", "vmaf", "ssimulacra2", "xpsnr", "butteraugli"),
                   default="auto", help="how Smart quality measures videos")
    g = p.add_argument_group("music and audio")
    g.add_argument("--audio-lossless", choices=AUDIO_LOSSLESS_MODES, default="flac",
                   help="WAV/AIFF/FLAC/ALAC/APE: flac (lossless, verified), lossy (--audio-format) or keep")
    g.add_argument("--audio-lossy", choices=AUDIO_LOSSY_MODES, default="keep",
                   help="MP3/AAC/Vorbis/WMA/Opus: keep, or reencode when the bitrate is well above the target")
    g.add_argument("--audio-format", choices=AUDIO_FORMATS, default="opus")
    g.add_argument("--audio-quality", choices=AUDIO_QUALITIES, default="balanced")
    g.add_argument("--keep-stereo", action="store_true", help="don't store identical-channel stereo as mono")
    g.add_argument("--cd-quality", action="store_true", help="reduce hi-res audio to 44.1/48 kHz, 16-bit")
    g.add_argument("--min-audio-kb", type=int, default=100)
    g = p.add_argument_group("quality")
    g.add_argument("--smart", choices=SMART_TARGETS, metavar="TARGET",
                   help="Smart quality: per file, the smallest result that measures at least "
                        "identical / high / good (slower)")
    g.add_argument("--preset", metavar="NAME",
                   help="use a preset's compression options (see `wick presets`); "
                        "output and rule flags still apply")
    g = p.add_argument_group("output")
    g.add_argument("--output", choices=OUTPUT_MODES, default="replace",
                   help="replace: original to Recycle Bin; beside: same name, new type, original kept; "
                        "suffix: new file next to it; folder: --output-dir")
    g.add_argument("--suffix", default="_compressed")
    g.add_argument("--output-dir", default="")
    g.add_argument("--flat", action="store_true", help="don't recreate subfolders in --output-dir")
    g.add_argument("--allow-delete", action="store_true",
                   help="replace originals on drives without a Recycle Bin (permanent!)")
    g = p.add_argument_group("rules")
    g.add_argument("--min-savings", type=float, default=5.0, help="only keep results saving at least this percent")
    g.add_argument("--min-savings-kb", type=int, default=0, help="...and at least this many KB")
    g.add_argument("--keep-any-saving", action="store_true", help="keep every result that is smaller at all")
    g.add_argument("--new-dates", action="store_true", help="don't copy the original file dates to results")
    g.add_argument("--min-image-kb", type=int, default=16)
    g.add_argument("--min-video-mb", type=int, default=10)
    g.add_argument("--include-program-folders", action="store_true")
    g.add_argument("--include-hidden", action="store_true")
    g.add_argument("--workers", type=int, default=0, help="parallel image jobs (0 = auto)")
    g.add_argument("--normal-priority", action="store_true", help="don't lower encoder priority")


def _settings(a: argparse.Namespace) -> Settings:
    s = Settings(
        image_format=a.images, jpeg_mode=a.jpeg, png_mode=a.png, image_quality=a.quality,
        image_effort=a.effort, jpeg_progressive=not a.baseline, jpeg_subsampling=a.subsampling,
        metadata=a.metadata,
        video_codec=a.video, video_encoder=a.encoder, video_quality=a.video_quality,
        output_mode=a.output, output_suffix=a.suffix, output_dir=a.output_dir,
        keep_structure=not a.flat, allow_permanent_delete=a.allow_delete,
        min_savings_pct=a.min_savings, min_image_kb=a.min_image_kb, min_video_mb=a.min_video_mb,
        min_savings_kb=a.min_savings_kb, require_savings=not a.keep_any_saving, preserve_dates=not a.new_dates,
        skip_program_folders=not a.include_program_folders, include_hidden=a.include_hidden,
        workers=a.workers, low_priority=not a.normal_priority,
        video_max_height=a.max_height, video_max_fps=a.max_fps, audio_bitrate=a.audio_bitrate,
        smart_quality=bool(a.smart), smart_target=a.smart or "identical",
        audio_lossless=a.audio_lossless, audio_lossy=a.audio_lossy, audio_format=a.audio_format,
        audio_quality=a.audio_quality, audio_mono=not a.keep_stereo, audio_cd_quality=a.cd_quality,
        min_audio_kb=a.min_audio_kb, video_grain="off" if a.no_film_grain else "auto",
        video_chunked=not a.whole, video_metric=a.video_metric,
    )
    if a.resize_long_edge:
        s.resize_mode, s.resize_long_edge = "long_edge", a.resize_long_edge
    elif a.resize_fit:
        try:
            w, h = (int(x) for x in a.resize_fit.lower().split("x"))
        except ValueError:
            sys.exit("--resize-fit must look like 1920x1080")
        s.resize_mode, s.resize_width, s.resize_height = "fit", w, h
    elif a.resize_percent:
        s.resize_mode, s.resize_percent = "percent", a.resize_percent
    if a.preset:
        from . import presets

        if presets.get(a.preset) is None:
            sys.exit(f"No preset called {a.preset!r}. Available: {', '.join(presets.names())}")
        s = presets.apply(a.preset, s)
    s.validate()
    return s


def _plan(a: argparse.Namespace):
    s = _settings(a)
    missing = [str(p) for p in a.paths if not p.exists()]
    if missing:
        sys.exit("Not found: " + ", ".join(missing))
    result = scan(a.paths, s, progress=lambda m, d, t: print(f"\r{m[:100]:<100}", end="", file=sys.stderr),
                  picked=True)              # (files named on the command line were chosen: no size minimums)
    print("\r" + " " * 100 + "\r", end="", file=sys.stderr)
    items = [(e, plan_entry(e, s)) for e in result.entries]
    return s, result, items


def cmd_scan(a: argparse.Namespace) -> int:
    s, result, items = _plan(a)
    todo = [(e, p) for e, p in items if p.convert]
    if a.json:
        print(json.dumps([{
            "path": str(e.path), "kind": e.kind, "size": e.size, "convert": p.convert,
            "reason": p.reason, "target_ext": p.target_ext, "est_size": p.est_size,
        } for e, p in items], indent=2, ensure_ascii=False))
        return 0
    for e, p in items:
        if p.convert:
            print(f"  {human(e.size):>9} → ~{human(p.est_size):>9}  {e.path}  [{p.reason}]")
        elif a.verbose:
            print(f"  {human(e.size):>9}   skip        {e.path}  [{p.reason}]")
    total = sum(e.size for e, _ in todo)
    est = sum(p.est_size for _, p in todo)
    print(f"\n{len(todo)} of {len(items)} files can be compressed: {human(total)} → ~{human(est)} "
          f"(save ~{human(total - est)})")
    if result.skipped_program_dirs:
        print(f"Skipped {len(result.skipped_program_dirs)} program/game folder(s).")
    if result.cloud_only_files:
        print(f"Skipped {result.cloud_only_files} cloud-only file(s) (OneDrive etc.).")
    return 0


class _CliEvents(JobEvents):
    def __init__(self, total: int):
        self.total, self.done = total, 0
        self.lock = threading.Lock()

    def file_progress(self, entry, fraction):
        print(f"\r  {fraction * 100:5.1f}%  {entry.path.name[:70]}", end="", file=sys.stderr)

    def file_finished(self, r: FileResult):
        with self.lock:
            self.done += 1
            mark = {"done": "OK  ", "skipped": "skip", "failed": "FAIL", "cancelled": "stop"}[r.status]
            extra = f" → {r.output.name} ({human(r.entry.size)} → {human(r.new_size)})" if r.status == DONE else ""
            print(f"\r[{self.done}/{self.total}] {mark} {r.entry.path}{extra}  {r.message}" + " " * 10)


def cmd_run(a: argparse.Namespace) -> int:
    s, result, items = _plan(a)
    todo = [(e, p) for e, p in items if p.convert]
    if not todo:
        print("Nothing to compress.")
        return 0
    total = sum(e.size for e, _ in todo)
    print(f"{len(todo)} files, {human(total)} (estimated saving ~{human(total - sum(p.est_size for _, p in todo))}).")
    print("Originals go to the Recycle Bin/Trash after each converted file is verified.")
    if not a.yes and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        return 1
    cancel = threading.Event()
    try:
        results, run_id = run_job(todo, s, result.root, _CliEvents(len(todo)), cancel)
    except KeyboardInterrupt:
        cancel.set()
        print("\nCancelled.")
        return 130
    saved = sum(r.saved for r in results)
    failed = sum(r.status == FAILED for r in results)
    print(f"\nDone: {sum(r.status == DONE for r in results)} converted, {failed} failed, saved {human(saved)}.")
    if run_id:
        print(f"Undo with: wick undo {run_id}")
        print("Space is freed once you empty the Recycle Bin/Trash.")
    return 1 if failed else 0


def cmd_history(a: argparse.Namespace) -> int:
    runs = journal.list_runs()
    if not runs:
        print("No runs yet.")
        return 0
    import time
    for r in runs:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.started))
        print(f"{r.run_id}  {when}  {r.active_files:>4} files  saved {human(r.saved_bytes):>9}  {r.root}")
        if a.verbose:
            for f in r.files:
                state = f"undone: {f.undo_note}" if f.undone else f.method
                print(f"      {f.original} → {Path(f.output).name}  [{state}]")
    return 0


def cmd_undo(a: argparse.Namespace) -> int:
    def report(rec, ok, msg):
        print(f"{'OK  ' if ok else 'FAIL'} {rec.original}  {msg}")
    try:
        ok, failed = undo_run(a.run_id, report)
    except FileNotFoundError as e:
        print(e)
        return 1
    print(f"{ok} restored, {failed} failed.")
    return 1 if failed else 0


def cmd_presets(a: argparse.Namespace) -> int:
    from . import presets

    for name in presets.names():
        print(f"{name}\n    {presets.description(name)}")
    return 0


def cmd_tools(a: argparse.Namespace) -> int:
    from .video import available_encoders

    for name, path in tools.tool_status().items():
        print(f"{name:8} {path or 'MISSING'}")
    print()
    for enc, ok in available_encoders().items():
        print(f"{enc:12} {'available' if ok else 'not available'}")
    return 0


def _installer_setup(args: list[str]) -> int:
    """``context=1 autostart=0 updates=weekly upgrade=0``: what was chosen in the installer. Ticked integrations
    are added; unticked ones are left as they are (on an upgrade they may have been turned on in Settings since).
    ``updates`` is how often to check (launch, weekly, monthly or never; older installers sent 1 or 0). It's only
    set on a first install, so a choice made in Settings later is kept."""
    from PySide6.QtCore import QSettings

    from . import APP_NAME
    from .gui import integration

    from . import updates

    opts = dict(a.split("=", 1) for a in args if "=" in a)
    on = {k: v == "1" for k, v in opts.items()}
    try:
        integration.register_file_types()        # Default apps and Open with (never the default for anything)
    except OSError:
        pass
    for key, add in (("context", integration.install_context_menu), ("autostart", lambda: integration.set_autostart(True))):
        if on.get(key):
            try:
                add()
            except Exception:  # noqa: BLE001 - the app's Settings can still turn it on
                pass
    from .gui.main_window import carry_over_settings

    carry_over_settings()          # (before writing anything under the new name, which would stop the carry-over)
    path = os.environ.get("WICK_SETTINGS_FILE")
    qs = QSettings(path, QSettings.IniFormat) if path else QSettings(APP_NAME, APP_NAME)
    freq = {"1": updates.DEFAULT_FREQUENCY, "0": "never"}.get(opts.get("updates", ""), opts.get("updates", ""))
    chosen_before = qs.contains("updates/frequency") or qs.contains("updates/auto")
    if freq in updates.FREQUENCIES and not on.get("upgrade") and not chosen_before:
        qs.setValue("updates/frequency", freq)
    qs.sync()
    return 0


def main(argv: list[str] | None = None) -> int:
    import os

    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "mcp":       # an AI agent's connection (stdio): before anything touches stdout
        from .mcp_server import main as mcp_main

        return mcp_main(args[1:])

    # The windowed .exe has no console: give print() somewhere harmless to write.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    # Windows consoles/pipes default to a legacy code page: "→" or "åäö" in a path
    # must never crash the program.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    trace = os.environ.get("WICK_TRACE")
    if trace:  # diagnostics: dump every thread's stack periodically
        import faulthandler

        faulthandler.dump_traceback_later(20, repeat=True, file=open(trace, "a", encoding="utf-8"))  # noqa: SIM115
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--uninstall-cleanup":   # the uninstaller: undo what the app added to Windows
        from .gui import integration

        from .gui import toasts

        from .gui import jumplist

        def old_temp_files() -> None:          # (versions before 0.1.1 kept their UI images in %TEMP%)
            import shutil
            import tempfile

            shutil.rmtree(os.path.join(tempfile.gettempdir(), "wick-ui"), ignore_errors=True)

        for undo in (lambda: integration.uninstall_context_menu(wait=True), lambda: integration.set_autostart(False),
                     toasts.unregister, integration.unregister_file_types, jumplist.clear, old_temp_files):
            try:
                undo()
            except Exception:  # noqa: BLE001 - remove what can be removed
                pass
        return 0
    if argv and argv[0] == "--installer-setup":   # the installer: apply the choices made on its options page
        return _installer_setup(argv[1:])
    if argv and argv[0] == "--ntfs-dump":       # the administrator helper for fast drive scans (ntfs.py)
        from .ntfs import _main as ntfs_main

        return ntfs_main(argv)
    commands = {"scan", "run", "history", "undo", "tools", "presets", "speedtest", "gui", "mcp", "-h", "--help",
                "--version"}
    opens_app = not argv or (argv[0] not in commands and (
        argv[0] in ("--background", "--preset", "--convert", "--open", "--url")
        or argv[0].startswith(("--preset=", "--convert=", "--open="))
        or not argv[0].startswith("-")))
    if opens_app:
        # No command: open the app (paths from Explorer's context menu or drag onto the .exe).
        from .gui.app import main as gui_main
        return gui_main(argv)

    ap = argparse.ArgumentParser(prog="wick", description="Compress images, videos and audio safely.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="show what would be compressed (changes nothing)")
    _add_settings_args(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true", help="also list skipped files")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("run", help="compress a folder (images, videos and audio)")
    _add_settings_args(p)
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("history", help="list past runs")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("undo", help="restore the originals of a run")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("speedtest", help="compare parallel settings on generated samples (changes nothing)")
    p.set_defaults(func=lambda a: (print(__import__("wick.benchmark", fromlist=["run"]).run(
        lambda m: print(m, file=sys.stderr))), 0)[1])

    p = sub.add_parser("presets", help="list presets")
    p.set_defaults(func=cmd_presets)

    p = sub.add_parser("tools", help="show encoder tools and GPU support")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("gui", help="open the app window")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=lambda a: __import__("wick.gui.app", fromlist=["main"]).main(a.paths))

    a = ap.parse_args(argv)
    if a.cmd in ("run", "undo"):
        from .log import setup as setup_log

        setup_log()
    return a.func(a)
