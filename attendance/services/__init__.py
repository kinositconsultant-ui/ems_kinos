"""Face-recognition support utilities for the attendance app.

These modules intentionally fail safely when the optional biometric
stack is unavailable. The existing project UI keeps the feature disabled
until a trusted provider is configured, while the service layer exposes
clean APIs for enrollment, verification and template encryption.
"""

from .face_encryption import (
    ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY,
    decrypt_embedding,
    encrypt_embedding,
    get_encryption_key,
    validate_embedding,
)
from .face_engine import (
    FaceEngine,
    FaceEngineError,
    get_face_model_directory,
    get_face_model_status,
)
from .face_matching import (
    cosine_similarity,
    match_embedding_to_templates,
    normalize_embedding,
)

__all__ = [
    "ENV_VAR_FACE_TEMPLATE_ENCRYPTION_KEY",
    "FaceEngine",
    "FaceEngineError",
    "decrypt_embedding",
    "encrypt_embedding",
    "get_encryption_key",
    "get_face_model_directory",
    "get_face_model_status",
    "cosine_similarity",
    "match_embedding_to_templates",
    "normalize_embedding",
    "validate_embedding",
]
