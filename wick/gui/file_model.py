"""Table model for scanned files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from ..engine import CANCELLED, DONE, FAILED, SKIPPED, FileResult
from ..mediainfo import cadence_note, describe
from ..formats import KIND_LABEL, human
from .filelist import format_date
from ..plan import Plan, plan_entry
from ..scanner import FileEntry
from ..settings import Settings
from .theme import muted_color
from .widgets import PROGRESS_ROLE, STATUS_ROLE

SORT_ROLE = Qt.UserRole + 1
COLUMNS = ("", "Name", "Folder", "Type", "Details", "Size", "After", "Created", "Modified", "Status")
_TARGET_NAME = {".jxl": "JPEG XL", ".webp": "WebP", ".avif": "AVIF", ".heic": "HEIC", ".png": "PNG",
                ".gif": "GIF", ".jpg": "JPEG", ".jpeg": "JPEG", ".tif": "TIFF", ".tiff": "TIFF",
                ".mp4": "MP4", ".mkv": "MKV", ".webm": "WebM"}


def _short_folder(e) -> str:
    """Folder relative to what the user added (full path is in the tooltip)."""
    root = e.root
    if root is not None:
        try:
            rel = e.path.parent.relative_to(root)
            return str(Path(root.name) / rel) if str(rel) != "." else root.name
        except ValueError:
            pass
    return e.path.parent.name or str(e.path.parent)
C_CHECK, C_NAME, C_FOLDER, C_TYPE, C_INFO, C_SIZE, C_EST, C_CREATED, C_MODIFIED, C_STATUS = range(10)



@dataclass
class Row:
    entry: FileEntry
    plan: Plan
    checked: bool
    result: FileResult | None = None
    progress: float | None = None
    running: bool = False
    step: str = ""                # what it's doing while running ("Encoding JPEG XL")
    created: float | None = -1.0  # read when first shown (-1: not yet)

    def created_at(self) -> float | None:
        if self.created == -1.0:
            from .filelist import created_time

            self.created = created_time(self.entry.path)
        return self.created


def _pct(before: int, after: int) -> str:
    """Change in size as a signed percentage: "−34%"."""
    if not before:
        return "–"
    p = round(100 * (after - before) / before)
    return f"−{-p}%" if p < 0 else f"+{p}%"


class FileTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[Row] = []
        self._index: dict[int, int] = {}
        self.locked = False  # no edits while a job runs

    # ---- data loading
    def set_entries(self, entries: list[FileEntry], settings: Settings) -> None:
        self.beginResetModel()
        self.rows = []
        for e in entries:
            p = plan_entry(e, settings)
            self.rows.append(Row(e, p, p.convert))
        self._index = {id(r.entry): i for i, r in enumerate(self.rows)}
        self.endResetModel()

    def remove_rows(self, rows: list[Row]) -> None:
        """Take rows off the list; the others keep their ticks and results."""
        gone = {id(r) for r in rows}
        self.beginResetModel()
        self.rows = [r for r in self.rows if id(r) not in gone]
        self._index = {id(r.entry): i for i, r in enumerate(self.rows)}
        self.endResetModel()

    def add_entries(self, entries: list[FileEntry], settings: Settings) -> None:
        known = set(self._index)
        new = [e for e in entries if id(e) not in known]
        if not new:
            return
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows) + len(new) - 1)
        for e in new:
            p = plan_entry(e, settings)
            self._index[id(e)] = len(self.rows)
            self.rows.append(Row(e, p, p.convert))
        self.endInsertRows()

    def replan(self, settings: Settings) -> None:
        for r in self.rows:
            if r.result is not None and r.result.status == DONE:
                continue
            was = r.plan.convert
            r.plan = plan_entry(r.entry, settings)
            if r.plan.convert != was:
                r.checked = r.plan.convert
            elif not r.plan.convert:
                r.checked = False
            r.result = None
        self._changed_all()

    def row_of(self, entry: FileEntry) -> int | None:
        return self._index.get(id(entry))

    def selected_items(self) -> list[tuple[FileEntry, Plan]]:
        return [(r.entry, r.plan) for r in self.rows
                if r.checked and r.plan.convert and not (r.result and r.result.status == DONE)]

    def failed_count(self) -> int:
        return sum(1 for r in self.rows if r.result and r.result.status in (FAILED, CANCELLED))

    def select_failed_only(self) -> int:
        """Check only failed/cancelled rows (and clear their result) so they can be re-run."""
        n = 0
        for r in self.rows:
            if r.result and r.result.status in (FAILED, CANCELLED):
                r.result, r.checked = None, r.plan.convert
                n += r.checked
            elif not (r.result and r.result.status == DONE):
                r.checked = False
        self._changed_all()
        return n

    def set_all_checked(self, checked: bool) -> None:
        for r in self._selectable_rows():
            r.checked = checked
        self._changed_all()

    def _selectable_rows(self) -> list:
        """Rows that can be selected: ones that would be compressed and haven't been yet."""
        return [r for r in self.rows if r.plan.convert and not (r.result and r.result.status == DONE)]

    def selectable(self) -> int:
        return len(self._selectable_rows())

    def check_state(self) -> Qt.CheckState:
        """Select-all state over the rows that can be selected."""
        rows = self._selectable_rows()
        n = sum(r.checked for r in rows)
        return (Qt.Unchecked if not rows or n == 0 else Qt.Checked if n == len(rows) else Qt.PartiallyChecked)

    def totals(self) -> tuple[int, int, int]:
        """(selected count, selected bytes, estimated bytes after)."""
        sel = [r for r in self.rows if r.checked and r.plan.convert
               and not (r.result and r.result.status == DONE)]
        return len(sel), sum(r.entry.size for r in sel), sum(r.plan.est_size for r in sel)

    # ---- job updates
    def mark_running(self, entry: FileEntry, running: bool = True) -> None:
        i = self.row_of(entry)
        if i is None:
            return
        self.rows[i].running = running
        self.rows[i].progress = 0.0 if running else None
        self.rows[i].step = ""
        self._changed_row(i)

    def set_step(self, entry: FileEntry, message: str) -> None:
        i = self.row_of(entry)
        if i is not None:
            self.rows[i].step = message
            self._changed_row(i, C_STATUS)

    def set_progress(self, entry: FileEntry, fraction: float) -> None:
        i = self.row_of(entry)
        if i is not None:
            self.rows[i].progress = fraction
            self._changed_row(i, C_STATUS)

    def set_result(self, result: FileResult) -> None:
        i = self.row_of(result.entry)
        if i is None:
            return
        r = self.rows[i]
        r.result, r.running, r.progress = result, False, None
        if result.status in (DONE, SKIPPED):
            r.checked = False
        self._changed_row(i)

    # ---- Qt model API
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return COLUMNS[section]
        if role == Qt.TextAlignmentRole:
            right = section in (C_SIZE, C_EST)
            return int((Qt.AlignRight if right else Qt.AlignLeft) | Qt.AlignVCenter)
        if role == Qt.AccessibleTextRole and section == C_CHECK:
            return "Include"
        return None

    def flags(self, index):
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        r = self.rows[index.row()]
        if index.column() == C_CHECK and r.plan.convert and not self.locked \
                and not (r.result and r.result.status == DONE):
            f |= Qt.ItemIsUserCheckable
        return f

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        c = index.column()
        e = r.entry

        if role == Qt.CheckStateRole and c == C_CHECK:
            if r.result and r.result.status == DONE:
                return None             # done: nothing left to choose (its status says what happened)
            return Qt.Checked if r.checked and r.plan.convert else Qt.Unchecked

        if role == Qt.DisplayRole:
            if c == C_NAME:
                return e.path.name
            if c == C_FOLDER:
                return _short_folder(e)
            if c == C_TYPE:
                label = KIND_LABEL.get(e.kind, e.kind)
                if e.image is not None and e.image.animated:
                    label += " (animated)"
                return label
            if c == C_INFO:
                return describe(e)
            if c == C_SIZE:
                return human(e.size)
            if c == C_EST:  # the size after compression (estimated until done), with the change in percent
                if r.result and r.result.status == DONE:
                    return f"{human(r.result.new_size)} ({_pct(e.size, r.result.new_size)})"
                return f"~{human(r.plan.est_size)} ({_pct(e.size, r.plan.est_size)})" if r.plan.convert else "–"
            if c == C_CREATED:
                return format_date(r.created_at())
            if c == C_MODIFIED:
                return format_date(e.mtime)
            if c == C_STATUS:
                return self._status_text(r)

        if role == SORT_ROLE:
            if c == C_CREATED:
                return r.created_at() or 0.0
            if c == C_MODIFIED:
                return e.mtime
            if c == C_SIZE:
                return e.size
            if c == C_EST:
                return r.result.new_size if r.result and r.result.status == DONE else r.plan.est_size
            if c == C_CHECK:
                return int(r.checked)
            return self.data(index, Qt.DisplayRole) or ""

        if role == STATUS_ROLE and c == C_STATUS:
            if r.running:
                return "running"
            if r.result:
                return {DONE: "done", FAILED: "failed"}.get(r.result.status, "skipped")
            if not r.plan.convert:
                return "excluded"
            return "ready" if r.checked else "skipped"
        if role == PROGRESS_ROLE and c == C_STATUS:
            return r.progress if r.running else None
        if role == Qt.AccessibleTextRole and c == C_CHECK:
            return "Will be compressed" if r.checked and r.plan.convert else "Not selected"

        if role == Qt.ToolTipRole:
            if c in (C_NAME, C_FOLDER):
                return str(e.path)
            if c == C_STATUS:
                return self._status_text(r)
            if c == C_INFO:
                note = cadence_note(e.video) if e.video is not None else ""
                return describe(e) + (f"\n{note}" if note else "")

        if role == Qt.ForegroundRole and c != C_STATUS:
            if not r.plan.convert and not r.result:
                return muted_color()

        if role == Qt.TextAlignmentRole and c in (C_SIZE, C_EST):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role == Qt.CheckStateRole and index.column() == C_CHECK and not self.locked:
            r = self.rows[index.row()]
            if not r.plan.convert:
                return False
            r.checked = Qt.CheckState(value) == Qt.Checked
            self._changed_row(index.row())
            return True
        return False

    @staticmethod
    def _status_text(r: Row) -> str:
        # Plain text: the delegate adds the symbol; screen readers read this as-is.
        if r.running:
            what = r.step or "Compressing"
            return f"{what}… {r.progress * 100:.0f}%" if r.progress else f"{what}…"
        if r.result:
            if r.result.status == FAILED and r.result.step:
                return f"{r.result.message} (while {r.result.step[0].lower() + r.result.step[1:]})"
            return r.result.message
        if r.plan.convert:
            target = _TARGET_NAME.get(r.plan.target_ext, r.plan.target_ext.lstrip(".").upper())
            return f"{target} · {r.plan.reason}" if r.checked else f"Not selected · {target}"
        return r.plan.reason

    def _changed_row(self, i: int, col: int | None = None) -> None:
        a = self.index(i, col if col is not None else 0)
        b = self.index(i, col if col is not None else len(COLUMNS) - 1)
        self.dataChanged.emit(a, b)

    def _changed_all(self) -> None:
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(COLUMNS) - 1))


class FileFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.show_skipped = False
        self.setSortRole(SORT_ROLE)

    def set_show_skipped(self, show: bool) -> None:
        self.show_skipped = show
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        if self.show_skipped:
            return True
        m: FileTableModel = self.sourceModel()  # type: ignore[assignment]
        r = m.rows[source_row]
        return r.plan.convert or r.result is not None


def open_in_file_manager_many(paths) -> None:
    """Show several files in the file manager with all of them selected: on Windows one Explorer window per
    folder (at most 5), the way Explorer itself does it; elsewhere the first one's folder."""
    paths = [os.path.normpath(str(p)) for p in paths]
    if not paths:
        return
    if os.name != "nt" or len(paths) == 1:
        open_in_file_manager(paths[0])
    elif not _explorer_select(paths):
        _open_folder(os.path.dirname(paths[0]))


def _explorer_select(paths: list[str]) -> bool:
    """SHOpenFolderAndSelectItems for each folder of ``paths``. False if Windows refused (the caller falls back)."""
    try:
        import ctypes
        from ctypes import wintypes

        shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32  # type: ignore[attr-defined]
        shell32.ILFindLastID.restype = ctypes.c_void_p
        shell32.ILFindLastID.argtypes = [ctypes.c_void_p]
        ole32.CoInitializeEx(None, 2)

        def parse(path: str) -> ctypes.c_void_p | None:
            out = ctypes.c_void_p()
            hr = shell32.SHParseDisplayName(ctypes.c_wchar_p(path), None, ctypes.byref(out), 0, None)
            return out if hr == 0 and out.value else None

        def pidl(path: str) -> ctypes.c_void_p | None:
            found = parse(path)
            if found is None and len(path) >= 248:     # past the shell's limit: its short (8.3) name may fit
                buf = ctypes.create_unicode_buffer(1024)
                n = ctypes.windll.kernel32.GetShortPathNameW("\\\\?\\" + path, buf, 1024)  # type: ignore[attr-defined]
                if 0 < n < 1024:
                    found = parse(buf.value.removeprefix("\\\\?\\"))
            return found

        groups: dict[str, list[str]] = {}
        for p in paths:
            groups.setdefault(os.path.dirname(p), []).append(p)
        opened = False
        for folder, items in list(groups.items())[:5]:
            fp = pidl(folder)
            if fp is None:
                continue
            children = [c for c in (pidl(p) for p in items) if c is not None]
            try:
                if children:
                    arr = (ctypes.c_void_p * len(children))(*[shell32.ILFindLastID(c) for c in children])
                    opened |= shell32.SHOpenFolderAndSelectItems(fp, wintypes.UINT(len(children)), arr, 0) == 0
                else:   # Windows can't name the file itself (a path past 260 characters): its folder at least
                    _open_folder(folder)
                    opened = True
            finally:
                for c in children:
                    ole32.CoTaskMemFree(c)
                ole32.CoTaskMemFree(fp)
        return opened
    except Exception:  # noqa: BLE001
        return False


def _open_folder(folder: str) -> None:
    try:
        os.startfile(folder)  # type: ignore[attr-defined]
    except OSError:
        pass


def open_in_file_manager(path: str) -> None:
    import subprocess
    import sys

    if os.name == "nt":
        # Through the shell, not "explorer /select,PATH": Explorer splits its arguments at commas, even inside
        # quotes, so a name with commas in it opened some other folder.
        path = os.path.normpath(path)
        if not _explorer_select([path]):
            _open_folder(path if os.path.isdir(path) else os.path.dirname(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])
