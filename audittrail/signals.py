from django.contrib.auth import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from audittrail.models import AuditLog
from audittrail.services import log_action


@receiver(user_logged_in)
def audit_user_login(sender, request, user, **kwargs):
    log_action(
        actor_user=user,
        action=AuditLog.Action.LOGIN,
        entity_type="User",
        object_id=user.pk,
        after_data={"username": user.username},
        ip_address=request.META.get("REMOTE_ADDR"),
    )


@receiver(user_logged_out)
def audit_user_logout(sender, request, user, **kwargs):
    if user and user.is_authenticated:
        log_action(
            actor_user=user,
            action=AuditLog.Action.LOGOUT,
            entity_type="User",
            object_id=user.pk,
            before_data={"username": user.username},
            ip_address=request.META.get("REMOTE_ADDR"),
        )


@receiver(user_login_failed)
def audit_user_login_failed(sender, credentials, request, **kwargs):
    username = credentials.get("username", "") if isinstance(credentials, dict) else ""
    log_action(
        actor_user=None,
        action=AuditLog.Action.LOGIN,
        entity_type="User",
        object_id=username,
        after_data={"username": username, "failed": True},
        reason="Failed login attempt",
        ip_address=request.META.get("REMOTE_ADDR") if request else None,
    )