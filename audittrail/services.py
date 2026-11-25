from django.core.exceptions import ValidationError
from django.db import models

from .models import AuditLog


SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "credential",
    "encrypted",
    "biometric",
    "template",
    "authorization",
)


def _sanitize_snapshot(value):
    if isinstance(value, dict):
        return {
            key: _sanitize_snapshot(item)
            for key, item in value.items()
            if not any(part in str(key).casefold() for part in SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_snapshot(item) for item in value]
    if isinstance(value, models.Model):
        return str(value.pk)
    return value


def log_action(
    *,
    actor_user=None,
    office=None,
    action,
    entity_type,
    object_id="",
    before_data=None,
    after_data=None,
    reason="",
    ip_address=None,
):
    if entity_type.casefold() == "auditlog":
        raise ValidationError("Audit log entries cannot recursively audit themselves.")
    entry = AuditLog(
        actor_user=actor_user,
        office=office,
        action=action,
        entity_type=entity_type,
        object_id=str(object_id),
        before_data=_sanitize_snapshot(before_data or {}),
        after_data=_sanitize_snapshot(after_data or {}),
        reason=reason,
        ip_address=ip_address,
    )
    entry.full_clean()
    entry.save()
    return entry
