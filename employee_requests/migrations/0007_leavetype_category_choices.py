from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("employee_requests", "0006_contractexpiryalert"),
    ]

    operations = [
        migrations.AlterField(
            model_name="leavetype",
            name="category",
            field=models.CharField(
                blank=True,
                choices=[
                    ("annual", "Annual leave"),
                    ("sick", "Sick leave"),
                    ("marriage_bereavement", "Marriage / bereavement / community or religious event"),
                    ("maternity", "Maternity leave"),
                    ("paternity", "Paternity leave"),
                    ("pregnancy_medical", "Pregnancy medical appointment / nursing time off"),
                    ("unpaid", "Unpaid leave"),
                    ("personal", "Personal leave"),
                    ("study_training", "Study / training leave"),
                    ("compassionate", "Additional compassionate leave"),
                    ("other", "Other"),
                ],
                default="other",
                max_length=40,
            ),
        ),
    ]