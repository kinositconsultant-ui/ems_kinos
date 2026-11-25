"""Tests for the MiniFASNetV2 face-liveness provider.

The class semantics follow the upstream reference implementation
(``fasnet_upstream/test.py``): the network emits three raw logits and
``argmax == 1`` is the bona-fide ("real face") class, while indices 0 and 2 are
presentation attacks. Upstream preprocessing keeps raw BGR ``float32`` values
in ``[0, 255]`` because its ``to_tensor`` helper keeps ``div(255)`` commented
out.
"""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from django.test import SimpleTestCase

from attendance.services import face_liveness
from attendance.services.face_liveness import FaceLivenessError, check_face_liveness

# Sentinel so an explicit ``None`` bbox can be tested (it is invalid, unlike
# an omitted argument).
_UNSET = object()


# Crop boxes produced by the upstream ``CropImage._get_new_box`` for the
# 2.7x scale used by the ``2.7_80x80_MiniFASNetV2`` checkpoint. The last three
# are the real YuNet detector boxes for the bundled upstream sample images.
GOLDEN_CROP_BOXES = (
    # (src_w, src_h, bbox, scale, expected inclusive crop bounds)
    (200, 160, (30, 20, 90, 100), 2.7, (3, 0, 146, 159)),
    (640, 480, (200, 150, 160, 200), 2.7, (88, 0, 471, 479)),
    (320, 320, (0, 0, 320, 320), 2.7, (0, 0, 319, 319)),
    (100, 100, (10, 10, 30, 30), 2.7, (0, 0, 81, 81)),
    (100, 100, (10, 10, 30, 30), 1.0, (10, 10, 40, 40)),
    (200, 160, (30, 20, 90, 100), 2.0, (3, 0, 146, 159)),
    (640, 480, (119, 113, 170, 240), 2.7, (34, 0, 373, 479)),  # image_T1.jpg
    (640, 480, (188, 110, 196, 240), 2.7, (90, 0, 481, 479)),  # image_F1.jpg
    (640, 480, (139, 217, 223, 298), 2.7, (71, 0, 429, 479)),  # image_F2.jpg
)


class FaceLivenessCropTests(SimpleTestCase):
    """The crop must reproduce the upstream boundary-adjusted shifted box."""

    def test_golden_crop_boxes_match_upstream(self):
        for src_w, src_h, bbox, scale, expected in GOLDEN_CROP_BOXES:
            with self.subTest(src_w=src_w, src_h=src_h, bbox=bbox, scale=scale):
                self.assertEqual(
                    face_liveness._shifted_crop_box(src_w, src_h, bbox, scale),
                    expected,
                )

    def test_crop_pixels_match_upstream_slice_and_resize(self):
        import cv2

        src_h, src_w = 160, 200
        frame = np.zeros((src_h, src_w, 3), dtype=np.uint8)
        frame[:, :, 0] = np.arange(src_w, dtype=np.uint8)[None, :]
        frame[:, :, 1] = np.arange(src_h, dtype=np.uint8)[:, None]
        frame[:, :, 2] = 128

        bbox = (30, 20, 90, 100)
        left_top_x, left_top_y, right_bottom_x, right_bottom_y = (
            face_liveness._shifted_crop_box(src_w, src_h, bbox, 2.7)
        )
        expected = cv2.resize(
            frame[left_top_y : right_bottom_y + 1, left_top_x : right_bottom_x + 1],
            (80, 80),
        )

        crop = face_liveness._face_crop(frame, bbox)
        self.assertEqual(crop.shape, (80, 80, 3))
        np.testing.assert_array_equal(crop, expected)

    def test_shift_preserves_scale_instead_of_clamping(self):
        # A face at the left edge must be shifted, not truncated, so the
        # requested 2.7x expansion is still sampled.
        box = face_liveness._shifted_crop_box(200, 160, (0, 20, 90, 100), 2.7)
        self.assertEqual(box[0], 0)
        self.assertEqual(box[2] - box[0], 143)

    def test_zero_width_box_is_rejected(self):
        with self.assertRaises(FaceLivenessError):
            face_liveness._face_crop(np.zeros((160, 200, 3), dtype=np.uint8), (20, 20, 0, 0))


