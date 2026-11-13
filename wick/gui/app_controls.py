"""Compact algorithm choices and a checkbox popup for file-type exclusions."""

import re

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QVBoxLayout, QWidget, QWidgetAction,
)

from ..cleanup import appcompress
from .style import Type, text_scale
from .widgets import button, hbox, text


def algorithm_picker(current):
    group = QWidget()
    layout = QVBoxLayout(group)
    layout.setContentsMargins(0, 0, 0, 0)
    combo = QComboBox()
    combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
    for key, (name, reason, _ratio) in appcompress.ALGORITHMS.items():
        combo.addItem(name, key)
        combo.setItemData(combo.count() - 1, reason, Qt.ToolTipRole)
    combo.setCurrentIndex(max(0, combo.findData(current)))
    reason = text("", Type.FOOTNOTE, secondary=True, wrap=True)
    # FlowLayout uses size hints: reserve the widest/tallest explanation up front
    # so changing the algorithm never moves the other controls or the results.
    width = max(combo.sizeHint().width(), round(280 * text_scale()))
    group.setFixedWidth(width)
    reason.setFixedWidth(width)
    metrics = reason.fontMetrics()
    height = max(metrics.boundingRect(QRect(0, 0, width, 10000), Qt.TextWordWrap, explanation).height()
                 for _name, explanation, _ratio in appcompress.ALGORITHMS.values())
    reason.setFixedHeight(height + metrics.leading() + 2)

    def explain():
        reason.setText(appcompress.ALGORITHMS[combo.currentData()][1])
        combo.setToolTip(reason.text())

    combo.currentIndexChanged.connect(explain)
    explain()
    group.setFocusProxy(combo)
    layout.addWidget(combo)
    layout.addWidget(reason)
    return group, combo


class FileTypePicker(QPushButton):
    changed = Signal()

    def __init__(self, values):
        super().__init__()
        self.setAccessibleName("Skip file types")
        menu = QMenu(self)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.types = QListWidget()
        self.types.setAccessibleName("File types to skip")
        self.types.setMinimumWidth(220)
        self.types.setFixedHeight(260)
        layout.addWidget(self.types)
        self.custom = QLineEdit()
        self.custom.setPlaceholderText("Custom type, e.g. .pak")
        self.custom.setAccessibleName("Custom file type to skip")
        add = button("Add")
        add.clicked.connect(self.add_custom)
        self.custom.returnPressed.connect(self.add_custom)
        layout.addLayout(hbox(self.custom, add))
        clear = button("Clear All")
        clear.clicked.connect(lambda: self.set_values([]))
        reset = button("Defaults")
        reset.clicked.connect(lambda: self.set_values(appcompress.DEFAULT_SKIP))
        layout.addLayout(hbox(clear, None, reset))
        action = QWidgetAction(menu)
        action.setDefaultWidget(panel)
        menu.addAction(action)
        self.setMenu(menu)
        self.set_values(values, notify=False)
        self.types.itemChanged.connect(self._changed)

    def values(self):
        return sorted(self.types.item(i).data(Qt.UserRole) for i in range(self.types.count())
                      if self.types.item(i).checkState() == Qt.Checked)

    def set_values(self, values, notify=True):
        selected = appcompress.exclusions(values)
        choices = selected | appcompress.exclusions(appcompress.DEFAULT_SKIP) | {
            ".exe", ".dll", ".pak", ".dat", ".ucas", ".utoc", ".wav", ".webp", ".pdf", ".zst"}
        choices.update(self.types.item(i).data(Qt.UserRole) for i in range(self.types.count()))
        self.types.blockSignals(True)
        self.types.clear()
        for extension in sorted(choices):
            item = QListWidgetItem(extension)
            item.setData(Qt.UserRole, extension)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if extension in selected else Qt.Unchecked)
            self.types.addItem(item)
        self.types.blockSignals(False)
        self._changed(notify)

    def _changed(self, notify=True):
        values = self.values()
        self.setText(f"{len(values)} types skipped" if values else "No types skipped")
        self.setToolTip(", ".join(values) or "All file types are eligible for compression")
        if notify:
            self.changed.emit()

    def add_custom(self):
        extension = "." + self.custom.text().strip().lower().lstrip(".*")
        if not re.fullmatch(r"\.[a-z0-9][a-z0-9_+-]*", extension):
            self.custom.setToolTip("Enter one file extension, such as .pak")
            return
        self.set_values([*self.values(), extension])
        self.custom.clear()
