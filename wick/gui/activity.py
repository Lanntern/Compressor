"""Sidebar activity and a live dashboard; samples existing job state on the GUI thread."""

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QHeaderView, QTableWidget, QTableWidgetItem, QVBoxLayout

from ..activity import Activity
from .style import Type
from .widgets import button, hbox, text


class ActivityMonitor:
    def __init__(self, window):
        self.window = window
        self.tasks: list[Activity] = []
        self.started: dict[str, float] = {}
        self.dialog = None
        self.timer = QTimer(window, interval=500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        window.sidebar.activity_requested.connect(self.show)

    def refresh(self):
        win, tasks, seen = self.window, [], set()

        def add(name, page, worker, status=None, section=None):
            if worker is None or not worker.isRunning() or id(worker) in seen:
                return
            seen.add(id(worker))
            key = str(id(worker))
            self.started.setdefault(key, time.monotonic())
            bar = getattr(status, "bar", None)
            fraction = bar.value() / bar.maximum() if bar is not None and bar.maximum() > 0 else None
            detail = getattr(status, "detail", None) or getattr(status, "status", None)
            stage = detail.text() if detail is not None else getattr(worker, "stage", "Working…")
            if getattr(worker, "combined", False):
                name = "Shared cleanup scan"
            tasks.append(Activity(key, name, stage, fraction, bool(getattr(status, "_paused_reason", "")),
                                  time.monotonic() - self.started[key], page, section))

        for name, page in (("Compress", "compress"), ("Convert", "converter"), ("Metadata", "metadata")):
            obj = win.compress if page == "compress" else win.built(page)
            if obj is not None:
                add(name, page, obj.job_thread, obj.job_status)
                add(name + " scan", page, obj.scan_thread, obj.job_status)
        add("Home analysis", "home", win.home.worker)
        cleanup = win.built("cleanup")
        if cleanup is not None:
            for i, sec in cleanup._built.items():
                if getattr(sec.worker, "combined", False) and getattr(sec.worker, "active_key", "") \
                        and sec.tool.key != sec.worker.active_key:
                    continue
                add(sec.tool.tab, "cleanup", sec.worker, sec, i)
        add("Recompress updated games", "cleanup", getattr(win, "_recompress", None))
        # Watch Folders exposes its own live worker, separate from the interactive Compress page.
        watch = getattr(win, "watch", None)
        add("Watch Folders", "watch", getattr(watch, "job", None), getattr(watch, "job_status", None))
        self.tasks = tasks
        active = {t.key for t in tasks}
        self.started = {k: v for k, v in self.started.items() if k in active}
        win.sidebar.set_activity(tasks)
        if self.dialog is not None and self.dialog.isVisible():
            self._fill()

    def show(self):
        if self.dialog is None:
            self.dialog = QDialog(self.window)
            self.dialog.setWindowTitle("Running tasks")
            self.dialog.resize(760, 360)
            layout = QVBoxLayout(self.dialog)
            layout.addWidget(text("Live work across every page. Double-click a task to return to its controls.",
                                  Type.BODY, wrap=True))
            self.table = QTableWidget(0, 5)
            self.table.setAccessibleName("Running tasks")
            self.table.setHorizontalHeaderLabels(["Task", "Stage", "Progress", "Elapsed", "State"])
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            self.table.setEditTriggers(QTableWidget.NoEditTriggers)
            self.table.setSelectionBehavior(QTableWidget.SelectRows)
            self.table.cellDoubleClicked.connect(self.go)
            layout.addWidget(self.table)
            go = button("Go to Task", "", None, "Open the selected task")
            go.clicked.connect(self.go)
            close = button("Close")
            close.clicked.connect(self.dialog.close)
            layout.addLayout(hbox(go, None, close))
        self.refresh()
        self._fill()
        self.dialog.show()
        self.dialog.raise_()

    def _fill(self):
        selected = self.table.currentRow()
        self.table.setRowCount(len(self.tasks))
        for row, task in enumerate(self.tasks):
            values = [task.name, task.stage, "Unknown" if task.fraction is None else f"{task.fraction:.0%}",
                      f"{int(task.elapsed) // 60}:{int(task.elapsed) % 60:02d}", "Paused" if task.paused else "Running"]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, col, item)
        if 0 <= selected < len(self.tasks):
            self.table.selectRow(selected)

    def go(self, row=None, _column=None):
        row = self.table.currentRow() if row is None or isinstance(row, bool) else row
        if 0 <= row < len(self.tasks):
            task = self.tasks[row]
            page = getattr(self.window, task.page)
            self.window.go_to(page)
            if task.page == "cleanup" and task.section is not None:
                page.tabs.setCurrentIndex(task.section)
            self.dialog.hide()
