"""The progress panel above a job's file list: what is happening now, then what happened.

While running: the file (or "3 of 12 files"), the step it is on ("Encoding JPEG XL", "Checking the result"),
the time so far, and a bar (bytes done; moving when the encoder can't say how far it is).
Afterwards it stays until dismissed: where the result was saved (Show in Folder), or what failed and at
which step (Copy Details, Open Log), so a problem can be narrowed down.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QHBoxLayout

from ..engine import CANCELLED, DONE, FAILED
from .. import report
from ..formats import human
from .style import Space, Type, tokens
from .widgets import Card, ElidedLabel, FlowLayout, ProgressBar, button, text


def _elapsed(seconds: float) -> str:
    s = int(seconds)
    return "under a second" if s < 1 else f"{s} s" if s < 60 else f"{s // 60} min {s % 60:02d} s"


def _folder(path: Path) -> str:
    home = str(Path.home())
    p = str(path)
    return "~" + p[len(home):] if p.startswith(home + os.sep) else p


class JobStatus(Card):
    stop_requested = Signal()
    pause_toggled = Signal(bool)     # the user paused (True) or resumed (False)

    def __init__(self, verb: str = "Compressing", done_word: str = "compressed", parent=None):
        super().__init__(parent, padding=Space.M, spacing=Space.XS)
        self.verb, self.done_word = verb, done_word
        self.setAccessibleName("Progress")
        self.title = ElidedLabel("", Type.BODY, bold=True)      # file names can be long
        self.detail = text("", Type.FOOTNOTE, secondary=True, wrap=True)
        self.detail.setTextFormat(Qt.PlainText)
        self.detail.setAccessibleName("Details")
        self.stop_btn = button("Stop", "", "stop", "Stop after the current file; its original is kept", "Esc")
        self.stop_btn.clicked.connect(self.stop_requested)
        self.pause_btn = button("Pause", "", None, "Pause after the current file (it finishes first)")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._pause_clicked)
        self._paused_reason = ""
        self.show_btn = button("Show in Folder", "", "folder", "Open the folder with the result selected")
        self.show_btn.clicked.connect(self._show_result)
        self.copy_btn = button("Copy Details", "", None, "Copy what failed, at which step and why")
        self.copy_btn.clicked.connect(self._copy_details)
        self.log_btn = button("Open Log", "", None, "The log records every failure with its full error")
        self.log_btn.clicked.connect(self._open_log)
        self.report_btn = button("Save Report…", "", None, "Save every file's result, sizes and where it was saved "
                                                         "as a spreadsheet (CSV)")
        self.report_btn.clicked.connect(self._save_report)
        self._results: list = []
        self.close_btn = button("Dismiss", "", None, "Hide this message")
        self.close_btn.clicked.connect(self.hide)
        # The title has the whole width (file names can be long); details and buttons share the line below.
        self.lay.addWidget(self.title)
        self.lay.addWidget(self.detail)              # the whole width: results can take a few lines
        self.bar = ProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName("Progress")
        self.lay.addSpacing(Space.XS)
        self.lay.addWidget(self.bar)
        row = QHBoxLayout()
        row.setSpacing(Space.L)
        row.addStretch(1)
        buttons = FlowLayout(spacing=Space.S)      # wraps on narrow windows instead of squeezing
        for b in (self.show_btn, self.report_btn, self.copy_btn, self.log_btn, self.pause_btn, self.stop_btn,
                  self.close_btn):
            buttons.add_group(b)
        row.addLayout(buttons)
        self.lay.addSpacing(Space.XS)
        self.lay.addLayout(row)
        self._tick = QTimer(self, interval=1000)
        self._tick.timeout.connect(self._render)
        self._outputs: list[Path] = []
        self._details = ""
        self._running: dict[int, list] = {}      # id(entry) -> [name, size, step, fraction]
        self.setVisible(False)

    # ---------------------------------------------------------------- simple busy message (scanning)
    def busy(self, message: str, done: int = 0, total: int = 0) -> None:
        self._set_buttons(stop=True)
        self._tick.stop()
        self.title.setText(message if len(message) < 90 else message[:87] + "…")
        self.detail.setVisible(False)
        self.bar.setVisible(True)
        self.bar.setRange(0, total)
        self.bar.setValue(done)
        self._color(None)
        self.setVisible(True)
        self._taskbar("normal" if total else "indeterminate", done / total if total else 0.0)

    def stopping(self) -> None:
        self.detail.setText("Stopping after the current file; its original is kept.")
        self.detail.setVisible(True)
        self.stop_btn.setEnabled(False)

    # ---------------------------------------------------------------- a job
    def start(self, n_files: int, total_bytes: int) -> None:
        self._n, self._bytes, self._bytes_done, self._finished = n_files, max(1, total_bytes), 0, 0
        self._running.clear()
        self._last_name = ""
        self._t0 = time.monotonic()
        self._set_buttons(stop=True, pause=True)
        self.pause_btn.blockSignals(True)
        self.pause_btn.setChecked(False)
        self.pause_btn.setText("Pause")
        self.pause_btn.blockSignals(False)
        self._paused_reason = ""
        self._color(None)
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self.detail.setVisible(True)
        self.title.setText(f"{self.verb} {n_files} file{'s' if n_files != 1 else ''}…")
        self.detail.setText("Starting…")
        self.setVisible(True)
        self._tick.start()
        self._taskbar("indeterminate")

    def file_started(self, entry) -> None:
        self._running[id(entry)] = [entry.path.name, entry.size, "Starting", None]
        self._last_name = entry.path.name
        self._render()

    def file_step(self, entry, message: str) -> None:
        r = self._running.get(id(entry))
        if r is not None:
            r[2] = message
            self._render()

    def file_progress(self, entry, fraction: float) -> None:
        r = self._running.get(id(entry))
        if r is not None:
            r[3] = max(0.0, min(1.0, fraction))
            self._render()

    def file_finished(self, result) -> None:
        r = self._running.pop(id(result.entry), None)
        self._bytes_done += r[1] if r else result.entry.size
        self._finished += 1
        self._render()

    def _render(self) -> None:
        if not self._tick.isActive():
            return
        spent = time.monotonic() - self._t0
        took = _elapsed(spent)
        running = list(self._running.values())
        paused = "Paused · " if self._paused_reason else ""
        if self._n == 1:
            name = running[0][0] if running else self._last_name
            self.title.setText(f"{paused}{self.verb} {name}")
        else:
            self.title.setText(f"{paused}{self.verb} {self._n} files · {self._finished} done")
        partial = sum(size * f for _n, size, _s, f in running if f is not None)
        known = self._bytes_done + partial
        left = ""
        if spent > 20 and 0.05 < known / self._bytes < 0.98 and not self._paused_reason:
            left = f" · about {_elapsed(spent * (self._bytes - known) / known)} left"   # by bytes done so far
        if self._paused_reason and not running:
            self.detail.setText(self._paused_reason)
        elif running:
            name, _size, step, _f = running[-1]
            who = "" if self._n == 1 else (f"{name}: " if len(running) == 1 else
                                            f"{name} and {len(running) - 1} more: ")
            then = f" · then paused: {self._paused_reason}" if self._paused_reason else ""
            self.detail.setText(f"{who}{step}… · {took}{left}{then}")
        else:
            self.detail.setText(f"Finishing… · {took}")
        if self._n == 1 and running and running[0][3] is None:
            self.bar.setRange(0, 0)          # the encoder doesn't say how far it is: keep it moving
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * known / self._bytes))
        self.bar.setAccessibleDescription(self.detail.text())
        moving = self.bar.maximum() == 0
        self._taskbar("paused" if self._paused_reason else "indeterminate" if moving else "normal",
                      known / self._bytes)

    def finish(self, results, original_note: str = "", replaced: bool = False) -> None:
        """Show the outcome; stays until dismissed. ``original_note``: e.g. "The original is in the Trash"."""
        self._tick.stop()
        took = _elapsed(time.monotonic() - self._t0) if hasattr(self, "_t0") else ""
        done = [r for r in results if r.status == DONE]
        failed = [r for r in results if r.status == FAILED]
        cancelled = [r for r in results if r.status == CANCELLED]
        skipped = [r for r in results if r.status not in (DONE, FAILED, CANCELLED)]
        self._outputs = [r.output for r in done if r.output]
        self._results = list(results)
        self._details = self._describe_failures(failed + skipped)
        self._taskbar("error" if failed else "none")      # (red until the window is looked at)
        self.bar.setVisible(False)
        self.detail.setVisible(True)
        lines: list[str] = []
        if len(results) == 1:
            r = results[0]
            name = r.entry.path.name
            if r.status == DONE and r.output:
                old, new = r.entry.size, r.new_size
                change = (f"{100 * (old - new) / old:.0f}% smaller" if old and new <= old
                          else f"{100 * (new - old) / max(1, old):.0f}% larger")
                self.title.setText(f"Done: {r.output.name}")
                lines.append(f"{human(old)} → {human(new)} ({change}) in {took}.")
                lines.append(f"Saved in {_folder(r.output.parent)}")
                if original_note:
                    lines.append(original_note)
                self._color("success")
            elif r.status == FAILED:
                self.title.setText(f"Couldn't finish {name}")
                lines.append((f"While {r.step[0].lower() + r.step[1:]}: " if r.step else "") + r.message)
                lines.append("The original wasn't changed.")
                self._color("danger")
            elif r.status == CANCELLED:
                self.title.setText(f"Stopped: {name} wasn't changed")
                self._color(None)
            else:
                self.title.setText(f"{name} wasn't changed")
                lines.append(r.message)
                self._color("warning")
        else:
            saved = sum(r.saved for r in done)
            head = f"Done: {len(done)} of {len(results)} files {self.done_word}"
            if self.done_word == "compressed" and done:
                head += f", {human(saved)} saved"
            if cancelled:
                head = f"Stopped: {len(done)} of {len(results)} files {self.done_word}"
            self.title.setText(head)
            folders = {o.parent for o in self._outputs}
            if len(folders) == 1:
                lines.append(f"Saved in {_folder(next(iter(folders)))} · {took}")
            elif folders:
                where = "Each one replaced its original" if replaced else "Saved next to each original"
                lines.append(f"{where} · {took}")
            if original_note and done:
                lines.append(original_note)
            if failed:
                first = failed[0]
                lines.append(f"{len(failed)} failed and {'was' if len(failed) == 1 else 'were'} left unchanged. "
                             f"{first.entry.path.name}" + (f" (while {first.step.lower()})" if first.step else "")
                             + f": {first.message}")
            if skipped:
                reasons = {r.message for r in skipped}
                why = skipped[0].message.rstrip(".") if len(reasons) == 1 else "each one's reason is in the list"
                why = why.removesuffix("; original kept").removesuffix(", original kept")   # (said already)
                lines.append(f"{len(skipped)} left as they were: {why[0].lower() + why[1:]}.")
            self._color("danger" if failed and not done else "warning" if failed else "success" if done else None)
        self.detail.setText("\n".join(lines))
        self._set_buttons(show=bool(self._outputs), copy=bool(failed or skipped), log=bool(failed), close=True,
                          report=len(results) > 1)
        self.setVisible(True)

    def failed(self, message: str) -> None:
        """The whole job stopped (not one file)."""
        self._tick.stop()
        self.bar.setVisible(False)
        self.title.setText("Stopped by an error")
        self._taskbar("error")
        self.detail.setText(message)
        self.detail.setVisible(True)
        self._details = message
        self._color("danger")
        self._set_buttons(copy=True, log=True, close=True)
        self.setVisible(True)

    # ---------------------------------------------------------------- helpers
    def _taskbar(self, state: str, fraction: float = 0.0) -> None:
        """Mirror this job on the window's taskbar button (gui/taskbar.py)."""
        tb = getattr(self.window(), "taskbar", None)
        if tb is not None:
            tb.report(self, state, fraction)

    def hideEvent(self, e) -> None:  # noqa: N802
        super().hideEvent(e)
        self._taskbar("none")

    def _describe_failures(self, results) -> str:
        from .. import APP_NAME, __version__

        out = [f"{APP_NAME} {__version__}"]
        for r in results:
            out += [f"{r.entry.path}", f"  result: {r.status}"]
            if r.step:
                out.append(f"  step: {r.step}")
            out.append(f"  message: {r.message}")
        return "\n".join(out)

    def set_paused(self, reason: str) -> None:
        """Show the job as paused ("" = running again). ``reason`` says why, and when it continues."""
        self._paused_reason = reason
        self.pause_btn.blockSignals(True)
        self.pause_btn.setChecked(bool(reason))
        self.pause_btn.setText("Resume" if reason else "Pause")
        self.pause_btn.blockSignals(False)
        self._render()

    def _pause_clicked(self, on: bool) -> None:
        self.set_paused("Paused by you. Press Resume to continue." if on else "")
        self.pause_toggled.emit(on)

    def _set_buttons(self, stop=False, show=False, copy=False, log=False, close=False, pause=False,
                     report=False) -> None:
        self.pause_btn.setVisible(pause)
        self.report_btn.setVisible(report)
        self.stop_btn.setVisible(stop)
        self.stop_btn.setEnabled(True)
        self.show_btn.setVisible(show)
        self.copy_btn.setVisible(copy)
        self.log_btn.setVisible(log)
        self.close_btn.setVisible(close)

    def _color(self, role: str | None) -> None:
        tk = tokens()
        color = {"success": tk.success, "danger": tk.danger, "warning": tk.text}.get(role or "", tk.text)
        self.title.setStyleSheet(f"color: {color};")

    def _show_result(self) -> None:
        from .file_model import open_in_file_manager

        if self._outputs:
            open_in_file_manager(str(self._outputs[0]))

    def _save_report(self) -> None:
        save_report(self, lambda: report.results_rows(self._results))

    def _copy_details(self) -> None:
        QApplication.clipboard().setText(self._details)
        self.copy_btn.setText("Copied")
        QTimer.singleShot(1500, lambda: self.copy_btn.setText("Copy Details"))

    def _open_log(self) -> None:
        from ..log import log_file
        from .filelist import open_file

        open_file(log_file())


