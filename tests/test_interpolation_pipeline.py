"""Exercise streaming concurrency, ordering and shutdown without using the GPU."""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from dlss_engine.core.jobs import Cancelled
from dlss_engine.frame_interpolation.pipeline import process_frames, queue_capacity
from dlss_engine.frame_interpolation.processor import DLSSGStage, NearestTimestampWriter, TimedFrame


class IdentityStage:
    def __init__(self, delay=0):
        self.delay = delay
        self.calls = []
        self.threads = set()

    def push(self, item):
        self.threads.add(threading.get_ident())
        self.calls.append(item)
        if self.delay:
            time.sleep(self.delay)
        return [item]


class PipelineTests(unittest.TestCase):
    def tearDown(self):
        self.assertFalse([t.name for t in threading.enumerate() if t.name.startswith("dlss-pipeline-")])

    def run_pipeline(self, source, stages, consume, *, cancel=None, abort=None, capacity=2, parallel=True):
        return process_frames(source, stages, consume, cancel=cancel or threading.Event(),
                              abort=abort or (lambda: None), capacity=capacity, parallel=parallel)

    def test_order_eof_and_one_owner_per_stage(self):
        stages = [IdentityStage(.0004), IdentityStage(.0006), IdentityStage(.0002)]
        actual = []
        stats = self.run_pipeline(range(35), stages, actual.append)
        self.assertEqual(actual, list(range(35)))
        self.assertEqual(stats.stage_inputs, [35] * 3)
        self.assertEqual(stats.stage_outputs, [35] * 3)
        self.assertTrue(all(len(stage.threads) == 1 for stage in stages))
        self.assertEqual(len(set.union(*(stage.threads for stage in stages))), 3)
        self.assertTrue(all(0 <= size <= 2 for size in stats.maximum_queue_frames))
        self.assertEqual(stats.mode, "parallel")

    def test_empty_source_and_zero_stage_passthrough(self):
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                actual = []
                stats = self.run_pipeline([], [IdentityStage()], actual.append, parallel=parallel)
                self.assertEqual(actual, [])
                self.assertEqual(stats.stage_inputs, [0])
                self.run_pipeline(range(5), [], actual.append, parallel=parallel)
                self.assertEqual(actual, list(range(5)))

    def test_stages_overlap_work(self):
        # A sequential pipeline cannot reach stage 0's second input until stage
        # 1 returns. This barrier proves overlap without a fragile speed ratio.
        first_later_stage = threading.Event()
        second_first_stage = threading.Event()

        class First:
            def push(self, item):
                if item == 1:
                    if not first_later_stage.wait(1):
                        raise AssertionError("Later stage never began")
                    second_first_stage.set()
                return [item]

        class Second:
            def push(self, item):
                if item == 0:
                    first_later_stage.set()
                    if not second_first_stage.wait(1):
                        raise AssertionError("Earlier stage could not run concurrently")
                return [item]

        actual = []
        self.run_pipeline(range(4), [First(), Second()], actual.append, capacity=1)
        self.assertEqual(actual, list(range(4)))

    def test_backpressure_bounds_source_read_ahead(self):
        decoded = []
        observed = []

        def source():
            for item in range(60):
                decoded.append(item)
                yield item

        def consume(item):
            if item == 0:
                time.sleep(.08)
                observed.append(len(decoded))

        stats = self.run_pipeline(source(), [IdentityStage(), IdentityStage()], consume, capacity=1)
        # Three one-frame queues + two active stages + decoder's current item
        # + the sink's current item. The entire input must never be buffered.
        self.assertLessEqual(observed[0], 7)
        self.assertEqual(stats.maximum_queue_frames, [1, 1, 1])
        self.assertEqual(len(decoded), 60)

    def test_stage_failure_unblocks_native_peer_and_closes_source(self):
        entered = threading.Event()
        release = threading.Event()
        source_closed = threading.Event()
        abort_calls = []

        def source():
            try:
                yield from range(100)
            finally:
                source_closed.set()

        class Failing:
            def push(self, item):
                if item == 1:
                    if not entered.wait(1):
                        raise AssertionError("Peer did not start")
                    raise RuntimeError("stage failure evidence")
                return [item]

        class BlockedNative:
            def push(self, item):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("Native abort was not called")
                raise RuntimeError("secondary broken pipe")

        def abort():
            abort_calls.append(True)
            release.set()

        before = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "stage failure evidence"):
            self.run_pipeline(source(), [Failing(), BlockedNative()], lambda _: None,
                              abort=abort, capacity=1)
        self.assertLess(time.monotonic() - before, 1)
        self.assertEqual(len(abort_calls), 1)
        self.assertTrue(source_closed.is_set())

    def test_source_and_writer_errors_propagate_without_hanging(self):
        def broken_source():
            yield 0
            raise ValueError("decode failed")

        def broken_writer(item):
            raise ValueError("encode failed")

        for source, consume, message in (
            (broken_source(), lambda _: None, "decode failed"),
            (range(100), broken_writer, "encode failed"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.run_pipeline(source, [IdentityStage(.001)] * 2, consume, capacity=1)

    def test_cancellation_with_full_queues_terminates_workers(self):
        cancel = threading.Event()
        released = threading.Event()
        entered = threading.Event()

        class WaitingStage:
            def push(self, item):
                entered.set()
                if not released.wait(2):
                    raise AssertionError("Cancelled native worker was not released")
                return [item]

        def cancel_when_entered():
            entered.wait(1)
            cancel.set()

        stopper = threading.Thread(target=cancel_when_entered)
        stopper.start()
        before = time.monotonic()
        try:
            with self.assertRaises(Cancelled):
                self.run_pipeline(range(100), [WaitingStage(), IdentityStage()], lambda _: None,
                                  cancel=cancel, abort=released.set, capacity=1)
        finally:
            stopper.join(1)
        self.assertTrue(released.is_set())
        self.assertLess(time.monotonic() - before, 1)

    def test_resolution_scaled_memory_limit(self):
        self.assertEqual(queue_capacity(736, 1280, 3), 4)
        self.assertEqual(queue_capacity(2208, 3840, 3), 1)
        self.assertEqual(queue_capacity(7680, 4320, 3), 1)
        for width, height, stages in [(0, 2, 1), (2, -1, 1), (2, 2, -1)]:
            with self.assertRaises(ValueError):
                queue_capacity(width, height, stages)

    def test_real_stage_cascade_preserves_pixels_timestamps_cuts_and_provenance(self):
        class Guides:
            def __init__(self, width, height):
                self.previous = None

            def process(self, rgba, force_reset=False):
                value = int(rgba[0, 0, 0])
                result = SimpleNamespace(
                    motion=np.zeros((2, 2, 2), dtype=np.float16),
                    reset=(force_reset or self.previous is None or abs(value - self.previous) > 100),
                    duplicate=value == self.previous,
                )
                self.previous = value
                return result

        class Session:
            def __init__(self):
                self.previous = None
                self.calls = []

            def process_frame(self, rgba, motion, timestamp, *, reset):
                self.calls.append((timestamp, reset, rgba.tobytes()))
                generated = [] if reset else [
                    ((rgba.astype(np.uint16) + self.previous.astype(np.uint16)) // 2).astype(np.uint8)
                ]
                self.previous = rgba
                time.sleep(.0002)
                return generated

        class Stream:
            def encode(self, frame=None):
                return [] if frame is None else [frame]

        class Nut:
            def __init__(self):
                self.frames = []

            def mux(self, frame):
                self.frames.append((frame.pts, frame.time_base, frame.to_ndarray(format="rgba").tobytes()))

        def render(parallel):
            sessions = [Session() for _ in range(3)]
            with patch("dlss_engine.frame_interpolation.processor.DLSSGGuideGenerator", Guides):
                stages = [DLSSGStage(session, 2, 2, 1, detect_source_cuts=index == 0)
                          for index, session in enumerate(sessions)]
            source = [TimedFrame(np.full((2, 2, 4), value, np.uint8), timestamp,
                                 int(index >= 5), "Source", index)
                      for index, (value, timestamp) in enumerate(zip(
                          [0, 5, 10, 10, 250, 245, 240, 235],
                          [Fraction(i, 24) for i in range(5)] + [Fraction(i, 24) for i in range(12, 15)],
                      ))]
            nut = Nut()
            writer = NearestTimestampWriter(nut, Stream(), Fraction(60), 38)
            sequence = []

            def consume(frame):
                sequence.append((frame.timestamp, frame.segment, frame.provenance,
                                 frame.source_index, frame.rgba.tobytes()))
                writer.push(frame)

            stats = self.run_pipeline(source, stages, consume, parallel=parallel, capacity=1)
            writer.finish()
            return (sequence, nut.frames, [session.calls for session in sessions],
                    [(stage.scene_cuts, stage.duplicates) for stage in stages],
                    (writer.generated, writer.copied, writer.max_error, writer.selected_real_ids),
                    stats.stage_inputs, stats.stage_outputs)

        serial = render(False)
        parallel = render(True)
        self.assertEqual(parallel, serial)
        self.assertGreater(serial[4][0], 0)
        self.assertGreater(serial[3][0][0], 0)
        self.assertGreater(serial[3][0][1], 0)


if __name__ == "__main__":
    unittest.main()
