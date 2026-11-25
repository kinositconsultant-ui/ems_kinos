from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import cv2
except ImportError:  # pragma: no cover - optional dependency is validated elsewhere.
    cv2 = None

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional dependency is validated elsewhere.
    np = None


MODEL_DIRECTORY = Path(__file__).resolve().parents[2] / "models" / "face_recognition"
DEFAULT_DETECTOR_NAME = "face_detection_yunet_2023mar.onnx"
DEFAULT_RECOGNIZER_NAME = "face_recognition_sface_2021dec.onnx"


class FaceEngineError(RuntimeError):
    """Raised when OpenCV face-recognition support is not available."""


def get_face_model_directory() -> Path:
    MODEL_DIRECTORY.mkdir(parents=True, exist_ok=True)
    return MODEL_DIRECTORY


def _model_path(name: str) -> Path:
    return get_face_model_directory() / name


def get_face_model_status() -> Dict[str, Any]:
    status = {
        "available": False,
        "model_directory": str(get_face_model_directory()),
        "detector_path": str(_model_path(DEFAULT_DETECTOR_NAME)),
        "recognizer_path": str(_model_path(DEFAULT_RECOGNIZER_NAME)),
        "detector_exists": _model_path(DEFAULT_DETECTOR_NAME).exists(),
        "recognizer_exists": _model_path(DEFAULT_RECOGNIZER_NAME).exists(),
        "cv2_available": cv2 is not None,
        "numpy_available": np is not None,
        "error": None,
    }
    if not status["cv2_available"] or not status["numpy_available"]:
        status["error"] = "OpenCV and NumPy are required for face recognition."
        return status
    if not status["detector_exists"] or not status["recognizer_exists"]:
        status["error"] = "Model files are missing in the face-recognition model directory."
        return status

    try:
        cv2.FaceDetectorYN.create(status["detector_path"], "", (320, 320))
        cv2.FaceRecognizerSF.create(status["recognizer_path"], "")
    except cv2.error as exc:  # pragma: no cover - verified at runtime against the installed OpenCV build.
        status["available"] = False
        status["error"] = f"OpenCV model initialization failed: {exc}"
        return status

    status["available"] = True
    return status


