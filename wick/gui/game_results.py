"""Search public game estimates locally after an explicit download."""

import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QAbstractItemView, QDialog, QHeaderView, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout

from ..cleanup import game_results
from .style import Type
from .widgets import button, hbox, text


class _ResultItem(QTableWidgetItem):
    def __lt__(self, other):
        return self.data(Qt.UserRole) < other.data(Qt.UserRole)


class GameResultsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Community game compression results")
        self.resize(820, 440)
        self.data = game_results.load()
        self.reply = None
        self.manager = QNetworkAccessManager(self)
        layout = QVBoxLayout(self)
        self.note = text("Public CompactGUI results, not performance benchmarks. Game versions, exclusions and hardware "
                         "can differ. Downloading fetches the public database only; your installed games are not sent.",
                         Type.BODY, wrap=True)
        layout.addWidget(self.note)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search game names or Steam IDs")
        self.search.setAccessibleName("Search community games")
        self.search.textChanged.connect(self.fill)
        layout.addWidget(self.search)
        self.table = QTableWidget(0, 6)
        self.table.setAccessibleName("Community game compression estimates")
        self.table.setHorizontalHeaderLabels(["Game", "Steam ID", "XPRESS4K", "XPRESS8K", "XPRESS16K", "LZX"])
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        for column in range(2, 6):
            self.table.horizontalHeaderItem(column).setToolTip("Space saved; hover over a result for sample counts")
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.AscendingOrder)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)
        self.download_button = button("Download / Refresh Public Results")
        self.download_button.clicked.connect(self.fetch)
        source = button("Source / Contribute…", "", None, "Open CompactGUI's public results and contribution information")
        source.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(game_results.PAGE)))
        close = button("Close")
        close.clicked.connect(self.close)
        layout.addLayout(hbox(self.download_button, source, None, close))
        self.fill()

    def fill(self):
        query = self.search.text().casefold()
        rows = [(key, game) for key, game in self.data.items() if query in game["name"].casefold() or query in str(key)]
        rows = sorted(rows, key=lambda pair: pair[1]["name"])[:500]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for i, (key, game) in enumerate(rows):
            for column, (value, order) in enumerate(((game["name"], game["name"].casefold()), (str(key), int(key)))):
                item = _ResultItem(value)
                item.setData(Qt.UserRole, order)
                item.setToolTip(value)
                self.table.setItem(i, column, item)
            for column, mode in enumerate(game_results.MODES, 2):
                result = game["modes"].get(mode)
                saving = 1 - result["ratio"] if result else -1.0
                item = _ResultItem(f"{saving:.0%}" if result else "No data")
                item.setData(Qt.UserRole, saving)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if result:
                    count = result["samples"]
                    item.setToolTip(f"{saving:.0%} saved ({count:,} sample{'s' if count != 1 else ''})")
                else:
                    item.setToolTip("No community results for this algorithm")
                self.table.setItem(i, column, item)
        self.table.setSortingEnabled(True)

    def fetch(self):
        if self.reply is not None:
            return
        if os.environ.get('WICK_NO_NETWORK'):
            self.note.setText('Network access is disabled.')
            return
        self.download_button.setEnabled(False)
        self.note.setText('Downloading public results… Your installed-game list stays on this PC.')
        self.content = bytearray()
        self.download_error = ''
        request = QNetworkRequest(QUrl(game_results.URL))
        request.setTransferTimeout(15000)
        self.reply = self.manager.get(request)
        self.reply.readyRead.connect(self.read)
        self.reply.finished.connect(self.download_finished)

    def read(self):
        self.content.extend(bytes(self.reply.readAll()))
        if len(self.content) > game_results.LIMIT:
            self.download_error = 'Download exceeded the size limit'
            self.reply.abort()

    def download_finished(self):
        reply = self.reply
        try:
            if self.download_error or reply.error() != QNetworkReply.NoError:
                raise RuntimeError(self.download_error or reply.errorString())
            self.read()
            self.received(game_results.save(bytes(self.content)))
        except (OSError, ValueError, RuntimeError) as error:
            from ..log import log

            log.warning('Community result download failed: %s', error)
            self.note.setText('Download failed: ' + str(error))
        finally:
            self.reply = None
            reply.deleteLater()
            self.download_button.setEnabled(True)

    def closeEvent(self, event):
        if self.reply is not None:
            self.reply.abort()
        super().closeEvent(event)

    def received(self, data):
        self.data = data
        self.note.setText(f"{len(data):,} public results cached. Matching Steam games use them on the next scan. "
                          "Refine the search if more than 500 games match.")
        self.fill()
