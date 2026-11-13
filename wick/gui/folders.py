"""Explicit scan scopes, edited as full paths before committing changes."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QListWidget, QListWidgetItem, QVBoxLayout,
)

from .style import Type
from .widgets import button, hbox, text


def summary(paths, label=str):
    paths = list(paths)
    if not paths:
        return "No folders selected"
    return label(paths[0]) + (f" (+{len(paths) - 1} more)" if len(paths) > 1 else "")


class FolderManager(QDialog):
    def __init__(self, parent, paths, defaults, settings, label=str, note="", read_only=False):
        super().__init__(parent)
        self.settings, self.defaults, self.label = settings, defaults, label
        self.setWindowTitle("Manage folders")
        self.resize(720, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(text(note or "These folders are included in scans. Changes take effect after Save.",
                              Type.BODY, wrap=True))
        self.list = QListWidget()
        self.list.setAccessibleName("Folders included in scans")
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.list)
        add = button("Add Folder…", "", "folder")
        add.clicked.connect(self.add)
        remove = button("Remove Selected")
        remove.clicked.connect(self.remove)
        reset = button("Reset to Defaults")
        reset.clicked.connect(lambda: self.fill(self.defaults()))
        for control in (add, remove, reset):
            control.setEnabled(not read_only)
        layout.addLayout(hbox(add, remove, None, reset))
        buttons = QDialogButtonBox(QDialogButtonBox.Close if read_only else
                                  QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.fill(paths)

    def paths(self):
        return [Path(self.list.item(i).data(Qt.UserRole)) for i in range(self.list.count())]

    def fill(self, paths):
        self.list.clear()
        for path in dict.fromkeys(paths):
            item = QListWidgetItem(self.label(path))
            item.setData(Qt.UserRole, str(path))
            item.setToolTip(str(path))
            self.list.addItem(item)

    def add(self):
        initial = str(self.settings.value("folders/last_location", str(Path.home()), str))
        selected = QFileDialog.getExistingDirectory(self, "Include a folder", initial)
        if selected:
            path = Path(selected)
            self.settings.setValue("folders/last_location", str(path))
            self.fill([*self.paths(), path])

    def remove(self):
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
