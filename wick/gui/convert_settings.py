"""Convert › Format and Settings: the format and settings for chosen files (or the page's defaults).

Only the sections for the kinds of file chosen are shown: images (format, quality, size), videos (format,
resolution, frame rate) and audio (format, bitrate, sample rate, channels).
"""

from __future__ import annotations

import dataclasses

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QSpinBox, QVBoxLayout, QWidget,
)

from .. import convert
from ..formats import ARCHIVE, AUDIO, COMIC, DOC, DOCUMENT, FONT, PDF, VIDEO
from .style import Size, Space, Type
from .widgets import text

IMAGE = "image"
DOCS = "documents"
PDFS = "pdfs"
FONTS = "fonts"
COMICS = "comics"
ARCHIVES = "archives"
DPIS = [("72 dpi (screen)", 72), ("150 dpi", 150), ("300 dpi (print)", 300)]
RESOLUTIONS = [("Keep", 0), ("2160p (4K)", 2160), ("1440p", 1440), ("1080p", 1080), ("720p", 720), ("480p", 480)]
FRAME_RATES = [("Keep", 0), ("60 fps", 60), ("30 fps", 30), ("25 fps", 25), ("24 fps", 24)]
BITRATES = [("Automatic", 0), ("320 kbps", 320), ("256 kbps", 256), ("192 kbps", 192), ("160 kbps", 160),
            ("128 kbps", 128), ("96 kbps", 96), ("64 kbps", 64)]
SAMPLE_RATES = [("Keep", 0), ("48 kHz", 48000), ("44.1 kHz", 44100), ("32 kHz", 32000), ("22.05 kHz", 22050)]
CHANNELS = [("Keep", 0), ("Stereo", 2), ("Mono", 1)]
TURNS = [("Don't turn", 0), ("Right (90°)", 90), ("Left (90°)", 270), ("Upside down (180°)", 180)]
FLIPS = [("Don't flip", ""), ("Mirror (left to right)", "h"), ("Upside down (top to bottom)", "v")]


def kind_group(kind: str) -> str:
    return kind if kind in (VIDEO, AUDIO) else DOCS if kind in (DOC, DOCUMENT) else PDFS if kind == PDF else \
        FONTS if kind == FONT else COMICS if kind == COMIC else ARCHIVES if kind == ARCHIVE else IMAGE


def _combo(items, current) -> QComboBox:
    c = QComboBox()
    for label, value in items:
        c.addItem(label, value)
    c.setCurrentIndex(max(0, c.findData(current)))
    return c


def _name_fields(form: QFormLayout) -> None:
    """Each field takes its row's label as its name for screen readers (dropdowns otherwise read out only their
    current value)."""
    for row in range(form.rowCount()):
        label = form.itemAt(row, QFormLayout.LabelRole)
        field = form.itemAt(row, QFormLayout.FieldRole)
        text = label.widget().text() if label is not None and isinstance(label.widget(), QLabel) else ""
        target = field.widget() if field is not None else None
        if text and target is not None and not target.accessibleName():
            target.setAccessibleName(text.replace("&", ""))


