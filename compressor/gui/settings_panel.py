"""The compression settings panel (left side of the Compress tab)."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from ..settings import Settings
from ..fileops import BIN_NAME  # noqa: F401  (others import it from here too)
from .style import Size, Space, Type, text_scale
from .widgets import Section, button, text

METADATA_CHOICES = [("Keep all", "keep"), ("Remove location", "strip_gps"),
                    ("Remove location & personal info", "strip_private"),
                    ("Remove all except date taken", "strip_all_keep_date"), ("Remove all", "strip_all")]
OUTPUT_CHOICES = [("Replace originals", "replace"), ("Next to originals", "beside"),
                  ("Save with a new name", "suffix"), ("Save to another folder", "folder")]
NAME_TIP = ("Plain text is added to the name (_compressed). Or use a pattern: %name% (the original name), %ext%, "
    "%date%, %time%, %datetime%, %unix%, %modified% (the file's modified date). Example: %name% %date%")


def _combo(items: list[tuple[str, str]], tip: str = "") -> QComboBox:
    c = QComboBox()
    # Don't let long option texts widen the panel; the popup still shows them in full.
    c.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    c.setMinimumContentsLength(8)
    for label, value in items:
        c.addItem(label, value)
    if tip:
        c.setToolTip(tip)
    return c


def _set_combo(c: QComboBox, value) -> None:
    i = c.findData(value)
    if i >= 0:
        c.setCurrentIndex(i)


VIDEO_CRF_TIP = ("The encoder's constant-quality value (CRF). Lower means better quality and larger files; about "
                 "6 steps doubles or halves the size. Typical: 20–28 for HEVC, 26–38 for AV1.")


def _spin(lo: int, hi: int, suffix: str = "", tip: str = "") -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    # Size for typical values, not the largest allowed one ("100000 px" would widen the panel).
    s.setMinimumWidth(int(Space.XXL * 3 * text_scale()))
    if suffix:
        s.setSuffix(suffix)
    if tip:
        s.setToolTip(tip)
    return s


def _muted(content: str) -> QLabel:
    return text(content, Type.FOOTNOTE, secondary=True, wrap=True)


class _Form(QFormLayout):
    """Form with one consistent label column width across every section."""

    def addRow(self, label, field=None):  # noqa: N802 - Qt naming
        if isinstance(label, str) and field is not None:
            lab = text(label, Type.BODY)
            lab.setFixedWidth(Size.FORM_LABEL)
            lab.setWordWrap(True)
            lab.setBuddy(field)
            field.setAccessibleName(label)
            return super().addRow(lab, field)
        return super().addRow(label) if field is None else super().addRow(label, field)


def _form(parent: QWidget) -> QFormLayout:
    f = _Form(parent)
    f.setContentsMargins(Space.XS, 0, 0, Space.S)
    f.setHorizontalSpacing(Space.M)
    f.setVerticalSpacing(Space.S)
    f.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    f.setRowWrapPolicy(QFormLayout.DontWrapRows)
    return f


def _vbox(parent: QWidget) -> QVBoxLayout:
    v = QVBoxLayout(parent)
    v.setContentsMargins(Space.XS, 0, 0, Space.S)
    v.setSpacing(Space.S)
    return v


class SettingsPanel(QWidget):
    changed = Signal()
    presets_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Space.L, Space.L, Space.L, Space.L)
        lay.setSpacing(Space.M)
        self.sections: list[Section] = []
        self._applying_preset = False
        self.preset = QComboBox()
        self.preset.setAccessibleName("Preset")
        self.preset.setMinimumContentsLength(8)
        self.preset.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.preset_more = button("", "", "ellipsis", "Save or delete presets")
        self.preset_more.setAccessibleName("Preset actions")
        self.preset_more.setFixedWidth(Size.CONTROL)
        self.preset_menu = QMenu(self)
        self.preset_save = self.preset_menu.addAction("Save Current Options as Preset…", self._preset_save)
        self.preset_delete = self.preset_menu.addAction("Delete Preset", self._preset_delete)
        self.preset_more.clicked.connect(
            lambda: self.preset_menu.exec(self.preset_more.mapToGlobal(self.preset_more.rect().bottomLeft())))
        label = QLabel("Preset")
        label.setBuddy(self.preset)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(Space.S)
        h.addWidget(label)
        h.addWidget(self.preset, 1)
        h.addWidget(self.preset_more)
        lay.addWidget(row)
        self.preset_note = _muted("")
        lay.addWidget(self.preset_note)
        self.preset.activated.connect(self._preset_chosen)

        # ------------------------------------------------ images
        g = QWidget()
        f = _form(g)
        self.image_format = _combo(
            [("Keep format", "keep"), ("JPEG XL", "jxl"),
             ("WebP", "webp"), ("AVIF", "avif"), ("Don't convert", "none")],
            "Keep format: same file type, just smaller - safest for sharing.\n"
            "JPEG XL: smallest, and JPEGs convert losslessly & reversibly; fewer apps open it (see below).\n"
            "WebP/AVIF: open in browsers and most apps.")
        self.jpeg_mode = _combo([("Lossless", "lossless"), ("Lossy", "lossy")],
                                "Lossless: JPEG XL rebuilds the exact original; 'Keep format' re-packs the "
                                "JPEG without touching image quality.")
        self.png_mode = _combo([("Lossless", "lossless"), ("Lossy", "lossy")],
                               "PNG, GIF, BMP, TIFF and lossless WebP. HEIC, AVIF and lossy WebP are always "
                               "re-encoded lossily (they're lossy to begin with).")
        self.quality = _spin(1, 100, tip="Quality for lossy images. 85-90 is visually near-identical for photos.")
        self.effort = _spin(1, 9, tip="JPEG XL effort: higher = smaller but slower. 7 is the sweet spot.")
        f.addRow("Convert to", self.image_format)
        from ..compat import jxl_note

        self.jxl_note = _muted(jxl_note())
        f.addRow(self.jxl_note)
        f.addRow("JPEG", self.jpeg_mode)
        f.addRow("Other images", self.png_mode)
        self.png_mode.setToolTip("PNG, GIF, BMP, TIFF and lossless WebP. HEIC, AVIF and lossy WebP are always "
                                 "re-encoded lossily (they're lossy to begin with).")
        f.addRow("Lossy quality", self.quality)
        self.smart = QCheckBox("Smart quality")
        self.smart.setToolTip("Find the smallest result per file that still meets a measured quality target "
                              "(SSIMULACRA2 for images, VMAF for videos). Slower: each file is encoded several "
                              "times.")
        self.smart_target = _combo([("Visually identical", "identical"), ("High", "high"), ("Good", "good")],
                                   "How close to the original the result must measure")
        f.addRow(self.smart)
        f.addRow("Target", self.smart_target)
        self.smart.toggled.connect(self._update_enabled)
        f.addRow("JPEG XL effort", self.effort)
        self.sections.append(Section("Images", g, True))
        lay.addWidget(self.sections[-1])

        # ------------------------------------------------ JPEG output
        g = QWidget()
        f = _form(g)
        self.jpeg_progressive = _combo([("Progressive", True), ("Baseline", False)],
                                       "Progressive JPEGs are smaller and load gradually. Lossless optimisation "
                                       "always writes progressive.")
        self.jpeg_subsampling = _combo(
            [("Automatic", "auto"), ("4:4:4", "444"), ("4:2:2", "422"), ("4:2:0", "420")],
            "Automatic keeps the source's subsampling; for other sources it uses 4:4:4 at quality ≥ 90.")
        f.addRow("Encoding", self.jpeg_progressive)
        f.addRow("Chroma", self.jpeg_subsampling)
        self.jpeg_group = g
        self.sections.append(Section("JPEG output", g, False))
        lay.addWidget(self.sections[-1])

        # ------------------------------------------------ resize
        g = QWidget()
        v = _vbox(g)
        self.resize_mode = _combo([("Don't resize", "none"), ("Limit long edge", "long_edge"),
                                   ("Fit inside a box", "fit"), ("Scale by percent", "percent")],
                                  "Images are only ever made smaller, never enlarged.")
        v.addWidget(self.resize_mode)
        self.resize_stack = QStackedWidget()
        self.resize_stack.addWidget(QWidget())
        self.resize_long = _spin(16, 100000, " px")
        self.resize_stack.addWidget(self._row(QLabel("Long edge"), self.resize_long))
        self.resize_w = _spin(16, 100000, " px")
        self.resize_h = _spin(16, 100000, " px")
        self.resize_stack.addWidget(self._row(self.resize_w, QLabel("×"), self.resize_h))
        self.resize_pct = _spin(1, 99, " %")
        self.resize_stack.addWidget(self._row(QLabel("Scale to"), self.resize_pct))
        v.addWidget(self.resize_stack)
        self.resize_mode.currentIndexChanged.connect(self.resize_stack.setCurrentIndex)
        self.sections.append(Section("Resize images", g, False))
        lay.addWidget(self.sections[-1])

        # ------------------------------------------------ size limit
        g = QWidget()
        v = _vbox(g)
        self.fit_on = QCheckBox("Make each file smaller than")
        self.fit_on.setToolTip("For upload and attachment limits (Discord, email, forms). Images get the best "
                               "quality that fits, and smaller dimensions only if they must; videos get the "
                               "bitrate that fits, at a lower resolution if needed. Files already under the "
                               "limit are compressed as usual.")
        self.fit_mb = QDoubleSpinBox()
        self.fit_mb.setRange(0.05, 100000)
        self.fit_mb.setDecimals(2)
        self.fit_mb.setValue(10)
        self.fit_mb.setSuffix(" MB")
        self.fit_mb.setAccessibleName("Size limit in megabytes")
        self.fit_on.toggled.connect(self.fit_mb.setEnabled)
        self.fit_mb.setEnabled(False)
        v.addWidget(self.fit_on)
        v.addWidget(self._row(self.fit_mb))
        self.sections.append(Section("Size limit", g, False))
        lay.addWidget(self.sections[-1])

        # ------------------------------------------------ metadata
        g = QWidget()
        f = _form(g)
        self.metadata = _combo(METADATA_CHOICES,
                               "Location: GPS coordinates. Personal info: also camera, serial numbers, owner, author "
                               "and comments.\nRotation and colour profile are always kept so images look right.")
        f.addRow("Metadata", self.metadata)
        self.preserve_dates = QCheckBox("Keep original file dates")
        self.preserve_dates.setToolTip("Results get the original's modified and created dates, so photos stay "
                                       "sorted by when they were taken.")
        f.addRow(self.preserve_dates)
        self.sections.append(Section("Privacy", g, True))
        lay.addWidget(self.sections[-1])

        # ------------------------------------------------ videos
        g = QWidget()
        f = _form(g)
        self.video_codec = _combo([("Automatic", "auto"), ("AV1", "av1"), ("HEVC (H.265)", "hevc"),
                                   ("Don't convert", "none")])
        self.video_encoder = _combo([("Automatic", "auto"), ("GPU (NVENC)", "gpu"),
                                     ("CPU", "cpu")])
        self.video_codec.setToolTip("Automatic: HEVC for videos from cameras and phones (AV1 smooths away fine grain "
                                    "and detail in those), AV1 for everything else.\nAV1: smallest files. HEVC: "
                                    "plays on more older devices.")
        self.video_encoder.setToolTip("Automatic uses the NVIDIA GPU when available (fast), otherwise the CPU (slow).")
        self.video_quality = _combo([("Visually identical", "identical"), ("High", "high"),
                                     ("Balanced", "balanced"), ("Smallest", "small"), ("Custom", "custom")],
                                    "Visually identical: test encodes short stretches and measures them against the "
                                    "original (VMAF), then uses the smallest setting that still looks the same. "
                                    "Slower, but you don't have to guess.\nCustom: set the encoder's quality value "
                                    "yourself.\nEvery finished video shows how close it is to the original.")
        self.video_crf = _spin(1, 63, tip=VIDEO_CRF_TIP)
        self.gpu_label = _muted("Checking GPU…")
        f.addRow("Codec", self.video_codec)
        f.addRow("Encoder", self.video_encoder)
        f.addRow("Quality", self.video_quality)
        f.addRow("Quality value", self.video_crf)
        self.video_height = _combo([("Original", 0), ("4K (2160p)", 2160), ("1440p", 1440), ("1080p", 1080),
                                    ("720p", 720), ("480p", 480)],
                                   "Videos larger than this are scaled down (never up). Measured on the short "
                                   "side, so portrait phone videos are handled too.")
        self.video_fps = _combo([("Original", 0), ("60 fps", 60), ("30 fps", 30), ("24 fps", 24)],
                                "Videos with a higher frame rate are reduced to this.")
        self.audio = _combo([("Copy as-is", 0), ("192 kbps", 192), ("128 kbps", 128), ("96 kbps", 96),
                             ("64 kbps", 64)], "Re-encode audio (AAC in MP4, Opus in MKV/WebM) or keep it unchanged.")
        f.addRow("Max resolution", self.video_height)
        f.addRow("Max frame rate", self.video_fps)
        self.audio.setAccessibleName("Soundtrack")
        f.addRow("Soundtrack", self.audio)
        self.video_tracks = _combo([("Keep all", "all"), ("Only my languages", "mine")],
                                   "Leave out audio and subtitle tracks in languages you don't use (films often "
                                   "carry several dubs). The main soundtrack and untagged tracks always stay.")
        self.video_tracks.setAccessibleName("Audio and subtitle tracks")
        f.addRow("Tracks", self.video_tracks)
        self._languages = ""
        self.languages_btn = button("", "", None, "Choose your languages")
        self.languages_btn.setAccessibleName("My languages")
        self.languages_btn.clicked.connect(self._choose_languages)
        f.addRow("Languages", self.languages_btn)
        self._show_languages()
        self.video_cadence = QCheckBox("Remove duplicated frames")
        self.video_cadence.setToolTip("Some uploads repeat frames to reach a standard rate (a 24 fps film stored as "
                                      "30 fps). The repeats are dropped and the original frame rate restored: "
                                      "smoother motion and a smaller file.")
        f.addRow(self.video_cadence)
        self.video_crop = QCheckBox("Remove black bars")
        self.video_crop.setToolTip("Letterbox or pillarbox bars (black bands around the picture) are cut off: "
                                   "the encoder stops spending space on them. Only bars that stay black through "
                                   "the whole video are removed.")
        f.addRow(self.video_crop)
        f.addRow(self.gpu_label)
        self.sections.append(Section("Videos", g, True))
        lay.addWidget(self.sections[-1])

        # ------------------------------------------------ music and other audio
        g = QWidget()
        f = _form(g)
        self.audio_lossless = _combo([("FLAC (lossless)", "flac"), ("Lossy format", "lossy"),
                                      ("Don't change", "keep")],
                                     "WAV, AIFF, ALAC, APE, WavPack and FLAC files. FLAC keeps every sample (checked "
                                     "after conversion) at about half the size of WAV.")
        self.audio_lossy = _combo([("Keep as they are", "keep"), ("Re-encode high bitrates", "reencode")],
                                  "MP3, AAC, Vorbis, WMA and Opus files. Re-encoding only happens when the file's "
                                  "bitrate is well above the target, since every lossy step costs some quality.")
        self.audio_format = _combo([("Opus (smallest)", "opus"), ("AAC (Apple devices)", "aac"),
                                    ("MP3 (plays everywhere)", "mp3")],
                                   "Opus sounds best for its size. AAC and MP3 play on more older devices.")
        self.audio_quality = _combo([("High", "high"), ("Balanced", "balanced"), ("Smallest", "small")],
                                    "Opus 160/128/96 kbps, AAC 256/192/128 kbps, MP3 VBR V0/V2/V5 (stereo).")
        self.audio_mono = QCheckBox("Mono when channels match")
        self.audio_mono.setToolTip("Stereo files whose left and right channels are identical are stored as mono: "
                                   "half the data, nothing audible changes.")
        self.audio_cd = QCheckBox("Reduce hi-res to CD quality")
        self.audio_cd.setToolTip("96/192 kHz and 24-bit audio becomes 44.1/48 kHz, 16-bit: removes ultrasonic "
                                 "content and dynamic range beyond human hearing.")
        f.addRow("Lossless files", self.audio_lossless)
        f.addRow("Compressed files", self.audio_lossy)
        f.addRow("Lossy format", self.audio_format)
        f.addRow("Quality", self.audio_quality)
        f.addRow(self.audio_mono)
        f.addRow(self.audio_cd)
        self.sections.append(Section("Audio", g, False))
        lay.addWidget(self.sections[-1])

        # ------------------------------------------------ documents
        g = QWidget()
        f = _form(g)
        self.documents = _combo([("Shrink, identical pictures", "lossless"), ("Also reduce large pictures", "reduce"),
                                 ("Don't change", "none")],
                                "Word, PowerPoint, Excel and OpenDocument files and PDFs are mostly pictures. "
                                "They're stored smaller with exactly the same pixels; everything else in the "
                                "document stays byte-for-byte the same (checked).\nReduce: pictures larger than "
                                "2560 px are also scaled down: much smaller, still sharp on screen.")
        self.documents.setAccessibleName("Documents")
        f.addRow("Pictures", self.documents)
        f.addRow(_muted("Signed and password-protected documents are left alone."))
        self.sections.append(Section("Documents", g, False))
        lay.addWidget(self.sections[-1])

        # ------------------------------------------------ output
        g = QWidget()
        v = _vbox(g)
        self.output_mode = _combo(OUTPUT_CHOICES)
        self.output_mode.setAccessibleName("Where results go")
        self.output_mode.setToolTip(f"Replace: originals go to the {BIN_NAME} after each file is verified.\n"
                                    "Next to originals: same name, new file type; originals kept.\n"
                                    "New name: the result is saved next to the original with a new name.\n"
                                    "Another folder: results go elsewhere; originals are untouched.")
        v.addWidget(self.output_mode)
        self.output_stack = QStackedWidget()
        self.allow_delete = QCheckBox(f"Delete permanently on drives\nwithout a {BIN_NAME}")
        self.allow_delete.setToolTip("USB sticks and network drives have no Recycle Bin: with this on, originals "
                                     "there are deleted permanently after the result is verified.")
        replace_box = QWidget()
        rl = QVBoxLayout(replace_box)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.allow_delete)
        self.output_stack.addWidget(replace_box)
        self.output_stack.addWidget(QWidget())  # next to originals: nothing to set
        self.suffix = QLineEdit()
        self.suffix.setToolTip(NAME_TIP)
        self.suffix.setAccessibleName("New name")
        self.suffix.setPlaceholderText("_compressed or %name% %date%")
        self.output_stack.addWidget(self._row(QLabel("Name"), self.suffix))
        folder_box = QWidget()
        fl = QVBoxLayout(folder_box)
        fl.setContentsMargins(0, 0, 0, 0)
        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("Output folder")
        browse = QPushButton("…")
        browse.setFixedWidth(32)
        browse.clicked.connect(self._browse_output)
        fl.addWidget(self._row(self.output_dir, browse))
        self.keep_structure = QCheckBox("Recreate subfolders")
        fl.addWidget(self.keep_structure)
        self.output_stack.addWidget(folder_box)
        v.addWidget(self.output_stack)
        self.output_mode.currentIndexChanged.connect(self.output_stack.setCurrentIndex)
        # Only keep a result when it saved enough
        self.require_savings = QCheckBox("Only keep results that save at least")
        self.require_savings.setToolTip("Otherwise the original stays exactly as it is and the result is thrown away.")
        self.min_savings = _spin(0, 90, " %", "Minimum saving, as a percentage of the original")
        self.min_savings.setAccessibleName("Minimum saving in percent")
        self.min_savings_kb = _spin(0, 1_000_000, " KB", "…and at least this much space (0 = no size minimum)")
        self.min_savings_kb.setAccessibleName("Minimum saving in kilobytes")
        v.addWidget(self.require_savings)
        v.addWidget(self._row(self.min_savings, QLabel("and"), self.min_savings_kb))
        self.require_savings.toggled.connect(lambda on: (self.min_savings.setEnabled(on),
                                                         self.min_savings_kb.setEnabled(on)))
        self.sections.append(Section("Output", g, True))
        lay.addWidget(self.sections[-1])

        # ------------------------------------------------ rules
        g = QWidget()
        f = _form(g)
        self.min_image_kb = _spin(0, 100000, " KB")
        self.min_video_mb = _spin(0, 100000, " MB")
        self.skip_programs = QCheckBox("Skip program folders")
        self.skip_programs.setToolTip("Program and game folders (any folder with .dll/.so files) are "
                                      "skipped: converting their images would break the program.")
        self.include_hidden = QCheckBox("Include hidden items")
        self.include_hidden.setToolTip("Also look in hidden files and folders")
        f.addRow("Skip images under", self.min_image_kb)
        f.addRow("Skip videos under", self.min_video_mb)
        self.min_audio_kb = _spin(0, 100000, " KB")
        f.addRow("Skip audio under", self.min_audio_kb)
        f.addRow(self.skip_programs)
        f.addRow(self.include_hidden)
        self.sections.append(Section("Rules", g, False))
        lay.addWidget(self.sections[-1])

        # ------------------------------------------------ performance
        g = QWidget()
        f = _form(g)
        self.workers = _spin(0, 64, tip="How many images are compressed at once. 0 = automatic.")
        self.workers.setSpecialValueText("Automatic")
        self.low_priority = QCheckBox("Low priority")
        self.low_priority.setToolTip("Encoders run at low priority so the computer stays responsive")
        f.addRow("Parallel images", self.workers)
        self.video_jobs = _combo([("Automatic", 0), ("One at a time", 1), ("Two at once", 2)],
                                 "Automatic: two at once with an NVIDIA GPU (RTX 40-series and newer have two video "
                                 "encoders), otherwise one, since CPU encoders already use every core.")
        f.addRow("Videos at once", self.video_jobs)
        f.addRow(self.low_priority)
        self.sections.append(Section("Performance", g, False))
        lay.addWidget(self.sections[-1])

        lay.addWidget(_muted("Every file is verified before anything is replaced. Everything can be undone later."))
        lay.addStretch(1)

        for w in (self.image_format, self.jpeg_mode, self.png_mode, self.video_codec, self.output_mode,
                  self.audio_lossless, self.audio_lossy, self.video_tracks, self.video_quality):
            w.currentIndexChanged.connect(self._update_enabled)
        for sig in self._signals():
            sig.connect(lambda *_: self.changed.emit())
        self.changed.connect(lambda: None if self._applying_preset else self.refresh_presets())
        self._update_enabled()

    @staticmethod
    def _row(*widgets) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for x in widgets:
            h.addWidget(x, 1 if isinstance(x, (QLineEdit, QSpinBox)) else 0)
        return w

    def _show_languages(self) -> None:
        from .. import languages

        keys = languages.parse(self._languages)
        label = languages.describe(keys or languages.detect())
        self.languages_btn.setText(label if keys else f"{label} (this PC)")
        self.languages_btn.setToolTip("Tracks in these languages are kept. Click to choose.")

    def _choose_languages(self) -> None:
        from .. import languages
        from .language_dialog import LanguageDialog

        dlg = LanguageDialog(languages.parse(self._languages) or languages.detect(), self)
        if dlg.exec():
            chosen = dlg.chosen()
            # Saved only when it differs from what this computer suggests (so it keeps following the PC).
            self._languages = "" if chosen == dlg.detected else ",".join(chosen)
            self._show_languages()
            self.changed.emit()

    def _browse_output(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.output_dir.text())
        if d:
            self.output_dir.setText(os.path.normpath(d))

    def _signals(self):
        combos = (self.image_format, self.jpeg_mode, self.png_mode, self.jpeg_progressive, self.jpeg_subsampling,
                  self.resize_mode, self.metadata, self.video_codec, self.video_encoder, self.video_quality,
                  self.output_mode, self.smart_target, self.video_height, self.video_fps, self.audio,
                  self.audio_lossless, self.audio_lossy, self.audio_format, self.audio_quality, self.video_jobs,
                  self.video_tracks, self.documents)
        spins = (self.quality, self.effort, self.resize_long, self.resize_w, self.resize_h, self.resize_pct,
                 self.min_savings, self.min_savings_kb, self.min_image_kb, self.min_video_mb, self.workers,
                 self.min_audio_kb, self.video_crf)
        checks = (self.allow_delete, self.keep_structure, self.skip_programs, self.include_hidden,
                  self.low_priority, self.preserve_dates, self.require_savings, self.smart, self.audio_mono,
                  self.audio_cd, self.video_cadence, self.fit_on, self.video_crop)
        return ([c.currentIndexChanged for c in combos] + [s.valueChanged for s in spins]
                + [c.toggled for c in checks] + [self.suffix.textChanged, self.output_dir.textChanged,
                                                 self.fit_mb.valueChanged])

    def set_file_kinds(self, kinds: set[str] | None) -> None:
        """Show only the options for the kinds of files in the list ("image"/"video"/"audio"; None = all)."""
        self._kinds = kinds
        self._update_enabled()

    @staticmethod
    def _show(w: QWidget, on: bool) -> None:
        """Show/hide a control together with its form label."""
        lay = w.parentWidget().layout() if w.parentWidget() is not None else None
        if isinstance(lay, QFormLayout) and lay.indexOf(w) >= 0:
            lay.setRowVisible(w, on)
        else:
            w.setVisible(on)

    def _update_enabled(self) -> None:
        kinds = getattr(self, "_kinds", None)
        has = (lambda k: True) if not kinds else (lambda k: k in kinds)
        show = self._show
        sec = {x.toggle.text(): x for x in self.sections}

        fmt = self.image_format.currentData()
        images = fmt != "none"
        smart = self.smart.isChecked()
        for w in (self.jpeg_mode, self.png_mode, self.smart):
            show(w, images)
        show(self.quality, images and not smart)
        show(self.smart_target, images and smart)
        show(self.effort, fmt == "jxl")
        show(self.jxl_note, fmt == "jxl")
        sec["Images"].setVisible(has("image"))
        sec["JPEG output"].setVisible(has("image") and fmt == "keep")
        sec["Resize images"].setVisible(has("image") and images)

        videos = self.video_codec.currentData() != "none"
        for w in (self.video_encoder, self.video_quality, self.video_height, self.video_fps, self.audio,
                  self.video_cadence, self.gpu_label, self.video_tracks):
            show(w, videos)
        show(self.languages_btn, videos and self.video_tracks.currentData() == "mine")
        show(self.video_crf, videos and self.video_quality.currentData() == "custom")
        sec["Videos"].setVisible(has("video"))

        lossless, lossy = self.audio_lossless.currentData(), self.audio_lossy.currentData()
        uses_lossy = lossless == "lossy" or lossy == "reencode"
        show(self.audio_format, uses_lossy)
        show(self.audio_quality, uses_lossy)
        show(self.audio_mono, lossless != "keep" or lossy != "keep")
        show(self.audio_cd, lossless == "flac")
        sec["Audio"].setVisible(has("audio"))
        sec["Documents"].setVisible(has("document"))

        show(self.min_image_kb, has("image") and images)
        show(self.min_video_mb, has("video") and videos)
        show(self.min_audio_kb, has("audio"))
        show(self.workers, has("image") or has("audio"))
        show(self.video_jobs, has("video") and videos)

    def load(self, s: Settings) -> None:
        _set_combo(self.image_format, s.image_format)
        _set_combo(self.jpeg_mode, s.jpeg_mode)
        _set_combo(self.png_mode, s.png_mode)
        self.quality.setValue(s.image_quality)
        self.effort.setValue(s.image_effort)
        _set_combo(self.jpeg_progressive, s.jpeg_progressive)
        _set_combo(self.jpeg_subsampling, s.jpeg_subsampling)
        _set_combo(self.resize_mode, s.resize_mode)
        self.resize_stack.setCurrentIndex(self.resize_mode.currentIndex())
        self.resize_long.setValue(s.resize_long_edge)
        self.resize_w.setValue(s.resize_width)
        self.resize_h.setValue(s.resize_height)
        self.resize_pct.setValue(s.resize_percent)
        self.fit_on.setChecked(s.fit_under_kb > 0)
        if s.fit_under_kb > 0:
            self.fit_mb.setValue(s.fit_under_kb / 1024)
        _set_combo(self.metadata, s.metadata)
        _set_combo(self.video_codec, s.video_codec)
        _set_combo(self.video_encoder, s.video_encoder)
        _set_combo(self.video_quality, s.video_quality)
        self.video_crf.setValue(s.video_crf)
        _set_combo(self.video_tracks, s.video_tracks)
        self._languages = s.languages
        self._show_languages()
        _set_combo(self.output_mode, s.output_mode)
        self.output_stack.setCurrentIndex(self.output_mode.currentIndex())
        self.suffix.setText(s.output_suffix)
        self.output_dir.setText(s.output_dir)
        self.keep_structure.setChecked(s.keep_structure)
        self.allow_delete.setChecked(s.allow_permanent_delete)
        self.min_savings.setValue(int(s.min_savings_pct))
        self.min_savings_kb.setValue(s.min_savings_kb)
        self.require_savings.setChecked(s.require_savings)
        self.min_savings.setEnabled(s.require_savings)
        self.min_savings_kb.setEnabled(s.require_savings)
        self.preserve_dates.setChecked(s.preserve_dates)
        self.smart.setChecked(s.smart_quality)
        _set_combo(self.smart_target, s.smart_target)
        _set_combo(self.video_height, s.video_max_height)
        _set_combo(self.video_fps, s.video_max_fps)
        _set_combo(self.audio, s.audio_bitrate)
        _set_combo(self.audio_lossless, s.audio_lossless)
        _set_combo(self.audio_lossy, s.audio_lossy)
        _set_combo(self.audio_format, s.audio_format)
        _set_combo(self.audio_quality, s.audio_quality)
        _set_combo(self.documents, s.documents)
        self.audio_mono.setChecked(s.audio_mono)
        self.audio_cd.setChecked(s.audio_cd_quality)
        self.video_cadence.setChecked(s.video_fix_cadence)
        self.video_crop.setChecked(s.video_crop_bars)
        _set_combo(self.video_jobs, s.video_jobs)
        self.min_audio_kb.setValue(s.min_audio_kb)
        self.refresh_presets()
        self.min_image_kb.setValue(s.min_image_kb)
        self.min_video_mb.setValue(s.min_video_mb)
        self.skip_programs.setChecked(s.skip_program_folders)
        self.include_hidden.setChecked(s.include_hidden)
        self.workers.setValue(s.workers)
        self.low_priority.setChecked(s.low_priority)
        self._update_enabled()

    def settings(self) -> Settings:
        s = Settings(
            image_format=self.image_format.currentData(), jpeg_mode=self.jpeg_mode.currentData(),
            png_mode=self.png_mode.currentData(), image_quality=self.quality.value(),
            image_effort=self.effort.value(), jpeg_progressive=bool(self.jpeg_progressive.currentData()),
            jpeg_subsampling=self.jpeg_subsampling.currentData(), resize_mode=self.resize_mode.currentData(),
            resize_long_edge=self.resize_long.value(), resize_width=self.resize_w.value(),
            resize_height=self.resize_h.value(), resize_percent=self.resize_pct.value(),
            metadata=self.metadata.currentData(),
            video_codec=self.video_codec.currentData(), video_encoder=self.video_encoder.currentData(),
            video_quality=self.video_quality.currentData(), video_crf=self.video_crf.value(),
            video_tracks=self.video_tracks.currentData(),
            languages=self._languages,
            output_mode=self.output_mode.currentData(), output_suffix=self.suffix.text(),
            output_dir=self.output_dir.text().strip(), keep_structure=self.keep_structure.isChecked(),
            allow_permanent_delete=self.allow_delete.isChecked(),
            min_savings_pct=float(self.min_savings.value()), min_image_kb=self.min_image_kb.value(),
            min_savings_kb=self.min_savings_kb.value(), require_savings=self.require_savings.isChecked(),
            preserve_dates=self.preserve_dates.isChecked(), smart_quality=self.smart.isChecked(),
            smart_target=self.smart_target.currentData(), video_max_height=int(self.video_height.currentData()),
            video_max_fps=int(self.video_fps.currentData()), audio_bitrate=int(self.audio.currentData()),
            min_video_mb=self.min_video_mb.value(), skip_program_folders=self.skip_programs.isChecked(),
            include_hidden=self.include_hidden.isChecked(), workers=self.workers.value(),
            low_priority=self.low_priority.isChecked(),
            fit_under_kb=int(round(self.fit_mb.value() * 1024)) if self.fit_on.isChecked() else 0,
            audio_lossless=self.audio_lossless.currentData(), audio_lossy=self.audio_lossy.currentData(),
            audio_format=self.audio_format.currentData(), audio_quality=self.audio_quality.currentData(),
            audio_mono=self.audio_mono.isChecked(), audio_cd_quality=self.audio_cd.isChecked(),
            min_audio_kb=self.min_audio_kb.value(), video_fix_cadence=self.video_cadence.isChecked(),
            video_crop_bars=self.video_crop.isChecked(),
            video_jobs=int(self.video_jobs.currentData()), documents=self.documents.currentData(),
        )
        s.validate()
        return s

    # ---- presets
    def refresh_presets(self) -> None:
        from .. import presets

        current = presets.matching(self.settings()) if hasattr(self, "audio") else None
        self.preset.blockSignals(True)
        self.preset.clear()
        self.preset.addItem("Custom", "")
        for name in presets.names():
            self.preset.addItem(name, name)
            self.preset.setItemData(self.preset.count() - 1, presets.description(name), Qt.ToolTipRole)
        self.preset.setCurrentIndex(max(0, self.preset.findData(current or "")))
        self.preset.blockSignals(False)
        deletable = bool(current) and not presets.is_builtin(current or "")
        self.preset_delete.setEnabled(deletable)
        self.preset_delete.setText(f"Delete “{current}”" if deletable else "Delete Preset")
        self.preset_note.setText(presets.description(current) if current else "Your own combination of options.")

    def _preset_chosen(self, _index: int) -> None:
        name = self.preset.currentData()
        if name:
            self.apply_preset(name)

    def apply_preset(self, name: str) -> bool:
        """Load a preset's options into the panel. False if there is no such preset."""
        from .. import presets

        if presets.get(name) is None:
            return False
        self._applying_preset = True
        try:
            self.load(presets.apply(name, self.settings()))
        finally:
            self._applying_preset = False
        self.changed.emit()
        return True

    def _preset_save(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        from .. import presets
        from .dialogs import inform

        name, ok = QInputDialog.getText(self, "Save preset", "Name for these options:")
        if not ok or not name.strip():
            return
        try:
            presets.save(name, self.settings())
        except ValueError as e:
            inform(self, "Couldn't save the preset", str(e))
            return
        self.refresh_presets()
        self.presets_changed.emit()

    def _preset_delete(self) -> None:
        from .. import presets
        from .dialogs import ask

        name = self.preset.currentData()
        if name and not presets.is_builtin(name) and ask(self, f"Delete the preset “{name}”?", "", "Delete",
                                                          destructive=True):
            presets.delete(name)
            self.refresh_presets()
            self.presets_changed.emit()

    def show_encoders(self, enc: dict) -> None:
        from ..video import GPU_ENCODERS

        def maker(encoder: str) -> str:
            return {"nvenc": "NVIDIA", "qsv": "Intel", "amf": "AMD"}[encoder.rsplit("_", 1)[1]]

        av1 = next((e for e in GPU_ENCODERS["av1"] if enc.get(e)), None)
        hevc = next((e for e in GPU_ENCODERS["hevc"] if enc.get(e)), None)
        if av1 and hevc:
            text = f"{maker(av1)} GPU found: AV1 and HEVC encode on the GPU (fast)."
        elif av1:
            text = f"{maker(av1)} GPU found: AV1 on the GPU (fast); HEVC uses the CPU."
        elif hevc:
            text = f"{maker(hevc)} GPU found: HEVC on the GPU; AV1 needs a newer GPU, so it uses the CPU (slow)."
        elif enc:
            text = "No supported GPU encoder found: videos encode on the CPU (slow)."
        else:
            text = "ffmpeg not found: video conversion unavailable."
        self.gpu_label.setText(text)
