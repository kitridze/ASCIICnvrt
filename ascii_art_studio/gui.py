"""PySide6 front-end for ASCII Art Studio.

All image work happens in the engine, on worker threads (QThreadPool), so the
window never freezes. Every option change re-runs the conversion after a short
debounce; results from outdated runs are discarded.
"""

from __future__ import annotations

import os
import sys
import traceback
from typing import Callable

from PIL import Image
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QFontDatabase, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QScrollArea, QSizePolicy,
    QSlider, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from . import engine
from .engine import EdgeMode, OutputFormat, SizeMode

APP_NAME = "ASCII Art Studio"
PREVIEW_FONT_SIZE = 10          # preview is drawn at a fixed, moderate size
OPEN_FILTER = ("Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff *.ppm *.pgm);;"
               "All files (*)")
RED_BORDER = "border: 1px solid #c0392b;"
RED_TEXT = "color: #c0392b;"
HINT_STYLE = "color: palette(mid);"


# --------------------------------------------------------------------------
# Background jobs
# --------------------------------------------------------------------------
class _Signals(QObject):
    finished = Signal(int, str, object)   # job id, kind, payload
    failed = Signal(int, str, str)        # job id, kind, user-facing message


class Job(QRunnable):
    """Runs `fn` on the thread pool and reports back through queued signals."""

    def __init__(self, job_id: int, kind: str, fn: Callable[[], object]):
        super().__init__()
        self.job_id, self.kind, self._fn = job_id, kind, fn
        self.signals = _Signals()
        self.setAutoDelete(False)   # the window owns the job until its result arrives

    def run(self) -> None:
        try:
            payload = self._fn()
        except engine.AsciiError as exc:
            self.signals.failed.emit(self.job_id, self.kind, str(exc))
        except Exception as exc:    # never let a worker die silently
            traceback.print_exc()
            self.signals.failed.emit(self.job_id, self.kind, f"Unexpected error: {exc}")
        else:
            self.signals.finished.emit(self.job_id, self.kind, payload)


# --------------------------------------------------------------------------
# Small widgets
# --------------------------------------------------------------------------
def pil_to_pixmap(img: Image.Image) -> QPixmap:
    rgb = img.convert("RGB")
    data = rgb.tobytes("raw", "RGB")
    qimg = QImage(data, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg)   # fromImage copies, so `data` may be freed


