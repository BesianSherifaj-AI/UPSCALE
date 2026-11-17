"""Check real GPU cancellation through the GUI while interpolation is running."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
import sys
import time
import tempfile
import gc
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool
import main

app = QApplication([])
state_directory = tempfile.TemporaryDirectory(prefix='upscale-cancel-')
with patch.object(main.MainWindow, 'restore_settings', lambda self: None):
    w = main.MainWindow(state_dir=state_directory.name)
w.output_dir = Path(state_directory.name) / 'outputs'
source = ROOT / 'tests' / 'fixtures' / 'test-with-audio.mp4'
w.add_paths([source])
deadline = time.monotonic() + 45
cancelled_at = None
started = False
while time.monotonic() < deadline:
    app.processEvents()
    if not started and w.start_button.isEnabled():
        w.start_button.click()
        started = True
    if w.worker and 'DLSSG output frame' in w.status.text() and cancelled_at is None:
        w.cancel_button.click()
        cancelled_at = time.monotonic()
    if started and w.worker is None:
        break
    time.sleep(.02)
assert cancelled_at is not None, 'Never reached interpolation'
assert w.worker is None, 'Native worker failed to cancel'
assert w.entries[0]['state'] == 'Cancelled', w.entries[0]
assert not list(w.output_dir.rglob('*.mp4')), 'Incomplete output survived cancellation'
assert w.retry_button.isEnabled(), 'Cancelled item should be retryable'
assert source.is_file()
print(f'PASS: GUI cancelled native interpolation in {time.monotonic()-cancelled_at:.2f}s; no incomplete outputs; retry enabled.')
while w.tasks:
    app.processEvents()
    time.sleep(.02)
w.close()
app.processEvents()
assert QThreadPool.globalInstance().waitForDone(5000), 'Background Qt tasks did not finish'
w.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
app.processEvents()
gc.collect()
state_directory.cleanup()
