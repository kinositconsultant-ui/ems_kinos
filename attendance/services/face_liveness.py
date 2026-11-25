"""Passive face presentation-attack detection (PAD) using MiniFASNetV2 ONNX.

This is an additional gate, not a certification or guarantee against every attack.
The model file must be obtained separately and placed under models/face_liveness/.

The ONNX export follows the reference Silent-Face-Anti-Spoofing implementation:

* Preprocessing keeps raw BGR ``float32`` values in ``[0, 255]``. The upstream
  ``to_tensor`` helper returns ``img.float()``; its ``div(255)`` call is
  commented out, so the model must not be fed values scaled to ``[0, 1]``.
* The face is cropped with the upstream boundary-adjusted *shifted* box
  (``generate_patches.py::_get_new_box``) using the 2.7x scale that matches the
  ``2.7_80x80_MiniFASNetV2`` checkpoint, then resized to 80x80.
* The network emits three raw logits; the softmax is applied here. Class index
  ``1`` is the bona-fide ("real face") class and every other index is a
  presentation attack, matching upstream ``test.py``.

The detection threshold is a provisional development value and must be
calibrated on representative cameras and presentation attacks before use.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

try:
    import numpy as np
except ImportError:  # pragma: no cover - runtime configuration is reported by status()
    np = None

try:
    import cv2
except ImportError:  # pragma: no cover - runtime configuration is reported by status()
    cv2 = None

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover - runtime configuration is reported by status()
    ort = None

MODEL_DIRECTORY = Path(__file__).resolve().parents[2] / "models" / "face_liveness"
MODEL_FILENAME = "minifasnet_v2.onnx"
MODEL_PATH = MODEL_DIRECTORY / MODEL_FILENAME
MODEL_VERSION = "MiniFASNetV2-ONNX"
DEFAULT_INPUT_SIZE = (80, 80)
DEFAULT_CROP_SCALE = 2.7

# Upstream ``test.py`` treats ``argmax(prediction) == 1`` as a real face and every
# other index as a fake face, so the bona-fide class is index 1 (index 0 and
# index 2 are both presentation attacks).
CLASS_COUNT = 3
REAL_CLASS_INDEX = 1

_SESSION = None
_SESSION_LOCK = threading.RLock()
_INFERENCE_LOCK = threading.RLock()


def _minimum_live_score() -> float:
    """Initial deployment threshold; calibrate it with local bona-fide/attack samples."""
    raw = os.environ.get("FACE_LIVENESS_MIN_SCORE", "0.80")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.80
    # Avoid nonsensical deployment configuration.
    return min(0.99, max(0.50, value))


class FaceLivenessError(RuntimeError):
    """Raised when the PAD model cannot safely evaluate an image."""


def _resolve_threshold(minimum_score: float | None) -> float:
    """Clamp the requested threshold into the supported range.

    An unusable threshold is a configuration error, so it is reported as a
    provider failure (fail closed) instead of raising a bare ``ValueError``.
    """
    if minimum_score is None:
        return _minimum_live_score()
    try:
        value = float(minimum_score)
    except (TypeError, ValueError) as exc:
        raise FaceLivenessError(
            f"The configured liveness threshold is invalid: {exc}"
        ) from exc
    return min(0.99, max(0.50, value))


def _get_session():
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            return _SESSION
        if cv2 is None or np is None:
            raise FaceLivenessError("OpenCV and NumPy are required for face liveness detection.")
        if ort is None:
            raise FaceLivenessError("onnxruntime is not installed in the active Python environment.")
        if not MODEL_PATH.is_file():
            raise FaceLivenessError(
                f"PAD model is missing. Place {MODEL_FILENAME} in {MODEL_DIRECTORY}."
            )
        try:
            _SESSION = ort.InferenceSession(
                str(MODEL_PATH),
                providers=["CPUExecutionProvider"],
            )
            if not _SESSION.get_inputs() or not _SESSION.get_outputs():
                _SESSION = None
                raise FaceLivenessError("PAD model has no valid ONNX input or output.")
        except FaceLivenessError:
            raise
        except Exception as exc:
            _SESSION = None
            raise FaceLivenessError(f"PAD model failed to initialize: {exc}") from exc
        return _SESSION


def get_face_liveness_status() -> dict[str, Any]:
    """Return truthful readiness status. Available means loadable, not certified."""
    status: dict[str, Any] = {
        "available": False,
        "provider": MODEL_VERSION,
        "model_path": str(MODEL_PATH),
        "error": "",
        "threshold": _minimum_live_score(),
        "production_validated": False,
    }
    try:
        _get_session()
        status["available"] = True
        status["error"] = ""
    except FaceLivenessError as exc:
        status["error"] = str(exc)
    return status


def _shifted_crop_box(
    src_w: int,
    src_h: int,
    bbox: tuple[int, int, int, int],
    scale: float,
):
    """Port of upstream ``generate_patches.py::CropImage._get_new_box``.

    Upstream *shifts* the expanded box back inside the frame instead of clamping
    it, so the requested 2.7x scale is preserved and only the sampled region
    moves. The returned bounds are inclusive, matching the upstream slice.
    """
    x, y, box_w, box_h = bbox

    scale = min((src_h - 1) / box_h, min((src_w - 1) / box_w, scale))

    new_width = box_w * scale
    new_height = box_h * scale
    center_x, center_y = box_w / 2 + x, box_h / 2 + y

    left_top_x = center_x - new_width / 2
    left_top_y = center_y - new_height / 2
    right_bottom_x = center_x + new_width / 2
    right_bottom_y = center_y + new_height / 2

    if left_top_x < 0:
        right_bottom_x -= left_top_x
        left_top_x = 0

    if left_top_y < 0:
        right_bottom_y -= left_top_y
        left_top_y = 0

    if right_bottom_x > src_w - 1:
        left_top_x -= right_bottom_x - src_w + 1
        right_bottom_x = src_w - 1

    if right_bottom_y > src_h - 1:
        left_top_y -= right_bottom_y - src_h + 1
        right_bottom_y = src_h - 1

    left_top_x = int(left_top_x)
    left_top_y = int(left_top_y)
    right_bottom_x = int(right_bottom_x)
    right_bottom_y = int(right_bottom_y)

    # Upstream slices with these bounds directly. Negative bounds would wrap
    # around in NumPy and inverted bounds would yield an empty crop, so both are
    # rejected instead of being silently misinterpreted.
    if left_top_x < 0 or left_top_y < 0:
        raise FaceLivenessError("The detected face region cannot be cropped from the captured image.")
    if right_bottom_x < left_top_x or right_bottom_y < left_top_y:
        raise FaceLivenessError("The detected face region cannot be cropped from the captured image.")
    return left_top_x, left_top_y, right_bottom_x, right_bottom_y


def _face_crop(frame: Any, bbox: Any, *, scale: float = DEFAULT_CROP_SCALE):
    """Crop and resize a detected face exactly like the upstream reference inference."""
    if cv2 is None or np is None:
        raise FaceLivenessError("OpenCV and NumPy are required for face liveness detection.")
    if frame is None or not hasattr(frame, "shape") or getattr(frame, "size", 0) == 0:
        raise FaceLivenessError("No valid camera frame was supplied for liveness checking.")
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise FaceLivenessError("The detected face bounding box is invalid for liveness checking.")

    try:
        x, y, width, height = (int(value) for value in bbox)
    except (TypeError, ValueError) as exc:
        raise FaceLivenessError("The detected face bounding box is invalid for liveness checking.") from exc

    src_h, src_w = frame.shape[:2]
    if width <= 0 or height <= 0 or src_h < 2 or src_w < 2:
        raise FaceLivenessError("The detected face region is too small for liveness checking.")

    left_top_x, left_top_y, right_bottom_x, right_bottom_y = _shifted_crop_box(
        src_w, src_h, (x, y, width, height), scale
    )

    face = frame[left_top_y : right_bottom_y + 1, left_top_x : right_bottom_x + 1]
    if face.size == 0:
        raise FaceLivenessError("The cropped face image is empty.")

    return cv2.resize(face, DEFAULT_INPUT_SIZE, interpolation=cv2.INTER_LINEAR)


def _preprocess(crop: Any):
    """Build the NCHW float32 tensor expected by the ONNX export.

    Upstream ``data_io_functional.py::to_tensor`` returns ``img.float()`` and
    keeps its ``div(255)`` call commented out, so raw BGR values in ``[0, 255]``
    are preserved. Scaling to ``[0, 1]`` here would change the model input
    distribution and invalidate the upstream decision boundary.
    """
    if np is None:
        raise FaceLivenessError("NumPy is required for face liveness detection.")
    tensor = np.transpose(crop.astype(np.float32), (2, 0, 1))[None, ...]
    return np.ascontiguousarray(tensor, dtype=np.float32)


def _validate_model_output(outputs: Any):
    """Validate the raw ONNX output before interpreting it (fail closed)."""
    if np is None:
        raise FaceLivenessError("NumPy is required for face liveness detection.")
    if not isinstance(outputs, (list, tuple)) or not outputs:
        raise FaceLivenessError("PAD model returned no output.")
    try:
        logits = np.asarray(outputs[0], dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise FaceLivenessError("PAD model returned an output that is not numeric.") from exc
    if logits.ndim == 1 and logits.shape[0] == CLASS_COUNT:
        logits = logits.reshape(1, CLASS_COUNT)
    elif logits.shape != (1, CLASS_COUNT):
        raise FaceLivenessError(
            f"PAD model must return exactly {CLASS_COUNT} logits for one sample."
        )
    if not np.all(np.isfinite(logits)):
        raise FaceLivenessError("PAD model returned non-finite logits.")
    return logits


def _softmax(logits: Any):
    """Numerically stable softmax over the class axis."""
    shifted = logits - np.max(logits, axis=1, keepdims=True)
    exponentials = np.exp(shifted)
    return exponentials / np.sum(exponentials, axis=1, keepdims=True)


def check_face_liveness(
    frame: Any,
    bbox: Any,
    *,
    minimum_score: float | None = None,
) -> dict[str, Any]:
    """Evaluate one captured frame and return passed/failed/inconclusive plus score.

    The ONNX export emits three raw logits for the classes used by the upstream
    MiniFASNetV2 checkpoint, where index 1 is the bona-fide ("real face") class
    and indices 0 and 2 are presentation attacks. The softmax is applied to the
    logits and the bona-fide probability is used as the score.

    The threshold is a provisional development setting and must be calibrated on
    representative cameras and presentation attacks before deployment.
    """
    threshold: float | None = None
    try:
        threshold = _resolve_threshold(minimum_score)
        session = _get_session()
        crop = _face_crop(frame, bbox)
        tensor = _preprocess(crop)
        input_name = session.get_inputs()[0].name
        output_name = session.get_outputs()[0].name
        with _INFERENCE_LOCK:
            outputs = session.run([output_name], {input_name: tensor})
        logits = _validate_model_output(outputs)

        probabilities = _softmax(logits)
        predicted_class = int(np.argmax(probabilities[0]))
        live_score = float(probabilities[0, REAL_CLASS_INDEX])

        if predicted_class == REAL_CLASS_INDEX and live_score >= threshold:
            return {
                "result": "passed",
                "score": live_score,
                "reason": "PAD model classified the face frame as bona fide (experimental single-frame check).",
                "model_version": MODEL_VERSION,
                "threshold": threshold,
            }

        return {
            "result": "failed",
            "score": live_score,
            "reason": "PAD model did not classify this frame as bona fide above the configured threshold.",
            "model_version": MODEL_VERSION,
            "threshold": threshold,
        }
    except Exception as exc:
        # Fail closed: provider errors never turn into a pass.
        return {
            "result": "inconclusive",
            "score": None,
            "reason": str(exc)[:255],
            "model_version": MODEL_VERSION,
            "threshold": threshold,
        }
