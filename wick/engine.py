"""Running a compression job and undoing it."""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import journal, steps, tools, trash_restore
from .log import log
from .fileops import (BIN_NAME, copy_times, final_path, link_or_copy, move_to_trash, notify_shell, read_zone_mark,
                      remove_quietly, temp_name, trash_available, write_zone_mark)
from .formats import ARCHIVE, AUDIO, COMIC, DOC, DOCUMENT, FONT, JPEG, KIND_LABEL, PDF, VIDEO, human
from .images import NotWorthIt, VerificationFailed, convert_image
from .plan import Plan
from .scanner import FileEntry
from .settings import Settings
from .video import convert_video

DONE, SKIPPED, FAILED, CANCELLED = "done", "skipped", "failed", "cancelled"


@dataclass
class FileResult:
    entry: FileEntry
    status: str
    message: str
    output: Path | None = None
    new_size: int = 0
    step: str = ""            # what it was doing when it failed ("Encoding JPEG XL"...)

    @property
    def saved(self) -> int:
        return self.entry.size - self.new_size if self.status == DONE else 0


class JobEvents:
    """Override what you need. Methods may be called from worker threads."""

    def file_started(self, entry: FileEntry) -> None: ...
    def file_progress(self, entry: FileEntry, fraction: float) -> None: ...
    def file_finished(self, result: FileResult) -> None: ...
    def file_step(self, entry: FileEntry, message: str) -> None: ...


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _method(entry: FileEntry, plan: Plan, encoder: str = "") -> str:
    if entry.kind == VIDEO:
        return f"{plan.reason} ({encoder})" if encoder else plan.reason
    if entry.kind in (AUDIO, DOCUMENT):
        return plan.reason
    src = KIND_LABEL.get(entry.kind, entry.kind)
    dst = {".jxl": "JPEG XL", ".webp": "WebP", ".avif": "AVIF", ".heic": "HEIC", ".png": "PNG",
           ".gif": "GIF", ".tif": "TIFF", ".tiff": "TIFF"}.get(plan.target_ext, "JPEG")
    return f"{src} → {dst} ({plan.reason[0].lower() + plan.reason[1:]})"


def _unique(path: Path) -> Path:
    candidate, n = path, 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


NAME_FIELDS = {"%name%": "the original name", "%ext%": "its extension", "%date%": "today (2026-11-06)",
               "%time%": "the time (14-05-09)", "%datetime%": "both", "%unix%": "seconds since 1970",
               "%modified%": "the file's modified date"}


def output_name(src: Path, template: str, now: float | None = None) -> str:
    """The new file's name (without extension). Plain text is a suffix ("_compressed" → photo_compressed);
    with placeholders it's a pattern ("%name% %date%" → photo 2026-11-06). Never contains characters a file
    name can't have."""
    import re
    import time as _time

    if "%" not in template:
        return src.stem + template
    t = _time.time() if now is None else now
    try:
        mtime = src.stat().st_mtime
    except OSError:
        mtime = t
    values = {"%name%": src.stem, "%ext%": src.suffix.lstrip("."),
              "%date%": _time.strftime("%Y-%m-%d", _time.localtime(t)),
              "%time%": _time.strftime("%H-%M-%S", _time.localtime(t)),
              "%datetime%": _time.strftime("%Y-%m-%d %H-%M-%S", _time.localtime(t)),
              "%unix%": str(int(t)), "%modified%": _time.strftime("%Y-%m-%d", _time.localtime(mtime))}
    name = re.sub(r"%[a-z]+%", lambda m: values.get(m.group(0), m.group(0)), template)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or src.stem