class FaceLivenessPreprocessTests(SimpleTestCase):
    """Preprocessing must keep raw [0, 255] BGR values in NCHW layout."""

    def test_tensor_is_nchw_float32_with_raw_pixel_values(self):
        crop = np.zeros((80, 80, 3), dtype=np.uint8)
        crop[:, :, 0] = 255  # blue channel
        crop[:, :, 1] = 128  # green channel
        crop[:, :, 2] = 7  # red channel

        tensor = face_liveness._preprocess(crop)

        self.assertEqual(tensor.shape, (1, 3, 80, 80))
        self.assertEqual(tensor.dtype, np.float32)
        # Raw values are preserved; upstream does NOT divide by 255.
        self.assertAlmostEqual(float(tensor[0, 0].max()), 255.0, places=5)
        self.assertAlmostEqual(float(tensor[0, 1].max()), 128.0, places=5)
        self.assertAlmostEqual(float(tensor[0, 2].max()), 7.0, places=5)
        self.assertAlmostEqual(float(tensor[0, 2].min()), 7.0, places=5)
        # A scaled-to-[0, 1] tensor would max out at 1.0 instead of 255.0.
        self.assertGreater(float(tensor.max()), 1.0)

    def test_tensor_is_contiguous(self):
        crop = np.zeros((80, 80, 3), dtype=np.uint8)
        tensor = face_liveness._preprocess(crop)
        self.assertTrue(tensor.flags["C_CONTIGUOUS"])


class FaceLivenessSoftmaxTests(SimpleTestCase):
    def test_softmax_is_stable_for_large_logits(self):
        logits = np.array([[1000.0, 1001.0, 999.0]], dtype=np.float32)
        probabilities = face_liveness._softmax(logits)
        self.assertTrue(np.all(np.isfinite(probabilities)))
        self.assertAlmostEqual(float(probabilities.sum()), 1.0, places=5)
        self.assertEqual(int(np.argmax(probabilities[0])), 1)

    def test_softmax_matches_naive_for_small_logits(self):
        logits = np.array([[1.0, 2.0, 3.0]], dtype=np.float32)
        expected = np.exp(logits) / np.exp(logits).sum()
        np.testing.assert_allclose(face_liveness._softmax(logits), expected, rtol=1e-6)


