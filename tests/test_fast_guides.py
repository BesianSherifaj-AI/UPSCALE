"""Ensure the fast CPU guide path preserves motion and reset decisions."""
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
    "fast_guide_module", ROOT / "engine/dlss_engine/frame_interpolation/guides.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FastGuideTests(unittest.TestCase):
    def guide_for_flow(self, calculated, width=None, height=None):
        source_height, source_width = calculated.shape[:2]
        width, height = width or source_width, height or source_height
        guide = MODULE.DLSSGGuideGenerator(width, height, flow_width=source_width)
        self.assertEqual((guide.flow_height, guide.flow_width), calculated.shape[:2])
        guide.previous = np.zeros(calculated.shape[:2], dtype=np.uint8)
        guide._gray = Mock(return_value=np.ones(calculated.shape[:2], dtype=np.uint8))
        guide.flow = Mock()
        guide.flow.calc.return_value = calculated.copy()
        rgba = np.zeros((height, width, 4), dtype=np.uint8)
        return guide, rgba

    def legacy_result(self, calculated, width, height):
        source_height, source_width = calculated.shape[:2]
        calculated = cv2.resize(calculated, (width, height), interpolation=cv2.INTER_LINEAR)
        calculated[..., 0] *= width / source_width
        calculated[..., 1] *= height / source_height
        finite = np.isfinite(calculated).all(axis=2)
        confidence = float(np.mean(finite))
        calculated[~finite] = 0
        reset = confidence < 0.98
        with np.errstate(over="ignore"):
            vectors = (np.zeros((height, width, 2), dtype=np.float16) if reset
                       else np.ascontiguousarray(calculated.astype(np.float16)))
        return vectors, confidence, reset

    def assert_legacy_equal(self, field, width=None, height=None):
        guide, rgba = self.guide_for_flow(field, width, height)
        expected, confidence, reset = self.legacy_result(field, guide.width, guide.height)
        result = guide.process(rgba)
        self.assertEqual(result.confidence, confidence)
        self.assertEqual(result.reset, reset)
        self.assertFalse(result.duplicate)
        self.assertEqual(result.motion.dtype, np.float16)
        self.assertTrue(result.motion.flags.c_contiguous)
        np.testing.assert_array_equal(result.motion.view(np.uint16), expected.view(np.uint16))
        return result

    def test_normal_resized_fields_are_bit_identical(self):
        field = np.random.default_rng(9139).normal(0, 12, (128, 128, 2)).astype(np.float32)
        self.assert_legacy_equal(field)
        self.assert_legacy_equal(field, 384, 384)

    def test_invalid_vectors_preserve_pixel_confidence_and_zeroing(self):
        field = np.ones((128, 128, 2), dtype=np.float32)
        field[0, 0, 0] = np.nan
        field[10, 10, 1] = np.inf
        field[11, 10, 0] = -np.inf
        result = self.assert_legacy_equal(field, 384, 384)
        self.assertFalse(result.reset)
        self.assertLess(result.confidence, 1.0)
        self.assertTrue(np.isfinite(result.motion).all())

    def test_invalid_threshold_resets_the_entire_motion_field(self):
        field = np.ones((128, 128, 2), dtype=np.float32)
        field[:10, :, 0] = np.nan
        result = self.assert_legacy_equal(field, 384, 384)
        self.assertTrue(result.reset)
        self.assertTrue((result.motion == 0).all())

    def test_first_frame_duplicate_and_cut_skip_flow(self):
        guide = MODULE.DLSSGGuideGenerator(128, 128)
        guide.flow = Mock()
        black = np.zeros((128, 128, 4), dtype=np.uint8)
        first = guide.process(black)
        self.assertTrue(first.reset)
        duplicate = guide.process(black)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.confidence, 1.0)
        cut = guide.process(np.full_like(black, 255))
        self.assertTrue(cut.reset)
        self.assertEqual(cut.confidence, 0.0)
        guide.flow.calc.assert_not_called()

    def test_half_float_conversion_rounding_subnormals_and_extremes(self):
        # Cover every half-precision midpoint and both neighboring float32
        # values, including subnormal boundaries and tie-to-even rounding.
        half = np.arange(65536, dtype=np.uint16).view(np.float16)
        finite = np.sort(half[np.isfinite(half)].astype(np.float32))
        midpoint = (finite[:-1] + finite[1:]) / 2
        rng = np.random.default_rng(9139)
        random = rng.integers(0, 2**32, 300000, dtype=np.uint32).view(np.float32)
        random = random[np.isfinite(random)]
        values = np.concatenate([
            midpoint, np.nextafter(midpoint, -np.inf), np.nextafter(midpoint, np.inf),
            random, np.array([0.0, -0.0, 65504, -65504, 65520, -65520], dtype=np.float32),
        ]).reshape(-1, 1)
        with np.errstate(over="ignore"):
            expected = values.astype(np.float16).view(np.uint16)
        actual = cv2.convertFp16(values).view(np.uint16)
        np.testing.assert_array_equal(actual, expected)


if __name__ == "__main__":
    unittest.main()