def output_path(entry: FileEntry, plan: Plan, s: Settings) -> Path:
    src = entry.path
    if entry.kind == ARCHIVE:          # ("photos.tar.gz" → "photos.7z", not "photos.tar.7z")
        from .archives import archive_ext

        ext = archive_ext(src)
        if ext:
            src = src.with_name(src.name[: -len(ext)] + ".archive")
    if plan.dest is not None:
        return plan.dest
    if s.output_mode == "replace":
        target = src.with_suffix(plan.target_ext)
        return entry.path if str(target).lower() == str(entry.path).lower() else final_path(src, plan.target_ext)
    if s.output_mode == "beside":  # same name, new extension, original kept
        target = src.with_suffix(plan.target_ext)
        if str(target).lower() in (str(src).lower(), str(entry.path).lower()):
            target = src.with_name(f"{output_name(src, s.output_suffix)}{plan.target_ext}")
        return _unique(target)
    if s.output_mode == "suffix":
        return _unique(src.with_name(f"{output_name(src, s.output_suffix)}{plan.target_ext}"))
    folder = Path(s.output_dir)
    rel = Path()
    if s.keep_structure and entry.root is not None:  # root is None for individually added files
        try:
            rel = Path(entry.root.name or "drive") / src.parent.relative_to(entry.root)
        except ValueError:
            rel = Path()
    target = folder / rel / (src.stem + plan.target_ext)
    return _unique(target)


_PLACE_LOCK = threading.Lock()


def _place(entry: FileEntry, st: os.stat_result, tmp: Path, choose: Callable[[], Path], *args) -> Path:
    """Choose the output's name and put it there as one step: files finishing at the same moment (photo.png and
    photo.gif both becoming photo.webp) would otherwise both pick the same free name."""
    with _PLACE_LOCK:
        return place_output(entry, st, tmp, choose(), *args)


def place_output(entry: FileEntry, st: os.stat_result, tmp: Path, dest: Path, s: Settings,
                 writer: journal.RunWriter | None, method: str, reversible: bool = False, sha: str = "") -> Path:
    """Move a finished temp file into place according to the output mode, and journal it.

    Replace mode: the original goes to the Recycle Bin (or is deleted, if allowed and the
    drive has no bin). Other modes: the original is untouched. Raises on failure, leaving
    the original exactly as it was.
    """
    src = entry.path
    new_size = _size(tmp)
    zone_mark = read_zone_mark(src)         # (before the original goes to the bin)
    steps.report("Moving the original to the Trash" if s.output_mode == "replace" else "Saving the result")
    if s.preserve_dates:
        copy_times(st, tmp)
    trashed_at = time.time()
    mode = "copy"
    if s.output_mode != "replace":
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(tmp, dest)
        except OSError:
            shutil.move(str(tmp), str(dest))
            if s.preserve_dates:
                copy_times(st, dest)
    else:
        permanent = not trash_available(src)
        if permanent and not s.allow_permanent_delete:
            raise RuntimeError(f"No {BIN_NAME} on this drive")
        mode = "deleted" if permanent else "replace"
        discard = os.remove if permanent else move_to_trash
        if dest == src:
            # Same name (e.g. .mp4 -> .mp4, PNG -> PNG): original must leave first.
            discard(src)
            try:
                os.replace(tmp, dest)
            except OSError:
                item = None if permanent else trash_restore.find_trashed(src, trashed_at)
                if item:
                    trash_restore.restore(item)
                raise
        else:
            if dest.exists():
                raise FileExistsError(f"{dest.name} appeared while converting")
            os.replace(tmp, dest)
            try:
                discard(src)
            except Exception:
                remove_quietly(dest)  # leave things exactly as they were
                raise
    if not dest.is_dir():
        write_zone_mark(dest, zone_mark)
    notify_shell(created=[] if dest == src else [dest], updated=[dest] if dest == src else [],
                 deleted=[src] if s.output_mode == "replace" and dest != src else [])
    moved_to = ""
    if s.originals_to and s.output_mode != "replace":
        steps.report("Moving the original")
        target = _unique(Path(s.originals_to) / src.name)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
            mode, moved_to = "moved", str(target)
        except OSError as e:
            log.warning("%s: the original stays where it is (%s)", src, e)
    if writer is not None:
        writer.add(journal.FileRecord(
            original=str(src), output=str(dest), original_size=st.st_size, output_size=new_size,
            kind=entry.kind, method=method, reversible=reversible, trashed_at=trashed_at,
            original_mtime_ns=st.st_mtime_ns, original_sha256=sha, mode=mode, moved_to=moved_to,
        ))
    return dest