class ConvertSettingsDialog(QDialog):
    def __init__(self, opts: convert.ConvertOptions, kinds: set[str], title: str, note: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(Size.INSPECTOR_W)
        self.opts = opts
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Space.L, Space.L, Space.L, Space.L)
        lay.setSpacing(Space.M)
        if note:
            lay.addWidget(text(note, Type.BODY, wrap=True))

        form = self.form = QFormLayout()     # (one form for every section, so the fields line up)
        form.setHorizontalSpacing(Space.L)
        form.setVerticalSpacing(Space.S)
        lay.addLayout(form)

        def section(heading: str) -> QFormLayout:
            if len(kinds) > 1:
                head = text(heading, Type.HEADLINE, bold=True)
                if form.rowCount():
                    head.setContentsMargins(0, Space.M, 0, 0)
                form.addRow(head)
            return form

        self.image_target = self.video_target = self.audio_target = None
        if IMAGE in kinds:
            form = section("Images")
            self.image_target = _combo([(v[0], k) for k, v in convert.IMAGE_TARGETS.items()]
                                       + [("Don't convert", "none")], opts.image_target)
            self.quality = QSpinBox()
            self.quality.setRange(1, 100)
            self.quality.setValue(opts.quality)
            self.lossless = QCheckBox("Lossless")
            self.lossless.setChecked(opts.lossless)
            self.limit = QCheckBox("No larger than")
            self.limit.setChecked(opts.max_side > 0)
            self.side = QSpinBox()
            self.side.setRange(16, 20000)
            self.side.setSingleStep(100)
            self.side.setSuffix(" px")
            self.side.setValue(opts.max_side or 1920)
            self.side.setAccessibleName("Longest side, in pixels")
            self.side.setToolTip("The longest side, in pixels. Smaller images are left as they are.")
            form.addRow("Format", self.image_target)
            form.addRow("Quality", self.quality)
            form.addRow("", self.lossless)
            size_row = QWidget()
            sl = QHBoxLayout(size_row)
            sl.setContentsMargins(0, 0, 0, 0)
            sl.setSpacing(Space.S)
            sl.addWidget(self.limit)
            sl.addWidget(self.side)
            sl.addStretch(1)
            form.addRow("Size", size_row)
            self.size_row = size_row
            self.pdf_page = _combo([("The picture's own size", "picture"), ("A4", "a4"), ("Letter", "letter")],
                                   opts.pdf_page)
            self.pdf_page.setToolTip("Pictures made into a PDF: each page the picture's size, or on A4 or Letter "
                                     "paper with a margin (never enlarged)")
            form.addRow("PDF page size", self.pdf_page)
            self.image_turn, self.image_flip = _combo(TURNS, opts.turn), _combo(FLIPS, opts.flip)
            self.image_turn.setToolTip("JPEGs are turned without re-encoding (only their orientation tag changes)")
            form.addRow("Turn", self.image_turn)
            form.addRow("Flip", self.image_flip)
            # Icons: the sizes inside, from a preset (MediaWiki and most sites: 16, 32, 48; Windows: up to 256).
            from PySide6.QtWidgets import QGridLayout

            from ..icons import ICO_PRESETS, ICO_SIZES, preset_of

            self.ico_preset = QComboBox()
            for key, (label, _sizes) in ICO_PRESETS.items():
                self.ico_preset.addItem(label, key)
            self.ico_preset.addItem("Choose sizes", "custom")
            self.ico_preset.setCurrentIndex(max(0, self.ico_preset.findData(preset_of(opts.ico_sizes))))
            self.ico_boxes: dict[int, QCheckBox] = {}
            boxes = QWidget()
            grid = QGridLayout(boxes)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(Space.L)
            grid.setVerticalSpacing(Space.XS)
            for i, size in enumerate(ICO_SIZES):
                b = QCheckBox(str(size))
                b.setAccessibleName(f"{size} pixels")
                b.setChecked(size in opts.ico_sizes)
                b.toggled.connect(self._ico_ticked)
                self.ico_boxes[size] = b
                grid.addWidget(b, i // 5, i % 5)
            grid.setColumnStretch(5, 1)
            self.ico_preset.currentIndexChanged.connect(self._ico_preset_chosen)
            form.addRow("Icon sizes", self.ico_preset)
            form.addRow("", boxes)
            self.ico_rows = (self.ico_preset, boxes)
            self.image_target.currentIndexChanged.connect(self._enable)
            self.lossless.toggled.connect(self._enable)
            self.limit.toggled.connect(self._enable)
        if VIDEO in kinds:
            form = section("Videos")
            self.video_target = _combo([(v[0], k) for k, v in convert.VIDEO_TARGETS.items()]
                                       + [("Don't convert", "none")], opts.video_target)
            self.resolution = _combo(RESOLUTIONS, opts.video_height)
            self.resolution.setToolTip("The short side, so portrait videos are treated the same. Smaller videos are "
                                   "left as they are.")
            self.fps = _combo(FRAME_RATES, opts.video_fps)
            self.fps.setToolTip("Videos with a lower frame rate are left as they are")
            form.addRow("Format", self.video_target)
            form.addRow("Resolution", self.resolution)
            form.addRow("Frame rate", self.fps)
            self.video_turn, self.video_flip = _combo(TURNS, opts.turn), _combo(FLIPS, opts.flip)
            self.video_turn.setToolTip("MP4 and MOV are turned without re-encoding (players turn the picture)")
            form.addRow("Turn", self.video_turn)
            form.addRow("Flip", self.video_flip)
            self.video_target.currentIndexChanged.connect(self._enable)
        if AUDIO in kinds:
            form = section("Audio")
            self.audio_target = _combo([(v[0], k) for k, v in convert.AUDIO_TARGETS.items()]
                                       + [("Don't convert", "none")], opts.audio_target)
            self.kbps = _combo(BITRATES, opts.audio_kbps)
            self.kbps.setToolTip("Automatic: high quality for the format (MP3 uses variable bitrate)")
            self.rate = _combo(SAMPLE_RATES, opts.sample_rate)
            self.rate.setToolTip("Opus always plays at 48 kHz")
            self.channels = _combo(CHANNELS, opts.channels)
            form.addRow("Format", self.audio_target)
            form.addRow("Bitrate", self.kbps)
            form.addRow("Sample rate", self.rate)
            form.addRow("Channels", self.channels)
            self.audio_target.currentIndexChanged.connect(self._enable)
        self.document_target = None
        if DOCS in kinds:
            form = section("Documents")
            self.document_target = _combo([(v[0], k) for k, v in convert.DOCUMENT_TARGETS.items()]
                                          + [("Don't convert", "none")], opts.document_target)
            form.addRow("Format", self.document_target)
        self.pdf_target = None
        if PDFS in kinds:
            form = section("PDFs")
            self.pdf_target = _combo([(convert.PDF_TARGETS[k][0], k) for k in convert.PDF_PAGE_TARGETS]
                                     + [("Don't convert", "none")], opts.pdf_target
                                     if opts.pdf_target in convert.PDF_PAGE_TARGETS else "none")
            self.pdf_dpi = _combo(DPIS, opts.pdf_dpi)
            self.pdf_dpi.setToolTip("For pages as images: how sharp they are (and how big)")
            form.addRow("Format", self.pdf_target)
            form.addRow("Resolution", self.pdf_dpi)
            self.pdf_target.currentIndexChanged.connect(self._enable)
        self.font_target = None
        if FONTS in kinds:
            form = section("Fonts")
            self.font_target = _combo([(v[0], k) for k, v in convert.FONT_TARGETS.items()]
                                      + [("Don't convert", "none")], opts.font_target)
            self.font_latin = _combo([("All of them", False), ("Latin only (smaller)", True)], opts.font_latin)
            self.font_latin.setToolTip("Latin only keeps the letters of English and most European languages, with "
                                       "their punctuation and symbols, and drops the rest")
            form.addRow("Format", self.font_target)
            form.addRow("Characters", self.font_latin)
        self.comic_target = None
        if COMICS in kinds:
            form = section("Comic books")
            self.comic_target = _combo([("PDF, a page per picture", "pdf"), ("Don't convert", "none")],
                                       opts.comic_target)
            form.addRow("Format", self.comic_target)
        self.archive_target = None
        if ARCHIVES in kinds:
            form = section("Archives")
            self.archive_target = _combo([(v[0], k) for k, v in convert.ARCHIVE_TARGETS.items()]
                                         + [("Don't repack", "none")], opts.archive_target)
            self.archive_target.setToolTip("Unpacks and packs each archive again, more tightly. Usually saves little "
                                           "(photos and videos inside are compressed already); a result that isn't "
                                           "smaller is thrown away.")
            form.addRow("Repack as", self.archive_target)
        lay.addStretch(1)
        _name_fields(self.form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._enable()

    def _ico_preset_chosen(self) -> None:
        from ..icons import ICO_PRESETS

        key = self.ico_preset.currentData()
        if key in ICO_PRESETS:
            for size, b in self.ico_boxes.items():
                b.blockSignals(True)
                b.setChecked(size in ICO_PRESETS[key][1])
                b.blockSignals(False)

    def _ico_ticked(self) -> None:
        from ..icons import preset_of

        self.ico_preset.blockSignals(True)
        self.ico_preset.setCurrentIndex(self.ico_preset.findData(preset_of(self._ico_sizes())))
        self.ico_preset.blockSignals(False)

    def _ico_sizes(self) -> tuple[int, ...]:
        return tuple(s for s, b in self.ico_boxes.items() if b.isChecked())

    def _enable(self) -> None:
        if getattr(self, "pdf_target", None) is not None:
            self.pdf_dpi.setEnabled(self.pdf_target.currentData() in ("png", "jpeg"))
        if self.image_target is not None:
            info = convert.IMAGE_TARGETS.get(self.image_target.currentData())
            on = info is not None
            self.lossless.setEnabled(bool(info and info[3] and info[4]))
            self.quality.setEnabled(bool(info and info[4]) and not (self.lossless.isChecked()
                                                                      and self.lossless.isEnabled()))
            icon = self.image_target.currentData() == "ico"
            self.limit.setEnabled(on)
            self.side.setEnabled(on and self.limit.isChecked())
            # (an icon has its own sizes and is stored losslessly: quality and size don't apply)
            pdf = self.image_target.currentData() == "pdf"
            for w, shown in ((self.quality, not icon and not pdf), (self.lossless, not icon and not pdf),
                             (self.size_row, not icon and not pdf), (self.pdf_page, pdf),
                             *((w, icon) for w in self.ico_rows)):
                w.setVisible(shown)
                label = self.form.labelForField(w)
                if label is not None:
                    label.setVisible(shown)
            self.adjustSize()
        if self.video_target is not None:
            picture = self.video_target.currentData() in convert.VIDEO_TARGETS and \
                self.video_target.currentData() not in convert.SOUND_TARGETS
            self.resolution.setEnabled(picture)
            self.fps.setEnabled(picture)
        if self.audio_target is not None:
            target = self.audio_target.currentData()
            on = target in convert.AUDIO_TARGETS
            self.kbps.setEnabled(on and target not in convert.LOSSLESS_AUDIO)
            self.rate.setEnabled(on and target != "opus")
            self.channels.setEnabled(on)

    def result_options(self) -> convert.ConvertOptions:
        o = self.opts
        changes: dict = {}
        if self.image_target is not None:
            changes.update(ico_sizes=self._ico_sizes() or o.ico_sizes, pdf_page=self.pdf_page.currentData(),
                           turn=int(self.image_turn.currentData()), flip=self.image_flip.currentData())
            changes.update(image_target=self.image_target.currentData(), quality=self.quality.value(),
                           lossless=self.lossless.isChecked(),
                           max_side=self.side.value() if self.limit.isChecked() else 0)
        if self.video_target is not None:
            changes.update(turn=int(self.video_turn.currentData()), flip=self.video_flip.currentData())
            changes.update(video_target=self.video_target.currentData(), video_height=int(self.resolution.currentData()),
                           video_fps=int(self.fps.currentData()))
        if self.audio_target is not None:
            changes.update(audio_target=self.audio_target.currentData(), audio_kbps=int(self.kbps.currentData()),
                           sample_rate=int(self.rate.currentData()), channels=int(self.channels.currentData()))
        if self.document_target is not None:
            changes.update(document_target=self.document_target.currentData())
        if self.archive_target is not None:
            changes.update(archive_target=self.archive_target.currentData())
        if self.comic_target is not None:
            changes.update(comic_target=self.comic_target.currentData())
        if self.font_target is not None:
            changes.update(font_target=self.font_target.currentData(), font_latin=bool(self.font_latin.currentData()))
        if self.pdf_target is not None:
            changes.update(pdf_target=self.pdf_target.currentData(), pdf_dpi=int(self.pdf_dpi.currentData()))
        return dataclasses.replace(o, **changes)


__all__ = ["ConvertSettingsDialog", "kind_group", "IMAGE", "DOCS", "PDFS", "FONTS", "COMICS", "ARCHIVES"]