class FaceEngine:
    """Lazy singleton for OpenCV YuNet + SFace support."""

    _instance: Optional["FaceEngine"] = None
    _detector = None
    _recognizer = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, detector_path: str | None = None, recognizer_path: str | None = None):
        self.detector_path = detector_path or str(_model_path(DEFAULT_DETECTOR_NAME))
        self.recognizer_path = recognizer_path or str(_model_path(DEFAULT_RECOGNIZER_NAME))
        self._status = get_face_model_status()

    @property
    def status(self) -> Dict[str, Any]:
        return get_face_model_status()

    def is_available(self) -> bool:
        return self.status["available"]

    def load(self) -> "FaceEngine":
        if cv2 is None or np is None:
            raise FaceEngineError("OpenCV and NumPy are required for face recognition.")
        if not (Path(self.detector_path).exists() and Path(self.recognizer_path).exists()):
            raise FaceEngineError(
                "Face-recognition model files are missing. Place YuNet and SFace ONNX files in the model directory."
            )
        if self._detector is None:
            try:
                self._detector = cv2.FaceDetectorYN.create(self.detector_path, "", (320, 320))
            except cv2.error as exc:
                raise FaceEngineError(f"YuNet model failed to initialize: {exc}") from exc
        if self._recognizer is None:
            try:
                self._recognizer = cv2.FaceRecognizerSF.create(self.recognizer_path, "")
            except cv2.error as exc:
                raise FaceEngineError(f"SFace model failed to initialize: {exc}") from exc
        return self

    def detect_faces(self, image: Any) -> List[Dict[str, Any]]:
        if image is None:
            raise ValueError("No image frame was provided for face detection.")
        if cv2 is None:
            raise FaceEngineError("OpenCV is required for face detection.")
        if self._detector is None:
            self.load()
        if not hasattr(image, "shape"):
            raise TypeError("The input image must be a NumPy array or OpenCV image object.")

        frame = image
        if len(frame.shape) == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        if frame.size == 0:
            raise ValueError("The input image is empty and cannot be processed for face detection.")

        height, width = frame.shape[:2]
        self._detector.setInputSize((width, height))
        detection_result = self._detector.detect(frame)
        results: List[Dict[str, Any]] = []
        if detection_result is None:
            return results

        if isinstance(detection_result, tuple):
            if len(detection_result) != 2:
                return results
            _retval, faces = detection_result
        else:
            faces = detection_result

        if faces is None:
            return results

        try:
            raw_faces = np.asarray(faces, dtype=np.float32)
        except (TypeError, ValueError):
            return results

        if raw_faces.size == 0:
            return results
        if raw_faces.ndim == 1:
            if raw_faces.size < 15:
                return results
            raw_faces = raw_faces.reshape(1, -1)
        if raw_faces.ndim != 2 or raw_faces.shape[1] < 15:
            return results

        for face in raw_faces:
            if face is None:
                continue
            values = np.asarray(face, dtype=np.float32).reshape(-1)
            if values.size < 15 or not np.all(np.isfinite(values[:15])):
                continue
            x, y, w, h = [float(value) for value in values[:4]]
            confidence = float(values[14])
            if w <= 0 or h <= 0 or confidence < 0 or confidence > 1:
                continue
            results.append(
                {
                    "bbox": (int(round(x)), int(round(y)), max(1, int(round(w))), max(1, int(round(h)))),
                    "confidence": confidence,
                    "face_row": values.copy(),
                }
            )
        return results

    def extract_embedding_from_face(self, image: Any, face_row: Any) -> np.ndarray:
        if self._recognizer is None:
            self.load()
        if image is None:
            raise ValueError("No image was provided for face alignment.")
        if face_row is None:
            raise ValueError("No detected face row was provided for alignment.")

        row = np.asarray(face_row, dtype=np.float32).reshape(-1)
        if row.size < 4 or not np.all(np.isfinite(row[:4])):
            raise FaceEngineError("The detected face row is incomplete or invalid for alignment.")

        try:
            aligned = self._recognizer.alignCrop(image, row.reshape(1, -1))
        except TypeError:
            aligned = self._recognizer.alignCrop(image, row)
        except cv2.error as exc:  # pragma: no cover - validated against OpenCV runtime.
            raise FaceEngineError(f"Face alignment failed: {exc}") from exc

        if aligned is None:
            raise FaceEngineError("Face alignment produced no image data.")
        if np.asarray(aligned).size == 0:
            raise FaceEngineError("Aligned face crop is empty.")

        embedding = self._recognizer.feature(aligned)
        if embedding is None:
            raise FaceEngineError("FaceRecognizerSF failed to extract a valid embedding.")
        emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if emb.size == 0 or not np.all(np.isfinite(emb)):
            raise FaceEngineError("Extracted embedding is empty or invalid.")
        return emb.astype(np.float32)

    def extract_embedding(self, aligned_face: Any) -> np.ndarray:
        if self._recognizer is None:
            self.load()
        if aligned_face is None:
            raise ValueError("No aligned face image was provided.")
        embedding = self._recognizer.feature(aligned_face)
        if embedding is None:
            raise FaceEngineError("FaceRecognizerSF failed to extract a valid embedding.")
        emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if emb.size == 0 or not np.all(np.isfinite(emb)):
            raise FaceEngineError("Extracted embedding is empty or invalid.")
        return emb.astype(np.float32)


def get_face_engine() -> FaceEngine:
    engine = FaceEngine()
    status = engine.status
    if not status["available"]:
        if status.get("error"):
            raise FaceEngineError(status["error"])
        raise FaceEngineError("Face-recognition engine is not configured. Place the official YuNet and SFace model files in models/face_recognition/.")
    return engine.load()