def save_report(parent, rows) -> None:
    """Ask where, then write the CSV report (``rows()`` makes its rows) and offer to show it."""
    from PySide6.QtWidgets import QFileDialog

    from .dialogs import warn

    start = Path.home() / "Documents"
    start = start if start.is_dir() else Path.home()
    name, _ = QFileDialog.getSaveFileName(parent, "Save the report", str(start / report.default_name()),
                                          "Spreadsheet (CSV) (*.csv)")
    if not name:
        return
    path = Path(name if name.lower().endswith(".csv") else name + ".csv")
    try:
        report.write(rows(), path)
    except OSError as e:
        warn(parent, "The report couldn't be saved", str(e))
        return
    from .file_model import open_in_file_manager

    open_in_file_manager(str(path))


class ResumeBanner(Card):
    """A job that didn't finish last time (closed, crashed, restarted): resume it, or let it go."""

    resume = Signal()
    discard = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, padding=Space.M, spacing=Space.XS)
        self.setAccessibleName("Unfinished job")
        self.title = ElidedLabel("", Type.BODY, bold=True)
        self.detail = text("", Type.FOOTNOTE, secondary=True, wrap=True)
        self.resume_btn = button("Resume", "primary", None, "Compress the files that weren't done yet")
        self.resume_btn.clicked.connect(self.resume)
        self.discard_btn = button("Discard", "", None, "Forget the unfinished job")
        self.discard_btn.clicked.connect(self.discard)
        self.lay.addWidget(self.title)
        row = QHBoxLayout()
        row.setSpacing(Space.L)
        row.addWidget(self.detail, 1)
        buttons = FlowLayout(spacing=Space.S)
        buttons.add_group(self.discard_btn)
        buttons.add_group(self.resume_btn)
        row.addLayout(buttons)
        self.lay.addLayout(row)
        self.setVisible(False)

    def show_pending(self, p) -> None:
        when = time.strftime("%a %d %b, %H:%M", time.localtime(p.started))
        left, total = len(p.remaining), len(p.paths)
        self.title.setText(f"A job started {when} didn't finish")
        self.detail.setText(f"{left:,} of {total:,} file{'s' if total != 1 else ''} weren't done yet (Wick "
                            "was closed or the computer restarted). Resume continues with them, using the options "
                            "the job was started with. The files that were done are in the history.")
        self.setVisible(True)


