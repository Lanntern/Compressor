"""Convert page: change file formats (not about saving space)."""

from __future__ import annotations

import dataclasses
import os
from typing import Any
from pathlib import Path

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QSpinBox, QWidget

from .. import convert
from ..engine import process_convert
from ..formats import ARCHIVE, COMIC, DOC, DOCUMENT, FONT, KIND_LABEL, PANDOC_INPUTS, PDF, PICTURE, SVG, human
from ..plan import Plan
from ..scanner import FileEntry
from ..settings import Settings
from .filejob_page import ROLE_ROW, FileJobPage, option_row, set_visible
from .settings_panel import BIN_NAME, METADATA_CHOICES
from .style import Space
from .widgets import Card, button

OUTPUT_CHOICES = [("Next to originals", "beside"), ("Save with a new name", "suffix"),
                  ("Save to another folder", "folder"), (f"Replace originals ({BIN_NAME})", "replace")]


_SAME = ({".jpg", ".jpeg", ".jpe", ".jfif"}, {".tif", ".tiff"}, {".heic", ".heif", ".hif"}, {".png", ".apng"},
         {".wav", ".wave"}, {".opus", ".ogg", ".oga"})


def _same_format(src_ext: str, target_ext: str) -> bool:
    a, b = src_ext.lower(), target_ext.lower()
    return a == b or any(a in g and b in g for g in _SAME)


def _combo(items, current, tip=""):
    c = QComboBox()
    for label, value in items:
        c.addItem(label, value)
    i = c.findData(current)
    c.setCurrentIndex(max(0, i))
    if tip:
        c.setToolTip(tip)
    return c


# Which settings belong to which kind of file (a file's own settings only change its kind's).
_FIELDS = {"image": {"image_target", "quality", "lossless", "max_side", "ico_sizes", "turn", "flip"},
           "video": {"video_target", "video_height", "video_fps", "turn", "flip"},
           "audio": {"audio_target", "audio_kbps", "sample_rate", "channels"},
           "documents": {"document_target"},
           "pdfs": {"pdf_target", "pdf_dpi"},
           "fonts": {"font_target", "font_latin"},
           "comics": {"comic_target"},
           "archives": {"archive_target"}}
_FIELDS["image"].add("pdf_page")


