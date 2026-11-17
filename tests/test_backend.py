"""Exercise adapter safety and stage orchestration without launching GPU workers."""
import json
from fractions import Fraction
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend


class BackendWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "original.mp4"
        self.source.write_bytes(b"irreplaceable source")
        self.destination = self.root / "output"
        self.base_engine = backend._engine()
        self.fake_engine = SimpleNamespace(**vars(self.base_engine))
        self.fake_engine.video = SimpleNamespace(convert_video=Mock(side_effect=self.render))
        self.fake_engine.interpolation = SimpleNamespace(interpolate_video=Mock(side_effect=self.interpolate))
        self.fake_engine.jobs = SimpleNamespace(
            Cancelled=self.base_engine.jobs.Cancelled, cancel_active_job=Mock())
        self.fallback = False
        self.render_hook = None
        self.output_dimensions = (640, 360)
        self.stage_metadata = {}
        self.stage_order = []
        self.last_mux_source = None
        self.metadata = dict(width=320, height=180, fps=24.0, rate=Fraction(24),
                             duration=1.0, frames=24, hdr=False)
        self.patches = [patch.object(backend, "_engine", return_value=self.fake_engine),
                        patch.object(backend, "APP_DIR", self.root),
                        patch.object(backend, "probe", side_effect=self.probe)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def probe(self, path):
        return dict(self.stage_metadata.get(str(path), self.metadata))

    def render(self, source, options, progress, *, output_directory,
               jobs_directory, logs_directory):
        output_directory.mkdir(parents=True)
        output = output_directory / "upscaled.mp4"
        self.stage_order.append("upscale")
        prior = Path(source).read_bytes()
        output.write_bytes(b"finished upscaled and interpolated video" if prior == b"interpolated only" else b"finished upscaled video")
        metadata = self.probe(source)
        metadata["width"], metadata["height"] = self.output_dimensions
        self.stage_metadata[str(output)] = metadata
        self.last_mux_source = options.mux_source_path
        report = logs_directory / "upscaled.report.json"
        report.write_text(json.dumps(dict(status="success", feature_18_confirmed=True,
                                         nr_upscaling_active=not self.fallback,
                                         nr_native_fallback=self.fallback)))
        progress(1.0, "complete")
        if self.render_hook:
            self.render_hook()
        return SimpleNamespace(output_path=str(output), report_path=str(report))

    def interpolate(self, source, options, progress, *, output_directory,
                    jobs_directory, logs_directory):
        self.stage_order.append("interpolation")
        prior = Path(source).read_bytes()
        self.assertIn(prior, (b"finished upscaled video", b"irreplaceable source"))
        output_directory.mkdir(parents=True)
        output = output_directory / "interpolated.mp4"
        output.write_bytes(b"interpolated only" if Path(source) == self.source else b"finished upscaled and interpolated video")
        self.stage_metadata[str(output)] = {**self.probe(source), "fps":60.0, "rate":Fraction(60), "frames":60}
        self.last_mux_source = options.mux_source_path
        report = logs_directory / "interpolated.report.json"
        report.write_text(json.dumps(dict(status="success", generated_frames=36, output_frames=60)))
        progress(1.0, "complete")
        return SimpleNamespace(output_path=str(output), report_path=str(report))

    def assert_no_published_or_staged_video(self):
        self.assertEqual(list(self.destination.rglob("*.mp4")), [])
        self.assertEqual(self.source.read_bytes(), b"irreplaceable source")

    def test_outputs_are_unique_source_is_preserved_and_reports_are_permanent(self):
        first = backend.process_video(self.source, backend.Settings(), self.destination)
        second = backend.process_video(self.source, backend.Settings(), self.destination)
        self.assertNotEqual(first["output_path"], second["output_path"])
        self.assertEqual(Path(first["output_path"]).read_bytes(), b"finished upscaled and interpolated video")
        self.assertEqual(self.source.read_bytes(), b"irreplaceable source")
        saved = json.loads(Path(first["report_path"]).read_text(encoding="utf-8"))
        self.assertTrue(saved["engine_reports"]["upscale"]["feature_18_confirmed"])
        self.assertEqual(saved["engine_reports"]["interpolation"]["generated_frames"], 36)
        self.assertEqual(saved["fps"], 60.0)
        self.assertEqual(saved["settings"]["target_fps"], "60")
        self.assertEqual(self.fake_engine.interpolation.interpolate_video.call_count, 2)
        self.assertEqual(self.stage_order, ["interpolation", "upscale"] * 2)
        self.assertEqual(self.last_mux_source, str(self.source))
        self.assertEqual(list((self.destination / ".work").iterdir()), [])

    def test_fallback_is_visible_and_strict_mode_never_publishes_it(self):
        self.fallback = True
        result = backend.process_video(self.source, backend.Settings(), self.destination)
        self.assertIn("fallback", result["warnings"][0])
        strict_destination = self.root / "strict"
        with self.assertRaisesRegex(RuntimeError, "No output was saved"):
            backend.process_video(self.source, backend.Settings(require_neural_upscaling=True),
                                  strict_destination)
        self.assertEqual(list(strict_destination.rglob("*.mp4")), [])

    def test_failed_interpolation_does_not_publish_the_intermediate(self):
        self.fake_engine.interpolation.interpolate_video.side_effect = RuntimeError("GPU unavailable")
        with self.assertRaisesRegex(RuntimeError, "GPU unavailable"):
            backend.process_video(self.source, backend.Settings(target_fps="60"), self.destination)
        self.assert_no_published_or_staged_video()

    def test_cancellation_between_stages_skips_interpolation(self):
        event = threading.Event()
        self.render_hook = event.set
        with self.assertRaises(backend.Cancelled):
            backend.process_video(self.source, backend.Settings(target_fps="60", processing_order="upscale_first"),
                                  self.destination, cancel_event=event)
        self.fake_engine.interpolation.interpolate_video.assert_not_called()
        self.assert_no_published_or_staged_video()

    def test_cancellation_reaches_upstream_while_render_is_blocked(self):
        event = threading.Event()
        reached_engine = threading.Event()
        self.fake_engine.jobs.cancel_active_job.side_effect = reached_engine.set

        def blocking_hook():
            event.set()
            self.assertTrue(reached_engine.wait(2), "Cancellation never reached the native engine")

        self.render_hook = blocking_hook
        with self.assertRaises(backend.Cancelled):
            backend.process_video(self.source, backend.Settings(), self.destination, cancel_event=event)
        self.assert_no_published_or_staged_video()

    def test_invalid_hdr_and_container_are_rejected_before_rendering(self):
        with self.assertRaisesRegex(ValueError, "ProRes"):
            backend.process_video(self.source, backend.Settings(codec="ProRes Proxy"), self.destination)
        self.metadata["hdr"] = True
        with self.assertRaisesRegex(ValueError, "Enable Preserve HDR"):
            backend.process_video(self.source, backend.Settings(), self.destination)
        self.fake_engine.video.convert_video.assert_not_called()

    def test_wrong_output_dimensions_are_not_published(self):
        self.output_dimensions = (320, 180)
        with self.assertRaisesRegex(RuntimeError, "dimensions"):
            backend.process_video(self.source, backend.Settings(), self.destination)
        self.assert_no_published_or_staged_video()

    def test_interpolation_cannot_be_disabled_or_lower_than_the_source(self):
        for target in (None, "", "23.976"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                backend.process_video(self.source, backend.Settings(target_fps=target), self.destination)
        self.fake_engine.video.convert_video.assert_not_called()

    def test_original_order_remains_available_with_original_audio(self):
        result = backend.process_video(self.source, backend.Settings(processing_order="upscale_first"), self.destination)
        self.assertEqual(self.stage_order, ["upscale", "interpolation"])
        self.assertEqual(result["processing_order"], self.stage_order)
        self.assertEqual(self.last_mux_source, str(self.source))
        self.assertEqual(result["frames"], 60)

    def test_invalid_order_is_rejected_before_native_work(self):
        with self.assertRaisesRegex(ValueError, "processing mode"):
            backend.process_video(self.source, backend.Settings(processing_order="unknown"), self.destination)
        self.assertEqual(self.stage_order, [])

    def test_parallel_preference_reaches_engine_without_disabling_interpolation(self):
        for parallel in (True, False):
            backend.process_video(self.source, backend.Settings(parallel_processing=parallel), self.destination)
            options = self.fake_engine.interpolation.interpolate_video.call_args.args[1]
            self.assertEqual(options.parallel_processing, parallel)
            self.assertEqual(options.target_fps, '60')
        with self.assertRaisesRegex(ValueError, 'Parallel processing'):
            backend.process_video(self.source, backend.Settings(parallel_processing=2), self.destination)


if __name__ == "__main__":
    unittest.main()