def process_file(
    entry: FileEntry,
    plan: Plan,
    s: Settings,
    writer: journal.RunWriter | None,
    cancel: threading.Event | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> FileResult:
    src = entry.path
    try:
        st = os.stat(src)
    except OSError:
        return FileResult(entry, SKIPPED, "File no longer exists")
    if st.st_size != entry.size:
        return FileResult(entry, SKIPPED, "File changed since the scan; scan again")

    tmp: Path | None = None
    try:
        encoder = ""
        if entry.kind == VIDEO:
            assert entry.video is not None
            tmp, encoder = convert_video(src, entry.video, plan, s, on_progress, cancel)
        elif entry.kind == AUDIO:
            from .audio import convert_audio

            assert entry.audio is not None
            tmp = convert_audio(src, entry.audio, plan, s, on_progress, cancel)
        elif entry.kind == DOCUMENT:
            from .documents import optimize

            steps.report("Shrinking the pictures inside" if entry.path.suffix.lower() != ".pdf"
                         else "Optimizing the PDF")
            tmp = optimize(src, reduce=s.documents == "reduce")
        else:
            tmp = convert_image(src, entry.kind, entry.image, plan, s)
        if cancel is not None and cancel.is_set():
            raise tools.Cancelled()

        new_size = tmp.stat().st_size
        from . import calibration

        calibration.record(entry, plan, new_size, plan.raw_est or plan.est_size)
        saved_pct = 100.0 * (st.st_size - new_size) / st.st_size if st.st_size else 0
        too_small = saved_pct < s.min_savings_pct or (st.st_size - new_size) < s.min_savings_kb * 1024
        if s.fit_under_kb and st.st_size > s.fit_under_kb * 1024 >= new_size:
            too_small = False             # made to fit a size limit: that's the point, however small the saving
        # Video Save As also honors the savings rule; opting out permits an explicit larger export.
        if (plan.dest is None or (entry.kind == VIDEO and s.require_savings)) and \
                (new_size >= st.st_size or (s.require_savings and too_small)):
            remove_quietly(tmp)
            need = f"{s.min_savings_pct:g}%" + (f" and {human(s.min_savings_kb * 1024)}" if s.min_savings_kb else "")
            return FileResult(entry, SKIPPED, f"Would only save {max(saved_pct, 0):.1f}% "
                              f"({human(max(0, st.st_size - new_size))}); needs {need}; original kept"
                              if new_size < st.st_size else "Result would be larger; original kept")

        sha = _sha256(src) if plan.reversible else ""
        dest = _place(entry, st, tmp, lambda: output_path(entry, plan, s), s, writer,
                      _method(entry, plan, encoder), plan.reversible, sha)
        tmp = None
        return FileResult(entry, DONE, f"Saved {saved_pct:.0f}%" + (f" · {plan.outcome}" if plan.outcome else ""),
                          dest, new_size)

    except tools.Cancelled:
        return FileResult(entry, CANCELLED, "Cancelled; original kept")
    except NotWorthIt as e:
        return FileResult(entry, SKIPPED, str(e))
    except VerificationFailed as e:
        return FileResult(entry, FAILED, f"Verification failed ({e}); original kept")
    except tools.ToolMissing as e:
        return FileResult(entry, FAILED, str(e))
    except Exception as e:  # noqa: BLE001 - report anything, keep going
        log.exception("Failed: %s", entry.path)
        return FileResult(entry, FAILED, f"{_why(e, entry.path)}; original kept")
    finally:
        remove_quietly(tmp)


def _guarded(entry: FileEntry, fn) -> FileResult:
    """Shared error handling for the convert/metadata processors."""
    try:
        return fn()
    except tools.Cancelled:
        return FileResult(entry, CANCELLED, "Cancelled; original kept")
    except NotWorthIt as e:
        return FileResult(entry, SKIPPED, str(e))
    except VerificationFailed as e:
        return FileResult(entry, FAILED, f"Verification failed ({e}); original kept")
    except tools.ToolMissing as e:
        return FileResult(entry, FAILED, str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("Failed: %s", entry.path)
        return FileResult(entry, FAILED, f"{_why(e, entry.path)}; original kept")


def _why(e: BaseException, path: Path) -> str:
    """An error as people should read it: Controlled folder access named, not "access denied"."""
    from . import APP_NAME, cfa

    return f"{cfa.SHORT} (allow {APP_NAME} in Windows Security to change files here)" if cfa.blocked(e, path) \
        else str(e)


def process_convert(entry: FileEntry, plan: Plan, s: Settings, writer, cancel=None, on_progress=None, *,
                    opts) -> FileResult:
    """Convert one file to the format in ``plan.route`` (see convert.py)."""
    from . import convert

    def work() -> FileResult:
        st = os.stat(entry.path)
        tmp = None
        try:
            note = ""
            if entry.kind == VIDEO and plan.route in convert.SOUND_TARGETS:
                tmp, note = convert.extract_audio(entry.path, plan.route, opts, s, on_progress, cancel)
            elif entry.kind == VIDEO and plan.route in convert.ANIMATION_TARGETS:
                assert entry.video is not None
                tmp = convert.video_to_animation(entry.path, entry.video, plan.route, opts, s, on_progress, cancel)
            elif entry.kind == VIDEO and plan.route == "keep":
                tmp = convert.turn_video(entry.path, opts, cancel)
            elif entry.kind == VIDEO:
                assert entry.video is not None
                vs = dataclasses.replace(s, video_max_height=opts.video_height, video_max_fps=opts.video_fps,
                                         require_savings=opts.only_if_smaller)
                tmp = convert.convert_video(entry.path, entry.video, plan.route, vs, on_progress, cancel)
                if opts.turn or opts.flip:
                    encoded = tmp
                    try:
                        tmp = convert.turn_video(encoded, opts, cancel)
                    finally:
                        remove_quietly(encoded)
            elif entry.kind == ARCHIVE:
                tmp = convert.convert_archive(entry.path, plan.route, on_progress, cancel)
            elif entry.kind == COMIC:
                tmp = convert.convert_comic(entry.path, opts, on_progress, cancel)
            elif entry.kind == FONT:
                tmp = convert.convert_font(entry.path, plan.route, opts)
            elif entry.kind == PDF:
                tmp = convert.convert_pdf(entry.path, plan.route, opts, on_progress, cancel)
            elif entry.kind in (DOCUMENT, DOC):
                tmp = convert.convert_document(entry.path, plan.route)
            elif plan.route == "mp4":
                tmp = convert.animation_to_video(entry.path, entry.image, opts, s, on_progress, cancel)
            elif entry.kind == AUDIO:
                tmp = convert.convert_audio(entry.path, entry.audio, plan.route, s, on_progress, cancel, opts)
            else:
                tmp = convert.convert_image(entry.path, entry.kind, entry.image, plan.route, opts, s)
            new_size = _size(tmp)
            if (opts.only_if_smaller or entry.kind == ARCHIVE) and new_size >= st.st_size and not tmp.is_dir():
                return FileResult(entry, SKIPPED, f"Result would be {human(new_size)} (not smaller); original kept")
            made = tmp
            dest = _place(entry, st, tmp, lambda: folder_output_path(entry, s) if made.is_dir() else
                          output_path(entry, plan, s), s, writer, f"{KIND_LABEL.get(entry.kind, entry.kind)} → {plan.reason}")
            tmp = None
            delta = new_size - st.st_size
            change = f"{'+' if delta > 0 else '−'}{human(abs(delta))}"
            if dest.is_dir():
                n = sum(1 for _ in dest.iterdir())
                return FileResult(entry, DONE, f"Converted: {n} file{'s' if n != 1 else ''} in “{dest.name}”",
                                  dest, new_size)
            return FileResult(entry, DONE, f"Converted ({change}{'; ' + note if note else ''})", dest, new_size)
        finally:
            if tmp is not None and tmp.is_dir():
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                remove_quietly(tmp)
    return _guarded(entry, work)


def _size(path: Path) -> int:
    """A file's size, or everything in a folder (a PDF's pages)."""
    if path.is_dir():
        return sum(p.stat().st_size for p in path.iterdir() if p.is_file())
    return path.stat().st_size


def folder_output_path(entry: FileEntry, s: Settings) -> Path:
    """Where a result that is several files goes: a new folder named after the original ("report pages"),
    next to it or in the chosen output folder."""
    src = entry.path
    base = Path(s.output_dir) if s.output_mode == "folder" and s.output_dir else src.parent
    return _unique(base / f"{src.stem} pages")


def process_metadata(entry: FileEntry, plan: Plan, s: Settings, writer, cancel=None, on_progress=None,
                     *, policy: str) -> FileResult:
    from . import metaclean

    def work() -> FileResult:
        st = os.stat(entry.path)
        tmp = None
        try:
            tmp = metaclean.clean(entry.path, policy, entry.video)
            dest = _place(entry, st, tmp, lambda: output_path(entry, plan, s), s, writer,
                          f"Metadata removed: {metaclean.POLICY_LABELS.get(policy, policy).lower()}")
            tmp = None
            return FileResult(entry, DONE, "Metadata removed", dest, dest.stat().st_size)
        finally:
            remove_quietly(tmp)
    return _guarded(entry, work)


def default_workers(s: Settings) -> int:
    return s.image_workers()


def run_job(
    items: list[tuple[FileEntry, Plan]],
    s: Settings,
    root: Path,
    events: JobEvents | None = None,
    cancel: threading.Event | None = None,
    processor=None,
    tool: str = "",
    pause: threading.Event | None = None,
) -> tuple[list[FileResult], str | None]:
    """Process files (images in parallel, then videos one at a time). While ``pause`` is set, no new file
    starts (the ones running finish); the computer is kept awake meanwhile (see power.py).

    Returns (results, run_id or None if nothing was converted).
    """
    events = events or JobEvents()
    cancel = cancel or threading.Event()
    tools.LOW_PRIORITY = s.low_priority
    from .scanner import preload_codecs

    preload_codecs(heif=any(e.kind == "heic" or p.route == "heic" or p.target_ext == ".heic" for e, p in items))
    writer = journal.RunWriter(root, {**s.to_dict(), "tool": tool} if tool else s.to_dict())
    processor = processor or process_file
    results: list[FileResult] = []
    lock = threading.Lock()

    def one(entry: FileEntry, plan: Plan) -> None:
        if s.low_priority:
            tools.lower_thread_priority()
        while pause is not None and pause.is_set() and not cancel.is_set():
            time.sleep(0.25)
        if cancel.is_set():
            r = FileResult(entry, CANCELLED, "Cancelled")
        else:
            events.file_started(entry)
            with steps.reporting(functools.partial(events.file_step, entry)):
                r = processor(entry, plan, s, writer, cancel, functools.partial(events.file_progress, entry))
                if r.status == FAILED and not r.step:
                    r.step = steps.last()
        with lock:
            results.append(r)
        if r.status == FAILED:
            log.warning("%s: %s", entry.path, r.message)
        events.file_finished(r)

    images = [(e, p) for e, p in items if e.kind != VIDEO and p.convert]
    videos = [(e, p) for e, p in items if e.kind == VIDEO and p.convert]
    from . import power
    from .video import parallel_videos

    def run_all(pool: ThreadPoolExecutor, jobs: list[tuple[FileEntry, Plan]]) -> None:
        futures = [pool.submit(one, e, p) for e, p in jobs]
        try:
            for fut in futures:
                fut.result()
        except BaseException:
            # Ctrl+C (or an event handler failing): stop starting new files before the pool's exit waits for
            # the queued ones, which would otherwise all run to the end first.
            cancel.set()
            raise

    with power.awake():
        with ThreadPoolExecutor(max_workers=default_workers(s)) as pool:
            run_all(pool, images)
        # Own threads so priority can be lowered; two at once on NVIDIA GPUs (two hardware encoders).
        with ThreadPoolExecutor(max_workers=parallel_videos(s) if videos else 1) as pool:
            run_all(pool, videos)

    from . import calibration

    writer.close()          # every journal record on disk
    calibration.save()
    counts = {k: sum(r.status == k for r in results) for k in (DONE, SKIPPED, FAILED, CANCELLED)}
    log.info("Job %s%s finished: %s", writer.run.run_id, f" ({tool})" if tool else "",
             ", ".join(f"{v} {k}" for k, v in counts.items() if v) or "nothing to do")
    if not writer.run.files:
        remove_quietly(writer.path)
        return results, None
    return results, writer.run.run_id


def preview_file(entry: FileEntry, plan: Plan, s: Settings) -> Path:
    """Convert one image into a temp file (original untouched). Caller deletes it."""
    if entry.kind == VIDEO:
        raise ValueError("Preview is for images")
    return convert_image(entry.path, entry.kind, entry.image, plan, s)


# ------------------------------------------------------------------ undo

def undo_record(run_id: str, rec: journal.FileRecord) -> tuple[bool, str]:
    if rec.undone:
        return True, "Already undone"
    orig, out = Path(rec.original), Path(rec.output)

    if rec.mode in ("hardlink", "compact"):
        from .cleanup import actions

        try:
            how = actions.undo_hardlink(rec) if rec.mode == "hardlink" else actions.undo_compact(rec)
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        journal.mark_undone(run_id, rec.original, how)
        rec.undone, rec.undo_note = True, how
        return True, how

    if rec.mode == "renamed":
        from .cleanup import actions

        try:
            how = actions.undo_rename(rec)
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        journal.mark_undone(run_id, rec.original, how)
        rec.undone, rec.undo_note = True, how
        return True, how
    if rec.mode == "vdisk":
        return False, "Nothing to undo: compacting only gave unused space back to Windows"
    if rec.mode == "compactos":
        from .cleanup import winclean

        try:
            how = winclean.undo_compact_os()
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        journal.mark_undone(run_id, rec.original, how)
        rec.undone, rec.undo_note = True, how
        return True, how
    if rec.mode == "system":
        return False, "Removed by Windows: it can't be put back"
    if rec.mode == "docker":
        return False, "Removed by Docker: it comes back when a project pulls or builds it again"
    if rec.mode == "removed":  # clean-up: the item went to the bin; nothing else to delete
        orig = Path(rec.original)
        if orig.exists():
            return False, "Something already exists at the original location"
        item = trash_restore.find_trashed(orig, rec.trashed_at)
        if item is None:
            return False, f"It isn't in the {BIN_NAME} any more (emptied?)"
        try:
            trash_restore.restore(item)
        except OSError as e:
            return False, str(e)
        how = f"Restored from {BIN_NAME}"
        journal.mark_undone(run_id, rec.original, how)
        rec.undone, rec.undo_note = True, how
        return True, how

    if rec.mode == "moved":                # the original was moved to a folder: bring it back, remove the result
        moved = Path(rec.moved_to)
        if orig.exists():
            return False, "Something else is at the original's place now"
        if not moved.exists():
            return False, f"The original isn't in {moved.parent} any more"
        shutil.move(str(moved), str(orig))
        if out.is_dir():
            _remove_made_folder(out)
        else:
            remove_quietly(out)
        how = "Moved the original back and removed the converted file"
        journal.mark_undone(run_id, rec.original, how)
        rec.undone, rec.undo_note = True, how
        return True, how

    if rec.mode == "copy":
        if out.is_dir():
            how = _remove_made_folder(out)
        else:
            remove_quietly(out)
            how = "Removed the copy (the original was never changed)"
        journal.mark_undone(run_id, rec.original, how)
        rec.undone, rec.undo_note = True, how
        return True, how

    same_path = orig == out
    aside: Path | None = None
    if not same_path and orig.exists():
        return False, "A file already exists at the original location"
    if same_path:
        if not out.exists():
            return False, "Converted file is missing"
        aside = temp_name(out.parent, out.suffix, "-undo")
        os.rename(out, aside)

    try:
        item = None if rec.mode == "deleted" else trash_restore.find_trashed(orig, rec.trashed_at)
        if item is not None:
            trash_restore.restore(item)
            how = f"Restored from {BIN_NAME}"
        elif rec.reversible and (aside or out).exists():
            _reconstruct_jpeg(aside or out, orig, rec)
            how = "Rebuilt the original JPEG from the JPEG XL file (bit-exact)"
        elif rec.mode == "deleted":
            raise FileNotFoundError(f"Original was deleted permanently (drive had no {BIN_NAME})")
        else:
            raise FileNotFoundError(f"Original isn't in the {BIN_NAME} any more (emptied?)")
    except Exception as e:  # noqa: BLE001
        if aside is not None:
            os.rename(aside, out)
        return False, str(e)

    if same_path:
        remove_quietly(aside)
    elif out.is_dir():            # a folder of pages (PDF → images) made in Replace mode
        _remove_made_folder(out)
    else:
        remove_quietly(out)
    journal.mark_undone(run_id, rec.original, how)
    rec.undone, rec.undo_note = True, how
    return True, how


_MADE = re.compile(r"^page \d+\.(png|jpg|pdf)$")


def _remove_made_folder(folder: Path) -> str:
    """Undo a folder of pages: the files we made go; anything added since stays (and so does the folder)."""
    left = 0
    for p in folder.iterdir():
        if p.is_file() and _MADE.match(p.name):
            remove_quietly(p)
        else:
            left += 1
    if left:
        return f"Removed the pages; {left} other item{'s' if left != 1 else ''} in “{folder.name}” were kept"
    try:
        folder.rmdir()
    except OSError:
        pass
    return "Removed the pages (the original was never changed)"


def cannot_undo(rec: journal.FileRecord, bin_index: dict) -> str:
    """Why ``rec`` can no longer be undone ("" if it can), given ``trash_restore.index(...)`` of its bin."""
    if rec.undone or rec.mode not in ("replace", "removed", "deleted"):
        return ""
    if rec.mode != "deleted" and trash_restore.in_index(bin_index, rec.original, rec.trashed_at):
        return ""
    if rec.mode != "removed" and rec.reversible and Path(rec.output).exists():
        return ""          # the exact JPEG can be rebuilt from its JPEG XL file
    if rec.mode == "deleted":
        return f"The original was deleted for good (its drive has no {BIN_NAME})"
    return f"The original is no longer in the {BIN_NAME} (it was emptied, or the file was removed from it)"


def _reconstruct_jpeg(jxl: Path, orig: Path, rec: journal.FileRecord) -> None:
    tmp = temp_name(jxl.parent, ".jpg", "-rebuild")
    staged = temp_name(jxl.parent, ".jxl", "-in")
    moved = False
    try:
        link_or_copy(jxl, staged)
        tools.run([tools.require("djxl"), staged.name, tmp.name], cwd=jxl.parent, timeout=600)
        if rec.original_sha256 and _sha256(tmp) != rec.original_sha256:
            raise VerificationFailed("Rebuilt JPEG doesn't match the original's checksum")
        if rec.original_mtime_ns:
            os.utime(tmp, ns=(rec.original_mtime_ns, rec.original_mtime_ns))
        os.rename(tmp, orig)
        moved = True
    finally:
        remove_quietly(None if moved else tmp, staged)


def undo_run(run_id: str, progress: Callable[[journal.FileRecord, bool, str], None] | None = None
             ) -> tuple[int, int]:
    run = journal.get_run(run_id)
    if run is None:
        raise FileNotFoundError(f"No run {run_id}")
    ok = failed = 0
    for rec in run.files:
        if rec.undone:
            continue
        try:
            success, msg = undo_record(run_id, rec)
        except Exception as e:  # noqa: BLE001 - one file's problem mustn't stop the rest (as UndoThread)
            log.exception("Undo failed: %s", rec.original)
            success, msg = False, str(e)
        ok += success
        failed += not success
        if progress:
            progress(rec, success, msg)
    return ok, failed


__all__ = ["run_job", "process_file", "undo_run", "undo_record", "preview_file", "output_path",
           "FileResult", "JobEvents", "DONE", "SKIPPED", "FAILED", "CANCELLED", "JPEG"]
