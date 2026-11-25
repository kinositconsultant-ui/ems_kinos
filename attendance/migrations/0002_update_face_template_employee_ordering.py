from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("attendance", "0001_initial"),
        ("employees", "0002_employee_profile_enhancement"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="facetemplate",
            options={"ordering": ["employee__employee_id", "-enrolled_at"]},
        ),
    ]
