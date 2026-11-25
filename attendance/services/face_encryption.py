from __future__ import annotations

import os
from typing import Any, Iterable

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional dependency is validated elsewhere.
    np = None

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - optional dependency is validated elsewhere.
    Fernet = None
    InvalidToken = ValueError

ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY = "FACE_TEMPLATE_ENCRYPTION_KEY"


def get_encryption_key() -> str:
    """Return the configured Fernet key, or raise a clear, safe exception."""
    key = os.environ.get(ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY)
    if not key:
        raise RuntimeError(
            f"Missing {ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY}; create a Fernet key and export it in the environment."
        )
    return key


def _fernet() -> Fernet:
    if Fernet is None:
        raise RuntimeError("cryptography is required for encrypted biometric templates.")
    return Fernet(get_encryption_key())


def validate_embedding(embedding: Any, *, expected_dim: int | None = None) -> "np.ndarray":
    """Validate and normalize an embedding vector into a 1-D float32 array."""
    if np is None:
        raise RuntimeError("numpy is required to validate biometric embeddings.")

    arr = np.asarray(embedding, dtype=np.float32)
    if arr.size == 0:
        raise ValueError("Embedding is empty.")
    arr = arr.reshape(-1)
    if not np.all(np.isfinite(arr)):
        raise ValueError("Embedding contains NaN or infinite values.")
    if expected_dim is not None and arr.shape[0] != expected_dim:
        raise ValueError(f"Embedding dimension mismatch: expected {expected_dim}, got {arr.shape[0]}.")
    return arr.astype(np.float32)


def encrypt_embedding(embedding: Any) -> bytes:
    """Encrypt a biometric embedding using the configured Fernet key."""
    arr = validate_embedding(embedding)
    payload = np.ascontiguousarray(arr, dtype=np.float32).tobytes()
    return _fernet().encrypt(payload)


def decrypt_embedding(ciphertext: bytes | bytearray | memoryview) -> "np.ndarray":
    """Decrypt a stored embedding and return a validated float32 array."""
    if ciphertext is None:
        raise ValueError("No encrypted template payload was provided.")
    try:
        payload = _fernet().decrypt(bytes(ciphertext))
    except InvalidToken as exc:  # pragma: no cover - behaviour is validated with tests.
        raise ValueError("Encrypted template is invalid or has been tampered with.") from exc

    if np is None:
        raise RuntimeError("numpy is required to decrypt biometric embeddings.")
    arr = np.frombuffer(payload, dtype=np.float32)
    if arr.size == 0:
        raise ValueError("Decrypted embedding is empty.")
    if not np.all(np.isfinite(arr)):
        raise ValueError("Decrypted embedding contains invalid floating-point values.")
    return arr.astype(np.float32)


def normalize_vector(values: Iterable[float]) -> "np.ndarray":
    if np is None:
        raise RuntimeError("numpy is required to normalize biometric embeddings.")
    arr = np.asarray(list(values), dtype=np.float32).reshape(-1)
    if arr.size == 0:
        raise ValueError("Embedding normalization requires at least one value.")
    if not np.all(np.isfinite(arr)):
        raise ValueError("Embedding contains NaN or infinite values.")
    norm = np.linalg.norm(arr)
    if norm == 0:
        raise ValueError("Embedding norm is zero; it cannot be normalized.")
    return (arr / norm).astype(np.float32)
