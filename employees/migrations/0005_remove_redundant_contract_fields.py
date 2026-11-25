from django.db import migrations


class Migration(migrations.Migration):
    """Drops duplicated attributes. 0004 must already have preserved their content."""

    dependencies = [
        ("employees", "0004_merge_contract_notes_into_notes"),
    ]

    operations = [
        migrations.RemoveField(model_name="employee", name="contract_notes"),
        migrations.RemoveField(model_name="employee", name="contract_status"),
        migrations.RemoveField(model_name="employee", name="contract_type"),
        migrations.RemoveField(model_name="employee", name="probation_status"),
    ]
