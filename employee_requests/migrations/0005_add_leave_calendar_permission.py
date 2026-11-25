# Generated for leave calendar permission (employee_requests.view_leave_calendar).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('employee_requests', '0004_employee_request_workflow_branch'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='employeerequest',
            options={
                'ordering': ['-submitted_at'],
                'indexes': [models.Index(fields=['status', 'request_type', 'submitted_at'], name='employee_re_status_921528_idx')],
                'permissions': [('view_leave_calendar', 'Can view department leave calendar')],
            },
        ),
    ]
