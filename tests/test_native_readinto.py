"""Check the DLSSG pipe reader without starting native workers or GPU renders."""
import io
from fractions import Fraction
from pathlib import Path
import struct
import sys
import threading
from types import SimpleNamespace
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from dlss_engine.frame_interpolation.native import (
    DirectDLSSGSession, FRAME_OUT_MAGIC, _read_exact_into,
)


class ShortReadStream(io.BytesIO):
    """A pipe may return fewer bytes than a read requested."""

    def read(self, size=-1):
        return super().read(min(size, 3) if size >= 0 else 3)

    def readinto(self, target):
        return super().readinto(target[:3])


class NativeFrameReaderTests(unittest.TestCase):
    def test_bytesio_fills_owned_contiguous_frame(self):
        expected = np.arange(48, dtype=np.uint8).reshape(3, 4, 4)
        actual = np.empty_like(expected)
        _read_exact_into(io.BytesIO(expected.tobytes()), actual)
        np.testing.assert_array_equal(actual, expected)
        self.assertTrue(actual.flags.c_contiguous)
        self.assertTrue(actual.flags.owndata)
        self.assertTrue(actual.flags.writeable)

    def test_short_reads_stop_at_the_frame_boundary(self):
        expected = np.arange(48, dtype=np.uint8).reshape(3, 4, 4)
        stream = ShortReadStream(expected.tobytes() + b"next")
        actual = np.empty_like(expected)
        _read_exact_into(stream, actual)
        np.testing.assert_array_equal(actual, expected)
        self.assertEqual(stream.tell(), expected.nbytes)
        self.assertEqual(stream.read(), b"nex")

    def test_truncated_frame_reports_eof(self):
        with self.assertRaisesRegex(RuntimeError, "after 7 of 16 frame bytes"):
            _read_exact_into(ShortReadStream(b"1234567"), np.empty((2, 2, 4), np.uint8))

    def session(self, payload):
        session = DirectDLSSGSession.__new__(DirectDLSSGSession)
        session.width = session.height = 2
        session.frame_bytes = 16
        session.closed = False
        session.controller = SimpleNamespace(cancel=threading.Event())
        session.process = SimpleNamespace(stdin=io.BytesIO(), stdout=ShortReadStream(payload))
        session._next_index = 0
        return session

    def test_multiple_generated_frames_have_independent_writable_storage(self):
        payload = struct.pack("<4I", FRAME_OUT_MAGIC, 0, 2, 0) + bytes(range(32))
        session = self.session(payload)
        frames = session.process_frame(
            np.zeros((2, 2, 4), np.uint8), np.zeros((2, 2, 2), np.float16),
            Fraction(1, 24), reset=False,
        )
        self.assertEqual(len(frames), 2)
        self.assertEqual(session._next_index, 1)
        np.testing.assert_array_equal(frames[0].ravel(), np.arange(16, dtype=np.uint8))
        np.testing.assert_array_equal(frames[1].ravel(), np.arange(16, 32, dtype=np.uint8))
        self.assertFalse(np.shares_memory(frames[0], frames[1]))
        first = frames.pop(0)
        first[:] = 99
        del first
        np.testing.assert_array_equal(frames[0].ravel(), np.arange(16, 32, dtype=np.uint8))
        self.assertTrue(frames[0].flags.owndata)
        self.assertTrue(frames[0].flags.c_contiguous)
        self.assertTrue(frames[0].flags.writeable)

    def test_disabled_worker_produces_no_frame(self):
        session = self.session(struct.pack("<4I", FRAME_OUT_MAGIC, 0, 1, 1))
        self.assertEqual(session.process_frame(
            np.zeros((2, 2, 4), np.uint8), np.zeros((2, 2, 2), np.float16),
            Fraction(0), reset=True,
        ), [])

    def test_partial_second_frame_rejects_the_response(self):
        payload = struct.pack("<4I", FRAME_OUT_MAGIC, 0, 2, 0) + bytes(range(23))
        session = self.session(payload)
        with self.assertRaisesRegex(RuntimeError, "after 7 of 16 frame bytes"):
            session.process_frame(
                np.zeros((2, 2, 4), np.uint8), np.zeros((2, 2, 2), np.float16),
                Fraction(0), reset=False,
            )


if __name__ == "__main__":
    unittest.main()
