# Generated manually for InstitutionProfile institution photo field
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0002_add_mission_vision"),
    ]

    operations = [
        migrations.AddField(
            model_name="institutionprofile",
            name="institution_photo",
            field=models.ImageField(
                blank=True,
                help_text="Large image displayed on the Institution Profile page.",
                null=True,
                upload_to="institution/profile/",
            ),
        ),
    ]
