import os
from unittest.mock import Mock, patch

import numpy as np
from cryptography.fernet import Fernet
from django.test import SimpleTestCase

from attendance.services.face_encryption import (
    ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY,
    decrypt_embedding,
    encrypt_embedding,
    get_encryption_key,
    validate_embedding,
)
from attendance.services.face_engine import FaceEngine
from attendance.services.face_matching import match_embedding_to_templates

VALID_FERNET_KEY = Fernet.generate_key().decode()


class FaceServiceTests(SimpleTestCase):
    @staticmethod
    def _face_row(x=10.0, confidence=0.91):
        return np.array(
            [x, 20.0, 80.0, 90.0, 20.0, 30.0, 45.0, 40.0, 65.0, 40.0, 80.0, 60.0, 56.0, 70.0, confidence],
            dtype=np.float32,
        )

    def test_missing_encryption_key_raises_clear_error(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertRaises(RuntimeError, get_encryption_key)

    def test_encrypt_and_decrypt_round_trip(self):
        with patch.dict(os.environ, {ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY: VALID_FERNET_KEY}, clear=True):
            payload = np.array([0.1, -0.2, 0.3], dtype=np.float32)
            encrypted = encrypt_embedding(payload)
            decrypted = decrypt_embedding(encrypted)
            np.testing.assert_allclose(decrypted, payload)

    def test_validate_embedding_rejects_nan_values(self):
        with self.assertRaises(ValueError):
            validate_embedding(np.array([1.0, np.nan, 3.0], dtype=np.float32))

    def test_blank_match_with_multiple_templates_for_same_employee(self):
        probe = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        with patch.dict(os.environ, {ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY: VALID_FERNET_KEY}, clear=True):
            templates = [
                {"employee_id": 7, "encrypted_template": encrypt_embedding(np.array([1.0, 0.0, 0.0], dtype=np.float32))},
                {"employee_id": 7, "encrypted_template": encrypt_embedding(np.array([0.99, 0.0, 0.0], dtype=np.float32))},
            ]
            result = match_embedding_to_templates(probe, templates, threshold=0.7)
        self.assertEqual(result["match_result"], "matched")
        self.assertEqual(result["employee_id"], 7)

    def test_detect_faces_handles_opencv_no_face_tuple(self):
        detector = Mock()
        detector.detect.return_value = (1, None)
        engine = FaceEngine()
        engine._detector = detector
        detection = engine.detect_faces(np.zeros((100, 100, 3), dtype=np.uint8))
        self.assertEqual(detection, [])
        detector.setInputSize.assert_called_once_with((100, 100))

    def test_detect_faces_handles_opencv_face_matrix_tuple_and_preserves_score(self):
        detector = Mock()
        detector.detect.return_value = (
            1,
            np.array([self._face_row()], dtype=np.float32),
        )
        engine = FaceEngine()
        engine._detector = detector
        detection = engine.detect_faces(np.zeros((120, 120, 3), dtype=np.uint8))
        self.assertEqual(len(detection), 1)
        self.assertEqual(detection[0]["bbox"][0], 10)
        self.assertAlmostEqual(detection[0]["confidence"], 0.91)
        self.assertIn("face_row", detection[0])
        self.assertEqual(detection[0]["face_row"].shape, (15,))

    def test_detect_faces_handles_empty_detection_matrix(self):
        detector = Mock()
        detector.detect.return_value = (1, np.empty((0, 15), dtype=np.float32))
        engine = FaceEngine()
        engine._detector = detector
        self.assertEqual(engine.detect_faces(np.zeros((100, 100, 3), dtype=np.uint8)), [])

    def test_detect_faces_counts_multiple_rows(self):
        detector = Mock()
        detector.detect.return_value = (
            1,
            np.array([self._face_row(10.0), self._face_row(110.0)], dtype=np.float32),
        )
        engine = FaceEngine()
        engine._detector = detector
        detection = engine.detect_faces(np.zeros((160, 240, 3), dtype=np.uint8))
        self.assertEqual(len(detection), 2)

    def test_detect_faces_rejects_empty_image(self):
        engine = FaceEngine()
        with self.assertRaises(ValueError):
            engine.detect_faces(np.zeros((0, 0, 3), dtype=np.uint8))

    def test_match_skips_undecryptable_template_and_matches_valid_template(self):
        """An undecryptable template must not brick verification for every employee."""
        other_key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY: other_key}, clear=True):
            bad = encrypt_embedding(np.array([1.0, 0.0, 0.0], dtype=np.float32))
        with patch.dict(os.environ, {ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY: VALID_FERNET_KEY}, clear=True):
            good = encrypt_embedding(np.array([1.0, 0.0, 0.0], dtype=np.float32))
            templates = [
                {"employee_id": 7, "encrypted_template": bad},
                {"employee_id": 8, "encrypted_template": good},
            ]
            probe = np.array([1.0, 0.0, 0.0], dtype=np.float32)
            result = match_embedding_to_templates(probe, templates, threshold=0.7)
        self.assertEqual(result["match_result"], "matched")
        self.assertEqual(result["employee_id"], 8)

    def test_malformed_embedding_is_never_accepted_as_a_match(self):
        """An invalid/malformed template embedding must not produce a false match."""
        templates = [
            {"employee_id": 7, "encrypted_template": b"not-a-valid-encrypted-embedding"},
        ]
        probe = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        with patch.dict(os.environ, {ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY: VALID_FERNET_KEY}, clear=True):
            good = encrypt_embedding(np.array([1.0, 0.0, 0.0], dtype=np.float32))
            templates.append({"employee_id": 8, "encrypted_template": good})
            result = match_embedding_to_templates(probe, templates, threshold=0.7)
        self.assertEqual(result["match_result"], "matched")
        self.assertEqual(result["employee_id"], 8)

    def test_all_templates_unreadable_means_no_match_and_no_attendance(self):
        """When no template can be decrypted/validated, verification fails closed."""
        other_key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY: other_key}, clear=True):
            bad = encrypt_embedding(np.array([1.0, 0.0, 0.0], dtype=np.float32))
        templates = [
            {"employee_id": 7, "encrypted_template": bad},
            {"employee_id": 8, "encrypted_template": bad},
        ]
        probe = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        with patch.dict(os.environ, {ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY: VALID_FERNET_KEY}, clear=True):
            result = match_embedding_to_templates(probe, templates, threshold=0.7)
        self.assertEqual(result["match_result"], "not_matched")
        self.assertIsNone(result["employee_id"])
        self.assertIn("No decryptable templates were available for comparison.", result["failure_reason"])

    def test_ambiguous_and_not_matched_behavior_remains_intact(self):
        """Existing match-result conventions are unchanged for valid templates."""
        probe = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        with patch.dict(os.environ, {ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY: VALID_FERNET_KEY}, clear=True):
            templates = [
                {"employee_id": 7, "encrypted_template": encrypt_embedding(np.array([1.0, 0.0, 0.0], dtype=np.float32))},
                {"employee_id": 8, "encrypted_template": encrypt_embedding(np.array([0.0, 1.0, 0.0], dtype=np.float32))},
            ]
            # Distinct embeddings: clear match.
            result = match_embedding_to_templates(probe, templates, threshold=0.7, top_two_margin=0.05)
            self.assertEqual(result["match_result"], "matched")
            self.assertEqual(result["employee_id"], 7)

            # Below-threshold probe: not matched, never a false positive.
            result = match_embedding_to_templates(
                np.array([-1.0, 0.0, 0.0], dtype=np.float32), templates, threshold=0.7, top_two_margin=0.05
            )
            self.assertEqual(result["match_result"], "not_matched")
            self.assertLess(result["match_score"], 0.7)
