# Data-preserving migration from employee_code/first_name/last_name to employee_id/full_name.
# Generated for the Employee profile enhancement.
import uuid

from django.db import migrations, models


def migrate_employee_profile_fields(apps, schema_editor):
    Employee = apps.get_model("employees", "Employee")
    alias = schema_editor.connection.alias
    used_ids = set()
    reserved_old_ids = {
        value.strip()
        for value in Employee.objects.using(alias).values_list("employee_code", flat=True)
        if value and value.strip()
    }

    for employee in Employee.objects.using(alias).all().order_by("pk").iterator():
        old_id = (getattr(employee, "employee_code", "") or "").strip()
        external_id = (getattr(employee, "external_employee_code", "") or "").strip()
        candidate = (getattr(employee, "employee_id", "") or old_id).strip()

        if not candidate or candidate in used_ids:
            number = 1
            while True:
                candidate = f"EMP-{number:06d}"
                already_exists = Employee.objects.using(alias).filter(employee_id=candidate).exclude(pk=employee.pk).exists()
                if candidate not in used_ids and candidate not in reserved_old_ids and not already_exists:
                    break
                number += 1

        old_first = (getattr(employee, "first_name", "") or "").strip()
        old_last = (getattr(employee, "last_name", "") or "").strip()
        existing_name = (getattr(employee, "full_name", "") or "").strip()
        full_name = existing_name or " ".join(part for part in (old_first, old_last) if part).strip()
        if not full_name:
            full_name = f"Employee {employee.pk}"

        employee.employee_id = candidate
        employee.full_name = full_name
        employee.personal_phone = getattr(employee, "personal_phone", "") or getattr(employee, "phone", "") or ""
        employee.legacy_external_identifier = external_id
        employee.qr_token = getattr(employee, "qr_token", None) or uuid.uuid4().hex
        employee.save(update_fields=["employee_id", "full_name", "personal_phone", "legacy_external_identifier", "qr_token"])
        used_ids.add(candidate)


def reverse_migration_is_not_supported(apps, schema_editor):
    # Old fields are removed after consolidating identities and names. A reverse migration
    # cannot reliably split a full name or reconstruct both original codes.
    pass


