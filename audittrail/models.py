from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    class Action(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"
        LOGIN = "login", "Login"
        LOGOUT = "logout", "Logout"
        APPROVE = "approve", "Approve"
        REJECT = "reject", "Reject"
        CORRECTION = "correction", "Correction"
        ENROLL_FACE = "enroll_face", "Face enrollment"
        REVOKE_FACE = "revoke_face", "Revoke face template"
        OTHER = "other", "Other"

    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_events"
    )
    office = models.ForeignKey(
        "organization.Office", on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs"
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    entity_type = models.CharField(max_length=100, help_text="Model/entity name affected by the action.")
    object_id = models.CharField(max_length=100, blank=True, help_text="String ID of the affected record.")
    before_data = models.JSONField(default=dict, blank=True)
    after_data = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["entity_type", "object_id"]),
            models.Index(fields=["office", "created_at"]),
            models.Index(fields=["action", "created_at"]),
        ]

    def __str__(self):
        return f"{self.action} — {self.entity_type} #{self.object_id}"
