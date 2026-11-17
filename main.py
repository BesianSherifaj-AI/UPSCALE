"""Local, native Windows GUI for the bundled NVIDIA DLSS video engine."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
import traceback

from PySide6.QtCore import Qt, QObject, QLockFile, QRunnable, QThread, QThreadPool, Signal, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

import backend
import queue_state

ROOT = Path(__file__).resolve().parent
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.wmv', '.ts', '.mts', '.m2ts', '.mpg', '.mpeg', '.flv', '.mxf'}
STYLE = """
QWidget { background: #111615; color: #eaf1ed; font-family: 'Segoe UI'; font-size: 13px; }
QMainWindow { background: #111615; }
QLabel { background: transparent; }
QLabel#brand { font-size: 24px; font-weight: 750; letter-spacing: 2px; }
QLabel#eyebrow { font-size: 10px; font-weight: 700; color: #9db0a6; letter-spacing: 2px; }
QLabel#muted { color: #96a89e; }
QLabel#sectionTitle { font-size: 17px; font-weight: 650; }
QLabel#gpu { color: #a6ebbd; background: #203329; border: 1px solid #314e3d; border-radius: 13px; padding: 7px 12px; }
QLabel#error { color: #ffb4a8; }
QFrame#panel { background: #1a211e; border: 1px solid #2c3731; border-radius: 16px; }
QFrame#panel QWidget { background: transparent; }
QPushButton { background: #242f29; border: 1px solid #39483f; border-radius: 8px; padding: 9px 14px; font-weight: 600; }
QPushButton:hover { background: #314337; border-color: #698a75; }
QPushButton:pressed { background: #3c5144; }
QPushButton:disabled { color: #69766e; background: #202822; border-color: #303a33; }
QFrame#panel QPushButton#primary { background: #b8f397; border-color: #b8f397; color: #162113; font-size: 15px; padding: 13px 18px; }
QFrame#panel QPushButton#primary:hover { background: #cefbb6; }
QFrame#panel QPushButton#primary:disabled { background: #394d31; color: #899d7e; border-color: #394d31; }
QPushButton#quiet { background: transparent; border-color: transparent; color: #b4c5ba; padding: 5px 8px; }
QPushButton#quiet:hover { background: #27352c; }
QPushButton#quiet:disabled { color: #69766e; background: transparent; }
QComboBox, QDoubleSpinBox { background: #121a15; border: 1px solid #3b493f; border-radius: 7px; padding: 8px 10px; min-height: 19px; }
QComboBox:focus, QDoubleSpinBox:focus { border-color: #a9d994; }
QComboBox::drop-down { border: 0; width: 25px; }
QComboBox QAbstractItemView { background: #202c24; selection-background-color: #405a39; border: 1px solid #50664c; }
QCheckBox { spacing: 9px; padding: 5px 0; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; border: 1px solid #6b816f; background: #152019; }
QCheckBox::indicator:checked { background: #b8f397; border-color: #b8f397; image: none; }
QListWidget { background: #151d18; border: 1px solid #2d3c31; border-radius: 10px; outline: none; padding: 4px; }
QListWidget::item { border-bottom: 1px solid #2a372d; padding: 11px 12px; border-radius: 5px; }
QListWidget::item:selected { background: #2c4030; color: #f4fff0; }
QListWidget::item:hover { background: #213127; }
QProgressBar { background: #26332a; border: 0; border-radius: 4px; min-height: 7px; max-height: 7px; }
QProgressBar::chunk { background: #b8f397; border-radius: 4px; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: transparent; width: 7px; }
QScrollBar::handle:vertical { background: #415546; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: #304034; color: #f1f6ef; border: 1px solid #58715b; padding: 6px; }
"""
STYLE += 'QComboBox::down-arrow { image: url("' + (ROOT / 'assets' / 'chevron.svg').as_posix() + '"); width: 12px; height: 12px; }'


def label(text: str, name: str = '') -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    return widget


def duration_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}'


def app_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor('#b8f397'))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor('#162113'), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(19, 44, 44, 19)
    painter.drawLine(22, 19, 44, 19)
    painter.drawLine(44, 19, 44, 41)
    painter.end()
    return QIcon(pixmap)


class DropZone(QFrame):
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setFixedHeight(94)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pixmap = None
        self.hovering = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(3)
        self.title = label('Drop your videos here')
        self.title.setStyleSheet('font-size: 19px; font-weight: 600;')
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle = label('Click to browse · Add more videos at any time', 'muted')
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        self.hint = label('MP4  /  MOV  /  MKV  /  WEBM  + more', 'eyebrow')
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.hint)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor('#213127' if self.hovering else '#19231d'))
        painter.setPen(QPen(QColor('#b8f397' if self.hovering else '#4a634d'), 1.5, Qt.PenStyle.DashLine))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 16, 16)
        if self.pixmap:
            preview = self.pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            painter.setOpacity(0.23)
            painter.drawPixmap((self.width() - preview.width()) // 2, (self.height() - preview.height()) // 2, preview)
        painter.end()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class TaskSignals(QObject):
    value = Signal(object)
    error = Signal(str)
    done = Signal()


class Task(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = TaskSignals()

    def run(self):
        try:
            self.signals.value.emit(self.function())
        except Exception as exc:
            logging.exception('Background task failed')
            self.signals.error.emit(str(exc))
        finally:
            self.signals.done.emit()


class RenderThread(QThread):
    progress = Signal(int, float, str)
    item_started = Signal(int)
    item_done = Signal(int, object)
    item_failed = Signal(int, str, bool)

    def __init__(self, entries, settings, output_dir, parent=None):
        super().__init__(parent)
        self.entries = entries
        self.settings = settings
        self.output_dir = output_dir
        self.cancel_event = threading.Event()

    def run(self):
        for index, path in self.entries:
            if self.cancel_event.is_set():
                break
            self.item_started.emit(index)
            try:
                result = backend.process_video(
                    path, self.settings, self.output_dir,
                    progress=lambda value, message: self.progress.emit(index, value, message),
                    cancel_event=self.cancel_event,
                )
                self.item_done.emit(index, result)
            except Exception as exc:
                cancelled = self.cancel_event.is_set() or isinstance(exc, backend.Cancelled)
                logging.exception('Video processing stopped: %s', path)
                self.item_failed.emit(index, str(exc), cancelled)
                if cancelled:
                    break

    def cancel(self):
        self.cancel_event.set()
        backend.cancel_processing()


class MainWindow(QMainWindow):
    def __init__(self, paths=(), check_environment=True, state_dir=None):
        super().__init__()
        self.setWindowTitle('Upscale + Frame Interpolation · NVIDIA DLSS')
        self.setWindowIcon(app_icon())
        self.resize(1220, 850)
        self.setMinimumSize(1000, 740)
        self.setAcceptDrops(True)
        self.entries, self.tasks = [], set()
        self.worker = None
        self.ready = self.checking_environment = False
        self.batch_count = self.batch_finished = 0
        self.batch_started_at = None
        self.batch_elapsed = 0.0
        self.close_after_cancel = False
        self.output_dir = ROOT / 'outputs'
        state_dir = Path(state_dir) if state_dir is not None else ROOT
        self.preferences = state_dir / 'settings.json'
        self.queue_file = state_dir / 'queue.json'
        self.restoring_queue = True
        self.queue_save_error = ''
        self.queue_save_enabled = True
        self.queue_save_timer = QTimer(self)
        self.queue_save_timer.setSingleShot(True)
        self.queue_save_timer.setInterval(300)
        self.queue_save_timer.timeout.connect(self.save_queue)
        self.build_ui()
        self.restore_settings()
        self.restore_queue()
        self.restoring_queue = False
        self.update_controls()
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(1000)
        self.elapsed_timer.timeout.connect(self.refresh_elapsed)
        self.gpu_poll_pending = False
        self.gpu_timer = QTimer(self)
        self.gpu_timer.setInterval(2500)
        self.gpu_timer.timeout.connect(self.refresh_gpu_usage)
        if check_environment:
            self.refresh_engine()
        if paths:
            self.add_paths(paths)

    def run_task(self, function, success, failure=None):
        task = Task(function)
        self.tasks.add(task)
        task.signals.value.connect(success)
        if failure:
            task.signals.error.connect(failure)
        task.signals.done.connect(lambda: self.tasks.discard(task))
        QThreadPool.globalInstance().start(task)

    def make_button(self, title, callback, tooltip=''):
        button = QPushButton(title)
        button.clicked.connect(callback)
        button.setToolTip(tooltip)
        return button

    def build_ui(self):
        root_widget = QWidget()
        self.setCentralWidget(root_widget)
        root = QVBoxLayout(root_widget)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(16)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.addWidget(label('UPSCALE', 'brand'))
        title_box.addWidget(label('Video upscaling + required frame interpolation', 'muted'))
        header.addLayout(title_box)
        header.addStretch()
        self.gpu_label = label('Checking your GPU…', 'gpu')
        header.addWidget(self.gpu_label)
        self.refresh_engine_button = self.make_button('Refresh engine', self.refresh_engine, 'Recheck the GPU and local engine without restarting the app.')
        header.addWidget(self.refresh_engine_button)
        root.addLayout(header)
        body = QHBoxLayout()
        body.setSpacing(18)
        left = QVBoxLayout()
        left.setSpacing(10)
        self.drop_zone = DropZone()
        self.drop_zone.clicked.connect(self.browse_videos)
        left.addWidget(self.drop_zone)
        queue_header = QHBoxLayout()
        self.queue_label = label('Video queue', 'sectionTitle')
        queue_header.addWidget(self.queue_label)
        queue_header.addStretch()
        self.add_button = self.make_button('+ Add videos', self.browse_videos, 'Ctrl+O · Add videos at any time, including during a run.')
        self.add_button.setStyleSheet('background:#b8f397;color:#162113;border-color:#b8f397;')
        queue_header.addWidget(self.add_button)
        left.addLayout(queue_header)
        self.queue_counts = label('No videos yet · Drop videos above or choose Add videos.', 'muted')
        self.queue_counts.setWordWrap(True)
        left.addWidget(self.queue_counts)
        self.preflight_label = label('Queue is saved automatically', 'muted')
        self.preflight_label.setWordWrap(True)
        self.preflight_label.setMaximumHeight(42)
        left.addWidget(self.preflight_label)
        self.queue = QListWidget()
        self.queue.setMinimumHeight(0)
        self.queue.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.queue.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.queue.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.queue.currentRowChanged.connect(self.select_entry)
        self.queue.itemSelectionChanged.connect(self.update_controls)
        self.queue.itemDoubleClicked.connect(lambda _item: self.open_result())
        left.addWidget(self.queue, 1)
        rerun_row = QHBoxLayout()
        rerun_row.setSpacing(7)
        self.rerun_button = self.make_button('Run selected again', self.rerun_selected, 'Start selected videos with the current settings. Previous output files are kept.')
        self.retry_button = self.make_button('Retry failed', self.retry_failed, 'Run failed and cancelled videos again with the current settings.')
        self.refresh_videos_button = self.make_button('Refresh files', self.refresh_videos, 'F5 · Reread selected video information, or all files when none are selected. Previous results are kept.')
        for button in (self.rerun_button, self.retry_button, self.refresh_videos_button):
            rerun_row.addWidget(button)
        rerun_row.addStretch()
        left.addLayout(rerun_row)
        manage_row = QHBoxLayout()
        manage_row.setSpacing(7)
        self.remove_button = self.make_button('Remove selected', self.remove_selected, 'Delete key · Remove from queue; source and output files are kept.')
        self.clear_finished_button = self.make_button('Clear finished', self.clear_finished, 'Remove successful entries from queue. Output files are kept.')
        self.new_batch_button = self.make_button('New batch', self.new_batch, 'Ctrl+N · Empty the queue and reset progress. Source and output files are kept.')
        for button in (self.remove_button, self.clear_finished_button, self.new_batch_button):
            button.setObjectName('quiet')
            manage_row.addWidget(button)
        manage_row.addStretch()
        left.addLayout(manage_row)
        self.selection_info = label('Select a video to see its source and target.', 'muted')
        self.selection_info.setWordWrap(True)
        self.selection_info.setMinimumHeight(43)
        self.selection_info.setMaximumHeight(54)
        self.selection_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        left.addWidget(self.selection_info)
        result_actions = QHBoxLayout()
        self.play_button = self.make_button('Open latest result ↗', self.open_result)
        self.report_button = self.make_button('View report', self.open_report)
        result_actions.addWidget(self.play_button)
        result_actions.addWidget(self.report_button)
        result_actions.addStretch()
        left.addLayout(result_actions)
        body.addLayout(left, 1)
        panel = QFrame()
        panel.setObjectName('panel')
        panel.setFixedWidth(362)
        panel_outer = QVBoxLayout(panel)
        panel_outer.setContentsMargins(17, 17, 13, 16)
        panel_outer.setSpacing(9)
        panel_outer.addWidget(label('Output settings', 'sectionTitle'))
        required = label('✓  Frame interpolation is always on')
        required.setStyleSheet('color:#b8f397;font-weight:600;')
        panel_outer.addWidget(required)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_body = QWidget()
        settings_layout = QVBoxLayout(self.settings_body)
        settings_layout.setContentsMargins(0, 0, 5, 0)
        settings_layout.setSpacing(6)
        self.fps = self.add_combo(settings_layout, 'Target frame rate', [(f'{v} fps', v) for v in backend.FPS_CHOICES], '60')
        self.scale = self.add_combo(settings_layout, 'Upscale amount', backend.SCALE_CHOICES, 2.0)
        self.processing_order = self.add_combo(settings_layout, 'Processing mode', [
            ('Fast · interpolate, then upscale', 'interpolate_first'),
            ('Detail · upscale, then interpolate', 'upscale_first'),
        ], 'interpolate_first')
        self.mode_hint = label('', 'muted')
        self.mode_hint.setWordWrap(True)
        settings_layout.addWidget(self.mode_hint)
        self.quality = self.add_combo(settings_layout, 'Encoding quality', backend.QUALITY_CHOICES, 'Best')
        self.encoding_summary = label('', 'muted')
        self.encoding_summary.setWordWrap(True)
        settings_layout.addWidget(self.encoding_summary)
        self.advanced_button = QPushButton('Advanced settings  +')
        self.advanced_button.setObjectName('quiet')
        self.advanced_button.setCheckable(True)
        settings_layout.addWidget(self.advanced_button)
        self.advanced = QWidget()
        advanced_layout = QVBoxLayout(self.advanced)
        advanced_layout.setContentsMargins(0, 2, 0, 0)
        advanced_layout.setSpacing(6)
        self.parallel_processing = QCheckBox('Parallel interpolation passes')
        self.parallel_processing.setChecked(True)
        self.parallel_processing.setToolTip('Overlap decoding, interpolation passes and encoding with bounded memory. Keeps frame order and motion history. Turn off to compare sequential processing.')
        advanced_layout.addWidget(self.parallel_processing)
        self.codec = self.add_combo(advanced_layout, 'Video codec', backend.CODEC_CHOICES, 'H.264 (NVIDIA NVENC)')
        self.container = self.add_combo(advanced_layout, 'File format', backend.CONTAINER_CHOICES, 'MP4')
        self.style = self.add_combo(advanced_layout, 'Look', backend.STYLE_CHOICES, 'Natural')
        self.interpolation_engine = self.add_combo(advanced_layout, 'Interpolation engine', ['Auto', 'Native DLSSG', 'Cascade'], 'Auto')
        self.hdr = QCheckBox('Preserve HDR · 10-bit output')
        self.hdr.setToolTip('For HDR sources. Requires H.265, AV1 or ProRes. H.264 is SDR only.')
        self.hdr.toggled.connect(self.hdr_changed)
        advanced_layout.addWidget(self.hdr)
        advanced_layout.addWidget(label('Neural rendering intensity', 'muted'))
        self.intensity = QDoubleSpinBox()
        self.intensity.setRange(0, 2)
        self.intensity.setSingleStep(.1)
        self.intensity.setValue(1)
        self.intensity.setDecimals(1)
        advanced_layout.addWidget(self.intensity)
        self.strict_neural = QCheckBox('Reject fallback output (strict)')
        self.strict_neural.setToolTip('Normally off. Rejects fallback output when direct neural upscaling is unavailable. This does not enable neural support.')
        advanced_layout.addWidget(self.strict_neural)
        neural_hint = label('The result report identifies neural upscaling or native fallback.', 'muted')
        neural_hint.setWordWrap(True)
        advanced_layout.addWidget(neural_hint)
        self.advanced.hide()
        settings_layout.addWidget(self.advanced)
        self.advanced_button.toggled.connect(self.toggle_advanced)
        settings_layout.addStretch()
        scroll.setWidget(self.settings_body)
        self.settings_scroll = scroll
        panel_outer.addWidget(scroll, 1)
        body.addWidget(panel)
        root.addLayout(body, 1)
        bottom = QFrame()
        bottom.setObjectName('panel')
        footer = QVBoxLayout(bottom)
        footer.setContentsMargins(17, 13, 17, 13)
        footer.setSpacing(9)
        destination = QHBoxLayout()
        destination.addWidget(label('SAVE TO', 'eyebrow'))
        self.output_label = label(str(self.output_dir), 'muted')
        self.output_label.setWordWrap(True)
        self.output_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        destination.addWidget(self.output_label, 1)
        self.output_button = self.make_button('Change folder', self.choose_output)
        self.output_button.setObjectName('quiet')
        destination.addWidget(self.output_button)
        self.folder_button = self.make_button('Open folder ↗', self.open_folder)
        self.folder_button.setObjectName('quiet')
        destination.addWidget(self.folder_button)
        footer.addLayout(destination)
        action_row = QHBoxLayout()
        status_box = QVBoxLayout()
        status_box.setSpacing(5)
        self.status = label('Checking the local engine…')
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.setMinimumHeight(36)
        self.status.setMaximumHeight(54)
        status_box.addWidget(self.status)
        self.elapsed_label = label('Add videos → choose settings → Start pending', 'muted')
        status_box.addWidget(self.elapsed_label)
        self.gpu_activity = label('GPU activity appears during processing', 'muted')
        self.gpu_activity.setToolTip('System-wide GPU counters. These include any other apps using your GPU.')
        status_box.addWidget(self.gpu_activity)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        status_box.addWidget(self.progress_bar)
        action_row.addLayout(status_box, 1)
        self.cancel_button = self.make_button('Cancel run', self.cancel_batch)
        action_row.addWidget(self.cancel_button)
        self.start_button = self.make_button('Start pending', self.start_batch)
        self.start_button.setObjectName('primary')
        self.start_button.setMinimumWidth(185)
        action_row.addWidget(self.start_button)
        footer.addLayout(action_row)
        root.addWidget(bottom)
        for combo in (self.scale, self.fps, self.processing_order, self.codec, self.container,
                      self.style, self.quality, self.interpolation_engine):
            combo.currentIndexChanged.connect(self.settings_changed)
        self.hdr.toggled.connect(self.settings_changed)
        self.strict_neural.toggled.connect(self.settings_changed)
        self.intensity.valueChanged.connect(self.settings_changed)
        self.parallel_processing.toggled.connect(self.settings_changed)
        for shortcut, callback in [('Ctrl+O', self.browse_videos), ('Delete', self.remove_selected), ('Ctrl+N', self.new_batch), ('F5', self.refresh_videos)]:
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            self.addAction(action)
        self.settings_changed()

    def add_combo(self, layout, title, choices, default):
        layout.addWidget(label(title, 'muted'))
        combo = QComboBox()
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for choice in choices:
            text, data = choice if isinstance(choice, tuple) else (choice, choice)
            combo.addItem(str(text), data)
        index = combo.findData(default)
        if index >= 0:
            combo.setCurrentIndex(index)
        layout.addWidget(combo)
        return combo

    def settings_changed(self, *_args):
        fast = self.processing_order.currentData() == 'interpolate_first'
        self.mode_hint.setText('Faster motion processing at source resolution, then upscale. Same size and FPS; fine motion detail can differ.' if fast else 'Interpolate at the enlarged resolution. May retain more fine motion detail and takes longer.')
        self.encoding_summary.setText(f"{self.codec.currentData()} · {self.container.currentData()}" + (' · HDR' if self.hdr.isChecked() else ''))
        for entry in self.entries:
            self.refresh_item(entry)
        self.refresh_selection()
        self.update_controls()

    def toggle_advanced(self, checked):
        self.advanced.setVisible(checked)
        self.advanced_button.setText('Advanced settings  −' if checked else 'Advanced settings  +')
        if checked:
            QTimer.singleShot(0, lambda: self.settings_scroll.ensureWidgetVisible(self.parallel_processing, 0, 24))

    def refresh_engine(self):
        if self.worker or self.checking_environment:
            return
        self.checking_environment, self.ready = True, False
        self.gpu_label.setText('Checking GPU…')
        self.status.setText('Checking the local engine and NVIDIA GPU…')
        self.update_controls()
        self.run_task(backend.environment_info, self.environment_ready, self.environment_failed)

    def environment_ready(self, info):
        self.checking_environment = False
        self.ready = bool(info.get('ready'))
        gpus = info.get('gpus') or []
        if gpus:
            gpu = gpus[0]
            name = (gpu.get('name') or gpu.get('label') or str(gpu)) if isinstance(gpu, dict) else str(gpu)
            self.gpu_label.setText(name.replace('NVIDIA GeForce ', ''))
        else:
            self.gpu_label.setText('GPU unavailable')
        self.gpu_label.setToolTip(json.dumps(info, indent=2, default=str))
        self.status.setText('Engine ready. Add videos, or start the pending queue.' if self.ready else '\n'.join(info.get('errors') or ['Engine is not ready. Run Setup.ps1.']))
        self.update_controls()

    def environment_failed(self, message):
        self.checking_environment = self.ready = False
        self.gpu_label.setText('Setup needed')
        self.status.setText(message)
        self.update_controls()

    def browse_videos(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Add videos', '', 'Videos (' + ' '.join('*' + ext for ext in sorted(VIDEO_EXTENSIONS)) + ');;All files (*)')
        self.add_paths(paths)

    def add_paths(self, paths):
        # Active workers own a snapshot of row indices. Appending is safe;
        # removing rows and all reordering are disabled until they finish.
        existing = {os.path.normcase(str(e['path'])): e for e in self.entries}
        rejected, duplicates = [], []
        added = 0
        for raw in paths:
            path = Path(raw).expanduser().resolve()
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                rejected.append(path.name)
                continue
            key = os.path.normcase(str(path))
            if key in existing:
                duplicates.append(existing[key])
                continue
            entry = {'path': path, 'metadata': None, 'state': 'Reading video…', 'pending': True,
                     'result': None, 'error': '', 'thumbnail': None, 'progress': 0.0, 'stage': '', 'started_at': None}
            existing[key] = entry
            self.entries.append(entry)
            item = QListWidgetItem()
            self.queue.addItem(item)
            entry['item'] = item
            self.refresh_item(entry)
            self.probe_entry(entry)
            added += 1
        if self.queue.currentRow() < 0 and self.entries:
            self.queue.setCurrentRow(0)
        if rejected:
            self.status.setText('Unsupported or missing files skipped: ' + ', '.join(rejected[:3]))
        elif added and not self.worker:
            self.status.setText(f'Added {added} video(s). Choose settings, then Start pending.')
        elif duplicates and not self.worker:
            self.queue.setCurrentItem(duplicates[0]['item'])
            self.status.setText('Already in your queue. Select it and choose Run selected again to make another output.')
        self.update_controls()

    def probe_entry(self, entry):
        self.run_task(lambda p=entry['path']: self.read_video(p), lambda data, e=entry: self.video_loaded(e, data), lambda error, e=entry: self.video_failed(e, error))

    @staticmethod
    def read_video(path):
        metadata = backend.probe(path)
        thumbnail = None
        try:
            import av
            with av.open(str(path)) as video:
                frame = next(video.decode(video=0))
                frame = frame.reformat(width=480, height=max(1, round(frame.height * 480 / frame.width)), format='rgb24')
                rgb = frame.to_ndarray()
                thumbnail = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format.Format_RGB888).copy()
        except Exception:
            logging.debug('Thumbnail unavailable', exc_info=True)
        return metadata, thumbnail

    def video_loaded(self, entry, data):
        if entry not in self.entries:
            return
        entry['metadata'], entry['thumbnail'] = data
        entry['state'] = entry.pop('state_before_probe', 'Ready')
        if not entry['state'].startswith('Failed') and entry['state'] != 'Cancelled':
            entry['error'] = ''
        if entry['state'] == 'Invalid video':
            entry.update(state='Ready', pending=True)
        # Adding files must never change settings while a batch is active.
        if not self.worker and not entry.get('result'):
            highest = max((float(e['metadata']['fps']) for e in self.entries if e['metadata'] and e['pending']), default=0)
            if float(self.fps.currentData()) <= highest:
                higher = [rate for rate in backend.FPS_CHOICES if float(rate) > highest]
                if higher:
                    self.fps.setCurrentIndex(self.fps.findData(higher[0]))
        self.refresh_item(entry)
        self.refresh_selection()
        self.update_controls()

    def video_failed(self, entry, error):
        if entry not in self.entries:
            return
        entry.pop('state_before_probe', None)
        entry.update(state='Invalid video', metadata=None, pending=False, error=error)
        self.refresh_item(entry)
        self.refresh_selection()
        self.update_controls()

    def refresh_item(self, entry):
        meta, state = entry['metadata'], entry['state']
        if state == 'Processing…':
            state = f"Processing · {entry['progress']:.0%}"
        elif state == 'Ready' and self.worker:
            state = 'Next batch'
        elif entry['pending'] and entry.get('validation_error') and not self.worker:
            state = 'Check settings'
        detail = 'Reading source information…'
        if meta:
            try:
                width, height = backend.output_size(meta['width'], meta['height'], self.scale.currentData())
                target = f"{width} × {height} · {self.fps.currentData()} fps"
            except Exception:
                target = 'Check output settings'
            detail = f"{meta['width']} × {meta['height']} · {float(meta['fps']):.3g} fps  →  {target}"
        footer = duration_text(meta['duration']) if meta else ''
        if meta and meta.get('hdr'):
            footer += ' · HDR'
        if entry['result']:
            footer += ' · Previous output kept' if entry['state'] != 'Complete ✓' else ' · Output saved'
        if entry.get('stage') and entry['state'] == 'Processing…':
            footer = entry['stage']
        entry['item'].setText(f"{entry['path'].name}  ·  {state}\n{detail}\n{footer}")
        error = entry.get('validation_error') or entry['error']
        entry['item'].setToolTip(str(entry['path']) + ('\n\n' + error if error else ''))

    def selected_entry(self):
        row = self.queue.currentRow()
        return self.entries[row] if 0 <= row < len(self.entries) else None

    def selected_entries(self):
        rows = sorted({self.queue.row(item) for item in self.queue.selectedItems()})
        return [self.entries[row] for row in rows]

    def select_entry(self, _row):
        self.refresh_selection()
        self.update_controls()

    def refresh_selection(self, *_args):
        entry = self.selected_entry()
        result = entry.get('result') if entry else None
        error = entry.get('error', '') if entry else ''
        self.play_button.setEnabled(bool(result and Path(result['output_path']).is_file()))
        self.report_button.setEnabled(bool(error or (result and Path(result['report_path']).is_file())))
        self.report_button.setText('Error details' if error else 'View report')
        self.selection_info.setToolTip('')
        self.drop_zone.pixmap = QPixmap.fromImage(entry['thumbnail']) if entry and entry.get('thumbnail') else None
        self.drop_zone.update()
        if not entry:
            self.selection_info.setText('Select a video to see its source and target.')
            return
        if error:
            summary = error.splitlines()[0].strip()
            if len(summary) > 150:
                summary = summary[:147].rstrip() + '…'
            self.selection_info.setText(summary + ('\nPrevious output is still available. ' if result else '\n') + 'Open Error details, then Retry failed.')
            self.selection_info.setToolTip(error)
            return
        if entry.get('validation_error') and not self.worker:
            self.selection_info.setText(entry['validation_error'] + '\nChange Output settings to continue.')
            self.selection_info.setToolTip(entry['validation_error'])
            return
        meta = entry['metadata']
        if not meta:
            self.selection_info.setText('Reading video information…')
            return
        try:
            width, height = backend.output_size(meta['width'], meta['height'], self.scale.currentData())
            text = f"Next output: {width} × {height} · {self.fps.currentData()} fps"
            if result:
                text += f"  |  Latest saved: {result['width']} × {result['height']} · {result['fps']:g} fps"
                if result.get('warnings'):
                    text += '\nEngine warning or fallback reported · View report for details.'
                self.selection_info.setToolTip(result['output_path'] + '\n' + '\n'.join(result.get('warnings', [])))
            elif meta.get('hdr') and not self.hdr.isChecked():
                text += '\nHDR source: enable Preserve HDR in Advanced settings.'
            target = float(self.fps.currentData())
            if target <= float(meta['fps']):
                text += '\nChoose FPS above the source to generate intermediate frames.'
            elif target > float(meta['fps']) * 6:
                text += '\nChoose a lower target: interpolation supports up to 6× the source FPS.'
            self.selection_info.setText(text)
        except Exception as exc:
            self.selection_info.setText(str(exc))

    def remove_entries(self, entries):
        if self.worker:
            return
        rows = sorted((self.entries.index(e) for e in entries if e in self.entries), reverse=True)
        self.queue.blockSignals(True)
        for row in rows:
            self.entries.pop(row)
            self.queue.takeItem(row)
        self.queue.blockSignals(False)
        self.refresh_selection()
        self.update_controls()

    def remove_selected(self):
        self.remove_entries(self.selected_entries())

    def clear_finished(self):
        self.remove_entries([e for e in self.entries if e['state'] == 'Complete ✓' and not e['pending']])

    def new_batch(self):
        if self.worker:
            return
        self.remove_entries(list(self.entries))
        self.batch_count = self.batch_finished = 0
        self.batch_started_at = None
        self.batch_elapsed = 0.0
        self.progress_bar.setValue(0)
        self.elapsed_label.setText('Add videos → choose settings → Start pending')
        self.gpu_activity.setText('GPU activity appears during processing')
        self.status.setText('New batch ready. Add or drop videos to begin.')
        self.update_controls()

    def refresh_videos(self):
        if self.worker:
            return
        count = 0
        for entry in self.selected_entries() or list(self.entries):
            if entry['state'] == 'Reading video…':
                continue
            entry['state_before_probe'] = entry['state']
            entry['state'] = 'Reading video…'
            self.refresh_item(entry)
            self.probe_entry(entry)
            count += 1
        if count:
            self.status.setText(f'Refreshing information for {count} video(s)…')
        self.update_controls()

    def rerun_selected(self):
        if not self.worker:
            self.begin_batch([e for e in self.selected_entries() if e['metadata'] and e['state'] != 'Reading video…'])

    def retry_failed(self):
        if not self.worker:
            self.begin_batch([e for e in self.entries if e['metadata'] and (e['state'].startswith('Failed') or e['state'] == 'Cancelled')])

    def choose_output(self):
        if self.worker:
            return
        path = QFileDialog.getExistingDirectory(self, 'Choose output folder', str(self.output_dir))
        if path:
            self.output_dir = Path(path)
            self.output_label.setText(str(self.output_dir))
            self.save_settings()

    def open_folder(self):
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_dir)))
        except OSError as exc:
            QMessageBox.warning(self, 'Cannot open folder', str(exc))

    def open_result(self):
        entry = self.selected_entry()
        if entry and entry.get('result'):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(entry['result']['output_path'])))

    def open_report(self):
        entry = self.selected_entry()
        if not entry:
            return
        if entry.get('error'):
            error = entry['error']
            next_step = 'Correct the issue described below, then choose Retry failed. Your video stays in the queue.'
            if 'Strict validation rejected' in error:
                next_step = "Open Advanced settings and turn off 'Reject fallback output (strict)', then choose Retry failed."
            details = QMessageBox(self)
            details.setWindowTitle('Video error details')
            details.setIcon(QMessageBox.Icon.Information)
            details.setTextFormat(Qt.TextFormat.PlainText)
            details.setText(f"{entry['path'].name} · {entry['state']}")
            details.setInformativeText(next_step)
            details.setDetailedText(error)
            details.setStandardButtons(QMessageBox.StandardButton.Ok)
            for button in details.buttons():
                if details.buttonRole(button) == QMessageBox.ButtonRole.ActionRole:
                    button.click()
                    break
            details.exec()
        elif entry.get('result'):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(entry['result']['report_path'])))

    def hdr_changed(self, checked):
        if checked and self.codec.currentData() in ('H.264', 'H.264 (NVIDIA NVENC)'):
            self.codec.setCurrentIndex(self.codec.findData('H.265 (NVIDIA NVENC)'))

    def collect_settings(self):
        return backend.Settings(
            scale=self.scale.currentData(), style=self.style.currentData(),
            intensity=self.intensity.value(), quality=self.quality.currentData(),
            codec=self.codec.currentData(), container=self.container.currentData(),
            preserve_hdr=self.hdr.isChecked(), target_fps=self.fps.currentData(),
            interpolation_engine=self.interpolation_engine.currentData(),
            require_neural_upscaling=self.strict_neural.isChecked(),
            processing_order=self.processing_order.currentData(),
            parallel_processing=self.parallel_processing.isChecked(),
        )

    def update_controls(self):
        busy = self.worker is not None
        pending = [e for e in self.entries if e['metadata'] and e['pending'] and e['state'] != 'Reading video…']
        probing = any(e['state'] == 'Reading video…' for e in self.entries)
        selected = self.selected_entries()
        failed = [e for e in self.entries if e['metadata'] and (e['state'].startswith('Failed') or e['state'] == 'Cancelled')]
        if not busy:
            settings = self.collect_settings()
            for entry in self.entries:
                error = ''
                if entry['metadata']:
                    try:
                        backend.validate_settings(settings, entry['metadata'])
                    except Exception as exc:
                        error = str(exc)
                if entry.get('validation_error', '') != error:
                    entry['validation_error'] = error
                    self.refresh_item(entry)
        blocked = [e for e in pending if e.get('validation_error')]
        issue = self.queue_save_error
        if blocked:
            issue = f"{len(blocked)} need settings changes · {blocked[0]['path'].name}: {blocked[0]['validation_error']}"
        self.preflight_label.setText((issue[:165] + '…' if len(issue) > 165 else issue) or
                                    ('Settings checked · Ready to start' if pending else 'Queue is saved automatically'))
        self.preflight_label.setToolTip('\n'.join(f"{e['path'].name}: {e['validation_error']}" for e in blocked) or
                                       self.queue_save_error or 'The queue and previous results return when you reopen Upscale. Interrupted work waits for Start pending.')
        self.preflight_label.setStyleSheet('color:#f2c67a;' if issue else 'color:#96a89e;')
        complete = sum(e['state'] == 'Complete ✓' for e in self.entries)
        active = sum(e['state'] in ('Processing…', 'Queued') for e in self.entries)
        next_count = sum(e['pending'] and e['state'] not in ('Queued', 'Processing…') for e in self.entries)
        parts = [f'{len(self.entries)} video(s)']
        if active:
            parts.append(f'{active} in current run')
        if next_count:
            parts.append(f'{next_count} {"for next batch" if busy else "pending"}')
        if complete:
            parts.append(f'{complete} completed')
        if failed:
            parts.append(f'{len(failed)} need retry')
        if probing:
            parts.append('reading files…')
        self.queue_counts.setText(' · '.join(parts) if self.entries else 'No videos yet · Drop videos above or choose Add videos.')
        self.start_button.setEnabled(self.ready and bool(pending) and not busy and not probing and not blocked)
        self.start_button.setToolTip('Fix the settings shown above the queue before starting.' if blocked else '')
        self.start_button.setText(f'Start pending ({len(pending)})' if not busy else 'Processing…')
        cancelling = busy and self.worker.cancel_event.is_set()
        self.cancel_button.setEnabled(busy and not cancelling)
        self.cancel_button.setText('Cancelling…' if cancelling else 'Cancel run')
        self.add_button.setEnabled(True)
        self.remove_button.setEnabled(not busy and bool(selected))
        self.clear_finished_button.setEnabled(not busy and bool(complete))
        self.new_batch_button.setEnabled(not busy and bool(self.entries))
        self.rerun_button.setEnabled(self.ready and not busy and bool(selected) and all(e['metadata'] and e['state'] != 'Reading video…' and not e.get('validation_error') for e in selected))
        self.retry_button.setEnabled(self.ready and not busy and bool(failed) and not any(e.get('validation_error') for e in failed))
        self.refresh_videos_button.setEnabled(not busy and bool(self.entries) and not probing)
        self.refresh_engine_button.setEnabled(not busy and not self.checking_environment)
        self.output_button.setEnabled(not busy)
        self.settings_body.setEnabled(not busy)
        self.drop_zone.subtitle.setText('New videos wait for the next batch' if busy else 'Click to browse · Add more videos at any time')
        if not self.restoring_queue:
            self.queue_save_timer.start()
        self.refresh_selection()

    def start_batch(self):
        if not self.worker:
            self.begin_batch([e for e in self.entries if e['metadata'] and e['pending'] and e['state'] != 'Reading video…'])

    def begin_batch(self, entries):
        if self.worker or not self.ready or not entries:
            return
        settings = self.collect_settings()
        try:
            for entry in entries:
                backend.validate_settings(settings, entry['metadata'])
        except Exception as exc:
            self.status.setText('Check output settings: ' + str(exc))
            self.update_controls()
            return
        self.save_settings()
        self.batch_count, self.batch_finished = len(entries), 0
        self.batch_started_at = time.monotonic()
        self.batch_elapsed = 0.0
        self.progress_bar.setValue(0)
        for entry in entries:
            entry.update(state='Queued', pending=False, progress=0.0, error='', stage='', started_at=None)
            self.refresh_item(entry)
        snapshot = [(self.entries.index(entry), entry['path']) for entry in entries]
        self.worker = RenderThread(snapshot, settings, self.output_dir, self)
        self.worker.item_started.connect(self.item_started)
        self.worker.item_done.connect(self.item_done)
        self.worker.item_failed.connect(self.item_failed)
        self.worker.progress.connect(self.render_progress)
        self.worker.finished.connect(self.batch_done)
        self.status.setText(f'Starting {len(entries)} video(s) · Frame interpolation enabled')
        self.refresh_selection()
        self.update_controls()
        self.refresh_elapsed()
        self.elapsed_timer.start()
        self.gpu_timer.start()
        self.refresh_gpu_usage()
        self.save_queue()
        self.worker.start()

    def item_started(self, index):
        entry = self.entries[index]
        entry.update(state='Processing…', error='', started_at=time.monotonic())
        self.queue.setCurrentRow(index)
        self.refresh_item(entry)
        self.status.setText(f"{self.batch_finished + 1} / {self.batch_count} · Starting {entry['path'].name}…")
        self.update_controls()

    def render_progress(self, index, value, message):
        percent = min(1.0, max(0.0, value))
        entry = self.entries[index]
        entry.update(progress=percent, stage=message)
        self.refresh_item(entry)
        self.progress_bar.setValue(round(1000 * (self.batch_finished + percent) / max(1, self.batch_count)))
        if not self.worker or not self.worker.cancel_event.is_set():
            text = f'{self.batch_finished + 1} / {self.batch_count} · {round(percent * 100)}% · {message}'
            summary = self.status.fontMetrics().elidedText(' '.join(text.split()), Qt.TextElideMode.ElideRight, max(160, self.status.width() * 2 - 20))
            self.status.setText(summary)
            self.status.setToolTip(message)
        self.refresh_elapsed()

    def refresh_elapsed(self):
        if self.batch_started_at is not None:
            self.batch_elapsed = time.monotonic() - self.batch_started_at
        text = f'Elapsed {duration_text(self.batch_elapsed)} · {self.batch_finished} / {self.batch_count} finished'
        active = next((e for e in self.entries if e['state'] == 'Processing…'), None)
        if active and active.get('started_at'):
            text += f" · Current video {duration_text(time.monotonic() - active['started_at'])}"
        self.elapsed_label.setText(text)

    def refresh_gpu_usage(self):
        if not self.worker or self.gpu_poll_pending:
            return
        self.gpu_poll_pending = True
        self.run_task(backend.gpu_usage, self.gpu_usage_ready, lambda _error: self.gpu_usage_ready({}))

    def gpu_usage_ready(self, info):
        self.gpu_poll_pending = False
        if not self.worker:
            return
        parts = []
        if info.get('gpu_percent') is not None:
            parts.append(f"GPU {float(info['gpu_percent']):.0f}%")
        if info.get('encoder_percent') is not None:
            parts.append(f"Encoder {float(info['encoder_percent']):.0f}%")
        if info.get('memory_used_mb') is not None and info.get('memory_total_mb'):
            parts.append(f"VRAM {float(info['memory_used_mb']) / 1024:.1f} / {float(info['memory_total_mb']) / 1024:.0f} GB")
        self.gpu_activity.setText('System GPU · ' + ' · '.join(parts) if parts else 'System GPU counters unavailable')

    def item_done(self, index, result):
        entry = self.entries[index]
        entry.update(state='Complete ✓', pending=False, result=result, progress=1.0, error='')
        self.batch_finished += 1
        self.refresh_item(entry)
        self.refresh_selection()
        self.update_controls()
        self.save_queue()

    def item_failed(self, index, error, cancelled):
        entry = self.entries[index]
        # Keep the latest successful result available when a rerun fails.
        entry.update(state='Cancelled' if cancelled else 'Failed', pending=False, error=error)
        self.batch_finished += 1
        self.refresh_item(entry)
        self.refresh_selection()
        self.update_controls()
        self.save_queue()

    def cancel_batch(self):
        if self.worker:
            self.worker.cancel()
            self.status.setText('Cancelling… Completed files are kept; unstarted videos remain pending.')
            self.update_controls()

    def batch_done(self):
        worker = self.worker
        cancelled = worker.cancel_event.is_set() if worker else False
        current = [self.entries[i] for i, _path in worker.entries] if worker else []
        completed = sum(e['state'] == 'Complete ✓' for e in current)
        failed = sum(e['state'].startswith('Failed') for e in current)
        for entry in current:
            if entry['state'] in ('Queued', 'Processing…'):
                entry.update(state='Ready', pending=True, stage='', progress=0.0)
        self.refresh_elapsed()
        self.batch_started_at = None
        self.elapsed_timer.stop()
        self.gpu_timer.stop()
        if self.gpu_activity.text().startswith('System GPU · '):
            self.gpu_activity.setText(self.gpu_activity.text().replace('System GPU · ', 'Last GPU sample · ', 1))
        if worker:
            worker.deleteLater()
        self.worker = None
        for entry in self.entries:
            self.refresh_item(entry)
        self.progress_bar.setValue(self.progress_bar.value() if cancelled else 1000)
        pending = sum(e['pending'] for e in self.entries)
        text = f"{'Run cancelled' if cancelled else 'Run finished'} · {completed} saved · {duration_text(self.batch_elapsed)}"
        if failed:
            text += f' · {failed} failed; choose Retry failed'
        elif pending:
            text += f' · {pending} pending; Start pending when ready'
        else:
            text += ' · Add videos or Run selected again'
        self.status.setText(text)
        self.refresh_selection()
        self.update_controls()
        if self.close_after_cancel:
            self.close()

    def save_queue(self):
        self.queue_save_timer.stop()
        if not self.queue_save_enabled:
            return
        try:
            queue_state.save_queue(self.queue_file, self.entries)
            self.queue_save_error = ''
        except Exception:
            logging.exception('Could not save queue')
            self.queue_save_error = 'Queue could not be saved. Check that the app folder is writable.'
            self.preflight_label.setText(self.queue_save_error)
            self.preflight_label.setStyleSheet('color:#f2c67a;')

    def restore_queue(self):
        try:
            rows = queue_state.load_queue(self.queue_file)
        except (OSError, ValueError, TypeError):
            logging.exception('Could not restore queue')
            # Never silently replace a damaged or newer snapshot with an empty one.
            self.queue_save_enabled = False
            self.queue_save_error = 'Saved queue could not be read and was kept. Rename queue.json to enable saving again.'
            return
        for row in rows:
            state = row.pop('state')
            entry = {**row, 'state': 'Reading video…', 'state_before_probe': state,
                     'metadata': None, 'thumbnail': None, 'progress': 0.0,
                     'stage': '', 'started_at': None}
            self.entries.append(entry)
            entry['item'] = QListWidgetItem()
            self.queue.addItem(entry['item'])
            self.refresh_item(entry)
            self.probe_entry(entry)
        if rows:
            self.queue.setCurrentRow(0)

    def save_settings(self):
        try:
            from dataclasses import asdict
            data = asdict(self.collect_settings())
            data['output_dir'] = str(self.output_dir)
            queue_state.atomic_write_json(self.preferences, data)
        except Exception:
            logging.exception('Could not save preferences')

    def restore_settings(self):
        try:
            data = json.loads(self.preferences.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for key, combo in [('scale', self.scale), ('style', self.style), ('quality', self.quality), ('codec', self.codec), ('container', self.container), ('target_fps', self.fps), ('interpolation_engine', self.interpolation_engine), ('processing_order', self.processing_order)]:
            if key in data:
                index = combo.findData(data[key])
                if index >= 0:
                    combo.setCurrentIndex(index)
        try:
            self.intensity.setValue(float(data.get('intensity', 1)))
            self.hdr.setChecked(bool(data.get('preserve_hdr', False)))
            self.strict_neural.setChecked(bool(data.get('require_neural_upscaling', False)))
            self.parallel_processing.setChecked(bool(data.get('parallel_processing', True)))
            self.output_dir = Path(data.get('output_dir') or self.output_dir)
            self.output_label.setText(str(self.output_dir))
        except (TypeError, ValueError):
            logging.exception('Invalid preferences')
        self.settings_changed()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.drop_zone.hovering = True
            self.drop_zone.update()

    def dragLeaveEvent(self, event):
        self.drop_zone.hovering = False
        self.drop_zone.update()
        event.accept()

    def dropEvent(self, event):
        self.drop_zone.hovering = False
        self.drop_zone.update()
        self.add_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
        event.acceptProposedAction()

    def closeEvent(self, event):
        if self.worker:
            answer = QMessageBox.question(self, 'Processing in progress', 'Cancel the current run and close Upscale?', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self.close_after_cancel = True
                self.cancel_batch()
            event.ignore()
            return
        if self.tasks:
            self.status.setText('Finishing video checks before closing…')
            QTimer.singleShot(200, self.close)
            event.ignore()
            return
        self.save_settings()
        self.save_queue()
        event.accept()




def main():
    parser = argparse.ArgumentParser(description='Upscale videos with NVIDIA DLSS')
    parser.add_argument('videos', nargs='*')
    args = parser.parse_args()
    (ROOT / 'logs').mkdir(exist_ok=True)
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(ROOT / 'logs' / 'gui.log', maxBytes=2_000_000, backupCount=2, encoding='utf-8')], format='%(asctime)s %(levelname)s %(message)s')
    app = QApplication(sys.argv[:1])
    app.setApplicationName('Upscale')
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    server_name = 'dlss-upscale-' + hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:16]
    # Windows allows multiple QLocalServers to listen on one pipe name. A file
    # lock owns the application; the pipe only forwards files and activates it.
    # Keep this object alive until app.exec() returns. Disable age-based expiry
    # so a long render cannot be mistaken for a stale instance (dead process
    # detection remains enabled by QLockFile).
    instance_lock = QLockFile(str(ROOT / '.upscale.lock'))
    instance_lock.setStaleLockTime(0)
    owns_instance = False
    for _attempt in range(20):
        if instance_lock.tryLock(0):
            owns_instance = True
            break
        if instance_lock.error() != QLockFile.LockError.LockFailedError:
            QMessageBox.critical(None, 'Upscale', 'Cannot create the application lock in ' + str(ROOT))
            return 1
        connection = QLocalSocket()
        connection.connectToServer(server_name)
        if connection.waitForConnected(150):
            payload = (json.dumps(args.videos) + '\n').encode()
            if connection.write(payload) == len(payload):
                connection.flush()
                if not connection.bytesToWrite() or connection.waitForBytesWritten(1000):
                    connection.disconnectFromServer()
                    return 0
            connection.abort()
        # The lock owner may still be between acquiring its lock and listening.
        # Retry ownership too, in case it exited during startup.
        QThread.msleep(100)
    if not owns_instance:
        QMessageBox.critical(None, 'Upscale', 'Upscale is already running but did not respond. Return to its existing window and try again.')
        return 1
    server = QLocalServer()
    # Only the proven owner may remove an endpoint left by a previous process.
    QLocalServer.removeServer(server_name)
    if not server.listen(server_name):
        QMessageBox.critical(None, 'Upscale', 'Cannot start the application instance: ' + server.errorString())
        return 1
    window = MainWindow(args.videos)
    sockets = set()

    def accept_connection():
        socket = server.nextPendingConnection()
        sockets.add(socket)
        buffer = bytearray()

        def receive():
            buffer.extend(bytes(socket.readAll()))
            if b'\n' not in buffer:
                return
            try:
                paths = json.loads(buffer.decode().split('\n')[0])
                if isinstance(paths, list) and all(isinstance(p, str) for p in paths):
                    window.add_paths(paths)
            except (ValueError, TypeError):
                logging.exception('Invalid launch message')
            window.showNormal()
            window.raise_()
            window.activateWindow()
            socket.disconnectFromServer()

        socket.readyRead.connect(receive)
        socket.disconnected.connect(lambda: sockets.discard(socket))
        socket.disconnected.connect(socket.deleteLater)
        receive()

    server.newConnection.connect(accept_connection)

    def report_exception(exc_type, value, tb):
        logging.error('Unhandled GUI error', exc_info=(exc_type, value, tb))
        QMessageBox.critical(window, 'Upscale error', str(value) + '\n\nDetails are in logs/gui.log.')

    sys.excepthook = report_exception
    window.show()
    return app.exec()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        logging.exception('Application startup failed')
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, traceback.format_exc(), 'Upscale could not start', 0x10)
        raise
