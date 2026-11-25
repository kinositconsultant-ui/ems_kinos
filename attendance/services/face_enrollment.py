from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional dependency is validated elsewhere.
    np = None

from .face_engine import FaceEngine
from .face_encryption import encrypt_embedding, validate_embedding


class EnrollmentValidationError(ValueError):
    """Raised when a captured face sample fails enrollment quality checks."""


@dataclass
class EnrollmentSample:
    frame: Any
    face_count: int = 0
    face_quality: float = 0.0
    is_valid: bool = False
    embedding: Optional[Any] = None
    failure_reason: str = ""


def validate_enrollment_sample(frame: Any, *, min_face_count: int = 1, min_confidence: float = 0.45) -> EnrollmentSample:
    """Validate that the captured frame contains a single, usable face sample."""
    if np is None:
        raise RuntimeError("numpy is required for face enrollment validation.")
    if frame is None:
        raise EnrollmentValidationError("No frame was captured.")
    if not hasattr(frame, "shape"):
        raise EnrollmentValidationError("The captured sample is not a valid image array.")
    if frame.size == 0:
        raise EnrollmentValidationError("The captured sample image is empty.")

    engine = FaceEngine()
    if not engine.is_available():
        raise EnrollmentValidationError("Face recognition support is not configured in this deployment.")

    detections = engine.detect_faces(frame)
    if not detections:
        raise EnrollmentValidationError("No face was detected in the sample image.")
    if len(detections) > min_face_count:
        raise EnrollmentValidationError("Multiple faces were detected; enrollment requires a single face.")

    face = detections[0]
    confidence = float(face.get("confidence", 0.0))
    if confidence < min_confidence:
        raise EnrollmentValidationError("The detected face is too low quality for enrollment.")

    bbox = face.get("bbox", (0, 0, 0, 0))
    width = bbox[2] if len(bbox) > 2 else 0
    height = bbox[3] if len(bbox) > 3 else 0
    if width <= 0 or height <= 0:
        raise EnrollmentValidationError("The detected face bounding box was invalid.")
    if "face_row" not in face or face["face_row"] is None:
        raise EnrollmentValidationError("The detected face row is incomplete; landmarks are required for alignment.")

    return EnrollmentSample(
        frame=frame,
        face_count=len(detections),
        face_quality=confidence,
        is_valid=True,
        embedding=None,
        failure_reason="",
    )


def build_enrollment_embedding(frame: Any, *, min_confidence: float = 0.45) -> Any:
    """Return a real embedding from a captured frame using the configured engine."""
    validate_enrollment_sample(frame, min_confidence=min_confidence)
    engine = FaceEngine()
    engine.load()
    detections = engine.detect_faces(frame)
    if not detections:
        raise EnrollmentValidationError("No face was detected in the sample image.")
    if len(detections) > 1:
        raise EnrollmentValidationError("Multiple faces were detected; enrollment requires a single face.")

    face = detections[0]
    face_row = face.get("face_row")
    if face_row is None:
        raise EnrollmentValidationError("The detected face row is incomplete; landmarks are required for alignment.")
    embedding = engine.extract_embedding_from_face(frame, face_row)
    return validate_embedding(embedding)


def encrypt_and_store_embedding(embedding: Any) -> bytes:
    """Encrypt a valid embedding before persisting to the database."""
    return encrypt_embedding(embedding)


def check_enrollment_risk(candidate_embedding: Any, existing_embeddings: list[Any], *, similarity_threshold: float = 0.92) -> bool:
    """Return True when a new embedding is too similar to another active employee template."""
    if not existing_embeddings:
        return False

    try:
        from .face_matching import cosine_similarity

        probe = validate_embedding(candidate_embedding)
        for existing in existing_embeddings:
            prior = validate_embedding(existing)
            if probe.shape[0] != prior.shape[0]:
                continue
            if cosine_similarity(probe, prior) >= similarity_threshold:
                return True
    except Exception:
        return True
    return False
