from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .models import AuditLog
from .services import log_action


class AuditTrailTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="audit-admin",
            email="audit-admin@example.com",
            password="test-password",
        )

    def test_logger_sanitizes_sensitive_snapshot_keys(self):
        entry = log_action(
            actor_user=self.user,
            action=AuditLog.Action.UPDATE,
            entity_type="AttendanceDevice",
            object_id=17,
            before_data={
                "status": "offline",
                "credential_hash": "sensitive-hash",
                "metadata": {"api_token": "sensitive-token", "location": "Lobby"},
            },
            after_data={"status": "active", "encrypted_template": "biometric-secret"},
        )

        self.assertEqual(
            entry.before_data,
            {"status": "offline", "metadata": {"location": "Lobby"}},
        )
        self.assertEqual(entry.after_data, {"status": "active"})

    def test_audit_list_does_not_expose_snapshot_values(self):
        log_action(
            actor_user=self.user,
            action=AuditLog.Action.UPDATE,
            entity_type="AttendanceDevice",
            object_id="DVC-1",
            before_data={"credential_hash": "do-not-render-this"},
            after_data={"encrypted_template": "private-biometric-data"},
            reason="Device status update",
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("audittrail:audit-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Device status update")
        self.assertNotContains(response, "do-not-render-this")
        self.assertNotContains(response, "private-biometric-data")

    def test_audit_list_is_permission_protected(self):
        user = get_user_model().objects.create_user(
            username="no-audit-permission",
            password="test-password",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("audittrail:audit-list"))

        self.assertEqual(response.status_code, 403)

    def test_audit_logger_rejects_recursive_audit_entries(self):
        with self.assertRaisesMessage(ValidationError, "cannot recursively audit"):
            log_action(
                actor_user=self.user,
                action=AuditLog.Action.CREATE,
                entity_type="AuditLog",
            )
