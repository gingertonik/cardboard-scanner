"""Main window — port of the C# MainWindow.xaml + MainViewModel.

Threading model: the camera thread and worker threads never touch widgets. They emit Qt
signals, which Qt delivers on the GUI thread. Opening a camera and all Scryfall calls run
off the GUI thread so the window never freezes (a busy device can block for seconds).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PySide6.QtCore import Qt, QObject, QTimer, Signal, Slot
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QSlider, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import camera as camera_mod
from ..camera import CameraDevice, CameraService
from ..database import Database
from ..detector import CardDetector
from ..exporter import ALL_FORMATS, export, file_info_for
from ..hashing import HASH_ALGO
from ..index_builder import IndexBuilder, IndexProgress
from ..matcher import CardMatcher
from ..models import CONDITIONS, LANGUAGES, MatchResult, ScannedCard
from ..ocr import OcrService
from ..scryfall import ScryfallClient
from .theme import MUTED

PROCESS_INTERVAL = 0.2  # seconds between analysed frames


class Bridge(QObject):
    """Signals used to hand data from background threads to the GUI thread.

    Everything crossing a thread boundary must go through a signal. QTimer.singleShot is
    *not* an alternative: it needs an event loop on the calling thread, so from a worker
    thread it silently never fires.
    """

    frame = Signal(object)
    match = Signal(object)
    status = Signal(str)
    index_message = Signal(str)
    printings = Signal(object, str)
    search_results = Signal(object)
    connect_result = Signal(bool, str, str)
    index_finished = Signal()
    devices = Signal(object)
    card_image = Signal(object)
    camera_error = Signal(str)


def _panel(object_name: str = "panel") -> QFrame:
    frame = QFrame()
    frame.setObjectName(object_name)
    return frame


def _muted(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("muted")
    return label


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Cardboard Scanner")
        self.resize(1280, 860)

        # --- services ---
        self.db = Database()
        self.scryfall = ScryfallClient()
        self.detector = CardDetector()
        self.ocr = OcrService()
        self.matcher = CardMatcher(self.db, self.scryfall)
        self.index_builder = IndexBuilder(self.db, self.scryfall)
        self.camera = CameraService()
        self.pool = ThreadPoolExecutor(max_workers=4)

        self.bridge = Bridge()
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_lock = threading.Lock()
        self._processing = False
        self._last_process = 0.0
        self._stop_worker = threading.Event()
        self._zoom = 1.0
        self._connecting = False
        self._building = False
        self._cancel_build = threading.Event()

        self._current_card: Optional[ScannedCard] = None
        self._printings: list[ScannedCard] = []
        self._printings_for: Optional[str] = None
        self._suppress_printing = False
        self._stable_id: Optional[str] = None
        self._stable_count = 0
        self._last_added: tuple[Optional[str], float] = (None, 0.0)
        self._search_results: list[ScannedCard] = []
        self._collection: list[ScannedCard] = []
        self._suppress_row_edit = False

        self._build_ui()
        self._wire_signals()

        self.matcher.reload_index()
        self._reload_collection()
        self._update_index_status()
        self.pool.submit(self._refresh_devices)

        self._worker = threading.Thread(target=self._process_loop, name="processor", daemon=True)
        self._worker.start()

        # Auto index update, deferred so the window paints first.
        QTimer.singleShot(600, self._maybe_auto_update_index)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        outer.addWidget(self._build_toolbar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_video_pane())
        splitter.addWidget(self._build_side_pane())
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)

        self.setCentralWidget(central)

        self.status_label = QLabel("Ready. Select a device and press Start.")
        self.index_label = _muted("")
        self.build_label = _muted("")
        self.build_label.setStyleSheet("color: #7C9CD0;")
        bar = QWidget()
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(6, 2, 6, 2)
        bar_layout.setSpacing(1)
        for widget in (self.status_label, self.index_label, self.build_label):
            bar_layout.addWidget(widget)
        self.statusBar().addPermanentWidget(bar, 1)

    def _build_toolbar(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        layout.addWidget(QLabel("Device"))
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(220)
        layout.addWidget(self.device_combo)

        self.refresh_button = QPushButton("Refresh")
        self.start_button = QPushButton("▶ Start")
        self.stop_button = QPushButton("■ Stop")
        self.stop_button.setEnabled(False)
        for button in (self.refresh_button, self.start_button, self.stop_button):
            layout.addWidget(button)

        self.auto_add_check = QCheckBox("Auto-add matches")
        self.auto_add_check.setChecked(True)
        layout.addWidget(self.auto_add_check)

        layout.addSpacing(12)
        self.update_index_button = QPushButton("Update index")
        self.update_index_button.setToolTip(
            "Import the bundled index if needed, then fetch only cards printed since the last sync.")
        self.full_index_button = QPushButton("Full rebuild")
        self.full_index_button.setToolTip("Re-hash every printing from Scryfall bulk data (slow).")
        self.cancel_index_button = QPushButton("Cancel")
        self.cancel_index_button.setEnabled(False)
        for button in (self.update_index_button, self.full_index_button, self.cancel_index_button):
            layout.addWidget(button)

        self.auto_update_check = QCheckBox("Auto-update index")
        self.auto_update_check.setChecked(self.db.get_meta("auto_index") != "0")
        layout.addWidget(self.auto_update_check)

        layout.addStretch(1)
        return row

    def _build_video_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.video_label = QLabel("Live feed will appear here once you press Start.")
        self.video_label.setObjectName("video")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet(f"color: {MUTED};")
        self.video_label.setMinimumSize(480, 360)
        layout.addWidget(self.video_label, 1)

        controls = _panel("raised")
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(10, 8, 10, 8)
        controls_layout.setSpacing(6)

        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("🔍 Zoom"))
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(10, 40)  # 1.0x .. 4.0x in tenths
        self.zoom_slider.setValue(10)
        zoom_row.addWidget(self.zoom_slider, 1)
        self.zoom_value = QLabel("1.0×")
        self.zoom_value.setMinimumWidth(38)
        zoom_row.addWidget(self.zoom_value)
        controls_layout.addLayout(zoom_row)

        focus_row = QHBoxLayout()
        focus_row.addWidget(QLabel("🎯 Focus"))
        self.auto_focus_check = QCheckBox("Auto")
        self.auto_focus_check.setChecked(True)
        focus_row.addWidget(self.auto_focus_check)
        self.refocus_button = QPushButton("Refocus")
        focus_row.addWidget(self.refocus_button)
        self.focus_slider = QSlider(Qt.Orientation.Horizontal)
        self.focus_slider.setRange(0, 255)
        self.focus_slider.setValue(128)
        self.focus_slider.setEnabled(False)
        self.focus_slider.setToolTip("Manual focus (turn Auto off). Left = far, right = close.")
        focus_row.addWidget(self.focus_slider, 1)
        self.camera_settings_button = QPushButton("Camera settings…")
        self.camera_settings_button.setToolTip(
            "Open the webcam driver's own dialog (Windows only).")
        focus_row.addWidget(self.camera_settings_button)
        controls_layout.addLayout(focus_row)

        layout.addWidget(controls)
        return pane

    def _build_side_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._build_match_panel())
        layout.addWidget(self._build_search_panel())
        layout.addWidget(self._build_library_panel(), 1)
        return pane

    def _build_match_panel(self) -> QWidget:
        panel = _panel()
        outer = QHBoxLayout(panel)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(12)

        self.card_image = QLabel()
        self.card_image.setObjectName("cardImage")
        self.card_image.setFixedSize(130, 181)
        self.card_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.card_image, 0, Qt.AlignmentFlag.AlignTop)

        right = QVBoxLayout()
        right.setSpacing(4)
        right.addWidget(_muted("Current match"))
        self.match_name = QLabel("—")
        self.match_name.setObjectName("matchName")
        self.match_name.setWordWrap(True)
        right.addWidget(self.match_name)
        self.match_details = _muted("")
        self.match_details.setWordWrap(True)
        right.addWidget(self.match_details)

        printing_row = QHBoxLayout()
        printing_row.addWidget(_muted("Printing"))
        self.printing_combo = QComboBox()
        self.printing_combo.setMinimumWidth(200)
        printing_row.addWidget(self.printing_combo, 1)
        right.addLayout(printing_row)

        copy_row = QHBoxLayout()
        self.foil_check = QCheckBox("Foil")
        copy_row.addWidget(self.foil_check)
        copy_row.addWidget(_muted("Cond"))
        self.condition_combo = QComboBox()
        self.condition_combo.addItems(list(CONDITIONS))
        copy_row.addWidget(self.condition_combo)
        copy_row.addWidget(_muted("Lang"))
        self.language_combo = QComboBox()
        for code, name in LANGUAGES:
            self.language_combo.addItem(name, code)
        copy_row.addWidget(self.language_combo, 1)
        right.addLayout(copy_row)

        conf_row = QHBoxLayout()
        conf_row.addWidget(_muted("Confidence:"))
        self.confidence_label = QLabel("0%")
        conf_row.addWidget(self.confidence_label)
        conf_row.addStretch(1)
        right.addLayout(conf_row)
        self.confidence_bar = QProgressBar()
        self.confidence_bar.setRange(0, 100)
        self.confidence_bar.setValue(0)
        right.addWidget(self.confidence_bar)

        self.method_label = _muted("")
        self.method_label.setWordWrap(True)
        right.addWidget(self.method_label)

        button_row = QHBoxLayout()
        self.add_button = QPushButton("＋ Add to library")
        self.add_button.setEnabled(False)
        self.scryfall_button = QPushButton("Scryfall ↗")
        self.scryfall_button.setEnabled(False)
        button_row.addWidget(self.add_button)
        button_row.addWidget(self.scryfall_button)
        button_row.addStretch(1)
        right.addLayout(button_row)

        outer.addLayout(right, 1)
        return panel

    def _build_search_panel(self) -> QWidget:
        panel = _panel()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        heading = QLabel("Manual search (Scryfall)")
        heading.setStyleSheet("font-weight: bold;")
        layout.addWidget(heading)

        row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Card name or Scryfall query…")
        row.addWidget(self.search_input, 1)
        self.search_button = QPushButton("Search")
        row.addWidget(self.search_button)
        layout.addLayout(row)

        self.search_list = QListWidget()
        self.search_list.setFixedHeight(96)
        layout.addWidget(self.search_list)
        return panel

    def _build_library_panel(self) -> QWidget:
        panel = _panel()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        heading = QLabel("My Library")
        heading.setObjectName("heading")
        header.addWidget(heading)
        header.addStretch(1)
        self.summary_label = _muted("")
        header.addWidget(self.summary_label)
        layout.addLayout(header)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter by name, set, or type…")
        layout.addWidget(self.filter_input)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["Qty", "Name", "Set", "#", "Foil", "Cond", "Lang", "$"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4, 5, 6, 7):
            header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)

        edit = _panel("raised")
        edit_layout = QHBoxLayout(edit)
        edit_layout.setContentsMargins(8, 6, 8, 6)
        edit_layout.addWidget(_muted("Selected:"))
        self.decrement_button = QPushButton("－")
        self.quantity_label = QLabel("0")
        self.quantity_label.setMinimumWidth(22)
        self.quantity_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.increment_button = QPushButton("＋")
        edit_layout.addWidget(self.decrement_button)
        edit_layout.addWidget(self.quantity_label)
        edit_layout.addWidget(self.increment_button)
        edit_layout.addSpacing(10)
        self.row_foil_check = QCheckBox("Foil")
        edit_layout.addWidget(self.row_foil_check)
        edit_layout.addWidget(_muted("Cond"))
        self.row_condition_combo = QComboBox()
        self.row_condition_combo.addItems(list(CONDITIONS))
        edit_layout.addWidget(self.row_condition_combo)
        edit_layout.addWidget(_muted("Lang"))
        self.row_language_combo = QComboBox()
        for code, name in LANGUAGES:
            self.row_language_combo.addItem(name, code)
        edit_layout.addWidget(self.row_language_combo)
        edit_layout.addSpacing(10)
        self.delete_button = QPushButton("🗑 Delete row")
        edit_layout.addWidget(self.delete_button)
        edit_layout.addStretch(1)
        self.edit_panel = edit
        edit.setEnabled(False)
        layout.addWidget(edit)

        export_row = _panel("raised")
        export_layout = QHBoxLayout(export_row)
        export_layout.setContentsMargins(8, 6, 8, 6)
        export_layout.addWidget(_muted("Export"))
        self.export_combo = QComboBox()
        for fmt, label in ALL_FORMATS:
            self.export_combo.addItem(label, fmt)
        export_layout.addWidget(self.export_combo, 1)
        self.export_file_button = QPushButton("Export to file…")
        self.export_copy_button = QPushButton("Copy")
        export_layout.addWidget(self.export_file_button)
        export_layout.addWidget(self.export_copy_button)
        layout.addWidget(export_row)

        return panel

    # ------------------------------------------------------------- signals

    def _wire_signals(self) -> None:
        self.bridge.frame.connect(self._on_frame)
        self.bridge.match.connect(self._on_match)
        self.bridge.status.connect(self.status_label.setText)
        self.bridge.index_message.connect(self.build_label.setText)
        self.bridge.printings.connect(self._on_printings)
        self.bridge.search_results.connect(self._on_search_results)
        self.bridge.connect_result.connect(self._on_connect_result)
        self.bridge.index_finished.connect(self._on_index_finished)
        self.bridge.devices.connect(self._populate_devices)
        self.bridge.card_image.connect(self._show_card_image)
        self.bridge.camera_error.connect(self._on_camera_error)

        # Both callbacks fire on the camera thread, so they only emit signals.
        self.camera.on_frame = lambda frame: self.bridge.frame.emit(frame)
        self.camera.on_error = lambda message: self.bridge.camera_error.emit(message)

        self.refresh_button.clicked.connect(lambda: self.pool.submit(self._refresh_devices))
        self.start_button.clicked.connect(self._start_camera)
        self.stop_button.clicked.connect(self._stop_camera)

        self.zoom_slider.valueChanged.connect(self._on_zoom_changed)
        self.auto_focus_check.toggled.connect(self._on_auto_focus_toggled)
        self.refocus_button.clicked.connect(self.camera.trigger_refocus)
        self.focus_slider.valueChanged.connect(lambda v: self.camera.set_focus(float(v)))
        self.camera_settings_button.clicked.connect(self.camera.open_native_settings)

        self.printing_combo.currentIndexChanged.connect(self._on_printing_selected)
        self.add_button.clicked.connect(self._add_current)
        self.scryfall_button.clicked.connect(self._open_scryfall)

        self.search_button.clicked.connect(self._run_search)
        self.search_input.returnPressed.connect(self._run_search)
        self.search_list.currentRowChanged.connect(self._on_search_selected)

        self.filter_input.textChanged.connect(self._apply_filter)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.increment_button.clicked.connect(lambda: self._adjust_selected(+1))
        self.decrement_button.clicked.connect(lambda: self._adjust_selected(-1))
        self.delete_button.clicked.connect(self._delete_selected)
        self.row_foil_check.toggled.connect(self._on_row_edited)
        self.row_condition_combo.currentIndexChanged.connect(self._on_row_edited)
        self.row_language_combo.currentIndexChanged.connect(self._on_row_edited)

        self.export_file_button.clicked.connect(self._export_to_file)
        self.export_copy_button.clicked.connect(self._copy_export)

        self.update_index_button.clicked.connect(lambda: self._start_index_job("update"))
        self.full_index_button.clicked.connect(lambda: self._start_index_job("full"))
        self.cancel_index_button.clicked.connect(self._cancel_build.set)
        self.auto_update_check.toggled.connect(
            lambda on: self.db.set_meta("auto_index", "1" if on else "0"))

    # -------------------------------------------------------------- camera

    def _refresh_devices(self) -> None:
        """Runs on a worker thread — enumeration can take a second per probed index."""
        self.bridge.status.emit("Detecting video devices…")
        self.bridge.devices.emit(camera_mod.enumerate_devices())

    @Slot(object)
    def _populate_devices(self, devices: list[CameraDevice]) -> None:
        keep = self.device_combo.currentData()
        self.device_combo.clear()
        for device in devices:
            self.device_combo.addItem(device.name, device.index)
        if keep is not None:
            match = self.device_combo.findData(keep)
            if match >= 0:
                self.device_combo.setCurrentIndex(match)
        self.status_label.setText(
            f"Found {len(devices)} device(s). Select one and press Start." if devices
            else "No video devices found. Connect a camera and press Refresh.")

    def _start_camera(self) -> None:
        index = self.device_combo.currentData()
        if index is None:
            QMessageBox.information(self, "No device selected", "Select a video device first.")
            return
        if self._connecting or self.camera.is_running:
            return

        name = self.device_combo.currentText()
        self._connecting = True
        self.start_button.setEnabled(False)
        self.status_label.setText(f"Connecting to “{name}”…")

        def connect() -> None:
            ok = self.camera.start(int(index))
            self.bridge.connect_result.emit(ok, name, self.camera.last_error or "")

        self.pool.submit(connect)

    @Slot(bool, str, str)
    def _on_connect_result(self, ok: bool, name: str, error: str) -> None:
        self._connecting = False
        self.start_button.setEnabled(not ok)
        self.stop_button.setEnabled(ok)
        if ok:
            self.status_label.setText(
                f"Scanning “{name}” ({self.camera.frame_width}x{self.camera.frame_height}). "
                f"Hold a card up to the camera.")
        else:
            self.status_label.setText(f"“{name}” is unavailable — it may be in use by another app.")
            QMessageBox.warning(self, "Camera unavailable", error or f"Could not start “{name}”.")

    def _stop_camera(self) -> None:
        self.camera.stop()
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_label.setText("Stopped.")

    @Slot(str)
    def _on_camera_error(self, message: str) -> None:
        """Delivered via signal, so this always runs on the GUI thread."""
        self._stop_camera()
        self.status_label.setText("Camera stopped — device error.")
        QMessageBox.warning(self, "Camera error", message)

    def _on_zoom_changed(self, value: int) -> None:
        self._zoom = value / 10.0
        self.zoom_value.setText(f"{self._zoom:.1f}×")

    def _on_auto_focus_toggled(self, on: bool) -> None:
        self.focus_slider.setEnabled(not on)
        self.camera.set_auto_focus(on)

    def _apply_zoom(self, frame: np.ndarray) -> np.ndarray:
        """Centred digital zoom, applied to preview and detection alike."""
        if self._zoom <= 1.01:
            return frame
        h, w = frame.shape[:2]
        cw, ch = max(32, int(w / self._zoom)), max(32, int(h / self._zoom))
        x, y = (w - cw) // 2, (h - ch) // 2
        return frame[y:y + ch, x:x + cw]

    @Slot(object)
    def _on_frame(self, frame: np.ndarray) -> None:
        view = self._apply_zoom(frame)
        with self._frame_lock:
            self._latest_frame = view

        rgb = cv2.cvtColor(view, cv2.COLOR_BGR2RGB)
        h, w, _ = rgb.shape
        image = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        self.video_label.setPixmap(QPixmap.fromImage(image).scaled(
            self.video_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    # ----------------------------------------------------------- processing

    def _process_loop(self) -> None:
        while not self._stop_worker.is_set():
            time.sleep(0.05)
            if time.monotonic() - self._last_process < PROCESS_INTERVAL:
                continue
            with self._frame_lock:
                frame = self._latest_frame
                self._latest_frame = None
            if frame is None:
                continue
            self._last_process = time.monotonic()
            try:
                detection = self.detector.detect(frame)
                if not detection.found or detection.warped is None:
                    self.bridge.status.emit("Searching for a card...")
                    continue
                name = self.ocr.read_title(detection.warped) if self.ocr.available else ""
                result = self.matcher.identify(detection.warped, name)
                self.bridge.match.emit(result)
            except Exception as e:
                self.bridge.status.emit(f"Processing error: {e}")

    @Slot(object)
    def _on_match(self, result: MatchResult) -> None:
        if not result.success or result.card is None:
            self._stable_id, self._stable_count = None, 0
            self.status_label.setText(result.notes or "No match.")
            return

        card = result.card
        method = result.method.value
        if result.ocr_text:
            method += f'  ·  read: "{result.ocr_text}"'
        self._set_current_card(card, method, result.confidence)

        if self._stable_id == card.scryfall_id:
            self._stable_count += 1
        else:
            self._stable_id, self._stable_count = card.scryfall_id, 1

        last_id, last_time = self._last_added
        cooled = last_id != card.scryfall_id or time.monotonic() - last_time > 3.0
        if (self.auto_add_check.isChecked() and result.confidence >= 0.80
                and self._stable_count >= 2 and cooled):
            self._add_card(card)
            self._last_added = (card.scryfall_id, time.monotonic())
        else:
            hint = ("Hold steady to auto-add…" if self.auto_add_check.isChecked()
                    else 'Press "Add to library".')
            self.status_label.setText(
                f"Match: {card.name} ({result.confidence * 100:.0f}%). {hint}")

    def _set_current_card(self, card: ScannedCard, method: str, confidence: float) -> None:
        self._current_card = card
        self.match_name.setText(card.name)
        self.match_details.setText(_format_details(card))
        self.method_label.setText(method)
        self.confidence_label.setText(f"{confidence * 100:.0f}%")
        self.confidence_bar.setValue(int(confidence * 100))
        self.add_button.setEnabled(True)
        self.scryfall_button.setEnabled(bool(card.scryfall_uri))
        self._load_card_image(card.image_uri)
        self._ensure_printings(card)

    def _load_card_image(self, url: Optional[str]) -> None:
        if not url:
            self.card_image.clear()
            return

        def fetch() -> None:
            data = self.scryfall.download_image(url)
            if data:
                self.bridge.card_image.emit(data)

        self.pool.submit(fetch)

    @Slot(object)
    def _show_card_image(self, data: bytes) -> None:
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            self.card_image.setPixmap(pixmap.scaled(
                self.card_image.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def _ensure_printings(self, card: ScannedCard) -> None:
        if card.name == self._printings_for:
            self._select_printing(card.scryfall_id)
            return
        self._printings_for = card.name
        name, prefer = card.name, card.scryfall_id
        self.pool.submit(
            lambda: self.bridge.printings.emit(self.scryfall.get_printings(name), prefer))

    @Slot(object, str)
    def _on_printings(self, printings: list[ScannedCard], prefer_id: str) -> None:
        self._printings = printings
        self._suppress_printing = True
        self.printing_combo.clear()
        for printing in printings:
            self.printing_combo.addItem(printing.printing_label, printing.scryfall_id)
        self._suppress_printing = False
        self._select_printing(prefer_id)

    def _select_printing(self, scryfall_id: str) -> None:
        index = self.printing_combo.findData(scryfall_id)
        if index >= 0:
            self._suppress_printing = True
            self.printing_combo.setCurrentIndex(index)
            self._suppress_printing = False

    def _on_printing_selected(self, index: int) -> None:
        if self._suppress_printing or index < 0 or index >= len(self._printings):
            return
        printing = self._printings[index]
        self._current_card = printing
        self.match_details.setText(_format_details(printing))
        self.method_label.setText("Printing selected")
        self.add_button.setEnabled(True)
        self.scryfall_button.setEnabled(bool(printing.scryfall_uri))
        self._load_card_image(printing.image_uri)

    # ------------------------------------------------------------- library

    def _add_current(self) -> None:
        if self._current_card is not None:
            self._add_card(self._current_card)

    def _add_card(self, card: ScannedCard) -> None:
        copy = ScannedCard(
            scryfall_id=card.scryfall_id, name=card.name, set_code=card.set_code,
            set_name=card.set_name, collector_number=card.collector_number,
            rarity=card.rarity, mana_cost=card.mana_cost, type_line=card.type_line,
            price_usd=card.price_usd, price_usd_foil=card.price_usd_foil,
            image_uri=card.image_uri, scryfall_uri=card.scryfall_uri,
            foil=self.foil_check.isChecked(),
            condition=self.condition_combo.currentText(),
            language=self.language_combo.currentData() or "en",
            scanned_at=datetime.now(),
        )
        quantity = self.db.add_or_increment(copy)
        self._reload_collection()
        finish = " foil" if copy.foil else ""
        total = self.db.collection_total_cards()
        self.status_label.setText(
            f"Added another {card.name}{finish} (now {quantity}). Library: {total} cards."
            if quantity > 1 else
            f"Added {card.name}{finish} to library. Library: {total} cards.")

    def _reload_collection(self) -> None:
        self._collection = self.db.get_collection()
        self._populate_table()
        value = sum((c.effective_price or 0.0) * c.quantity for c in self._collection)
        self.summary_label.setText(
            f"{len(self._collection)} unique · {self.db.collection_total_cards()} total · ${value:,.2f}")

    def _populate_table(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for card in self._collection:
            row = self.table.rowCount()
            self.table.insertRow(row)
            price = f"{card.effective_price:.2f}" if card.effective_price is not None else ""
            values = [str(card.quantity), card.name, (card.set_code or "").lower(),
                      card.collector_number or "", "✓" if card.foil else "",
                      card.condition, card.language, price]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, card.id)
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self._apply_filter(self.filter_input.text())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        by_id = {c.id: c for c in self._collection}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            card = by_id.get(item.data(Qt.ItemDataRole.UserRole)) if item else None
            if not needle or card is None:
                self.table.setRowHidden(row, False)
                continue
            haystack = " ".join(filter(None, [
                card.name, card.set_code, card.set_name, card.type_line])).lower()
            self.table.setRowHidden(row, needle not in haystack)

    def _selected_card(self) -> Optional[ScannedCard]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return None
        card_id = item.data(Qt.ItemDataRole.UserRole)
        return next((c for c in self._collection if c.id == card_id), None)

    def _on_row_selected(self) -> None:
        card = self._selected_card()
        self.edit_panel.setEnabled(card is not None)
        if card is None:
            self.quantity_label.setText("0")
            return
        self._suppress_row_edit = True
        self.quantity_label.setText(str(card.quantity))
        self.row_foil_check.setChecked(card.foil)
        self.row_condition_combo.setCurrentText(card.condition)
        language_index = self.row_language_combo.findData(card.language)
        if language_index >= 0:
            self.row_language_combo.setCurrentIndex(language_index)
        self._suppress_row_edit = False

    def _on_row_edited(self) -> None:
        if self._suppress_row_edit:
            return
        card = self._selected_card()
        if card is None:
            return
        card.foil = self.row_foil_check.isChecked()
        card.condition = self.row_condition_combo.currentText()
        card.language = self.row_language_combo.currentData() or "en"
        self.db.update_attributes(card.id, card.foil, card.condition, card.language)
        self._reload_collection()

    def _adjust_selected(self, delta: int) -> None:
        card = self._selected_card()
        if card is None:
            return
        self.db.set_quantity(card.id, card.quantity + delta)
        self._reload_collection()

    def _delete_selected(self) -> None:
        card = self._selected_card()
        if card is None:
            return
        self.db.delete_row(card.id)
        self._reload_collection()

    # -------------------------------------------------------------- search

    def _run_search(self) -> None:
        query = self.search_input.text().strip()
        if not query:
            return
        self.status_label.setText(f'Searching Scryfall for "{query}"…')
        self.pool.submit(lambda: self.bridge.search_results.emit(self.scryfall.search(query)))

    @Slot(object)
    def _on_search_results(self, results: list[ScannedCard]) -> None:
        self._search_results = results[:50]
        self.search_list.clear()
        for card in self._search_results:
            self.search_list.addItem(QListWidgetItem(card.printing_label))
        self.status_label.setText(
            f"{len(self._search_results)} result(s). Select one to load it, then Add."
            if self._search_results else "No cards found.")

    def _on_search_selected(self, row: int) -> None:
        if 0 <= row < len(self._search_results):
            self._set_current_card(self._search_results[row], "Manual (Scryfall search)", 1.0)

    def _open_scryfall(self) -> None:
        uri = self._current_card.scryfall_uri if self._current_card else None
        if uri:
            import webbrowser
            webbrowser.open(uri)

    # -------------------------------------------------------------- export

    def _visible_cards(self) -> list[ScannedCard]:
        """Respect the current filter and on-screen ordering."""
        by_id = {c.id: c for c in self._collection}
        cards: list[ScannedCard] = []
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            item = self.table.item(row, 0)
            card = by_id.get(item.data(Qt.ItemDataRole.UserRole)) if item else None
            if card is not None:
                cards.append(card)
        return cards

    def _copy_export(self) -> None:
        cards = self._visible_cards()
        if not cards:
            self.status_label.setText("Nothing to export (library is empty or filtered out).")
            return
        fmt = self.export_combo.currentData()
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(export(cards, fmt))
        self.status_label.setText(
            f"Copied {len(cards)} rows to clipboard ({self.export_combo.currentText()}).")

    def _export_to_file(self) -> None:
        cards = self._visible_cards()
        if not cards:
            self.status_label.setText("Nothing to export (library is empty or filtered out).")
            return
        fmt = self.export_combo.currentData()
        extension, suggested = file_info_for(fmt)
        filter_text = ("CSV file (*.csv);;All files (*.*)" if extension == ".csv"
                       else "Text file (*.txt);;All files (*.*)")
        path, _ = QFileDialog.getSaveFileName(self, "Export library", suggested, filter_text)
        if not path:
            return
        try:
            Path(path).write_text(export(cards, fmt), encoding="utf-8")
            self.status_label.setText(f"Exported {len(cards)} rows to {path}.")
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))

    # --------------------------------------------------------------- index

    def _update_index_status(self) -> None:
        count = self.db.index_count()
        algo = self.db.get_meta("index_hash_algo")
        synced = self.db.get_meta("index_synced_through")
        if count == 0:
            self.index_label.setText(
                "Image index: empty (OCR-only matching). Press Update index.")
        elif algo != HASH_ALGO:
            self.index_label.setText(
                f"Image index: {count:,} cards built by '{algo or 'the Windows app'}' — "
                f"not usable here. Press Update index to rebuild.")
        else:
            suffix = f" · synced through {synced}" if synced else ""
            self.index_label.setText(f"Image index: {count:,} cards{suffix}.")

    def _maybe_auto_update_index(self) -> None:
        if not self.auto_update_check.isChecked() or self._building:
            return
        stale = self.db.get_meta("index_hash_algo") != HASH_ALGO or self.db.index_count() == 0
        synced = self.db.get_meta("index_synced_through")
        if not stale and synced:
            try:
                days = (datetime.now().date() - datetime.strptime(synced, "%Y-%m-%d").date()).days
                if days < 7:
                    return
            except ValueError:
                pass
        self._start_index_job("update")

    def _start_index_job(self, kind: str) -> None:
        if self._building:
            return
        self._building = True
        self._cancel_build.clear()
        self.update_index_button.setEnabled(False)
        self.full_index_button.setEnabled(False)
        self.cancel_index_button.setEnabled(True)

        def report(progress: IndexProgress) -> None:
            message = progress.message or (
                f"processed {progress.processed:,} · added {progress.added:,} "
                f"· skipped {progress.skipped:,}")
            self.bridge.index_message.emit(message)

        def run() -> None:
            try:
                if kind == "full":
                    self.index_builder.build("default_cards", report, self._cancel_build.is_set)
                else:
                    self.index_builder.ensure_index(report, self._cancel_build.is_set)
            except Exception as e:
                self.bridge.index_message.emit(f"Index update failed: {e}")
            finally:
                self.bridge.index_finished.emit()

        self.pool.submit(run)

    @Slot()
    def _on_index_finished(self) -> None:
        self._building = False
        self.update_index_button.setEnabled(True)
        self.full_index_button.setEnabled(True)
        self.cancel_index_button.setEnabled(False)
        self.matcher.reload_index()
        self._update_index_status()

    # -------------------------------------------------------------- teardown

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._cancel_build.set()
        self._stop_worker.set()
        self.camera.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.scryfall.close()
        super().closeEvent(event)


def _format_details(card: ScannedCard) -> str:
    parts: list[str] = []
    if card.type_line:
        parts.append(card.type_line)
    if card.set_name:
        parts.append(f"{card.set_name} ({(card.set_code or '').upper()}) #{card.collector_number}")
    elif card.set_code:
        parts.append(f"{card.set_code.upper()} #{card.collector_number}")
    if card.rarity:
        parts.append(card.rarity.capitalize())
    if card.price_usd is not None:
        parts.append(f"${card.price_usd:.2f}")
    if card.price_usd_foil is not None:
        parts.append(f"foil ${card.price_usd_foil:.2f}")
    return "   ·   ".join(parts)
