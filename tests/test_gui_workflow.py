"""Exercise the real Qt queue/worker lifecycle with deterministic fake media.

No GPU, actual videos, live output folders, or saved user preferences are used.
Run: .venv/Scripts/python.exe -m unittest discover -s tests -p test_gui_workflow.py -v
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFontDatabase
from PySide6.QtWidgets import QApplication
import main


class GuiWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        for font in ('segoeui.ttf', 'seguisb.ttf', 'segoeuib.ttf', 'seguisym.ttf'):
            path = Path('C:/Windows/Fonts') / font
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
        cls.app.setStyle('Fusion')
        cls.app.setStyleSheet(main.STYLE)

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail('Qt workflow timed out: ' + self.window.status.text())

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.release = threading.Event()
        self.release.set()
        self.calls = []
        self.fail_next = False
        self.patches = [
            patch.object(main.MainWindow, 'restore_settings', lambda _self: None),
            patch.object(main.MainWindow, 'read_video', staticmethod(lambda _path: (
                {'width': 736, 'height': 1280, 'fps': 24., 'duration': 10., 'hdr': False}, None))),
            patch.object(main.backend, 'output_size', lambda w, h, scale: (round(w * scale), round(h * scale))),
            patch.object(main.backend, 'process_video', self.process_video),
            patch.object(main.backend, 'cancel_processing', lambda: None),
            patch.object(main.backend, 'environment_info', lambda: {'ready': True, 'gpus': [{'name': 'NVIDIA GeForce RTX 4090'}]}),
            patch.object(main.backend, 'gpu_usage', lambda: {'gpu_percent': 47, 'encoder_percent': 9, 'memory_used_mb': 1536, 'memory_total_mb': 24576}),
        ]
        for p in self.patches:
            p.start()
        self.window = main.MainWindow(check_environment=False, state_dir=self.root)
        self.window.preferences = self.root / 'preferences.json'
        self.window.output_dir = self.root / 'outputs'
        self.window.output_dir.mkdir()
        self.window.output_label.setText(str(self.window.output_dir))
        self.window.environment_ready({'ready': True, 'gpus': [{'name': 'NVIDIA GeForce RTX 4090'}]})
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.release.set()
        if self.window.worker:
            self.window.cancel_batch()
            self.wait_for(lambda: self.window.worker is None)
        self.wait_for(lambda: not self.window.tasks)
        self.window.close()
        self.app.processEvents()
        for p in reversed(self.patches):
            p.stop()
        self.directory.cleanup()

    def source(self, name):
        path = self.root / name
        path.write_bytes(b'fake source, never modified')
        return path

    def add(self, *paths):
        self.window.add_paths(paths)
        self.wait_for(lambda: not self.window.tasks)

    def process_video(self, path, settings, output_dir, progress, cancel_event):
        self.calls.append((path, settings))
        progress(.25, 'Frame interpolation · generating intermediate frames')
        while not self.release.wait(.01):
            if cancel_event.is_set():
                raise main.backend.Cancelled('Cancelled by user')
        if cancel_event.is_set():
            raise main.backend.Cancelled('Cancelled by user')
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError('Injected render failure')
        progress(.80, 'Upscaling · encoding output')
        output = Path(output_dir) / f'{path.stem}-{len(self.calls)}.mp4'
        output.write_bytes(b'fake successful result')
        report = output.with_suffix('.json')
        report.write_text('{}', encoding='utf-8')
        return {'output_path': str(output), 'report_path': str(report),
                'width': round(736 * settings.scale), 'height': round(1280 * settings.scale),
                'fps': float(settings.target_fps), 'warnings': []}

    def finish(self):
        self.wait_for(lambda: self.window.worker is None)
        self.wait_for(lambda: not self.window.tasks)

    def reopen(self):
        self.finish()
        self.window.close()
        self.app.processEvents()
        self.window = main.MainWindow(check_environment=False, state_dir=self.root)
        self.window.output_dir = self.root / 'outputs'
        self.window.environment_ready({'ready': True, 'gpus': []})
        self.window.show()
        self.wait_for(lambda: not self.window.tasks)

    def test_restart_restores_pending_results_and_does_not_autostart(self):
        one, two = self.source('one.mp4'), self.source('two.mp4')
        self.add(one)
        self.window.start_button.click()
        self.finish()
        result = self.window.entries[0]['result']
        self.add(two)
        self.reopen()
        self.assertEqual([e['path'] for e in self.window.entries], [one, two])
        self.assertEqual(self.window.entries[0]['state'], 'Complete ✓')
        self.assertEqual(self.window.entries[0]['result']['output_path'], result['output_path'])
        self.assertTrue(self.window.entries[1]['pending'])
        self.assertIsNone(self.window.worker)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.window.play_button.isEnabled())
        self.assertTrue(self.window.start_button.isEnabled())

    def test_start_records_queue_before_worker_can_finish(self):
        self.add(self.source('one.mp4'), self.source('two.mp4'))
        self.release.clear()
        self.window.start_button.click()
        self.wait_for(lambda: len(self.calls) == 1)
        saved = main.queue_state.load_queue(self.window.queue_file)
        self.assertEqual([e['state'] for e in saved], ['Interrupted', 'Interrupted'])
        self.assertTrue(all(e['pending'] for e in saved))
        self.window.cancel_batch()
        self.finish()

    def test_empty_queue_stays_empty_after_restart(self):
        self.add(self.source('one.mp4'))
        self.window.new_batch()
        self.reopen()
        self.assertEqual(self.window.entries, [])

    def test_invalid_queue_survives_close_and_shows_recovery_warning(self):
        self.window.close()
        self.app.processEvents()
        queue_file = self.root / 'queue.json'
        queue_file.write_text('{incomplete', encoding='utf-8')
        with self.assertLogs(level='ERROR'):
            self.window = main.MainWindow(check_environment=False, state_dir=self.root)
        self.assertIn('could not be read', self.window.preflight_label.text())
        self.assertFalse(self.window.queue_save_enabled)
        self.add(self.source('one.mp4'))
        self.window.save_queue()
        self.assertEqual(queue_file.read_text(encoding='utf-8'), '{incomplete')

    def test_preflight_blocks_hdr_and_recovers_when_fixed(self):
        self.add(self.source('hdr.mp4'))
        self.window.entries[0]['metadata']['hdr'] = True
        self.window.settings_changed()
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertIn('HDR', self.window.preflight_label.text())
        self.window.start_batch()
        self.assertIsNone(self.window.worker)
        self.assertTrue(self.window.entries[0]['pending'])
        self.assertEqual(self.calls, [])
        self.window.hdr.setChecked(True)
        self.assertEqual(self.window.codec.currentData(), 'H.265 (NVIDIA NVENC)')
        self.assertTrue(self.window.start_button.isEnabled())

    def test_preflight_checks_container_strict_mode_and_frame_limits(self):
        self.add(self.source('one.mp4'))
        self.window.codec.setCurrentIndex(self.window.codec.findData('ProRes Proxy'))
        self.assertFalse(self.window.start_button.isEnabled())
        self.window.container.setCurrentIndex(self.window.container.findData('MOV'))
        self.assertTrue(self.window.start_button.isEnabled())
        self.window.scale.setCurrentIndex(self.window.scale.findData(1.0))
        self.window.strict_neural.setChecked(True)
        self.assertFalse(self.window.start_button.isEnabled())
        self.window.strict_neural.setChecked(False)
        self.assertTrue(self.window.start_button.isEnabled())
        self.window.entries[0]['metadata']['fps'] = 10.
        self.window.fps.setCurrentIndex(self.window.fps.findData('120'))
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertIn('6x', self.window.preflight_label.text())
        self.window.fps.setCurrentIndex(self.window.fps.findData('60'))
        self.assertTrue(self.window.start_button.isEnabled())

    def test_missing_source_on_restore_keeps_previous_result_accessible(self):
        source = self.source('one.mp4')
        self.add(source)
        self.window.start_button.click()
        self.finish()
        source.unlink()
        def missing(_path):
            raise FileNotFoundError('Source moved')
        with patch.object(main.MainWindow, 'read_video', staticmethod(missing)):
            with self.assertLogs(level='ERROR'):
                self.reopen()
        self.assertEqual(self.window.entries[0]['state'], 'Invalid video')
        self.assertTrue(self.window.play_button.isEnabled())
        self.assertFalse(self.window.start_button.isEnabled())

    def test_run_completed_video_again_with_new_settings_and_keep_outputs(self):
        source = self.source('portrait.mp4')
        self.add(source)
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertEqual(self.window.processing_order.currentData(), 'interpolate_first')
        self.assertTrue(self.window.parallel_processing.isChecked())
        self.window.start_button.click()
        self.finish()
        first = self.window.entries[0]['result']
        self.assertTrue(Path(first['output_path']).is_file())
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.rerun_button.isEnabled())
        self.window.scale.setCurrentIndex(self.window.scale.findData(3.0))
        self.window.fps.setCurrentIndex(self.window.fps.findData('120'))
        self.window.parallel_processing.setChecked(False)
        self.release.clear()
        self.window.rerun_button.click()
        self.wait_for(lambda: len(self.calls) == 2)
        self.assertFalse(self.calls[1][1].parallel_processing)
        self.assertEqual(self.calls[1][1].target_fps, '120')
        self.assertFalse(self.window.parallel_processing.isEnabled())
        self.assertIs(self.window.entries[0]['result'], first)
        self.assertTrue(self.window.play_button.isEnabled())
        self.release.set()
        self.finish()
        second = self.window.entries[0]['result']
        self.assertEqual((second['width'], second['height'], second['fps']), (2208, 3840, 120.0))
        self.assertNotEqual(first['output_path'], second['output_path'])
        self.assertTrue(Path(first['output_path']).is_file())
        self.assertEqual(source.read_bytes(), b'fake source, never modified')
        self.assertIn('Add videos or Run selected again', self.window.status.text())
        self.assertTrue(self.window.parallel_processing.isEnabled())

    def test_add_and_drop_during_run_wait_for_next_batch(self):
        one, two, three = (self.source(name) for name in ('one.mp4', 'two.mp4', 'three.mp4'))
        self.add(one)
        self.release.clear()
        self.window.start_button.click()
        self.wait_for(lambda: len(self.calls) == 1)
        self.add(two)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(three))])
        drag = QDragEnterEvent(QPointF(120, 130).toPoint(), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.window, drag)
        self.assertTrue(drag.isAccepted())
        drop = QDropEvent(QPointF(120, 130), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.window, drop)
        self.wait_for(lambda: not self.window.tasks)
        self.assertTrue(self.window.add_button.isEnabled())
        self.assertFalse(self.window.remove_button.isEnabled())
        self.assertFalse(self.window.new_batch_button.isEnabled())
        self.assertEqual(len(self.window.worker.entries), 1)
        self.assertEqual(len(self.window.entries), 3)
        self.assertIn('2 for next batch', self.window.queue_counts.text())
        self.window.remove_selected()
        self.window.new_batch()
        self.assertEqual(len(self.window.entries), 3)
        self.release.set()
        self.finish()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertIn('(2)', self.window.start_button.text())
        self.window.start_button.click()
        self.finish()
        self.assertEqual([call[0] for call in self.calls], [one, two, three])
        self.assertTrue(all(e['state'] == 'Complete ✓' for e in self.window.entries))

    def test_cancel_retains_unstarted_pending_and_new_batch_resets(self):
        self.add(self.source('one.mp4'), self.source('two.mp4'))
        self.release.clear()
        self.window.start_button.click()
        self.wait_for(lambda: self.window.entries[0]['state'] == 'Processing…')
        self.window.cancel_button.click()
        self.assertFalse(self.window.cancel_button.isEnabled())
        self.finish()
        self.assertEqual(self.window.entries[0]['state'], 'Cancelled')
        self.assertEqual(self.window.entries[1]['state'], 'Ready')
        self.assertTrue(self.window.entries[1]['pending'])
        self.assertTrue(self.window.retry_button.isEnabled())
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertEqual(len(self.calls), 1)
        self.window.new_batch_button.click()
        self.assertEqual(self.window.entries, [])
        self.assertEqual(self.window.queue.count(), 0)
        self.assertEqual(self.window.progress_bar.value(), 0)
        self.assertFalse(self.window.play_button.isEnabled())
        self.assertFalse(self.window.start_button.isEnabled())
        self.add(self.source('new.mp4'))
        self.release.set()
        self.window.start_button.click()
        self.finish()
        self.assertEqual(self.window.entries[0]['state'], 'Complete ✓')

    def test_failed_rerun_keeps_result_and_retry_does_not_include_completed(self):
        self.add(self.source('one.mp4'), self.source('two.mp4'))
        self.window.start_button.click()
        self.finish()
        self.window.queue.setCurrentRow(0)
        original = self.window.entries[0]['result']
        self.fail_next = True
        self.window.rerun_button.click()
        self.finish()
        self.assertEqual(self.window.entries[0]['state'], 'Failed')
        self.assertIs(self.window.entries[0]['result'], original)
        self.assertEqual(self.window.report_button.text(), 'Error details')
        self.assertTrue(self.window.play_button.isEnabled())
        self.assertIn('0 saved', self.window.status.text())
        self.window.retry_button.click()
        self.finish()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.window.entries[0]['state'], 'Complete ✓')
        self.assertTrue(Path(original['output_path']).is_file())

    def test_refresh_duplicate_and_clear_finished_preserve_files(self):
        source = self.source('one.mp4')
        self.add(source)
        self.window.start_button.click()
        self.finish()
        result = self.window.entries[0]['result']
        self.add(source)
        self.assertEqual(len(self.window.entries), 1)
        self.assertIn('Run selected again', self.window.status.text())
        self.window.refresh_videos_button.click()
        self.wait_for(lambda: not self.window.tasks)
        self.assertIs(self.window.entries[0]['result'], result)
        self.assertEqual(self.window.entries[0]['state'], 'Complete ✓')
        self.window.refresh_engine_button.click()
        self.wait_for(lambda: not self.window.tasks)
        self.assertTrue(self.window.ready)
        self.add(self.source('pending.mp4'))
        self.window.clear_finished_button.click()
        self.assertEqual([e['path'].name for e in self.window.entries], ['pending.mp4'])
        self.assertTrue(Path(result['output_path']).is_file())
        self.assertTrue(source.is_file())

    def test_compact_layout_keeps_queue_actions_below_queue(self):
        self.add(self.source('one.mp4'), self.source('two.mp4'))
        self.window.resize(1000, 740)
        self.app.processEvents()
        queue_bottom = self.window.queue.mapTo(self.window, self.window.queue.rect().bottomLeft()).y()
        actions_top = self.window.rerun_button.mapTo(self.window, self.window.rerun_button.rect().topLeft()).y()
        self.assertGreater(actions_top, queue_bottom)
        self.assertTrue(self.window.advanced_button.isVisible())
        self.assertTrue(self.window.start_button.isVisible())

    def test_compact_busy_and_long_error_do_not_overlap_controls(self):
        self.add(self.source('one.mp4'))
        self.window.resize(1000, 740)
        self.release.clear()
        self.window.start_button.click()
        self.wait_for(lambda: self.window.entries[0]['state'] == 'Processing…')
        self.add(self.source('queued-for-next-batch.mp4'))
        self.window.render_progress(0, .4, 'Generating intermediate frames from full resolution video with NVIDIA frame interpolation. ' * 8)
        self.app.processEvents()
        self.assert_compact_geometry()
        self.window.cancel_batch()
        self.finish()
        self.window.entries[0].update(error='The engine could not process this video due to an example validation error. ' * 15, state='Failed')
        self.window.queue.setCurrentRow(0)
        self.window.refresh_selection()
        self.window.update_controls()
        self.app.processEvents()
        self.assert_compact_geometry()
        self.assertTrue(self.window.report_button.isEnabled())
        self.assertTrue(self.window.start_button.isEnabled())

    def assert_compact_geometry(self):
        w = self.window
        def top(widget):
            return widget.mapTo(w, widget.rect().topLeft()).y()
        def bottom(widget):
            return widget.mapTo(w, widget.rect().bottomLeft()).y()
        self.assertGreater(top(w.rerun_button), bottom(w.queue))
        self.assertGreater(top(w.remove_button), bottom(w.rerun_button))
        self.assertGreater(top(w.selection_info), bottom(w.remove_button))
        self.assertGreater(top(w.play_button), bottom(w.selection_info))
        self.assertGreater(top(w.output_label), bottom(w.play_button))


if __name__ == '__main__':
    unittest.main()
