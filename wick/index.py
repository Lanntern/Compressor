"""A small persistent index of file details, so repeat scans don't re-read unchanged files.

Keyed by path; an entry is only used when size and modification time still match.
Stored in SQLite in the app data folder. Safe to delete at any time.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .formats import ImageInfo
from .probe import AudioInfo, StreamInfo, VideoInfo

SCHEMA = 4   # 3: videos carry their frame cadence; 4: Motion Photos, HDR, VFR, interlacing
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_enabled = True


def set_enabled(on: bool) -> None:
    global _enabled
    _enabled = on


def path() -> Path:
    from .journal import data_dir

    return data_dir() / "index.sqlite"


def _db() -> sqlite3.Connection:
    try:
        return _open_db()
    except sqlite3.DatabaseError:
        _rebuild()                  # damaged: it's only a cache, so start it again
        return _open_db()


def _rebuild() -> None:
    """Close and remove a damaged index (and its write-ahead files); the next scan fills a new one."""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except sqlite3.Error:
            pass
        _conn = None
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(path()) + suffix)
        except OSError:
            pass


def _corrupt(e: sqlite3.Error) -> bool:
    return isinstance(e, sqlite3.DatabaseError) and ("malformed" in str(e) or "not a database" in str(e))


def _open_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        p = path()
        _conn = sqlite3.connect(str(p), check_same_thread=False, timeout=10)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        row = _conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
        if row is None or int(row[0]) != SCHEMA:
            _conn.execute("DROP TABLE IF EXISTS files")
            _conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
        _conn.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, "
                      "kind TEXT, info TEXT, seen REAL)")
        _conn.execute("CREATE TABLE IF NOT EXISTS extra (name TEXT, path TEXT, size INTEGER, mtime REAL, "
                      "value TEXT, PRIMARY KEY (name, path))")
        _conn.commit()
    return _conn


def _key(p: Path) -> str:
    return os.path.normcase(str(p))


def lookup(p: Path, size: int, mtime: float, kind: str):
    """ImageInfo / VideoInfo if known and unchanged, else None."""
    if not _enabled:
        return None
    try:
        with _lock:
            row = _db().execute("SELECT size, mtime, kind, info FROM files WHERE path=?", (_key(p),)).fetchone()
    except sqlite3.Error as e:
        if _corrupt(e):
            with _lock:
                _rebuild()
        return None
    if row is None or row[0] != size or abs(row[1] - mtime) > 1e-3 or row[2] != kind:
        return None
    try:
        data = json.loads(row[3])
    except ValueError:
        return None
    if data is None:
        return None
    try:
        if kind == "video":
            streams = [StreamInfo(**s) for s in data.pop("streams", [])]
            return VideoInfo(**data, streams=streams)
        if kind == "audio":
            return AudioInfo(**data)
        return ImageInfo(**data)
    except TypeError:  # stored by an older version with different fields
        return None


def store(items: list[tuple[Path, int, float, str, Any]]) -> None:
    """items: (path, size, mtime, kind, ImageInfo | VideoInfo | None)."""
    if not _enabled or not items:
        return
    now = time.time()
    rows = [(_key(p), size, mtime, kind, json.dumps(asdict(info)) if info is not None else "null", now)
            for p, size, mtime, kind, info in items]
    try:
        with _lock:
            db = _db()
            db.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)", rows)
            db.commit()
    except sqlite3.Error as e:
        if _corrupt(e):
            with _lock:
                _rebuild()


def lookup_value(name: str, p: Path, size: int, mtime: float):
    """A cached computed value (e.g. a photo fingerprint) if the file hasn't changed."""
    if not _enabled:
        return None
    try:
        with _lock:
            row = _db().execute("SELECT size, mtime, value FROM extra WHERE name=? AND path=?",
                                (name, _key(p))).fetchone()
        if row is None or row[0] != size or abs(row[1] - mtime) > 1e-3:
            return None
        return json.loads(row[2])
    except (sqlite3.Error, ValueError):
        return None


def store_values(name: str, items: list[tuple[Path, int, float, object]]) -> None:
    if not _enabled or not items:
        return
    rows = [(name, _key(p), size, mtime, json.dumps(v)) for p, size, mtime, v in items]
    try:
        with _lock:
            db = _db()
            db.executemany("INSERT OR REPLACE INTO extra VALUES (?,?,?,?,?)", rows)
            db.commit()
    except sqlite3.Error as e:
        if _corrupt(e):
            with _lock:
                _rebuild()


def prune(max_age_days: float = 365) -> int:
    """Forget entries not refreshed for a long time (files that were moved or deleted). Returns how many."""
    try:
        with _lock:
            db = _db()
            n = db.execute("DELETE FROM files WHERE seen < ?", (time.time() - max_age_days * 86400,)).rowcount
            db.commit()
        return max(0, n)
    except sqlite3.Error:
        return 0


def clear() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(path()) + suffix)
            except OSError:
                pass


def stats() -> tuple[int, int]:
    """(entries, bytes on disk)."""
    try:
        with _lock:
            n = _db().execute("SELECT COUNT(*) FROM files").fetchone()[0]
        size = sum(os.path.getsize(str(path()) + s) for s in ("", "-wal") if os.path.exists(str(path()) + s))
        return n, size
    except (sqlite3.Error, OSError):
        return 0, 0