class PreviewLabel(QLabel):
    """Shows a pixmap scaled to fit, re-scaling smoothly whenever the pane is resized."""

    def __init__(self, placeholder: str):
        super().__init__(placeholder)
        self._placeholder = placeholder
        self._pixmap: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(220, 220)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setStyleSheet("background: palette(base); border: 1px solid palette(mid);")

    def set_full_pixmap(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap
        if pixmap is None:
            self.setPixmap(QPixmap())
            self.setText(self._placeholder)
        else:
            self._rescale()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is not None:
            self.setPixmap(self._pixmap.scaled(
                self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(HINT_STYLE)
    return label


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    CUSTOM = "Custom…"

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1360, 860)
        self.setAcceptDrops(True)

        self._source: Image.Image | None = None
        self._source_path: str | None = None
        self._result: engine.AsciiResult | None = None
        self._preview_img: Image.Image | None = None
        self._text_dirty = False
        self._job_seq = 0
        self._convert_job = 0          # id of the newest conversion; older ones are ignored
        self._jobs: dict[int, Job] = {}
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)

        self._debounce = QTimer(self, singleShot=True, interval=220)
        self._debounce.timeout.connect(self._run_conversion)

        self._build_ui()
        self._sync_enabled()
        self.statusBar().showMessage("Open an image to begin — or drop one onto this window.")

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        panel = QWidget()
        col = QVBoxLayout(panel)
        for group in (self._group_image(), self._group_chars(), self._group_size(),
                      self._group_edges(), self._group_tone(), self._group_output()):
            col.addWidget(group)
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(420)
        scroll.setWidget(panel)

        # right side: source | result
        self.source_view = PreviewLabel("No image loaded.\nUse “Open image…” or drop a file here.")
        self.result_view = PreviewLabel("The ASCII result appears here.")
        self.text_view = QPlainTextEdit(readOnly=True)
        self.text_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.text_view.setPlaceholderText("The ASCII text appears here.")

        self.error_banner = QLabel()
        self.error_banner.setWordWrap(True)
        self.error_banner.setStyleSheet("background:#fdecea; color:#a93226; padding:6px; border-radius:4px;")
        self.error_banner.hide()

        self.tabs = QTabWidget()
        self.tabs.addTab(self.result_view, "Rendered")
        self.tabs.addTab(self.text_view, "Text")
        self.tabs.currentChanged.connect(lambda _i: self._refresh_text_tab())

        source_box = QWidget()
        sv = QVBoxLayout(source_box)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.addWidget(QLabel("<b>Source</b>"))
        sv.addWidget(self.source_view, 1)

        result_box = QWidget()
        rv = QVBoxLayout(result_box)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(QLabel("<b>ASCII result</b>"))
        rv.addWidget(self.error_banner)
        rv.addWidget(self.tabs, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(source_box)
        splitter.addWidget(result_box)
        splitter.setSizes([1, 1])

        central = QWidget()
        h = QHBoxLayout(central)
        h.addWidget(scroll)
        h.addWidget(splitter, 1)
        self.setCentralWidget(central)

        self.busy = QProgressBar()
        self.busy.setRange(0, 0)           # indeterminate
        self.busy.setMaximumWidth(140)
        self.busy.setTextVisible(False)
        self.busy.hide()
        self.statusBar().addPermanentWidget(self.busy)

    def _group_image(self) -> QGroupBox:
        box = QGroupBox("1 · Image")
        v = QVBoxLayout(box)
        self.open_btn = QPushButton("Open image…")
        self.open_btn.setShortcut("Ctrl+O")
        self.open_btn.setToolTip("Choose a PNG, JPEG, WebP, BMP, GIF or TIFF file (Ctrl+O).")
        self.open_btn.clicked.connect(self._choose_file)
        self.file_label = QLabel("No file selected")
        self.file_label.setWordWrap(True)
        v.addWidget(self.open_btn)
        v.addWidget(self.file_label)
        return box

    def _group_chars(self) -> QGroupBox:
        box = QGroupBox("2 · Characters")
        v = QVBoxLayout(box)
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(engine.PRESETS) + [self.CUSTOM])
        self.preset_combo.setToolTip("Ready-made symbol sets, or choose “Custom…” and type your own.")
        self.charset_edit = QLineEdit(engine.PRESETS[engine.DEFAULT_PRESET])
        self.charset_edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.charset_edit.setPlaceholderText("Symbols from darkest to lightest, e.g.  @#*+-:. ")
        self.charset_edit.setToolTip("Type any symbols, ordered from darkest to lightest.\n"
                                     "Any length works, even a single symbol. Spaces count.")
        self.charset_info = hint("")
        self.charset_error = QLabel()
        self.charset_error.setWordWrap(True)
        self.charset_error.setStyleSheet(RED_TEXT)
        self.charset_error.hide()
        v.addWidget(self.preset_combo)
        v.addWidget(self.charset_edit)
        v.addWidget(self.charset_info)
        v.addWidget(self.charset_error)
        v.addWidget(hint("Order: darkest → lightest. A trailing space makes the lightest tone empty."))

        self.preset_combo.currentTextChanged.connect(self._on_preset)
        self.charset_edit.textChanged.connect(self._sync_preset_combo)
        self.charset_edit.textChanged.connect(self._schedule)
        return box

    def _group_size(self) -> QGroupBox:
        box = QGroupBox("3 · Size")
        v = QVBoxLayout(box)
        self.size_group = QButtonGroup(self)
        self._size_modes = [SizeMode.ORIGINAL, SizeMode.UPSCALE, SizeMode.DOWNSCALE, SizeMode.NEW]
        labels = [
            ("Keep original proportions", "Grid width = “Columns”; the height follows the picture's shape."),
            ("Upscale (more characters)", "Columns × scale factor, with a factor above 1."),
            ("Downscale (fewer characters)", "Columns × scale factor, with a factor below 1."),
            ("Generate new ASCII grid", "Recomputes the grid from the source pixels (one character per N "
                                        "pixels) and equalises contrast — not a scaled copy of an earlier result."),
        ]
        for i, (text, tip) in enumerate(labels):
            radio = QRadioButton(text)
            radio.setToolTip(tip)
            radio.setChecked(i == 0)
            self.size_group.addButton(radio, i)
            v.addWidget(radio)

        form = QFormLayout()
        self.columns_spin = QSpinBox(minimum=engine.MIN_COLS, maximum=engine.MAX_COLS, value=100)
        self.columns_spin.setToolTip("Number of characters per row.")
        self.scale_spin = QDoubleSpinBox(decimals=2, singleStep=0.1)
        self.scale_spin.setRange(0.1, 8.0)
        self.scale_spin.setValue(1.0)
        self.scale_spin.setToolTip("Multiplier applied to “Columns” in upscale / downscale mode.")
        self.detail_spin = QSpinBox(minimum=1, maximum=200, value=8)
        self.detail_spin.setSuffix(" px")
        self.detail_spin.setToolTip("Generate-new mode: how many source pixels one character covers.\n"
                                    "Smaller = more detail.")
        self.auto_aspect = QCheckBox("Auto (measured from the font)")
        self.auto_aspect.setChecked(True)
        self.auto_aspect.setToolTip("Characters are taller than wide; this corrects for it so the\n"
                                    "result isn't stretched. Untick to set the ratio yourself.")
        self.aspect_spin = QDoubleSpinBox(decimals=2, singleStep=0.05)
        self.aspect_spin.setRange(0.2, 1.5)
        self.aspect_spin.setValue(0.5)
        form.addRow("Columns", self.columns_spin)
        form.addRow("Scale factor", self.scale_spin)
        form.addRow("Source px / char", self.detail_spin)
        form.addRow("Character shape", self.auto_aspect)
        form.addRow("Width ÷ height", self.aspect_spin)
        v.addLayout(form)
        self.grid_label = hint("")
        v.addWidget(self.grid_label)

        self.size_group.idClicked.connect(self._on_size_mode)
        for w in (self.columns_spin, self.scale_spin, self.detail_spin, self.aspect_spin):
            w.valueChanged.connect(self._schedule)
        self.auto_aspect.toggled.connect(self._sync_enabled)
        self.auto_aspect.toggled.connect(self._schedule)
        return box

    def _group_edges(self) -> QGroupBox:
        box = QGroupBox("4 · Edges")
        v = QVBoxLayout(box)
        self.edge_group = QButtonGroup(self)
        self._edge_modes = [EdgeMode.NONE, EdgeMode.SHARP, EdgeMode.GRADIENT]
        labels = [
            ("None (plain boundary)", "No special treatment at the borders (default)."),
            ("Sharp edges", "A solid frame of the densest symbol: a hard cut-off at the border."),
            ("Smooth gradient fade", "Symbols thin out and fade gradually toward the borders."),
        ]
        for i, (text, tip) in enumerate(labels):
            radio = QRadioButton(text)
            radio.setToolTip(tip)
            radio.setChecked(i == 0)
            self.edge_group.addButton(radio, i)
            v.addWidget(radio)

        form = QFormLayout()
        self.sharp_spin = QSpinBox(minimum=1, maximum=20, value=1)
        self.sharp_spin.setSuffix(" row(s)")
        self.sharp_spin.setToolTip("Thickness of the solid border.")
        self.fade_slider = QSlider(Qt.Orientation.Horizontal)
        self.fade_slider.setRange(1, 50)
        self.fade_slider.setValue(15)
        self.fade_slider.setToolTip("Width of the fade zone as a percentage of the image's shorter side.")
        self.fade_label = QLabel("15 %")
        fade_row = QHBoxLayout()
        fade_row.addWidget(self.fade_slider, 1)
        fade_row.addWidget(self.fade_label)
        form.addRow("Border thickness", self.sharp_spin)
        form.addRow("Fade width", fade_row)
        v.addLayout(form)

        self.edge_group.idClicked.connect(lambda _i: (self._sync_enabled(), self._schedule()))
        self.sharp_spin.valueChanged.connect(self._schedule)
        self.fade_slider.valueChanged.connect(lambda val: self.fade_label.setText(f"{val} %"))
        self.fade_slider.valueChanged.connect(self._schedule)
        return box

    def _group_tone(self) -> QGroupBox:
        box = QGroupBox("5 · Tone")
        v = QVBoxLayout(box)
        self.invert_check = QCheckBox("Invert brightness")
        self.invert_check.setToolTip("Swap dark and light. Useful for light-on-dark output.")
        self.contrast_check = QCheckBox("Auto-contrast")
        self.contrast_check.setChecked(True)
        self.contrast_check.setToolTip("Stretch the picture's tones to use the whole symbol range.")
        self.line_check = QCheckBox("Line-art mode (black / white threshold)")
        self.line_check.setToolTip("Only the darkest and the lightest symbol are used, so the background\n"
                                   "stays empty. Good for outline drawings.")
        self.line_spin = QSpinBox(minimum=0, maximum=255, value=128)
        self.line_spin.setPrefix("Threshold ")
        v.addWidget(self.invert_check)
        v.addWidget(self.contrast_check)
        v.addWidget(self.line_check)
        v.addWidget(self.line_spin)
        for w in (self.invert_check, self.contrast_check):
            w.toggled.connect(self._schedule)
        self.line_check.toggled.connect(self._sync_enabled)
        self.line_check.toggled.connect(self._schedule)
        self.line_spin.valueChanged.connect(self._schedule)
        return box

    def _group_output(self) -> QGroupBox:
        box = QGroupBox("6 · Output")
        v = QVBoxLayout(box)
        form = QFormLayout()
        self.format_combo = QComboBox()
        # Kept in a list (not as item data): Qt would turn the str-based Enum into a plain str.
        self._formats = [OutputFormat.PNG, OutputFormat.JPEG, OutputFormat.TXT]
        self.format_combo.addItems(["PNG image", "JPEG image", "TXT (plain text)"])
        self.format_combo.setToolTip("PNG/JPEG draw the characters on a canvas; TXT writes one row per line.")
        self.font_spin = QSpinBox(minimum=6, maximum=48, value=engine.DEFAULT_FONT_SIZE)
        self.font_spin.setSuffix(" px")
        self.font_spin.setToolTip("Character size in PNG/JPEG output (larger = bigger picture).")
        self.canvas_combo = QComboBox()
        self.canvas_combo.addItems(["Black on white", "White on black"])
        self.canvas_combo.setToolTip("Colours of the preview and of PNG/JPEG output.\n"
                                     "On a dark canvas, tick “Invert brightness” for a natural look.")
        self.quality_spin = QSpinBox(minimum=50, maximum=100, value=92)
        form.addRow("Format", self.format_combo)
        form.addRow("Font size", self.font_spin)
        form.addRow("Canvas", self.canvas_combo)
        form.addRow("JPEG quality", self.quality_spin)
        v.addLayout(form)
        self.save_btn = QPushButton("Save as…")
        self.save_btn.setShortcut("Ctrl+S")
        self.save_btn.setToolTip("Choose where to save the result (Ctrl+S).")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        v.addWidget(self.save_btn)

        self.format_combo.currentIndexChanged.connect(self._sync_enabled)
        self.format_combo.currentIndexChanged.connect(self._schedule)
        self.font_spin.valueChanged.connect(self._schedule)
        self.canvas_combo.currentIndexChanged.connect(self._schedule)
        return box

    # ------------------------------------------------------- option helpers
    def _size_mode(self) -> SizeMode:
        return self._size_modes[self.size_group.checkedId()]

    def _edge_mode(self) -> EdgeMode:
        return self._edge_modes[self.edge_group.checkedId()]

    def _format(self) -> OutputFormat:
        return self._formats[self.format_combo.currentIndex()]

    def _dark_canvas(self) -> bool:
        return self.canvas_combo.currentIndex() == 1

    def _effective_font_size(self) -> int:
        # In TXT mode the font size is irrelevant to the file, so use the default
        # (it only influences the automatic character aspect and the preview).
        return engine.DEFAULT_FONT_SIZE if self._format() is OutputFormat.TXT else self.font_spin.value()

    def _sync_enabled(self, *_args) -> None:
        mode, edge, fmt = self._size_mode(), self._edge_mode(), self._format()
        self.columns_spin.setEnabled(mode is not SizeMode.NEW)
        self.scale_spin.setEnabled(mode in (SizeMode.UPSCALE, SizeMode.DOWNSCALE))
        self.detail_spin.setEnabled(mode is SizeMode.NEW)
        self.aspect_spin.setEnabled(not self.auto_aspect.isChecked())
        self.sharp_spin.setEnabled(edge is EdgeMode.SHARP)
        self.fade_slider.setEnabled(edge is EdgeMode.GRADIENT)
        self.fade_label.setEnabled(edge is EdgeMode.GRADIENT)
        self.line_spin.setEnabled(self.line_check.isChecked())
        self.contrast_check.setEnabled(not self.line_check.isChecked())
        self.font_spin.setEnabled(fmt is not OutputFormat.TXT)
        self.quality_spin.setEnabled(fmt is OutputFormat.JPEG)

    def _on_preset(self, name: str) -> None:
        if name in engine.PRESETS:
            self.charset_edit.setText(engine.PRESETS[name])   # setText → textChanged → re-convert
        else:
            self.charset_edit.setFocus()

    def _sync_preset_combo(self, text: str) -> None:
        """Keep the preset box honest: it names a preset only if the field matches it exactly."""
        name = next((n for n, chars in engine.PRESETS.items() if chars == text), self.CUSTOM)
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentText(name)
        self.preset_combo.blockSignals(False)

    def _on_size_mode(self, mode_id: int) -> None:
        mode = self._size_modes[mode_id]
        self.scale_spin.blockSignals(True)
        if mode is SizeMode.UPSCALE:
            self.scale_spin.setRange(1.1, 8.0)
            self.scale_spin.setValue(2.0)
        elif mode is SizeMode.DOWNSCALE:
            self.scale_spin.setRange(0.1, 0.9)
            self.scale_spin.setValue(0.5)
        else:
            self.scale_spin.setRange(0.1, 8.0)
            self.scale_spin.setValue(1.0)
        self.scale_spin.blockSignals(False)
        self._sync_enabled()
        self._schedule()

    # ------------------------------------------------------------- loading
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.open_path(url.toLocalFile())
                break

    def _choose_file(self) -> None:
        start = os.path.dirname(self._source_path) if self._source_path else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "Open image", start, OPEN_FILTER)
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> None:
        self.statusBar().showMessage(f"Loading {os.path.basename(path)}…")
        self._start_job("load", lambda: (path, engine.load_image(path)))

    # ---------------------------------------------------------- conversion
    def _schedule(self, *_args) -> None:
        """Called on any option change: restart the debounce timer."""
        if self._source is not None:
            self.save_btn.setEnabled(False)     # the visible result is about to be outdated
            self._debounce.start()

    def _collect_settings(self) -> engine.ConversionSettings | None:
        try:
            charset = engine.parse_charset(self.charset_edit.text())
        except engine.CharsetError as exc:
            self.charset_error.setText(str(exc))
            self.charset_error.show()
            self.charset_edit.setStyleSheet(RED_BORDER)
            self.charset_info.setText("")
            return None
        self.charset_error.hide()
        self.charset_edit.setStyleSheet("")
        self.charset_info.setText(f"{len(charset)} symbol(s)")
        return engine.ConversionSettings(
            charset=charset,
            size_mode=self._size_mode(),
            columns=self.columns_spin.value(),
            scale=self.scale_spin.value(),
            detail_px=self.detail_spin.value(),
            cell_aspect=None if self.auto_aspect.isChecked() else self.aspect_spin.value(),
            edge_mode=self._edge_mode(),
            sharp_width=self.sharp_spin.value(),
            fade_percent=float(self.fade_slider.value()),
            invert=self.invert_check.isChecked(),
            auto_contrast=self.contrast_check.isChecked(),
            line_art=self.line_check.isChecked(),
            line_threshold=self.line_spin.value(),
        )

    def _run_conversion(self) -> None:
        if self._source is None:
            return
        settings = self._collect_settings()
        if settings is None:
            self.statusBar().showMessage("Fix the symbol set to continue.")
            return
        image, font_size, dark = self._source, self._effective_font_size(), self._dark_canvas()

        def work():
            result = engine.convert(image, settings, font_size)
            preview = engine.render_image(result, PREVIEW_FONT_SIZE, dark)
            return result, preview

        self._convert_job = self._start_job("convert", work)
        self.statusBar().showMessage("Converting…")

    # ---------------------------------------------------------------- jobs
    def _start_job(self, kind: str, fn: Callable[[], object]) -> int:
        self._job_seq += 1
        job = Job(self._job_seq, kind, fn)
        job.signals.finished.connect(self._on_job_finished)
        job.signals.failed.connect(self._on_job_failed)
        self._jobs[job.job_id] = job
        self._update_busy()
        self._pool.start(job)
        return job.job_id

    def _update_busy(self) -> None:
        self.busy.setVisible(bool(self._jobs))

    def _on_job_finished(self, job_id: int, kind: str, payload: object) -> None:
        self._jobs.pop(job_id, None)
        self._update_busy()
        if kind == "load":
            path, image = payload
            self._source, self._source_path, self._result = image, path, None
            self.source_view.set_full_pixmap(pil_to_pixmap(image))
            self.file_label.setText(f"{os.path.basename(path)}\n{image.width} × {image.height} px")
            self.error_banner.hide()
            self._run_conversion()
        elif kind == "convert":
            if job_id != self._convert_job:
                return                           # a newer conversion is already on its way
            result, preview = payload
            self._result, self._preview_img = result, preview
            self.result_view.set_full_pixmap(pil_to_pixmap(preview))
            self._text_dirty = True
            self._refresh_text_tab()
            self.error_banner.hide()
            self.grid_label.setText(f"Grid: {result.cols} × {result.rows} characters · "
                                    f"cell width/height {result.cell_aspect:.2f}")
            self.save_btn.setEnabled(True)
            self.statusBar().showMessage("Ready.")
        elif kind == "export":
            self.save_btn.setEnabled(self._result is not None)
            self.statusBar().showMessage(f"Saved: {payload}")

    def _on_job_failed(self, job_id: int, kind: str, message: str) -> None:
        self._jobs.pop(job_id, None)
        self._update_busy()
        if kind == "load":
            self.statusBar().showMessage("Could not open the image.")
            QMessageBox.critical(self, "Can't open image", message)
        elif kind == "convert":
            if job_id != self._convert_job:
                return
            self.error_banner.setText(message)
            self.error_banner.show()
            self.statusBar().showMessage("Conversion failed.")
        elif kind == "export":
            self.save_btn.setEnabled(self._result is not None)
            self.statusBar().showMessage("Saving failed.")
            QMessageBox.critical(self, "Can't save file", message)

    def _refresh_text_tab(self) -> None:
        """The (potentially large) text is only pushed into the editor while its tab is visible."""
        if self._text_dirty and self.tabs.currentIndex() == 1 and self._result is not None:
            self.text_view.setPlainText(self._result.to_text(strip=False))
            self._text_dirty = False

    # --------------------------------------------------------------- saving
    def _save(self) -> None:
        if self._result is None:
            return
        fmt = self._format()
        stem = os.path.splitext(os.path.basename(self._source_path or "image"))[0]
        folder = os.path.dirname(self._source_path) if self._source_path else os.path.expanduser("~")
        suggested = os.path.join(folder, f"{stem}_ascii{fmt.default_extension}")
        path, _ = QFileDialog.getSaveFileName(self, "Save ASCII art", suggested, fmt.file_filter)
        if not path:
            return
        if not path.lower().endswith(fmt.extensions):
            path += fmt.default_extension

        result, font_size, dark, quality = (self._result, self.font_spin.value(),
                                            self._dark_canvas(), self.quality_spin.value())

        def work():
            engine.export(result, path, fmt, font_size, dark, quality)
            return path

        self.save_btn.setEnabled(False)
        self.statusBar().showMessage("Saving…")
        self._start_job("export", work)


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("ascii-art-studio")   # lets Wayland/GNOME/KDE match the launcher icon
    window = MainWindow()
    window.show()
    args = app.arguments()
    if len(args) > 1:
        window.open_path(args[1])
    return app.exec()