class Migration(migrations.Migration):
    dependencies = [("employees", "0001_initial")]

    operations = [
        migrations.RemoveIndex(
            model_name="employee",
            name="employees_e_employm_8848f6_idx",
        ),
        migrations.AddField(
            model_name="employee",
            name="employee_id",
            field=models.CharField(blank=True, max_length=40, null=True),
        ),
        migrations.AddField(
            model_name="employee",
            name="full_name",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="employee",
            name="legacy_external_identifier",
            field=models.CharField(blank=True, editable=False, help_text="Archived legacy external identifier retained for migration safety.", max_length=60),
        ),
        migrations.AddField(
            model_name="employee",
            name="photo",
            field=models.ImageField(blank=True, null=True, upload_to="employees/photos/%Y/%m/"),
        ),
        migrations.AddField(model_name="employee", name="preferred_name", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="employee", name="date_of_birth", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="employee", name="place_of_birth", field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="employee", name="nationality", field=models.CharField(blank=True, default="Timor-Leste", max_length=80)),
        migrations.AddField(
            model_name="employee", name="identity_type",
            field=models.CharField(blank=True, choices=[("national_id", "National ID"), ("passport", "Passport"), ("drivers_license", "Driver's License"), ("other", "Other")], max_length=30),
        ),
        migrations.AddField(model_name="employee", name="identity_number", field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name="employee", name="identity_expiry_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(
            model_name="employee", name="gender",
            field=models.CharField(blank=True, choices=[("male", "Male"), ("female", "Female"), ("other", "Other"), ("prefer_not_to_say", "Prefer not to say")], max_length=20),
        ),
        migrations.AddField(model_name="employee", name="personal_phone", field=models.CharField(blank=True, max_length=35)),
        migrations.AddField(model_name="employee", name="personal_email", field=models.EmailField(blank=True, max_length=254)),
        migrations.AddField(model_name="employee", name="residential_address", field=models.TextField(blank=True)),
        migrations.AddField(model_name="employee", name="permanent_address", field=models.TextField(blank=True)),
        migrations.AddField(model_name="employee", name="municipality", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="employee", name="administrative_post", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="employee", name="suco", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="employee", name="aldeia", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(
            model_name="employee", name="marital_status",
            field=models.CharField(blank=True, choices=[("single", "Single"), ("married", "Married"), ("divorced", "Divorced"), ("widowed", "Widowed"), ("separated", "Separated")], default="single", max_length=20),
        ),
        migrations.AddField(model_name="employee", name="spouse_name", field=models.CharField(blank=True, max_length=150)),
        migrations.AddField(model_name="employee", name="spouse_phone", field=models.CharField(blank=True, max_length=35)),
        migrations.AddField(model_name="employee", name="spouse_email", field=models.EmailField(blank=True, max_length=254)),
        migrations.AddField(model_name="employee", name="emergency_contact_name", field=models.CharField(blank=True, max_length=150)),
        migrations.AddField(model_name="employee", name="emergency_contact_relationship", field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name="employee", name="emergency_contact_phone", field=models.CharField(blank=True, max_length=35)),
        migrations.AddField(model_name="employee", name="emergency_contact_alternative_phone", field=models.CharField(blank=True, max_length=35)),
        migrations.AddField(model_name="employee", name="emergency_contact_address", field=models.TextField(blank=True)),
        migrations.AddField(model_name="employee", name="emergency_contact_notes", field=models.TextField(blank=True)),
        migrations.AlterField(model_name="employee", name="hire_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="employee", name="probation_status", field=models.CharField(blank=True, max_length=25)),
        migrations.AddField(
            model_name="employee", name="contract_number", field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="employee", name="contract_type",
            field=models.CharField(blank=True, choices=[("permanent", "Permanent"), ("fixed_term", "Fixed-term"), ("probation", "Probation"), ("part_time", "Part-time"), ("other", "Other")], default="permanent", max_length=20),
        ),
        migrations.AddField(model_name="employee", name="contract_start_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="employee", name="contract_end_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(
            model_name="employee", name="contract_status",
            field=models.CharField(choices=[("active", "Active"), ("expired", "Expired"), ("terminated", "Terminated"), ("draft", "Draft")], default="active", max_length=20),
        ),
        migrations.AddField(model_name="employee", name="contract_notes", field=models.TextField(blank=True)),
        migrations.AddField(model_name="employee", name="education_level", field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name="employee", name="field_of_study", field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="employee", name="certifications", field=models.TextField(blank=True)),
        migrations.AddField(
            model_name="employee", name="qr_token",
            field=models.CharField(blank=True, max_length=64, null=True, unique=True),
        ),
        migrations.RunPython(migrate_employee_profile_fields, reverse_migration_is_not_supported),
        migrations.AlterField(
            model_name="employee", name="employee_id",
            field=models.CharField(blank=True, db_index=True, max_length=40, unique=True),
        ),
        migrations.AlterField(model_name="employee", name="full_name", field=models.CharField(max_length=200)),
        migrations.RemoveField(model_name="employee", name="employee_code"),
        migrations.RemoveField(model_name="employee", name="external_employee_code"),
        migrations.RemoveField(model_name="employee", name="first_name"),
        migrations.RemoveField(model_name="employee", name="last_name"),
        migrations.RemoveField(model_name="employee", name="phone"),
        migrations.AlterModelOptions(name="employee", options={"ordering": ["employee_id"]}),
        migrations.AlterModelOptions(name="employeeassignment", options={"ordering": ["-start_date", "employee__employee_id"]}),
        migrations.AddIndex(
            model_name="employee",
            index=models.Index(fields=["employment_status", "employee_id"], name="employee_status_id_idx"),
        ),
    ]
