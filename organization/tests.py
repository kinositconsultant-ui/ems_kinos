from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

from audittrail.models import AuditLog

from .models import Department, Office, Position


class OrganizationPageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="organization-admin",
            email="organization-admin@example.com",
            password="test-password",
        )
        self.client.force_login(self.user)

    def test_office_list_search_and_pagination(self):
        Office.objects.create(office_code="DIL", office_name="Dili Office")
        Office.objects.create(office_code="BAU", office_name="Baucau Office")

        response = self.client.get(reverse("organization:office-list"), {"q": "Dili"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dili Office")
        self.assertNotContains(response, "Baucau Office")
        self.assertEqual(response.context["page_obj"].paginator.count, 1)

    def test_office_create_saves_valid_form(self):
        response = self.client.post(
            reverse("organization:office-create"),
            {
                "office_code": "DIL",
                "office_name": "Dili Office",
                "office_type": Office.OfficeType.HEADQUARTERS,
                "municipality": "Dili",
                "administrative_post": "",
                "suco": "",
                "aldeia": "",
                "address": "",
                "phone": "",
                "email": "",
                "latitude": "",
                "longitude": "",
                "timezone": "Asia/Dili",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("organization:office-list"))
        self.assertTrue(Office.objects.filter(office_code="DIL").exists())

    def test_invalid_department_parent_office_is_rejected(self):
        office_a = Office.objects.create(office_code="DIL", office_name="Dili Office")
        office_b = Office.objects.create(office_code="BAU", office_name="Baucau Office")
        parent = Department.objects.create(
            office=office_a,
            department_code="HR",
            department_name="Human Resources",
        )

        response = self.client.post(
            reverse("organization:department-create"),
            {
                "office": office_b.pk,
                "parent_department": parent.pk,
                "department_code": "FIN",
                "department_name": "Finance",
                "description": "",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Parent department must belong to the selected office.")
        self.assertFalse(Department.objects.filter(department_code="FIN").exists())

    def test_missing_office_detail_returns_404(self):
        response = self.client.get(
            reverse("organization:office-detail", kwargs={"pk": 9999})
        )

        self.assertEqual(response.status_code, 404)

    def test_user_without_permission_cannot_view_offices(self):
        user = get_user_model().objects.create_user(
            username="read-only-no-permission",
            password="test-password",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("organization:office-list"))

        self.assertEqual(response.status_code, 403)

    def test_unused_department_can_be_deleted_with_audit(self):
        office = Office.objects.create(office_code="DIL", office_name="Dili Office")
        department = Department.objects.create(
            office=office,
            department_code="HR",
            department_name="Human Resources",
        )
        url = reverse("organization:department-delete", kwargs={"pk": department.pk})

        confirmation = self.client.get(url)
        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "Human Resources")
        self.assertTrue(Department.objects.filter(pk=department.pk).exists())
        self.assertContains(
            self.client.get(reverse("organization:department-list")),
            reverse("organization:department-delete", kwargs={"pk": department.pk}),
        )
        self.assertContains(
            self.client.get(
                reverse("organization:department-detail", kwargs={"pk": department.pk})
            ),
            reverse("organization:department-delete", kwargs={"pk": department.pk}),
        )

        response = self.client.post(url)

        self.assertRedirects(response, reverse("organization:department-list"))
        self.assertFalse(Department.objects.filter(pk=department.pk).exists())
        audit_entry = AuditLog.objects.get(
            action=AuditLog.Action.DELETE,
            entity_type="Department",
            object_id=str(department.pk),
        )
        self.assertEqual(audit_entry.office_id, office.pk)
        self.assertEqual(audit_entry.before_data, {"label": str(department)})

    def test_department_with_position_is_protected(self):
        office = Office.objects.create(office_code="DIL", office_name="Dili Office")
        department = Department.objects.create(
            office=office,
            department_code="HR",
            department_name="Human Resources",
        )
        Position.objects.create(
            office=office,
            department=department,
            position_code="HRM",
            position_title="HR Manager",
        )

        response = self.client.post(
            reverse("organization:department-delete", kwargs={"pk": department.pk})
        )

        self.assertRedirects(response, reverse("organization:department-list"))
        self.assertTrue(Department.objects.filter(pk=department.pk).exists())
        self.assertEqual(
            AuditLog.objects.filter(
                action=AuditLog.Action.DELETE,
                entity_type="Department",
                object_id=str(department.pk),
            ).count(),
            0,
        )

    def test_office_with_related_records_is_not_deletable(self):
        office = Office.objects.create(office_code="DIL", office_name="Dili Office")
        Department.objects.create(
            office=office,
            department_code="HR",
            department_name="Human Resources",
        )

        response = self.client.post(
            reverse("organization:office-delete", kwargs={"pk": office.pk})
        )

        self.assertRedirects(response, reverse("organization:office-list"))
        self.assertTrue(Office.objects.filter(pk=office.pk).exists())
        list_response = self.client.get(reverse("organization:office-list"))
        detail_response = self.client.get(
            reverse("organization:office-detail", kwargs={"pk": office.pk})
        )
        self.assertNotContains(
            list_response,
            reverse("organization:office-delete", kwargs={"pk": office.pk}),
        )
        self.assertContains(
            list_response,
            reverse("organization:office-deactivate", kwargs={"pk": office.pk}),
        )
        self.assertContains(
            detail_response,
            reverse("organization:office-deactivate", kwargs={"pk": office.pk}),
        )

    def test_unused_office_can_be_deleted_with_audit(self):
        office = Office.objects.create(office_code="DIL", office_name="Dili Office")

        response = self.client.post(
            reverse("organization:office-delete", kwargs={"pk": office.pk})
        )

        self.assertRedirects(response, reverse("organization:office-list"))
        self.assertFalse(Office.objects.filter(pk=office.pk).exists())
        self.assertTrue(
            AuditLog.objects.filter(
                entity_type="Office",
                object_id=str(office.pk),
                action=AuditLog.Action.DELETE,
            ).exists()
        )

    def test_office_delete_requires_superuser_even_with_model_permission(self):
        office = Office.objects.create(office_code="DIL", office_name="Dili Office")
        user = get_user_model().objects.create_user(username="office-scoped-user")
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="organization",
                codename="delete_office",
            )
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("organization:office-delete", kwargs={"pk": office.pk})
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Office.objects.filter(pk=office.pk).exists())

    def test_delete_confirmation_is_csrf_protected(self):
        office = Office.objects.create(office_code="DIL", office_name="Dili Office")
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        url = reverse("organization:office-delete", kwargs={"pk": office.pk})

        get_response = csrf_client.get(url)
        post_response = csrf_client.post(url)

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(post_response.status_code, 403)
        self.assertTrue(Office.objects.filter(pk=office.pk).exists())
