# Dedicated read-only page permission for Attendance Exceptions.
# Generated as part of converting "Exceptions & Review" into a read-only page.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0008_attendancepersistencefailure"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="attendancerecord",
            options={
                "ordering": ["-schedule__work_date"],
                "permissions": [
                    ("use_mobile_face_attendance", "Can use mobile face attendance"),
                    ("view_attendance_exceptions", "Can view attendance exceptions"),
                ],
            },
        ),
    ]
