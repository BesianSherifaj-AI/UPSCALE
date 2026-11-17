"""Queue durability, crash recovery, and non-destructive error handling."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import queue_state


class QueueStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / 'queue.json'

    def tearDown(self):
        self.directory.cleanup()

    def entry(self, state='Ready', **changes):
        return dict(path=self.root / 'source.mp4', state=state, pending=True,
                    error='', result=None, **changes)

    def test_interrupted_jobs_wait_for_explicit_restart(self):
        for state in ('Queued', 'Processing…'):
            entry = self.entry(state)
            entry['pending'] = False
            queue_state.save_queue(self.path, [entry])
            restored = queue_state.load_queue(self.path)[0]
            self.assertEqual(restored['state'], 'Interrupted')
            self.assertTrue(restored['pending'])

    def test_failed_atomic_replace_preserves_previous_snapshot(self):
        queue_state.save_queue(self.path, [self.entry()])
        before = self.path.read_bytes()
        with patch.object(Path, 'replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                queue_state.save_queue(self.path, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_large_reports_and_thumbnails_are_not_copied_into_snapshot(self):
        output = self.root / 'result.mp4'
        output.write_bytes(b'completed video')
        entry = self.entry('Complete ✓')
        entry.update(pending=False, thumbnail=object(), result=dict(
            output_path=str(output), report_path=str(output.with_suffix('.json')),
            width=640, height=384, fps=60., warnings=['native fallback'],
            engine_reports={'large': 'x' * 10000}))
        queue_state.save_queue(self.path, [entry])
        restored = queue_state.load_queue(self.path)[0]
        self.assertEqual(restored['state'], 'Complete ✓')
        self.assertEqual(restored['result']['warnings'], ['native fallback'])
        self.assertLess(self.path.stat().st_size, 1000)

    def test_missing_source_is_kept_for_gui_diagnostics(self):
        queue_state.save_queue(self.path, [self.entry()])
        self.assertEqual(queue_state.load_queue(self.path)[0]['path'], self.root / 'source.mp4')

    def test_missing_output_requires_retry_instead_of_false_success(self):
        entry = self.entry('Complete ✓')
        entry.update(pending=False, result=dict(output_path=str(self.root / 'missing.mp4'),
                     report_path=str(self.root / 'report.json'), width=640, height=384, fps=60))
        queue_state.save_queue(self.path, [entry])
        restored = queue_state.load_queue(self.path)[0]
        self.assertEqual(restored['state'], 'Failed')
        self.assertIsNone(restored['result'])
        self.assertFalse(restored['pending'])

    def test_bad_or_newer_snapshot_is_not_modified(self):
        for data in ('{', '{"version": 2, "entries": []}', '{"version": 1, "entries": [null]}'):
            self.path.write_text(data, encoding='utf-8')
            with self.assertRaises(ValueError):
                queue_state.load_queue(self.path)
            self.assertEqual(self.path.read_text(encoding='utf-8'), data)

    def test_refresh_preserves_previous_state_and_deduplicates(self):
        entry = self.entry('Reading video…', state_before_probe='Cancelled')
        queue_state.save_queue(self.path, [entry, entry])
        restored = queue_state.load_queue(self.path)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]['state'], 'Cancelled')


if __name__ == '__main__':
    unittest.main()
