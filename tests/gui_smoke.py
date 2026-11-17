"""Real Qt queue/worker/GPU integration test with a one-second generated clip.

Run with .venv/Scripts/python.exe -X utf8 tests/gui_smoke.py.
Uses Qt's offscreen renderer and writes only under tests/.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPointF, Qt, QThreadPool, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFontDatabase
from PySide6.QtWidgets import QApplication
import main


def wait_for(predicate, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('Timed out waiting for Qt integration test')


app = QApplication([])
for font in ('segoeui.ttf', 'seguisb.ttf', 'segoeuib.ttf', 'seguisym.ttf'):
    QFontDatabase.addApplicationFont('C:/Windows/Fonts/' + font)
app.setStyle('Fusion')
app.setStyleSheet(main.STYLE)
state_directory = tempfile.TemporaryDirectory(prefix='upscale-smoke-')
with patch.object(main.MainWindow, 'restore_settings', lambda self: None):
    w = main.MainWindow(state_dir=state_directory.name)
w.output_dir = ROOT / 'tests' / 'gui-results'
w.output_label.setText(str(w.output_dir))
w.show()
assert w.fps.currentData() == '60'
assert w.fps.findData(None) == -1
assert 'frame interpolation' in w.windowTitle().lower()
source = ROOT / 'tests' / 'fixtures' / 'test-with-audio.mp4'
if not source.exists():
    source.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(main.backend._engine().paths.FFMPEG), '-v', 'error',
                    '-f', 'lavfi', '-i', 'testsrc2=size=320x192:rate=24',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                    '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-shortest', str(source)], check=True,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
before = hashlib.sha256(source.read_bytes()).digest()
mime = QMimeData()
mime.setUrls([QUrl.fromLocalFile(str(source)), QUrl.fromLocalFile(str(source))])
drag = QDragEnterEvent(QPointF(120, 150).toPoint(), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
QApplication.sendEvent(w, drag)
assert drag.isAccepted()
drop = QDropEvent(QPointF(120, 150), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
QApplication.sendEvent(w, drop)
assert drop.isAccepted()
wait_for(lambda: not w.tasks)
assert w.ready, w.status.text()
assert len(w.entries) == 1, 'Duplicate drop must be ignored'
assert w.entries[0]['metadata']['fps'] == 24
assert '640' in w.selection_info.text()
assert w.start_button.isEnabled()
w.grab().save(str(ROOT / 'tests' / 'gui-ready.png'))
w.start_button.click()
assert w.worker is not None
assert not w.start_button.isEnabled()
assert w.cancel_button.isEnabled()
wait_for(lambda: w.worker is None)
result = w.entries[0]['result']
assert result, w.entries[0]['error']
assert result['fps'] == 60
assert (result['width'], result['height']) == (640, 384)
assert result['engine_reports']['interpolation']['generated_frames'] > 0
assert w.play_button.isEnabled() and w.report_button.isEnabled()
assert hashlib.sha256(source.read_bytes()).digest() == before
ffprobe = str(main.backend._engine().paths.FFPROBE)
streams = json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-show_streams', '-of', 'json', result['output_path']], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))['streams']
assert any(stream['codec_type'] == 'audio' for stream in streams)
subprocess.run([str(main.backend._engine().paths.FFMPEG), '-v', 'error',
                '-xerror', '-i', result['output_path'], '-map', '0:v',
                '-map', '0:a', '-f', 'null', '-'], check=True,
               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
w.grab().save(str(ROOT / 'tests' / 'gui-complete.png'))
w.resize(920, 720)
app.processEvents()
w.grab().save(str(ROOT / 'tests' / 'gui-compact.png'))
summary = {key: result[key] for key in ('output_path', 'width', 'height', 'fps', 'frames', 'duration', 'elapsed_seconds', 'warnings')}
summary['generated_frames'] = result['engine_reports']['interpolation']['generated_frames']
(ROOT / 'tests' / 'gpu-verification.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
print(json.dumps(summary, indent=2))
wait_for(lambda: not w.tasks)
w.close()
app.processEvents()
restored = main.MainWindow(check_environment=False, state_dir=state_directory.name)
wait_for(lambda: not restored.tasks)
assert len(restored.entries) == 1
assert restored.entries[0]['state'] == 'Complete ✓'
assert restored.entries[0]['result']['output_path'] == result['output_path']
assert restored.worker is None
assert restored.play_button.isEnabled()
restored.close()
app.processEvents()
assert QThreadPool.globalInstance().waitForDone(5000)
w.deleteLater()
restored.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
app.processEvents()
state_directory.cleanup()
print('PASS: video/audio decoded; source unchanged; completed result restored without starting work.')
