"""Jobs that didn't finish (the app was closed, crashed, or the computer restarted): what's left to do.

When a job starts, its files and options are saved; each file is ticked off as it finishes (an append-only
list, so this costs nothing per file); a job that ends normally (finished or stopped by the user) is
forgotten. Whatever is left when Wick starts again can be resumed.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .journal import data_dir


@dataclass
class Pending:
    tool: str
    started: float
    root: str
    settings: dict
    paths: list[str] = field(default_factory=list)      # every file of the job
    remaining: list[str] = field(default_factory=list)  # not done yet, and still there


def _dir() -> Path:
    d = data_dir() / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _files(tool: str) -> tuple[Path, Path]:
    return _dir() / f"{tool}.json", _dir() / f"{tool}.done"


def start(tool: str, settings: dict, root: str, paths: list[str]) -> None:
    from . import journal

    if not journal.ENABLED:          # no history: no list of file names on disk either (no resuming)
        return
    job, done = _files(tool)
    tmp = job.with_suffix(".tmp")
    tmp.write_text(json.dumps({"tool": tool, "started": time.time(), "root": root, "settings": settings,
                               "paths": paths}), "utf-8")
    os.replace(tmp, job)
    done.write_text("", "utf-8")


def done(tool: str, path: str) -> None:
    _, done_file = _files(tool)
    if not done_file.exists():
        return
    try:
        with open(done_file, "a", encoding="utf-8") as f:
            f.write(path + "\n")
    except OSError:
        pass


def finish(tool: str) -> None:
    for p in _files(tool):
        try:
            p.unlink()
        except OSError:
            pass


def load(tool: str) -> Pending | None:
    job, done_file = _files(tool)
    try:
        data = json.loads(job.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    try:
        finished = {os.path.normcase(line) for line in done_file.read_text("utf-8").splitlines() if line}
    except OSError:
        finished = set()
    paths = [str(p) for p in data.get("paths", [])]
    remaining = [p for p in paths if os.path.normcase(p) not in finished and os.path.isfile(p)]
    return Pending(data.get("tool", tool), float(data.get("started", 0)), str(data.get("root", "")),
                   dict(data.get("settings", {})), paths, remaining)


__all__ = ["Pending", "start", "done", "finish", "load"]
