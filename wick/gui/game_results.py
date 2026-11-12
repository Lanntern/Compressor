"""Search public game estimates locally after an explicit download."""

import os

from PySide6.QtCore import QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHeaderView, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout

from ..cleanup import game_results
from .style import Type
from .widgets import button, hbox, text


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
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
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
        self.table.setRowCount(len(rows))
        for i, (key, game) in enumerate(rows):
            values = [game["name"], str(key)]
            for mode in game_results.MODES:
                result = game["modes"].get(mode)
                values.append(f"{1 - result['ratio']:.0%} saved ({result['samples']} samples)" if result else "No data")
            for j, value in enumerate(values):
                self.table.setItem(i, j, QTableWidgetItem(value))

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
