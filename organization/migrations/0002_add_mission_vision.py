# Generated manually for InstitutionProfile mission and vision fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="institutionprofile",
            name="mission",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Enter the institution's official mission statement.",
            ),
        ),
        migrations.AddField(
            model_name="institutionprofile",
            name="vision",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Enter the institution's official vision statement.",
            ),
        ),
    ]