class FaceLivenessProviderTests(SimpleTestCase):
    def setUp(self):
        self.frame = np.zeros((160, 200, 3), dtype=np.uint8)
        self.bbox = (30, 20, 90, 100)
        self.session = Mock()
        self.session.get_inputs.return_value = [
            SimpleNamespace(name="input", shape=[1, 3, 80, 80])
        ]
        self.session.get_outputs.return_value = [
            SimpleNamespace(name="output")
        ]

    def _run(self, logits, bbox=_UNSET, **kwargs):
        self.session.run.return_value = [np.array(logits, dtype=np.float32)]
        target = self.bbox if bbox is _UNSET else bbox
        with patch.object(face_liveness, "_get_session", return_value=self.session):
            return check_face_liveness(self.frame, target, **kwargs)

    def test_real_class_index_one_over_threshold_passes(self):
        # Upstream: argmax == 1 is the bona-fide class.
        result = self._run([[6.0, 0.0, -1.0]], minimum_score=0.80)
        self.assertEqual(result["result"], "failed")

        self.session.run.reset_mock()
        result = self._run([[0.0, 6.0, -1.0]], minimum_score=0.80)
        self.assertEqual(result["result"], "passed")
        self.assertGreaterEqual(result["score"], 0.80)
        self.assertEqual(result["model_version"], face_liveness.MODEL_VERSION)
        self.session.run.assert_called_once()

    def test_attack_class_zero_does_not_pass(self):
        result = self._run([[6.0, 0.0, -1.0]], minimum_score=0.80)
        self.assertEqual(result["result"], "failed")
        self.assertLess(result["score"], 0.80)

    def test_attack_class_two_does_not_pass(self):
        result = self._run([[-1.0, 0.0, 6.0]], minimum_score=0.80)
        self.assertEqual(result["result"], "failed")
        self.assertLess(result["score"], 0.80)

    def test_real_class_that_is_not_the_top_score_fails(self):
        # Class 1 has a high probability but is not the highest: upstream uses
        # argmax, so this must not be accepted.
        result = self._run([[0.0, 4.0, 6.0]], minimum_score=0.50)
        self.assertEqual(result["result"], "failed")
        self.assertLess(result["score"], 0.50)

    def test_weak_real_score_fails_closed(self):
        result = self._run([[0.0, 0.5, 0.0]], minimum_score=0.80)
        self.assertEqual(result["result"], "failed")

    def test_score_is_bona_fide_probability(self):
        result = self._run([[0.0, 6.0, -1.0]], minimum_score=0.50)
        expected = float(
            face_liveness._softmax(np.array([[0.0, 6.0, -1.0]], dtype=np.float32))[0, 1]
        )
        self.assertAlmostEqual(result["score"], expected, places=6)

    def test_threshold_is_clamped_to_the_supported_range(self):
        low = self._run([[0.0, 6.0, -1.0]], minimum_score=0.10)
        high = self._run([[0.0, 6.0, -1.0]], minimum_score=2.0)
        self.assertEqual(low["threshold"], 0.50)
        self.assertEqual(high["threshold"], 0.99)

    def test_invalid_minimum_score_is_inconclusive_not_an_exception(self):
        # A bad threshold must fail closed instead of raising out of the
        # provider (regression: float() used to run outside the protected
        # block).
        result = self._run([[0.0, 6.0, -1.0]], minimum_score="not-a-number")
        self.assertEqual(result["result"], "inconclusive")
        self.assertIsNone(result["score"])
        self.assertIsNone(result["threshold"])
        self.assertIn("threshold", result["reason"].lower())

    def test_provider_failure_is_inconclusive_not_passed(self):
        with patch.object(
            face_liveness,
            "_get_session",
            side_effect=FaceLivenessError("simulated missing PAD model"),
        ):
            result = check_face_liveness(self.frame, self.bbox)
        self.assertEqual(result["result"], "inconclusive")
        self.assertIsNone(result["score"])

    def test_inference_exception_is_inconclusive_not_passed(self):
        self.session.run.side_effect = RuntimeError("onnxruntime exploded")
        with patch.object(face_liveness, "_get_session", return_value=self.session):
            result = check_face_liveness(self.frame, self.bbox, minimum_score=0.80)
        self.assertEqual(result["result"], "inconclusive")
        self.assertIsNone(result["score"])

    def test_invalid_face_box_is_inconclusive(self):
        result = self._run([[0.0, 6.0, -1.0]], bbox=(20, 20, 0, 0), minimum_score=0.80)
        self.assertEqual(result["result"], "inconclusive")
        self.assertIsNone(result["score"])

    def test_malformed_bbox_is_inconclusive(self):
        for bbox in (None, (1, 2), "nope", (1, 2, 3, 4, 5)):
            with self.subTest(bbox=bbox):
                result = self._run([[0.0, 6.0, -1.0]], bbox=bbox, minimum_score=0.80)
                self.assertEqual(result["result"], "inconclusive")
                self.assertIsNone(result["score"])

    def test_empty_frame_is_inconclusive(self):
        with patch.object(face_liveness, "_get_session", return_value=self.session):
            result = check_face_liveness(np.zeros((0, 0, 3), dtype=np.uint8), self.bbox)
        self.assertEqual(result["result"], "inconclusive")
        self.assertIsNone(result["score"])

    def test_malformed_model_output_is_inconclusive(self):
        outputs_list = (
            [],
            (),
            [np.array([[1.0, 2.0]], dtype=np.float32)],
            ["not numeric"],
        )
        for outputs in outputs_list:
            with self.subTest(outputs=outputs):
                self.session.run.return_value = outputs
                with patch.object(face_liveness, "_get_session", return_value=self.session):
                    result = check_face_liveness(self.frame, self.bbox, minimum_score=0.80)
                self.assertEqual(result["result"], "inconclusive")
                self.assertIsNone(result["score"])

    def test_non_finite_model_output_is_inconclusive(self):
        for logits in ([0.0, float("nan"), -1.0], [0.0, float("inf"), -1.0]):
            with self.subTest(logits=logits):
                self.session.run.return_value = [np.array([logits], dtype=np.float32)]
                with patch.object(face_liveness, "_get_session", return_value=self.session):
                    result = check_face_liveness(self.frame, self.bbox, minimum_score=0.80)
                self.assertEqual(result["result"], "inconclusive")
                self.assertIsNone(result["score"])


class FaceLivenessStatusTests(SimpleTestCase):
    def test_status_reports_production_validation_as_pending(self):
        with patch.object(
            face_liveness, "_get_session", side_effect=FaceLivenessError("missing")
        ):
            status = face_liveness.get_face_liveness_status()
        self.assertFalse(status["available"])
        self.assertFalse(status["production_validated"])
        self.assertIn("missing", status["error"])

