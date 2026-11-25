"""Employee <-> User account linking (unapplied migration in this workspace).

- Creates UserEmployeeLink: User OneToOne (each User -> at most one Employee),
  Employee FK (one Employee -> many Users). Authoritative post-migration source.
- Keeps Employee.user as nullable legacy ForeignKey (editable=False, legacy related_name)
  for compatibility; code must not write it as a second source of truth.
- Data migration copies every existing non-null Employee.user association into
  UserEmployeeLink without deleting users/employees/requests/approvals/history.
  It uses the historical Employee.user_id value and does not overwrite an existing
  UserEmployeeLink for the same user. Conflicting/ambiguous legacy states cannot be
  invented from a former OneToOne; review data before activation if custom scripts exist.

Activation (do NOT run against the development database automatically):
1. Backup the database.
2. In the active environment, from attendance_system: python manage.py migrate --plan
3. Review plan (should include employees 0006_user_employee_links).
4. Apply when approved: python manage.py migrate
5. Verify existing User-Employee links (admin, dashboards, request submission).

Reverse/rollback: RunPython reverse is noop. After activation, creating multiple User
accounts linked to the same Employee cannot be represented by legacy Employee.user;
rollback after such multi-account links requires manual reconciliation and an approved plan.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def copy_existing_links(apps, schema_editor):
    Employee = apps.get_model("employees", "Employee")
    UserEmployeeLink = apps.get_model("employees", "UserEmployeeLink")
    for emp in Employee.objects.exclude(user_id__isnull=True).only("id", "user_id").iterator():
        uid = emp.user_id
        if not uid:
            continue
        existing = UserEmployeeLink.objects.filter(user_id=uid).only("id", "employee_id").first()
        if existing:
            # Preserve explicit/existing link; do not silently move a user.
            continue
        UserEmployeeLink.objects.create(user_id=uid, employee_id=emp.id, is_primary=False)


class Migration(migrations.Migration):
    dependencies = [
        ("employees", "0005_remove_redundant_contract_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserEmployeeLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_primary", models.BooleanField(default=False, help_text="Optional display marker only. Do not use for workflow reviewers, routing, permissions, or authorization.")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="user_account_links", to="employees.employee")),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="employee_account_link", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "User-Employee link",
                "verbose_name_plural": "User-Employee links",
                "ordering": ["employee_id", "user__username"],
            },
        ),
        migrations.AlterField(
            model_name="employee",
            name="user",
            field=models.ForeignKey(blank=True, editable=False, help_text="Legacy compatibility field (read-only). Authoritative source is UserEmployeeLink; do not write.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="legacy_employee_profiles", to=settings.AUTH_USER_MODEL),
        ),
        migrations.RunPython(copy_existing_links, migrations.RunPython.noop),
    ]
