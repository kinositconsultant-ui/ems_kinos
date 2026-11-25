"""Upload validation for employee certificate documents.

The file name, extension and browser-supplied content type are never trusted on
their own: the leading bytes (PDF) or a full Pillow decode (images) must agree
with the extension.
"""
import os

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile

from PIL import Image, UnidentifiedImageError

MAX_CERTIFICATE_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_CERTIFICATE_EXTENSIONS = {
    ".pdf": "PDF",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}
ALLOWED_CERTIFICATE_LABEL = "PDF, JPG, PNG or WEBP"
ACCEPT_ATTRIBUTE = ".pdf,.jpg,.jpeg,.png,.webp,application/pdf,image/jpeg,image/png,image/webp"


def validate_certificate_file(upload):
    """Validate a *new* upload. Already-stored files (not UploadedFile) are skipped."""
    if not upload or not isinstance(upload, UploadedFile):
        return upload

    if upload.size > MAX_CERTIFICATE_FILE_SIZE:
        raise ValidationError("The document must not be larger than 10 MB.")
    if upload.size == 0:
        raise ValidationError("The uploaded document is empty.")

    extension = os.path.splitext(upload.name or "")[1].lower()
    expected_format = ALLOWED_CERTIFICATE_EXTENSIONS.get(extension)
    if not expected_format:
        raise ValidationError(f"Unsupported file type. Upload a {ALLOWED_CERTIFICATE_LABEL} file.")

    upload.seek(0)
    if expected_format == "PDF":
        if upload.read(5) != b"%PDF-":
            raise ValidationError("The file content is not a valid PDF document.")
    else:
        try:
            image = Image.open(upload)
            detected = (image.format or "").upper()
            image.verify()
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
            raise ValidationError("The file content is not a valid image.")
        if detected != expected_format:
            raise ValidationError("The file content does not match its file extension.")
    upload.seek(0)
    return upload
