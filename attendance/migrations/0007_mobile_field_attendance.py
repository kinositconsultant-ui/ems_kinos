from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0006_alter_attendancerecord_options_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="faceverificationlog",
            name="device",
            field=models.ForeignKey(
                blank=True,
                help_text="Null for authenticated BYOD mobile attendance; physical kiosk device remains required for kiosk attendance.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="verification_logs",
                to="attendance.attendancedevice",
            ),
        ),
        migrations.AlterField(
            model_name="attendanceevent",
            name="location_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("inside", "Inside geofence"),
                    ("outside", "Outside geofence"),
                    ("inconclusive", "Inconclusive"),
                    ("not_provided", "Not provided"),
                    ("dynamic", "Dynamic field location"),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="attendancepolicy",
            name="require_liveness",
            field=models.BooleanField(
                default=True,
                help_text="Liveness/PAD is mandatory for face attendance and cannot be disabled: every face-based check-in must pass PAD before an event is recorded.",
            ),
        ),
    ]
