"""Cancellation must close PyAV before closing the pipe it wraps."""
from contextlib import ExitStack
from fractions import Fraction
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
from dlss_engine.frame_interpolation import processor
from dlss_engine.frame_interpolation.models import FrameInterpolationOptions
from dlss_engine.core.jobs import Cancelled


class InterpolationCleanupTests(unittest.TestCase):
    def test_cancellation_closes_muxer_before_pipe_even_when_flush_fails(self):
        for flush_error in (False, True):
            with self.subTest(flush_error=flush_error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'source.mp4'
                source.write_bytes(b'synthetic input')
                closed = []

                class Pipe(io.BytesIO):
                    def close(self):
                        closed.append('pipe')
                        super().close()

                pipe = Pipe()
                encoder = Mock(stdin=pipe, stderr=None)
                encoder.poll.return_value = 1
                thread = Mock()
                stream = SimpleNamespace()
                muxer = Mock()
                muxer.add_stream.return_value = stream

                def close_muxer():
                    self.assertFalse(pipe.closed, 'PyAV must finish before its IO is closed')
                    closed.append('muxer')
                    if flush_error:
                        raise BrokenPipeError('encoder already stopped')

                muxer.close.side_effect = close_muxer
                metadata = dict(width=320, height=192, rate=Fraction(24), frames=24,
                                fps=24., duration=1., hdr=False, rotation=0)
                patches = [
                    patch.object(processor, 'prepare_runtime', return_value=SimpleNamespace(gpus=[], runtime_bundle=None)),
                    patch.object(processor, 'resolve_runtime_ai_gpu', return_value={}),
                    patch.object(processor, 'probe_frame_interpolation_capabilities', return_value=SimpleNamespace(available=True, native_multiplier=4)),
                    patch.object(processor.ffmpeg, 'probe_video', return_value=metadata),
                    patch.object(processor.ffmpeg, 'resolve_video_gpu', return_value=None),
                    patch.object(processor.ffmpeg, 'start_encoder', return_value=(encoder, thread, [], 'fake', 'Best')),
                    patch.object(processor, 'DirectDLSSGSession'),
                    patch.object(processor.av, 'open', return_value=muxer),
                    patch.object(processor, 'process_frames', side_effect=Cancelled('stopped after muxer opened')),
                ]
                with ExitStack() as stack:
                    for item in patches:
                        stack.enter_context(item)
                    with self.assertRaises(Cancelled):
                        processor.interpolate_video(source, FrameInterpolationOptions(),
                            output_directory=root/'outputs', jobs_directory=root/'jobs', logs_directory=root/'logs')
                self.assertEqual(closed, ['muxer', 'pipe'])
                self.assertEqual(list((root/'outputs').iterdir()), [])
                self.assertEqual(list((root/'jobs').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