class ConvertTab(FileJobPage):
    COLUMNS = ["Name", "Folder", "From", "Size", "To", "Status"]
    STATUS_COL = 5
    action_label = "Convert"
    tool = "convert"
    confirm_title = "Convert {n} file{s}?"
    status_words = ("Converting", "converted")

    def __init__(self, window):
        self.qs = window.qsettings
        self.overrides: dict[str, convert.ConvertOptions] = {}     # a file's own format and settings
        super().__init__(window, "Convert", "",
                         "Drop images, videos, audio or documents to convert",
                         "Choose the formats above, then press Convert. Originals are kept unless you choose to replace them.")

    def build_options(self, card: Card) -> None:
        qs = self.qs
        images = [(f"{v[0]}", k) for k, v in convert.IMAGE_TARGETS.items()] + [("Don't convert", "none")]
        videos = [(v[0], k) for k, v in convert.VIDEO_TARGETS.items()] + [("Don't convert", "none")]
        self.image_target = _combo(images, qs.value("convert/image", "png", str), "Format for images")
        self.video_target = _combo(videos, qs.value("convert/video", "mp4_h264", str), "Format for videos")
        audios = [(v[0], k) for k, v in convert.AUDIO_TARGETS.items()] + [("Don't convert", "none")]
        self.audio_target = _combo(audios, qs.value("convert/audio", "mp3", str), "Format for music and other audio")
        docs = [(v[0], k) for k, v in convert.DOCUMENT_TARGETS.items()] + [("Don't convert", "none")]
        self.document_target = _combo(docs, qs.value("convert/document", "docx", str),
                                      "Format for documents (Word, OpenDocument, Markdown, web pages, EPUB, RTF)")
        fonts = [(v[0], k) for k, v in convert.FONT_TARGETS.items()] + [("Don't convert", "none")]
        self.font_target = _combo(fonts, qs.value("convert/font", "none", str),
                                  "What to make of fonts (WOFF2 for websites; Latin-only in More Settings)")
        pdfs = [(convert.PDF_TARGETS[k][0], k) for k in convert.PDF_PAGE_TARGETS] + [("Don't convert", "none")]
        self.pdf_target = _combo(pdfs, qs.value("convert/pdf", "none", str),
                                 "What to make of PDFs (more in the context menu: turn pages, passwords, combine)")
        self.quality = QSpinBox()
        self.quality.setRange(1, 100)
        self.quality.setValue(qs.value("convert/quality", 90, int))
        self.quality.setToolTip("For lossy formats (JPEG, WebP, AVIF, JPEG XL, HEIC)")
        self.lossless = QCheckBox("Lossless")
        self.lossless.setChecked(qs.value("convert/lossless", False, bool))
        self.lossless.setToolTip("WebP and JPEG XL can store images losslessly")
        more = button("More Settings…", "", None, "Image size, video resolution and frame rate, audio bitrate, "
                                                  "sample rate and channels")
        more.clicked.connect(self._page_settings)
        self.more_btn = more
        card.lay.addLayout(option_row(("Images to", self.image_target), ("Videos to", self.video_target),
                                      ("Audio to", self.audio_target), ("Documents to", self.document_target),
                                      ("PDFs to", self.pdf_target), ("Fonts to", self.font_target),
                                      ("Quality", self.quality), ("", self.lossless)))
        self.output = _combo(OUTPUT_CHOICES, qs.value("convert/output", "beside", str))
        self.suffix = QLineEdit(qs.value("convert/suffix", "_converted", str))
        self.suffix.setMaximumWidth(Space.XXL * 5)
        from .settings_panel import NAME_TIP

        self.suffix.setToolTip(NAME_TIP)
        self.suffix.setAccessibleName("New name")
        self.folder = QLineEdit(qs.value("convert/folder", "", str))
        self.folder.setPlaceholderText("Output folder")
        self.folder.setAccessibleName("Output folder")
        pick = button("…", "", None, "Choose the output folder")
        pick.clicked.connect(self._pick_folder)
        self.metadata = _combo(METADATA_CHOICES, qs.value("convert/metadata", "keep", str))
        self.originals = _combo([("Leave them where they are", "keep"), ("Move them to a folder", "move")],
                                qs.value("convert/originals", "keep", str),
                                "After converting, each original can go into a folder of your choosing (History "
                                "can put it back)")
        self.originals_dir = QLineEdit(qs.value("convert/originals_dir", "", str))
        self.originals_dir.setPlaceholderText("Folder for the originals")
        self.originals_dir.setAccessibleName("Folder for the originals")
        pick_orig = button("…", "", None, "Choose the folder the originals go into")
        pick_orig.clicked.connect(self._pick_originals)
        self._originals_widgets = (self.originals_dir, pick_orig)
        card.lay.addLayout(option_row(("Save", self.output), ("Name", self.suffix), ("", (self.folder, pick)),
                                      ("Originals", self.originals), ("", (self.originals_dir, pick_orig)),
                                      ("Metadata", self.metadata)))
        self.dates = QCheckBox("Keep original file dates")
        self.dates.setChecked(qs.value("convert/dates", True, bool))
        self.smaller = QCheckBox("Only keep a result if it's smaller")
        self.smaller.setChecked(qs.value("convert/smaller", True, bool))
        card.lay.addLayout(option_row(("", self.dates), ("", self.smaller), ("", self.more_btn),
                                      ("", self._presets_button())))
        from ..compat import jxl_note
        from .style import Type
        from .widgets import text

        self.jxl_note = text(jxl_note(), Type.FOOTNOTE, secondary=True, wrap=True)
        card.lay.addWidget(self.jxl_note)
        # Documents need Pandoc: downloaded on request (Windows), else installed from the system's packages.
        from .. import addons

        self.pandoc_note = text("", Type.FOOTNOTE, wrap=True)
        self.pandoc_btn = button(f"Download Pandoc ({addons.PANDOC.size / 1e6:.0f} MB)…", "", None,
                                 "Download Pandoc from its official releases on GitHub")
        self.pandoc_btn.clicked.connect(self._get_pandoc)
        self.pandoc_row = QWidget()
        pr = QHBoxLayout(self.pandoc_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(Space.M)
        pr.addWidget(self.pandoc_note, 1)
        pr.addWidget(self.pandoc_btn, 0, Qt.AlignVCenter)
        self.pandoc_row.hide()
        card.lay.addWidget(self.pandoc_row)
        self._pandoc_worker: QThread | None = None
        self._folder_widgets = (self.folder, pick)
        for w in (self.image_target, self.video_target, self.audio_target, self.document_target, self.pdf_target,
                  self.font_target, self.output, self.metadata, self.originals):
            w.currentIndexChanged.connect(self._changed)
        for w in (self.lossless, self.dates, self.smaller):
            w.toggled.connect(self._changed)
        self.quality.valueChanged.connect(self._changed)
        self.suffix.textChanged.connect(self._changed)
        self.folder.textChanged.connect(self._changed)
        self.originals_dir.textChanged.connect(self._changed)
        self._visibility()

    def accepts(self, path: Path) -> bool:
        from ..formats import DOCUMENT, PANDOC_INPUTS, classify

        from ..office import OFFICE_EXTS

        return classify(path) != DOCUMENT or path.suffix.lower() in (*PANDOC_INPUTS, *OFFICE_EXTS, ".pdf")

    # ---- Pandoc, for documents
    def _has_documents(self) -> bool:
        from ..formats import DOC, DOCUMENT

        return any(e.kind in (DOC, DOCUMENT) for e in self.entries)

    def _show_pandoc(self) -> None:
        from .. import addons, tools

        missing = tools.find_tool("pandoc") is None and any(
            e.kind in (DOC, DOCUMENT) and (t := self.opts_for(e).target_for(e.kind)) is not None
            and convert.needs_pandoc(e.path, t[0]) for e in self.entries)
        self.pandoc_row.setVisible(missing)
        if missing and self._pandoc_worker is None:
            self.pandoc_note.setText("Converting documents needs Pandoc, a free document converter. "
                                     + addons.install_hint(addons.PANDOC))
            self.pandoc_btn.setVisible(addons.can_download(addons.PANDOC))
            self.pandoc_btn.setEnabled(True)

    def _get_pandoc(self) -> None:
        from PySide6.QtCore import Signal

        from .. import addons
        from .dialogs import ask, inform

        a = addons.PANDOC
        if not ask(self, f"Download Pandoc {a.version}?", f"{a.what} It's {a.size / 1e6:.0f} MB, downloaded from "
                   "Pandoc's official releases on GitHub and checked against a fingerprint before it's used. It's "
                   "kept with Wick's data and used only for converting documents.", "Download"):
            return

        class Fetch(QThread):
            step = Signal(int, int)
            done = Signal(str)

            def run(self) -> None:
                try:
                    addons.download(a, lambda d, t: self.step.emit(d, t))
                    self.done.emit("")
                except Exception as e:  # noqa: BLE001 - reported to the user
                    self.done.emit(str(e) or type(e).__name__)

        def finished(error: str) -> None:
            self._pandoc_worker = None
            if error:
                self.pandoc_btn.setEnabled(True)
                inform(self, "Pandoc couldn't be downloaded.", error)
                self._show_pandoc()
                return
            self.pandoc_row.hide()
            if self.entries and not self.busy():
                self.refresh_rows()

        w = Fetch(self)
        w.step.connect(lambda d, t: self.pandoc_note.setText(
            f"Downloading Pandoc… {d / 1e6:.0f} of {t / 1e6:.0f} MB"))
        w.done.connect(finished)
        self.pandoc_btn.setEnabled(False)
        self._pandoc_worker = w
        w.start()

    def after_scan(self) -> None:
        self._show_pandoc()

    # ---- Explorer's "Convert with Wick"
    _quick: tuple[str, set[str]] | None = None

    @property
    def _quick_timer(self):
        from PySide6.QtCore import QTimer

        if not hasattr(self, "_qt"):
            self._qt = QTimer(self, singleShot=True, interval=600)
            self._qt.timeout.connect(self._quick_collected)
        return self._qt

    def quick_convert(self, paths: list[str], target: str) -> None:
        """These files to ``target`` (a format id from the context menu), saved next to the originals and
        started right away; "choose" lists them here to pick a format. Explorer starts one process per selected
        file, so they're collected for a moment first. Anything already listed stays as it was."""
        keys = {os.path.normcase(os.path.normpath(p)) for p in paths}
        if self._quick is not None and self._quick[0] == target:
            keys |= self._quick[1]
        self._quick = (target, keys)
        self._quick_paths = getattr(self, "_quick_paths", []) + list(paths)
        self._quick_timer.start()

    def _quick_collected(self) -> None:
        paths, self._quick_paths = self._quick_paths, []
        if self._quick is not None and self._quick[0] == "choose":
            self._quick = None
        if self.busy():                         # (after the job or scan that's running: see _finished)
            self._pending_paths = getattr(self, "_pending_paths", []) + paths
            return
        self.add_paths(paths)

    def _finished(self) -> None:
        super()._finished()
        if self.busy():
            return
        pending = getattr(self, "_pending_paths", [])
        if pending:
            self._pending_paths = []
            self.add_paths(pending)             # (scanned, then back here to start them)
        elif self._quick is not None and not self._quick_timer.isActive():
            self._run_quick()

    def _run_quick(self) -> None:
        assert self._quick is not None
        target, keys = self._quick
        self._quick = None
        from ..formats import AUDIO, VIDEO
        from . import convert_presets

        chosen = set()
        out = None
        preset = None
        if target.startswith("preset:"):
            preset = convert_presets.load(self.qs).get(target.split(":", 1)[1])
            if preset is None:
                self.win.tray_message(f"There's no Convert preset called “{target.split(':', 1)[1]}” any more.")
                return
            out = dict(preset.get("output") or {})
        for e in self.entries:
            key = self._key(e)
            if key not in keys:
                continue
            if preset is not None:
                self.overrides[key] = convert_presets.options(preset)
                if convert_presets.options(preset).target_for(e.kind) is not None:
                    chosen.add(key)
                continue
            field = ("video_target" if e.kind == VIDEO and target in convert.VIDEO_TARGETS else
                     "audio_target" if e.kind == AUDIO and target in convert.AUDIO_TARGETS else
                     "image_target" if e.kind not in (VIDEO, AUDIO, DOC, DOCUMENT) and target in convert.IMAGE_TARGETS
                     else "")
            if field:
                change: dict[str, Any] = {field: target}
                self.overrides[key] = dataclasses.replace(self.opts_for(e), **change)
                chosen.add(key)
        if not chosen:
            return
        # Saved next to the originals, whatever the page says; a preset says where for itself.
        self._quick_out = out if out is not None else {"output_mode": "beside", "originals_to": ""}
        try:
            self.refresh_rows()
            self.start(confirm=False, only=chosen)
        finally:
            self._quick_out = None               # (the job has its settings already)

    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Save converted files in", self.folder.text())
        if d:
            self.folder.setText(d)

    def _pick_originals(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Move the originals into", self.originals_dir.text())
        if d:
            self.originals_dir.setText(d)

    def _visibility(self) -> None:
        out = self.output.currentData()
        set_visible(self.originals, out != "replace")
        for w in self._originals_widgets:
            set_visible(w, out != "replace" and self.originals.currentData() == "move")
        set_visible(self.suffix, out == "suffix")
        for w in self._folder_widgets:
            set_visible(w, out == "folder")
        target = self.image_target.currentData()
        self.jxl_note.setVisible(target == "jxl")
        info = convert.IMAGE_TARGETS.get(target)
        self.lossless.setEnabled(bool(info and info[3] and info[4]))
        self.quality.setEnabled(bool(info and info[4]) and not (self.lossless.isChecked() and self.lossless.isEnabled()))

    def _changed(self, *_) -> None:
        qs = self.qs
        qs.setValue("convert/image", self.image_target.currentData())
        qs.setValue("convert/video", self.video_target.currentData())
        qs.setValue("convert/audio", self.audio_target.currentData())
        qs.setValue("convert/document", self.document_target.currentData())
        qs.setValue("convert/pdf", self.pdf_target.currentData())
        qs.setValue("convert/font", self.font_target.currentData())
        qs.setValue("convert/quality", self.quality.value())
        qs.setValue("convert/lossless", self.lossless.isChecked())
        qs.setValue("convert/output", self.output.currentData())
        qs.setValue("convert/suffix", self.suffix.text())
        qs.setValue("convert/folder", self.folder.text())
        qs.setValue("convert/originals", self.originals.currentData())
        qs.setValue("convert/originals_dir", self.originals_dir.text())
        qs.setValue("convert/metadata", self.metadata.currentData())
        qs.setValue("convert/dates", self.dates.isChecked())
        qs.setValue("convert/smaller", self.smaller.isChecked())
        self._visibility()
        self._show_pandoc()
        if self.entries and not self.busy():
            self.refresh_rows()

    _EXTRA = ("max_side", "video_height", "video_fps", "audio_kbps", "sample_rate", "channels")

    def opts(self) -> convert.ConvertOptions:
        """The page's settings (files with their own settings use those instead: ``opts_for``)."""
        return convert.ConvertOptions(self.image_target.currentData(), self.video_target.currentData(),
                                      self.quality.value(), self.lossless.isChecked() and self.lossless.isEnabled(),
                                      self.smaller.isChecked(), self.audio_target.currentData(),
                                      self.document_target.currentData(),
                                      **{k: self.qs.value(f"convert/{k}", 0, int) for k in self._EXTRA},
                                      ico_sizes=self._saved_ico_sizes(), pdf_target=self.pdf_target.currentData(),
                                      pdf_dpi=int(self.qs.value("convert/pdf_dpi", 150, int) or 150),
                                      pdf_page=str(self.qs.value("convert/pdf_page", "picture", str)),
                                      font_target=self.font_target.currentData(),
                                      font_latin=bool(self.qs.value("convert/font_latin", False, bool)),
                                      comic_target=str(self.qs.value("convert/comic", "none", str)),
                                      archive_target=self._archive_target)

    def _saved_ico_sizes(self) -> tuple[int, ...]:
        raw = str(self.qs.value("convert/ico_sizes", "16,32,48", str))
        sizes = tuple(sorted({int(x) for x in raw.split(",") if x.strip().isdigit() and 0 < int(x) <= 256}))
        return sizes or (16, 32, 48)

    @staticmethod
    def _key(e: FileEntry) -> str:
        return os.path.normcase(str(e.path))

    _quick_out: dict | None = None

    def _output(self) -> str:
        """Where results go: the page's choice, or what Explorer's menu asked for (next to the originals, or a
        preset's own choice)."""
        if self._quick_out is not None:
            return str(self._quick_out.get("output_mode", "beside"))
        return self.output.currentData()

    def _page_output(self) -> dict:
        """The page's choices about where results go (what a preset keeps besides the formats)."""
        mode = self.output.currentData()
        return {"output_mode": mode, "output_suffix": self.suffix.text() or "_converted",
                "output_dir": self.folder.text(), "metadata": self.metadata.currentData(),
                "preserve_dates": self.dates.isChecked(),
                "originals_to": self.originals_dir.text() if mode != "replace" and
                self.originals.currentData() == "move" else ""}

    def opts_for(self, e: FileEntry) -> convert.ConvertOptions:
        own = self.overrides.get(self._key(e))
        return dataclasses.replace(own, only_if_smaller=self.smaller.isChecked()) if own else self.opts()

    _made_worker: QThread | None = None
    _archive_target = "none"         # page-wide repacking: only for this session, after choosing it

    def busy(self) -> bool:
        return super().busy() or (self._made_worker is not None and self._made_worker.isRunning())

    def clear(self) -> None:
        if not self.busy():
            self.overrides.clear()
        super().clear()

    # ---- presets
    def _presets_button(self):
        from PySide6.QtWidgets import QMenu

        btn = button("Presets", "", None, "Save these choices under a name, or use saved ones. Presets also appear "
                                          "in Explorer's Convert with Wick menu.")
        menu = QMenu(btn)
        menu.aboutToShow.connect(lambda: self._fill_presets(menu))
        btn.setMenu(menu)
        self.presets_btn = btn
        return btn

    def _fill_presets(self, menu) -> None:
        from . import convert_presets

        menu.clear()
        presets = convert_presets.load(self.qs)
        for name, preset in sorted(presets.items(), key=lambda kv: kv[0].lower()):
            a = menu.addAction(name, lambda n=name: self.apply_preset(n))
            a.setToolTip(convert_presets.describe(preset))
        if presets:
            menu.addSeparator()
        menu.addAction("Save as Preset…", self._save_preset)
        if presets:
            delete = menu.addMenu("Delete Preset")
            for name in sorted(presets, key=str.lower):
                delete.addAction(name, lambda n=name: self._delete_preset(n))
        menu.setToolTipsVisible(True)

    def _save_preset(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        from . import convert_presets
        from .dialogs import ask

        name, ok = QInputDialog.getText(self, "Save as Preset", "Name (as it appears in Explorer's menu):")
        name = name.strip()
        if not ok or not name:
            return
        presets = convert_presets.load(self.qs)
        if name in presets and not ask(self, f"Replace the preset “{name}”?", "It gets the choices on this page now.",
                                       "Replace Preset"):
            return
        presets[name] = convert_presets.make(self.opts(), self._page_output())
        convert_presets.save(self.qs, presets)
        self._presets_changed()

    def _delete_preset(self, name: str) -> None:
        from . import convert_presets

        presets = convert_presets.load(self.qs)
        presets.pop(name, None)
        convert_presets.save(self.qs, presets)
        self._presets_changed()

    def _presets_changed(self) -> None:
        from . import integration

        integration.refresh_context_menu()

    def apply_preset(self, name: str) -> bool:
        """Put a preset's choices on the page."""
        from . import convert_presets

        preset = convert_presets.load(self.qs).get(name)
        if preset is None:
            return False
        o, out = convert_presets.options(preset), preset.get("output") or {}
        for k in self._EXTRA:
            self.qs.setValue(f"convert/{k}", getattr(o, k))
        self.qs.setValue("convert/ico_sizes", ",".join(map(str, o.ico_sizes)))
        self.qs.setValue("convert/pdf_dpi", o.pdf_dpi)
        self.qs.setValue("convert/pdf_page", o.pdf_page)
        self.qs.setValue("convert/font_latin", o.font_latin)
        self.qs.setValue("convert/comic", o.comic_target)
        self._archive_target = o.archive_target
        widgets = [(self.image_target, o.image_target), (self.video_target, o.video_target),
                   (self.audio_target, o.audio_target), (self.document_target, o.document_target),
                   (self.pdf_target, o.pdf_target), (self.font_target, o.font_target),
                   (self.output, out.get("output_mode", "beside")),
                   (self.metadata, out.get("metadata", "keep")),
                   (self.originals, "move" if out.get("originals_to") else "keep")]
        for combo, value in widgets:
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, combo.findData(value)))
            combo.blockSignals(False)
        for w, value in ((self.suffix, out.get("output_suffix", "_converted")), (self.folder, out.get("output_dir", "")),
                         (self.originals_dir, out.get("originals_to", ""))):
            w.blockSignals(True)
            w.setText(str(value))
            w.blockSignals(False)
        for box, value in ((self.lossless, o.lossless), (self.dates, out.get("preserve_dates", True)),
                           (self.smaller, o.only_if_smaller)):
            box.blockSignals(True)
            box.setChecked(bool(value))
            box.blockSignals(False)
        self.quality.blockSignals(True)
        self.quality.setValue(o.quality)
        self.quality.blockSignals(False)
        self._changed()
        return True

    # ---- format and settings: the page's, and a file's own
    def _page_settings(self) -> None:
        from ..formats import AUDIO, VIDEO
        from .convert_settings import ARCHIVES, COMICS, DOCS, FONTS, IMAGE, PDFS, ConvertSettingsDialog

        dlg = ConvertSettingsDialog(self.opts(), {IMAGE, VIDEO, AUDIO, DOCS, PDFS, FONTS, COMICS, ARCHIVES},
                                    "Convert Settings",
                                    "For every file without its own settings. To choose for some files, "
                                    "select them in the list and use the context menu.", self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        o = dlg.result_options()
        for k in self._EXTRA:
            self.qs.setValue(f"convert/{k}", getattr(o, k))
        self.qs.setValue("convert/ico_sizes", ",".join(map(str, o.ico_sizes)))
        self.qs.setValue("convert/pdf_dpi", o.pdf_dpi)
        self.qs.setValue("convert/pdf_page", o.pdf_page)
        self.qs.setValue("convert/font_latin", o.font_latin)
        self.qs.setValue("convert/comic", o.comic_target)
        self._archive_target = o.archive_target            # (not remembered: it's never a default)
        for combo, value in ((self.image_target, o.image_target), (self.video_target, o.video_target),
                             (self.audio_target, o.audio_target), (self.document_target, o.document_target),
                             (self.pdf_target, o.pdf_target), (self.font_target, o.font_target)):
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, combo.findData(value)))
            combo.blockSignals(False)
        self.quality.blockSignals(True)
        self.quality.setValue(o.quality)
        self.quality.blockSignals(False)
        self.lossless.blockSignals(True)
        self.lossless.setChecked(o.lossless)
        self.lossless.blockSignals(False)
        self._changed()

    def _set_own(self, entries: list[FileEntry], make) -> None:
        for e in entries:
            self.overrides[self._key(e)] = make(self.opts_for(e))
        self.refresh_rows()

    def menu_entries(self, entries: list[FileEntry]) -> list:
        from .convert_settings import ConvertSettingsDialog, kind_group

        groups = {kind_group(e.kind) for e in entries}
        out: list = []
        if len(groups) == 1:
            group = groups.pop()
            all_targets: dict[str, dict] = {"video": convert.VIDEO_TARGETS, "audio": convert.AUDIO_TARGETS,
                                            "documents": convert.DOCUMENT_TARGETS, "fonts": convert.FONT_TARGETS,
                                            "comics": {"pdf": ("PDF, a page per picture", ".pdf")},
                                            "archives": convert.ARCHIVE_TARGETS,
                                            "pdfs": {k: convert.PDF_TARGETS[k] for k in convert.PDF_PAGE_TARGETS}}
            targets = all_targets.get(group, convert.IMAGE_TARGETS)
            if group == "image":
                targets = convert.suggested_image_targets(entries)
            elif group == "video":
                targets = {k: v for k, v in targets.items() if k != "keep" and
                           (k not in convert.SOUND_TARGETS or all(e.video and e.video.audio_streams() for e in entries)) and
                           not all(e.video and e.video.codec == v[2] for e in entries)}
            elif group == "audio":
                lossless = {"flac", "alac", "pcm_s16le", "pcm_s24le", "pcm_s32le", "wavpack", "ape"}
                targets = {k: v for k, v in targets.items() if
                           not all(e.path.suffix.lower() == v[1] for e in entries) and
                           (k not in ("flac", "wav") or all(e.audio and e.audio.codec in lossless for e in entries))}
            field = {"video": "video_target", "audio": "audio_target", "documents": "document_target",
                     "pdfs": "pdf_target", "fonts": "font_target", "comics": "comic_target",
                     "archives": "archive_target"}.get(group, "image_target")
            out.append(("Convert To", [(v[0], lambda k=k: self._set_own(entries, lambda o: dataclasses.replace(
                o, **{field: k}))) for k, v in targets.items()]))
            if group == "pdfs":
                out.append(("PDF", self._pdf_menu(entries)))
            if group in ("image", "video"):
                out.append(("Turn", self._turn_menu(entries, field)))
            groups = {group}

        def edit() -> None:
            n = len(entries)
            title = f"Settings for {entries[0].path.name}" if n == 1 else f"Settings for {n} Files"
            dlg = ConvertSettingsDialog(self.opts_for(entries[0]), groups, title, parent=self)
            if dlg.exec() == dlg.DialogCode.Accepted:
                chosen = dlg.result_options()
                fields = set().union(*(_FIELDS[g] for g in groups))
                self._set_own(entries, lambda o: dataclasses.replace(o, **{k: getattr(chosen, k) for k in fields}))

        out.append(("Format and Settings…", edit))
        from ..formats import AUDIO, VIDEO

        if len(entries) == 1 and entries[0].kind not in (VIDEO, AUDIO, DOC, DOCUMENT, PDF):
            out.append(("Make Website Icons…", lambda: self._web_icons(entries[0])))
        if len(entries) == 1 and entries[0].kind in (VIDEO, AUDIO):
            out.append(("Trim…", lambda: self._trim(entries[0])))
        if len(entries) > 1 and len({e.kind for e in entries}) == 1 and entries[0].kind in (VIDEO, AUDIO):
            out.append(("Join Into One File…", lambda: self._join(entries)))
        from ..formats import ICO, IMAGE_KINDS, PSD

        if all(e.kind in (*IMAGE_KINDS, ICO, PSD, PICTURE, PDF) for e in entries):
            out.append(("Combine Into One PDF…", lambda: self._combine_pdf(entries)))
        if any(self._key(e) in self.overrides for e in entries):
            def reset() -> None:
                for e in entries:
                    self.overrides.pop(self._key(e), None)
                self.refresh_rows()
            out.append(("Use the Page's Settings", reset))
        return out

    def _turn_menu(self, entries: list[FileEntry], field: str) -> list:
        """Turn or flip these files, adding up as you'd expect (Right twice is upside down; a mirror after a turn
        mirrors what you see). They keep their format unless one was chosen for them."""
        def change(turn: int = 0, flip: str = "") -> None:
            for e in entries:
                o = self.opts_for(e)
                new_turn, new_flip = o.turn, o.flip
                if flip:
                    axis = flip if o.turn % 180 == 0 else {"h": "v", "v": "h"}[flip]   # (flips come first)
                    if not o.flip:
                        new_flip = axis
                    elif o.flip == axis:
                        new_flip = ""
                    else:                                    # (both mirrors = turned upside down)
                        new_flip, new_turn = "", (o.turn + 180) % 360
                new_turn = (new_turn + turn) % 360
                own = self._key(e) in self.overrides
                target: dict[str, Any] = {field: getattr(o, field) if own else "keep"}
                self.overrides[self._key(e)] = dataclasses.replace(o, turn=new_turn, flip=new_flip, **target)
            self.refresh_rows()

        return [("Right", lambda: change(90)), ("Left", lambda: change(270)), ("Upside Down", lambda: change(180)),
                None, ("Mirror", lambda: change(flip="h")), ("Flip Upside Down", lambda: change(flip="v"))]

    # ---- PDFs
    def _pdf_menu(self, entries: list[FileEntry]) -> list:
        def own(target: str, password: str = "") -> None:
            self._set_own(entries, lambda o: dataclasses.replace(o, pdf_target=target, pdf_password=password))

        def protect() -> None:
            pw = self._ask_password("Add a Password", "The copy opens only with this password. Keep it "
                                    "somewhere safe: it can't be recovered.", confirm=True)
            if pw:
                own("protect", pw)

        def unlock() -> None:
            pw = self._ask_password("Remove the Password", "The PDF's current password:")
            if pw:
                own("unlock", pw)

        return [("Turn Pages Right", lambda: own("rotate90")), ("Turn Pages Left", lambda: own("rotate270")),
                ("Turn Pages Upside Down", lambda: own("rotate180")), None,
                ("Split Into Single Pages", lambda: own("split")), None,
                ("Add a Password…", protect), ("Remove the Password…", unlock)]

    def _ask_password(self, title: str, text_: str, confirm: bool = False) -> str:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QVBoxLayout

        from .style import Type
        from .widgets import text

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(Space.L, Space.L, Space.L, Space.L)
        lay.setSpacing(Space.M)
        lay.addWidget(text(text_, Type.BODY, wrap=True))
        form = QFormLayout()
        first, again = QLineEdit(), QLineEdit()
        for f in (first, again):
            f.setEchoMode(QLineEdit.Password)
        form.addRow("Password", first)
        if confirm:
            form.addRow("Again", again)
        lay.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = buttons.button(QDialogButtonBox.Ok)

        def valid() -> None:
            ok.setEnabled(bool(first.text()) and (not confirm or first.text() == again.text()))
        first.textChanged.connect(valid)
        again.textChanged.connect(valid)
        valid()
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        return first.text() if dlg.exec() == QDialog.Accepted else ""

    def _combine_pdf(self, entries: list[FileEntry]) -> None:
        """The chosen pictures and PDFs, in the list's order, as one new PDF (JPEGs as they are)."""
        from .. import pdftools

        order = {id(it.data(0, ROLE_ROW)[0]): i for i, it in
                 ((i, self.tree.topLevelItem(i)) for i in range(self.tree.topLevelItemCount()))}
        entries = sorted(entries, key=lambda e: order.get(id(e), 0))
        first = entries[0].path
        name = f"{first.stem}.pdf" if len(entries) == 1 else "Combined.pdf"
        dest, _ = QFileDialog.getSaveFileName(self, "Save the PDF as", str(first.parent / name), "PDF (*.pdf)")
        if not dest:
            return
        page = self.opts().pdf_page
        paths = [e.path for e in entries]
        self.run_made(Path(dest).with_suffix(".pdf"), lambda out, step, cancel: pdftools.combine(
            paths, out, page, step, cancel), paths, f"{len(paths)} file{'s' if len(paths) != 1 else ''} → one PDF",
            "Combining into one PDF")

    # ---- trimming and joining
    def _ordered(self, entries: list[FileEntry]) -> list[FileEntry]:
        order = {id(self.tree.topLevelItem(i).data(0, ROLE_ROW)[0]): i for i in range(self.tree.topLevelItemCount())}
        return sorted(entries, key=lambda e: order.get(id(e), 0))

    def _trim(self, e: FileEntry) -> None:
        from .. import cutjoin
        from ..engine import _unique
        from ..formats import VIDEO
        from .trim_dialog import TrimDialog, clock

        duration = e.video.duration if e.video is not None else e.audio.duration if e.audio is not None else 0.0
        dlg = TrimDialog(e.path, duration, e.kind == VIDEO, self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        start, end = dlg.times()
        exact = dlg.exact.isChecked()
        dest = _unique(e.path.with_name(f"{e.path.stem} (trimmed){e.path.suffix}"))
        self.run_made(dest, lambda out, step, cancel: cutjoin.trim(
            e.path, start, end, out, exact, lambda f: step(round(f * 100), 100), cancel), [e.path],
            f"Trimmed to {clock(start)}–{clock(end)}" + (" (re-encoded)" if exact else " (copied, no quality lost)"),
            "Trimming")

    def _join(self, entries: list[FileEntry]) -> None:
        from .. import cutjoin
        from ..formats import VIDEO
        from .dialogs import ask

        entries = self._ordered(entries)
        paths = [e.path for e in entries]
        problem = cutjoin.join_problem(paths)
        reencode = bool(problem)
        if problem and not ask(self, "Join them by re-encoding?", f"{problem}, so they can't simply be copied one "
                               "after another. Re-encoding fits each into the first one's picture (MP4, H.264, at a "
                               "quality that looks the same) and takes longer." if entries[0].kind == VIDEO else
                               f"{problem}, so they can't simply be copied one after another. They can be re-encoded "
                               "into the first one's format.", "Re-encode and Join"):
            return
        ext = ".mp4" if reencode and entries[0].kind == VIDEO else paths[0].suffix.lower()
        dest, _ = QFileDialog.getSaveFileName(self, "Save the joined file as", str(paths[0].parent / f"Joined{ext}"))
        if not dest:
            return
        out = Path(dest).with_suffix(ext)
        self.run_made(out, lambda tmp, step, cancel: cutjoin.join(
            paths, tmp, reencode, lambda f: step(round(f * 100), 100), cancel), paths,
            f"{len(paths)} files joined" + (" (re-encoded)" if reencode else " (copied, no quality lost)"), "Joining")

    def run_made(self, dest: Path, work, inputs: list[Path], method: str, busy: str) -> None:
        """Make one new file from several (``work(temp, progress, cancel)``) in the background, then put it at
        ``dest`` (a file already there goes to the Recycle Bin first) and record it in History."""
        import threading
        import time as _time

        from PySide6.QtCore import Signal

        from .. import journal
        from ..fileops import move_to_trash, remove_quietly, temp_name
        from .dialogs import choose, inform

        if self.busy():
            return
        tmp = temp_name(dest.parent, dest.suffix)
        cancel = threading.Event()

        class Make(QThread):
            step = Signal(int, int)
            done = Signal(str)

            def run(self) -> None:
                try:
                    work(tmp, lambda d, t: self.step.emit(d, t), cancel)
                    if dest.exists():
                        move_to_trash(dest)
                    os.replace(tmp, dest)
                    writer = journal.RunWriter(dest.parent, {"tool": "convert", "action": "combine"})
                    writer.add(journal.FileRecord(
                        original=str(inputs[0]), output=str(dest), original_size=sum(p.stat().st_size for p in inputs),
                        output_size=dest.stat().st_size, kind=dest.suffix.lstrip("."), method=method,
                        reversible=False, trashed_at=_time.time(), mode="copy"))
                    writer.close()
                    self.done.emit("")
                except Exception as e:  # noqa: BLE001 - shown to the user
                    remove_quietly(tmp)
                    self.done.emit(str(e) or type(e).__name__)

        def finished(error: str) -> None:
            self._made_worker = None
            self.job_status.hide()
            self._update()
            if error:
                inform(self, "It couldn't be made.", error)
                return
            self.win.history.refresh()
            if choose(self, f"Made “{dest.name}”.", f"In {dest.parent}. The originals weren't changed; History can "
                      "remove the new file again.", "Done", "Show in Folder"):
                from .file_model import open_in_file_manager

                open_in_file_manager(str(dest))

        w = Make(self)
        w.step.connect(lambda d, t: self.job_status.busy(f"{busy}… {d}%" if t == 100 else f"{busy}… {d} of {t}", d, t))
        w.done.connect(finished)
        self._made_worker = w
        self.job_status.busy(f"{busy}…")
        self.job_status.show()
        w.start()

    def settings(self) -> Settings:
        base = self.win.compress.panel.settings()  # encoder and effort preferences
        out = {**self._page_output(), **(self._quick_out or {})}
        s = Settings.from_dict({**base.to_dict(), **out, "min_image_kb": 0, "min_video_mb": 0, "min_audio_kb": 0,
                                "resize_mode": "none", "scan_jxl": True, "convert_inputs": True})
        return s

    def plan(self, e: FileEntry) -> Plan | None:
        opts = self.opts_for(e)
        t = opts.target_for(e.kind)
        if t is None:
            return None
        if e.kind in (DOC, DOCUMENT):
            if self._document_problem(e, t[0]):
                return None
        elif e.info is None and e.kind not in (PDF, FONT, COMIC, ARCHIVE):
            return None
        if self._output() == "folder" and not self.folder.text().strip():
            return None
        if t[0] == "mp4" and not (e.image is not None and e.image.frames > 1):
            return None                                  # (a still picture can't become a video)
        if t[0] == "svg" and e.kind != SVG:
            return None                                  # (drawings only: a photo isn't traced into one)
        if self._turn_problem(e, opts, t):
            return None
        if t[0] == "keep":
            t = (t[0], e.path.suffix.lower())
        if e.kind == FONT:
            from .. import fonts

            try:
                t = (t[0], fonts.target_ext(e.path, t[0]))
            except Exception:  # noqa: BLE001 - not a font fontTools reads
                return None
        if _same_format(e.path.suffix, t[1]) and t[0] not in ("webp_anim", "svg", "ico", "keep") \
                and e.kind not in (PDF, ARCHIVE):
            return None
        own = " · own settings" if self._key(e) in self.overrides else ""
        return Plan(True, convert.describe(e.kind, opts) + own, t[1], route=t[0])

    @staticmethod
    def _document_problem(e: FileEntry, target: str) -> str:
        """Why this document can't be converted to ``target`` here ("" when it can)."""
        from .. import office, tools

        ext = e.path.suffix.lower()
        if target == "pdf":
            if not office.available():
                return "Needs Microsoft Office or LibreOffice"
        elif ext not in PANDOC_INPUTS:
            return "Only Office or LibreOffice reads this: choose PDF"
        if convert.needs_pandoc(e.path, target) and tools.find_tool("pandoc") is None:
            return "Needs Pandoc (see above)"
        return ""

    @staticmethod
    def _turn_problem(e: FileEntry, opts: convert.ConvertOptions, t: tuple[str, str]) -> str:
        """Why this file can't be turned or flipped as chosen ("" when it can, or nothing is to turn)."""
        from ..formats import VIDEO

        if t[0] == "keep" and not (opts.turn or opts.flip):
            return "Nothing to change (choose a turn or a flip, or a format)"
        if e.kind == VIDEO and (opts.turn or opts.flip):
            if t[0] == "keep" and e.path.suffix.lower() not in convert.MP4_FAMILY:
                return "Only MP4 and MOV turn without re-encoding: choose MP4"
            if t[0] != "keep" and t[0] not in ("mp4_h264", "mp4_hevc", *convert.ANIMATION_TARGETS) \
                    and t[0] not in convert.SOUND_TARGETS:
                return "To turn a video, convert it to MP4 (or keep its format, for MP4 and MOV)"
        if t[0] == "keep" and e.kind not in (*convert.KEEP_TARGET, VIDEO):
            return f"{KIND_LABEL.get(e.kind, e.kind)} can't be saved in its own format: choose one"
        return ""

    def row_values(self, e: FileEntry, p: Plan | None) -> list[str]:
        t = self.opts_for(e).target_for(e.kind)
        problem = self._turn_problem(e, self.opts_for(e), t) if t is not None else ""
        if p is not None:
            status = "Ready"
        elif problem:
            status = problem
        elif t is not None and e.kind in (DOC, DOCUMENT) and self._document_problem(e, t[0]):
            status = self._document_problem(e, t[0])
        elif t is not None and t[0] == "mp4":
            status = "Not an animation (only animations become videos)"
        elif t is not None and t[0] == "svg":
            status = "Not an SVG (only SVG drawings are optimized)"
        elif t is not None and _same_format(e.path.suffix, t[1]):
            status = f"Already {KIND_LABEL.get(e.kind, e.kind)}"
        elif self.output.currentData() == "folder" and not self.folder.text().strip():
            status = "Choose an output folder"
        elif e.kind == PDF and t is None:
            status = "Choose “PDFs to” above (more in the context menu)"
        elif e.kind == COMIC and t is None:
            status = "To make it a PDF: Convert To in its context menu"
        elif e.kind == ARCHIVE and t is None:
            status = "To repack it: Convert To in its context menu"
        else:
            status = "Not converted (check the format settings)"
        kind = e.path.suffix[1:].upper() if e.kind == PICTURE else KIND_LABEL.get(e.kind, e.kind)
        return [e.path.name, e.path.parent.name, kind, human(e.size),
                p.reason if p else "–", status]

    def _web_icons(self, e: FileEntry) -> None:
        """favicon.ico, icon.svg, apple-touch-icon.png, icon-192/512.png and site.webmanifest, in a new folder
        next to the picture; the lines for the page's <head> go to the clipboard."""
        from PySide6.QtWidgets import QApplication

        from .. import icons
        from .dialogs import choose, inform
        from .file_model import open_in_file_manager

        dest = e.path.parent / f"{e.path.stem} website icons"
        n = 2
        while dest.exists():
            dest = e.path.parent / f"{e.path.stem} website icons ({n})"
            n += 1
        try:
            made = icons.web_icons(e.path, dest)
        except Exception as ex:  # noqa: BLE001 - shown to the user
            inform(self, "The icons couldn't be made.", str(ex))
            return
        snippet = icons.web_snippet(any(p.name == "icon.svg" for p in made))
        QApplication.clipboard().setText(snippet)
        names = ", ".join(p.name for p in made)
        if choose(self, f"Made {len(made)} website icons in “{dest.name}”.",
                  f"{names}.\n\nPut them in your site's top folder. These lines for each page's <head> are on "
                  f"the clipboard:\n\n{snippet}", "Done", "Show Folder"):
            open_in_file_manager(str(dest))

    def processor(self):
        page, own, smaller = self.opts(), dict(self.overrides), self.smaller.isChecked()

        def run(entry, *a, **k):
            o = own.get(self._key(entry))
            return process_convert(entry, *a, **k, opts=dataclasses.replace(o, only_if_smaller=smaller) if o
                                   else page)
        return run
