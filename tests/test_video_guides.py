"""Preserve upscale motion fields while using vectorized half conversion."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "video_guide_test_module", ROOT / "engine/dlss_engine/video/guides.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class VideoGuideTests(unittest.TestCase):
    def assert_half_equal(self, values):
        with np.errstate(over="ignore", invalid="ignore"):
            expected = np.ascontiguousarray(values.astype(np.float16))
            actual = MODULE._half_motion(values)
        self.assertEqual(actual.dtype, np.float16)
        self.assertTrue(actual.flags.c_contiguous)
        np.testing.assert_array_equal(actual.view(np.uint16), expected.view(np.uint16))

    def test_half_rounding_subnormals_extremes_and_random_bits(self):
        half = np.arange(65536, dtype=np.uint16).view(np.float16)
        finite = np.sort(half[np.isfinite(half)].astype(np.float32))
        midpoint = (finite[:-1] + finite[1:]) / 2
        random = np.random.default_rng(8041).integers(
            0, 2**32, 300000, dtype=np.uint32
        ).view(np.float32)
        random = random[np.isfinite(random)]
        values = np.concatenate([
            midpoint, np.nextafter(midpoint, -np.inf), np.nextafter(midpoint, np.inf),
            random, np.array([0.0, -0.0, 65504, -65504, 65520, -65520], dtype=np.float32),
        ]).reshape(-1, 1)
        self.assert_half_equal(values)

    def test_exceptional_values_preserve_nan_payloads_and_signs(self):
        values = np.array([
            0x7FC00000, 0x7FFFFFFF, 0xFFA12345, 0x7F800001,
            0xFF800000, 0x7F800000, 0x80000000, 0x3F800000,
        ], dtype=np.uint32).view(np.float32).reshape(2, 2, 2)
        self.assert_half_equal(values)

    def test_noncontiguous_motion_is_packed_without_changing_values(self):
        field = np.random.default_rng(23).normal(0, 12, (128, 128, 2)).astype(np.float32)
        self.assert_half_equal(field[:, ::2])

    def test_resized_and_scaled_motion_matches_original_generator(self):
        guide = MODULE.TemporalGuideGenerator(384, 384, flow_width=128)
        field = np.random.default_rng(912).normal(0, 12, (128, 128, 2)).astype(np.float32)
        guide.previous_gray = np.zeros((128, 128), dtype=np.uint8)
        current = np.ones((128, 128), dtype=np.uint8)
        guide._small_gray = Mock(return_value=current)
        guide.dis = Mock()
        guide.dis.calc.return_value = field.copy()
        expected = cv2.resize(field, (384, 384), interpolation=cv2.INTER_LINEAR)
        expected[..., 0] *= 3
        expected[..., 1] *= 3
        expected = np.ascontiguousarray(expected.astype(np.float16))
        result = guide.process(np.zeros((384, 384, 4), dtype=np.uint8))
        self.assertFalse(result.reset)
        self.assertEqual(result.scene_score, 1 / 255)
        self.assertIs(guide.previous_gray, current)
        np.testing.assert_array_equal(result.motion.view(np.uint16), expected.view(np.uint16))

    def test_first_frame_and_scene_cut_keep_zero_motion(self):
        guide = MODULE.TemporalGuideGenerator(128, 128)
        guide.dis = Mock()
        for color in (0, 255):
            result = guide.process(np.full((128, 128, 4), color, dtype=np.uint8))
            self.assertTrue(result.reset)
            self.assertEqual(result.motion.dtype, np.float16)
            self.assertTrue((result.motion == 0).all())
        guide.dis.calc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
