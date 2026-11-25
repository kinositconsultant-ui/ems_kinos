from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from . import face_liveness
from .face_engine import get_face_engine
from .face_liveness import check_face_liveness

_ORIGINAL_CHECK_FACE_LIVENESS = check_face_liveness

from .face_matching import match_embedding_to_templates


@dataclass
class VerificationOutcome:
    match_result: str
    match_score: float = 0.0
    employee_id: Optional[int] = None
    failure_reason: str = ""
    liveness_result: str = "not_checked"
    liveness_score: Optional[float] = None
    liveness_failure_reason: str = ""
    liveness_model_version: str = ""
    device_id: Optional[int] = None
    schedule_id: Optional[int] = None


def verify_attendance_frame(
    frame: Any,
    *,
    templates: Iterable[Any],
    threshold: float = 0.65,
    top_two_margin: float = 0.05,
    require_liveness: bool = False,
) -> Dict[str, Any]:
    """Detect and match one face; when required, run PAD as a separate gate.

    Identity matching and PAD are distinct checks. A successful identity match never
    overrides a failed/inconclusive PAD result; the view must reject attendance unless
    liveness_result == "passed" when the effective policy requires PAD.
    """
    if frame is None:
        return {
            "match_result": "not_matched",
            "match_score": 0.0,
            "employee_id": None,
            "failure_reason": "No capture was provided.",
            "liveness_result": "not_checked",
        }

    engine = get_face_engine()
    detections = engine.detect_faces(frame)
    if not detections:
        return {
            "match_result": "not_matched",
            "match_score": 0.0,
            "employee_id": None,
            "failure_reason": "No face was detected in the frame.",
            "liveness_result": "not_checked",
        }
    if len(detections) > 1:
        return {
            "match_result": "not_matched",
            "match_score": 0.0,
            "employee_id": None,
            "failure_reason": "Multiple faces were detected; verification requires a single face.",
            "liveness_result": "not_checked",
        }

    detection = detections[0]
    liveness = {
        "result": "not_checked",
        "score": None,
        "reason": "Liveness is not required by the effective policy.",
        "model_version": "",
    }
    if require_liveness:
        # Resolve the PAD entry point dynamically so tests can patch it either
        # where it is defined or where it is used without changing behaviour.
        checker = face_liveness.check_face_liveness
        if checker is _ORIGINAL_CHECK_FACE_LIVENESS:
            module_checker = checker
            global_checker = globals().get("check_face_liveness")
            if global_checker is not _ORIGINAL_CHECK_FACE_LIVENESS:
                checker = global_checker
        liveness = checker(frame, detection.get("bbox"))

    face_row = detection.get("face_row")
    if face_row is None:
        return {
            "match_result": "not_matched",
            "match_score": 0.0,
            "employee_id": None,
            "failure_reason": "The detected face landmarks are missing.",
            "liveness_result": liveness["result"],
            "liveness_score": liveness.get("score"),
            "liveness_failure_reason": liveness.get("reason", ""),
            "liveness_model_version": liveness.get("model_version", ""),
        }

    embedding = engine.extract_embedding_from_face(frame, face_row)
    result = match_embedding_to_templates(
        embedding,
        list(templates),
        threshold=threshold,
        top_two_margin=top_two_margin,
    )
    result["liveness_result"] = liveness["result"]
    result["liveness_score"] = liveness.get("score")
    result["liveness_failure_reason"] = liveness.get("reason", "")
    result["liveness_model_version"] = liveness.get("model_version", "")
    return result
