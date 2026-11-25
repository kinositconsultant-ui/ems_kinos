import django.db.models.deletion
from django.db import migrations, models

import employees.models


class Migration(migrations.Migration):

    dependencies = [
        ("employees", "0002_employee_profile_enhancement"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmployeeCertification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("certificate_name", models.CharField(max_length=200)),
                ("issuing_organization", models.CharField(blank=True, max_length=200)),
                ("certificate_number", models.CharField(blank=True, max_length=80)),
                ("issue_date", models.DateField(blank=True, null=True)),
                ("expiry_date", models.DateField(blank=True, null=True)),
                ("document_file", models.FileField(blank=True, max_length=255, upload_to=employees.models.certification_upload_to)),
                ("original_filename", models.CharField(blank=True, editable=False, max_length=255)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="employee_certifications", to="employees.employee")),
            ],
            options={
                "ordering": ["certificate_name", "pk"],
            },
        ),
        migrations.AddConstraint(
            model_name="employeecertification",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("issue_date__isnull", True),
                    ("expiry_date__isnull", True),
                    ("expiry_date__gte", models.F("issue_date")),
                    _connector="OR",
                ),
                name="certification_expiry_not_before_issue",
            ),
        ),
    ]