class PauseControl:
    """Pauses a job's thread when the user presses Pause, or on battery power (Settings): the file being
    worked on finishes, nothing new starts. Battery pauses lift on their own; the user's don't."""

    BATTERY = "On battery power: continues when you plug in (Settings › Pause on battery power)."

    def __init__(self, status: JobStatus, window):
        self.status, self.win, self.thread, self.by_user = status, window, None, False
        status.pause_toggled.connect(self._user)
        window.power_changed.connect(lambda _on: self.apply())

    def attach(self, thread) -> None:
        self.thread, self.by_user = thread, False
        self.apply()

    def _running(self) -> bool:
        return self.thread is not None and self.thread.isRunning() and hasattr(self.thread, "pause")

    def _user(self, on: bool) -> None:
        if not self._running():
            return
        self.by_user = on
        if on:
            self.thread.pause.set()
        else:
            self.thread.pause.clear()
            self.apply()

    def apply(self) -> None:
        if not self._running() or self.by_user:
            return
        if self.win.battery_pause():
            if not self.thread.pause.is_set():
                self.thread.pause.set()
                self.status.set_paused(self.BATTERY)
        elif self.thread.pause.is_set():
            self.thread.pause.clear()
            self.status.set_paused("")


__all__ = ["JobStatus", "PauseControl"]
