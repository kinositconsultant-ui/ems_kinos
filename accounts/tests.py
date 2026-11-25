from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse

from audittrail.models import AuditLog


class SystemLoginTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="test-password",
        )

    def test_login_page_renders_with_adminlte_form(self):
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password"')

    def test_anonymous_dashboard_redirects_to_login(self):
        response = self.client.get(reverse("dashboard"))

        self.assertRedirects(response, f"{reverse('login')}?next=/")

    def test_login_redirects_to_dashboard(self):
        response = self.client.post(
            reverse("login"), {"username": "admin", "password": "test-password"}
        )

        self.assertRedirects(response, reverse("dashboard"))

    def test_wrong_password_stays_on_login(self):
        response = self.client.post(
            reverse("login"), {"username": "admin", "password": "salah"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_remember_me_unchecked_expires_on_browser_close(self):
        self.client.post(reverse("login"), {"username": "admin", "password": "test-password"})

        self.assertTrue(self.client.session.get_expire_at_browser_close())

    def test_remember_me_checked_keeps_session(self):
        self.client.post(
            reverse("login"),
            {"username": "admin", "password": "test-password", "remember": "on"},
        )

        self.assertFalse(self.client.session.get_expire_at_browser_close())

    def test_logout_requires_post_and_returns_to_login(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("logout"))

        self.assertRedirects(response, reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_authenticated_user_visiting_login_goes_to_dashboard(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("login"))

        self.assertRedirects(response, reverse("dashboard"))

class UserManagementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_superuser("admin", "admin@example.com", "test-password")
        cls.viewer = User.objects.create_user("viewer", password="test-password")
        cls.viewer.user_permissions.add(Permission.objects.get(codename="view_user"))
        cls.creator = User.objects.create_user("creator", password="test-password")
        cls.creator.user_permissions.add(
            Permission.objects.get(codename="view_user"),
            Permission.objects.get(codename="add_user"),
        )
        cls.nobody = User.objects.create_user("nobody", password="test-password")
        cls.group = Group.objects.create(name="HR Staff")

    def valid_data(self, **overrides):
        data = {
            "username": "new.user",
            "first_name": "New",
            "last_name": "User",
            "email": "new.user@example.com",
            "password1": "Kata-Sandi-Kuat-2026",
            "password2": "Kata-Sandi-Kuat-2026",
            "is_active": "on",
            "groups": [self.group.pk],
        }
        data.update(overrides)
        return data

    # --- akses & izin (server side) ---
    def test_anonymous_is_redirected_to_login(self):
        for name in ("user-list", "user-create"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response["Location"].startswith(reverse("login")))

    def test_user_without_permission_gets_403(self):
        self.client.force_login(self.nobody)

        self.assertEqual(self.client.get(reverse("user-list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("user-create")).status_code, 403)

    def test_view_permission_can_list_but_not_create(self):
        self.client.force_login(self.viewer)

        self.assertEqual(self.client.get(reverse("user-list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("user-create")).status_code, 403)
        response = self.client.post(reverse("user-create"), self.valid_data())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(get_user_model().objects.filter(username="new.user").exists())

    def test_add_button_only_shown_with_add_permission(self):
        self.client.force_login(self.viewer)
        self.assertNotContains(self.client.get(reverse("user-list")), reverse("user-create"))

        self.client.force_login(self.creator)
        self.assertContains(self.client.get(reverse("user-list")), reverse("user-create"))

    # --- pembuatan user ---
    def test_create_user_saves_hashed_password_groups_and_audit(self):
        self.client.force_login(self.creator)

        response = self.client.post(reverse("user-create"), self.valid_data())

        self.assertRedirects(response, reverse("user-list"))
        user = get_user_model().objects.get(username="new.user")
        self.assertTrue(user.check_password("Kata-Sandi-Kuat-2026"))
        self.assertNotEqual(user.password, "Kata-Sandi-Kuat-2026")
        self.assertTrue(user.is_active)
        self.assertEqual(list(user.groups.all()), [self.group])
        entry = AuditLog.objects.get(entity_type="User", object_id=str(user.pk))
        self.assertEqual(entry.action, AuditLog.Action.CREATE)
        self.assertEqual(entry.actor_user, self.creator)
        self.assertEqual(entry.after_data["groups"], ["HR Staff"])
        self.assertNotIn("password", str(entry.after_data).lower())

    def test_new_user_can_log_in_through_system_login(self):
        self.client.force_login(self.creator)
        self.client.post(reverse("user-create"), self.valid_data())
        self.client.logout()

        response = self.client.post(
            reverse("login"), {"username": "new.user", "password": "Kata-Sandi-Kuat-2026"}
        )

        self.assertRedirects(response, reverse("dashboard"))

    def test_cannot_grant_staff_or_superuser_from_form(self):
        self.client.force_login(self.creator)

        self.client.post(
            reverse("user-create"),
            self.valid_data(is_staff="on", is_superuser="on"),
        )

        user = get_user_model().objects.get(username="new.user")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_password_mismatch_creates_nothing(self):
        self.client.force_login(self.creator)
        initial_audit_count = AuditLog.objects.filter(entity_type="User").count()

        response = self.client.post(
            reverse("user-create"), self.valid_data(password2="Berbeda-Sekali-99")
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(username="new.user").exists())
        self.assertEqual(
            AuditLog.objects.filter(entity_type="User").count(),
            initial_audit_count,
        )

    def test_weak_password_is_rejected(self):
        self.client.force_login(self.creator)

        response = self.client.post(
            reverse("user-create"), self.valid_data(password1="12345678", password2="12345678")
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(username="new.user").exists())

    def test_duplicate_username_is_rejected_case_insensitively(self):
        self.client.force_login(self.creator)

        response = self.client.post(reverse("user-create"), self.valid_data(username="ADMIN"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_user_model().objects.filter(username__iexact="admin").count(), 1)

    # --- daftar user ---
    def test_list_search_filters_users(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("user-list"), {"q": "viewer"})

        self.assertContains(response, "viewer")
        self.assertNotContains(response, "nobody")

    def test_menu_user_accounts_points_to_system_page(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, f'href="{reverse("user-list")}"')


class UserDeactivationTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="account-admin",
            email="account-admin@example.com",
            password="test-password",
        )
        self.client.force_login(self.admin)
        self.target = get_user_model().objects.create_user(username="employee-login")

    def test_user_can_be_deactivated_without_deleting_account_history(self):
        url = reverse("user-deactivate", kwargs={"pk": self.target.pk})

        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertContains(self.client.get(reverse("user-list")), url)
        response = self.client.post(url)

        self.assertRedirects(response, reverse("user-list"))
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active)
        self.assertTrue(get_user_model().objects.filter(pk=self.target.pk).exists())
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.UPDATE,
                entity_type="User",
                object_id=str(self.target.pk),
                after_data__is_active=False,
            ).exists()
        )

    def test_non_superuser_cannot_deactivate_an_account_by_direct_post(self):
        user = get_user_model().objects.create_user(username="account-scoped-user")
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="auth",
                codename="change_user",
            )
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("user-deactivate", kwargs={"pk": self.target.pk})
        )

        self.assertEqual(response.status_code, 403)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)

    def test_user_cannot_deactivate_self_or_another_superuser(self):
        self_url = reverse("user-deactivate", kwargs={"pk": self.admin.pk})
        self.assertEqual(self.client.post(self_url).status_code, 302)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

        protected_admin = get_user_model().objects.create_superuser(
            username="protected-admin",
            email="protected-admin@example.com",
            password="test-password",
        )
        response = self.client.post(
            reverse("user-deactivate", kwargs={"pk": protected_admin.pk})
        )
        self.assertEqual(response.status_code, 302)
        protected_admin.refresh_from_db()
        self.assertTrue(protected_admin.is_active)


class UserUpdateAuthorizationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser(
            username="account-admin-edit",
            email="account-admin-edit@example.com",
            password="test-password",
        )
        self.target = User.objects.create_user(
            username="editable-user",
            email="editable-user@example.com",
            password="test-password",
        )
        self.viewer = User.objects.create_user(
            username="unauthorized-editor",
            password="test-password",
        )
        self.viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="auth",
                codename="change_user",
            )
        )

    def test_admin_can_open_edit_link_and_save(self):
        self.client.force_login(self.admin)
        list_response = self.client.get(reverse("user-list"))
        edit_url = reverse("user-update", kwargs={"pk": self.target.pk})
        self.assertContains(list_response, edit_url)
        self.assertEqual(self.client.get(edit_url).status_code, 200)

        response = self.client.post(
            edit_url,
            {
                "username": self.target.username,
                "full_name": "Updated User",
                "email": "updated@example.com",
                "groups": [],
                "is_active": "on",
            },
        )
        self.assertRedirects(response, reverse("user-list"))
        self.target.refresh_from_db()
        self.assertEqual(self.target.first_name, "Updated User")
        self.assertEqual(self.target.email, "updated@example.com")

    def test_user_with_change_permission_but_not_superuser_cannot_edit(self):
        self.client.force_login(self.viewer)
        edit_url = reverse("user-update", kwargs={"pk": self.target.pk})

        self.assertEqual(self.client.get(edit_url).status_code, 403)
        response = self.client.post(
            edit_url,
            {
                "username": self.target.username,
                "full_name": "Unauthorized Update",
                "email": self.target.email,
                "groups": [],
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.target.refresh_from_db()
        self.assertNotEqual(self.target.first_name, "Unauthorized Update")

    def test_superuser_account_cannot_be_edited_through_update_view(self):
        self.client.force_login(self.admin)
        edit_url = reverse("user-update", kwargs={"pk": self.admin.pk})

        self.assertEqual(self.client.get(edit_url).status_code, 403)