"""Run history: what was converted, so it can be reported and undone.

Each run is a JSON-lines file in the app data folder; records are appended
as files finish, so a crash never loses what was already done.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import APP_NAME


def data_dir() -> Path:
    env = os.environ.get("COMPRESSOR_DATA_DIR")
    if env:
        d = Path(env)
    elif os.name == "nt":
        d = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    else:
        d = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME.lower()
    d.mkdir(parents=True, exist_ok=True)
    return d


ENABLED = True     # Settings › "Keep a history and log": off, nothing about the files changed is written


def runs_dir() -> Path:
    d = data_dir() / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class FileRecord:
    original: str
    output: str
    original_size: int
    output_size: int
    kind: str
    method: str                   # e.g. "JPEG → JXL (lossless, reversible)"
    reversible: bool
    trashed_at: float
    original_mtime_ns: int = 0
    original_sha256: str = ""     # for reversible JPEG->JXL reconstruction checks
    undone: bool = False
    undo_note: str = ""
    mode: str = "replace"         # replace (original in bin) | copy (original untouched) | deleted
    #                               | moved (original moved to ``moved_to``, the result at ``output``)
    moved_to: str = ""


@dataclass
class Run:
    run_id: str
    started: float
    root: str
    settings: dict
    files: list[FileRecord] = field(default_factory=list)

    @property
    def saved_bytes(self) -> int:
        return sum(f.original_size - f.output_size for f in self.files if not f.undone)

    @property
    def active_files(self) -> int:
        return sum(1 for f in self.files if not f.undone)


_id_lock = threading.Lock()


def _new_run_id(claim: bool = True) -> str:
    """Time, process and a counter: two runs started in the same second (Watch Folders and a page, say) are
    never written into one file."""
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid() % 1000:03d}"
    if not claim:
        return stamp
    with _id_lock:
        n = 0
        while True:
            run_id = stamp if n == 0 else f"{stamp}-{n}"
            try:
                (runs_dir() / f"{run_id}.jsonl").open("x").close()     # claimed: nobody else gets this name
                return run_id
            except FileExistsError:
                n += 1


class RunWriter:
    """Appends records of an in-progress run (thread-safe)."""

    def __init__(self, root: Path, settings: dict):
        self.recording = ENABLED          # (decided once, so a run is never half written)
        self.run = Run(_new_run_id(self.recording), time.time(), str(root), settings)
        self.path = runs_dir() / f"{self.run.run_id}.jsonl"
        self._lock = threading.Lock()
        self._append({"type": "run", "run_id": self.run.run_id, "started": self.run.started,
                      "root": self.run.root, "settings": settings}, sync=True)

    SYNC_EVERY = 0.5   # seconds between forced disk syncs (each record is still written immediately)

    def _append(self, obj: dict, sync: bool = False) -> None:
        if not self.recording:
            return
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                f.flush()   # in the OS right away: survives the app crashing
                # A disk sync per file costs ~5-20 ms; at most twice a second it's nearly free, and the run's
                # end syncs everything (close()).
                now = time.monotonic()
                if sync or now - getattr(self, "_synced", 0.0) >= self.SYNC_EVERY:
                    os.fsync(f.fileno())
                    self._synced = now
                else:
                    self._pending = True

    def close(self) -> None:
        """Make sure every record is on disk (end of the run)."""
        with self._lock:
            if getattr(self, "_pending", False) and self.path.exists():
                with open(self.path, "a", encoding="utf-8") as f:
                    os.fsync(f.fileno())
                self._pending = False

    def add(self, rec: FileRecord) -> None:
        self.run.files.append(rec)
        self._append({"type": "file", **asdict(rec)})


def mark_undone(run_id: str, original: str, note: str) -> None:
    path = runs_dir() / f"{run_id}.jsonl"
    if not path.exists():
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "undo", "original": original, "note": note,
                            "at": time.time()}, ensure_ascii=False) + "\n")


def load_run(path: Path) -> Run | None:
    run = None
    by_original: dict[str, FileRecord] = {}
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # torn last line after a crash
        t = obj.pop("type", None)
        if t == "run":
            run = Run(obj["run_id"], obj["started"], obj["root"], obj.get("settings", {}))
        elif t == "file" and run is not None:
            known = {k: v for k, v in obj.items() if k in FileRecord.__dataclass_fields__}
            rec = FileRecord(**known)
            run.files.append(rec)
            by_original[rec.original] = rec
        elif t == "undo" and obj.get("original") in by_original:
            rec = by_original[obj["original"]]
            rec.undone = True
            rec.undo_note = obj.get("note", "")
    return run


_cache: dict[str, tuple[int, int, Run | None]] = {}   # path -> (mtime_ns, size, parsed run)
_cache_lock = threading.Lock()


def _load_cached(p: Path) -> Run | None:
    """load_run, re-reading a file only when it changed (undo appends to it)."""
    try:
        st = p.stat()
    except OSError:
        return None
    key = str(p)
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    run = load_run(p)
    with _cache_lock:
        _cache[key] = (st.st_mtime_ns, st.st_size, run)
    return run


def signature() -> tuple:
    """Changes whenever any run is added, changed or removed (cheap: one directory listing)."""
    out = []
    for p in runs_dir().glob("*.jsonl"):
        try:
            st = p.stat()
            out.append((p.name, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    return tuple(sorted(out))


def list_runs() -> list[Run]:
    runs = [r for p in runs_dir().glob("*.jsonl") if (r := _load_cached(p)) is not None]
    runs = [r for r in runs if r.files]
    runs.sort(key=lambda r: r.started, reverse=True)
    return runs


def delete_runs(run_ids) -> int:
    """Remove runs from History (their files can't be undone from here afterwards). Returns how many."""
    removed = 0
    for run_id in run_ids:
        p = runs_dir() / f"{run_id}.jsonl"
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
        _cache.pop(str(p), None)
    return removed


def forget_files(run_id: str, originals) -> int:
    """Remove some files' records (names included) from a run; the run goes when none are left."""
    p = runs_dir() / f"{run_id}.jsonl"
    drop = set(originals)
    try:
        lines = p.read_text("utf-8").splitlines()
    except OSError:
        return 0
    kept, removed, files_left = [], 0, 0
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") in ("file", "undo") and obj.get("original") in drop:
            removed += obj.get("type") == "file"
            continue
        files_left += obj.get("type") == "file"
        kept.append(line)
    if not files_left:
        delete_runs([run_id])
    else:
        tmp = p.with_suffix(".tmp")
        tmp.write_text("\n".join(kept) + "\n", "utf-8")
        os.replace(tmp, p)
        _cache.pop(str(p), None)
    return removed


def clear() -> int:
    """Delete the whole History."""
    return delete_runs([p.stem for p in runs_dir().glob("*.jsonl")])


def prune(max_age_days: float) -> int:
    """Delete history older than ``max_age_days`` (those runs can no longer be undone). Returns how many."""
    if max_age_days <= 0:
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for p in runs_dir().glob("*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff:  # appended to on every undo, so mtime = last activity
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def get_run(run_id: str) -> Run | None:
    return load_run(runs_dir() / f"{run_id}.jsonl")
