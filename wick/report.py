"""Reports: every file of a job (or of runs in History) as a CSV that spreadsheets open directly.

UTF-8 with a byte-order mark (Excel then reads accents and other scripts correctly); sizes in bytes, so
they sort and add up; one row per file.
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

HEADER = ["File", "Folder", "Result", "Original size (bytes)", "New size (bytes)", "Saved (bytes)", "Saved (%)",
          "Saved as", "Details", "When"]

_RESULT = {"done": "Done", "skipped": "Left as it was", "failed": "Failed", "cancelled": "Stopped"}


def _pct(before: int, after: int) -> str:
    return f"{100 * (before - after) / before:.1f}" if before else ""


def results_rows(results) -> list[list]:
    """Rows for a job's ``FileResult``s (including the files that were skipped or failed, and why)."""
    rows = []
    now = time.strftime("%Y-%m-%d %H:%M")
    for r in results:
        p = Path(r.entry.path)
        done = r.status == "done"
        after = r.new_size if done else ""
        details = r.message + (f" (while {r.step[0].lower() + r.step[1:]})" if r.status == "failed" and r.step else "")
        rows.append([p.name, str(p.parent), _RESULT.get(r.status, r.status), r.entry.size, after,
                     r.entry.size - r.new_size if done else "", _pct(r.entry.size, r.new_size) if done else "",
                     str(r.output) if done and r.output else "", details, now])
    return rows


def runs_rows(runs) -> list[list]:
    """Rows for History runs (what was changed, and what has been undone since)."""
    rows = []
    for run in runs:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.started))
        for rec in run.files:
            p = Path(rec.original)
            result = f"Undone ({rec.undo_note})" if rec.undone else "Done"
            rows.append([p.name, str(p.parent), result, rec.original_size, rec.output_size,
                         rec.original_size - rec.output_size, _pct(rec.original_size, rec.output_size),
                         rec.output, rec.method, when])
    return rows


def write(rows: list[list], path: Path) -> Path:
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    return path


def default_name() -> str:
    return time.strftime("Wick report %Y-%m-%d %H-%M.csv")


__all__ = ["HEADER", "results_rows", "runs_rows", "write", "default_name"]
