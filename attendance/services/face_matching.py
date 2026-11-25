from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Sequence

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional dependency is validated elsewhere.
    np = None

from .face_encryption import decrypt_embedding, validate_embedding

logger = logging.getLogger(__name__)


def cosine_similarity(left: Any, right: Any) -> float:
    """Compute cosine similarity between two 1-D embeddings."""
    if np is None:
        raise RuntimeError("numpy is required to compare biometric embeddings.")
    a = validate_embedding(left)
    b = validate_embedding(right)
    if a.shape[0] != b.shape[0]:
        raise ValueError(f"Embedding mismatch: cannot compare vectors of size {a.shape[0]} and {b.shape[0]}.")
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        raise ValueError("Embedding cosine similarity cannot be computed with zero-length vectors.")
    return float(np.dot(a, b) / denom)


def normalize_embedding(embedding: Any) -> "np.ndarray":
    """Return a unit-length embedding for comparison."""
    if np is None:
        raise RuntimeError("numpy is required to normalize biometric embeddings.")
    arr = validate_embedding(embedding)
    norm = float(np.linalg.norm(arr))
    if norm == 0:
        raise ValueError("Embedding norm is zero; it cannot be normalized.")
    return (arr / norm).astype(np.float32)


def _extract_embedding(candidate: Any) -> "np.ndarray":
    if hasattr(candidate, "embedding"):
        return validate_embedding(candidate.embedding)
    if hasattr(candidate, "encrypted_template") and candidate.encrypted_template is not None:
        return decrypt_embedding(candidate.encrypted_template)
    if isinstance(candidate, dict):
        if "encrypted_template" in candidate and candidate["encrypted_template"] is not None:
            return decrypt_embedding(candidate["encrypted_template"])
        if "embedding" in candidate:
            return validate_embedding(candidate["embedding"])
        if "template" in candidate:
            return validate_embedding(candidate["template"])
    if isinstance(candidate, (bytes, bytearray, memoryview)):
        return decrypt_embedding(candidate)
    return validate_embedding(candidate)


def match_embedding_to_templates(
    probe_embedding: Any,
    templates: Sequence[Any],
    *,
    threshold: float = 0.65,
    top_two_margin: float = 0.05,
) -> Dict[str, Any]:
    """Match a probe embedding against a list of candidate templates.

    Results are grouped by employee so that multiple active templates for the
    same employee do not become independent, competing candidates.
    """
    if np is None:
        raise RuntimeError("numpy is required to compare biometric embeddings.")
    if not templates:
        return {
            "match_result": "not_matched",
            "match_score": 0.0,
            "employee_id": None,
            "template_id": None,
            "failure_reason": "No active templates were available for comparison.",
        }

    probe = normalize_embedding(probe_embedding)
    best_by_employee: Dict[Any, Dict[str, Any]] = {}
    skipped_templates = 0

    for index, candidate in enumerate(templates):
        try:
            embedding = normalize_embedding(_extract_embedding(candidate))
        except ValueError:
            # Fail closed for this template only: a template whose ciphertext
            # cannot be decrypted with the currently configured key (or whose
            # plaintext is invalid) must not brick verification for every
            # enrolled employee. It simply cannot be matched.
            skipped_templates += 1
            candidate_employee_id = (
                candidate.get("employee_id") if isinstance(candidate, dict) else getattr(candidate, "employee_id", None)
            )
            logger.warning(
                "Skipping face template %s for employee %s: embedding could not be decrypted or validated with the configured key.",
                index,
                candidate_employee_id,
            )
            continue
        score = float(np.dot(probe, embedding))
        employee_id = candidate.get("employee_id") if isinstance(candidate, dict) else getattr(candidate, "employee_id", None)
        if employee_id is None:
            employee_id = candidate.get("employee") if isinstance(candidate, dict) else getattr(candidate, "employee", None)
        if employee_id is None:
            employee_id = f"template-{index}"

        current = best_by_employee.get(employee_id)
        if current is None or score > current["score"]:
            best_by_employee[employee_id] = {
                "employee_id": employee_id,
                "score": score,
                "template_id": index,
            }

    if not best_by_employee:
        if skipped_templates:
            return {
                "match_result": "not_matched",
                "match_score": 0.0,
                "employee_id": None,
                "template_id": None,
                "failure_reason": "No decryptable templates were available for comparison.",
            }
        return {
            "match_result": "not_matched",
            "match_score": 0.0,
            "employee_id": None,
            "template_id": None,
            "failure_reason": "No active templates were available for comparison.",
        }

    ranked = sorted(best_by_employee.values(), key=lambda item: item["score"], reverse=True)
    best = ranked[0]
    second_score = ranked[1]["score"] if len(ranked) > 1 else -1.0
    if best["score"] < threshold:
        return {
            "match_result": "not_matched",
            "match_score": float(best["score"]),
            "employee_id": best["employee_id"],
            "template_id": best["template_id"],
            "failure_reason": "Top candidate fell below the configured threshold.",
        }
    if best["score"] - second_score < top_two_margin:
        return {
            "match_result": "ambiguous",
            "match_score": float(best["score"]),
            "employee_id": best["employee_id"],
            "template_id": best["template_id"],
            "failure_reason": "The top two candidates are too close to make a confident identity decision.",
        }

    return {
        "match_result": "matched",
        "match_score": float(best["score"]),
        "employee_id": best["employee_id"],
        "template_id": best["template_id"],
        "failure_reason": "",
    }
