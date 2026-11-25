from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_request_branches(apps, schema_editor):
    EmployeeRequest = apps.get_model("employee_requests", "EmployeeRequest")
    for request_record in EmployeeRequest.objects.filter(branch__isnull=True).select_related(
        "employee_assignment__position"
    ).iterator():
        if request_record.employee_assignment.position_id:
            EmployeeRequest.objects.filter(pk=request_record.pk).update(
                branch_id=request_record.employee_assignment.position.office_id
            )


class Migration(migrations.Migration):

    dependencies = [
        ("employee_requests", "0003_remove_requestapproval_unique_request_approval_step_and_more"),
        ("organization", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="employeerequest",
            name="branch",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="employee_requests", to="organization.office"),
        ),
        migrations.AddField(
            model_name="employeerequest",
            name="forwarded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="employeerequest",
            name="schedule_processed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="employeerequest",
            name="schedule_processed_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="employee_requests_schedule_processed", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="employeerequest",
            name="workflow_stage",
            field=models.CharField(default="xefe_review", max_length=30),
        ),
        migrations.AddField(
            model_name="employeerequest",
            name="xefe_review_note",
            field=models.TextField(blank=True),
        ),
        migrations.RunPython(
            code=backfill_request_branches,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
