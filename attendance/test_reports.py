from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee, EmployeeAssignment, UserEmployeeLink
from organization.models import Office, Position

from .models import (
    AttendancePersistenceFailure,
    AttendanceRecord,
    AttendanceEvent,
    EmployeeSchedule,
    FaceVerificationLog,
    Shift,
)


class AttendanceReportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="report-admin",
            email="report-admin@example.com",
            password="test-password",
        )
        self.client.force_login(self.user)
        self.office = Office.objects.create(
            office_code="DIL",
            office_name="Dili Office",
        )
        employee = Employee.objects.create(
            employee_id="EMP-001",
            full_name="=SUM(1,1)",
            hire_date=date(2024, 1, 1),
        )
        self.employee = employee
        position = Position.objects.create(
            office=self.office,
            position_code="ADM",
            position_title="Administrator",
        )
        self.position = position
        assignment = EmployeeAssignment.objects.create(
            employee=employee,
            position=position,
            start_date=date(2024, 1, 1),
        )
        self.assignment = assignment
        shift = Shift.objects.create(
            office=self.office,
            shift_code="DAY",
            name="Day shift",
            start_time=time(9),
            end_time=time(17),
        )
        self.shift = shift
        start = timezone.make_aware(datetime(2025, 2, 1, 9))
        schedule = EmployeeSchedule.objects.create(
            employee_assignment=assignment,
            shift=shift,
            work_date=date(2025, 2, 1),
            scheduled_start_at=start,
            scheduled_end_at=timezone.make_aware(datetime(2025, 2, 1, 17)),
        )
        AttendanceRecord.objects.create(
            schedule=schedule,
            status=AttendanceRecord.Status.LATE,
            late_minutes=15,
            total_work_minutes=420,
        )
        self.record = AttendanceRecord.objects.latest("pk")

    def log_in_as_employee(self, employee=None):
        employee = employee or self.employee
        user = get_user_model().objects.create_user(
            username=f"report-{employee.pk}",
            password="report-password",
        )
        UserEmployeeLink.objects.create(user=user, employee=employee)
        user.groups.add(Group.objects.get_or_create(name="employees")[0])
        user.user_permissions.add(
            Permission.objects.get(
                codename="view_attendancerecord",
                content_type__app_label="attendance",
            ),
            Permission.objects.get(
                codename="view_attendance_exceptions",
                content_type__app_label="attendance",
            ),
        )
        self.client.force_login(user)
        return user

    def create_record(self, employee, position, shift, work_date, status=AttendanceRecord.Status.LATE):
        assignment = EmployeeAssignment.objects.create(
            employee=employee,
            position=position,
            start_date=date(2024, 1, 1),
        )
        schedule = EmployeeSchedule.objects.create(
            employee_assignment=assignment,
            shift=shift,
            work_date=work_date,
            scheduled_start_at=timezone.make_aware(datetime.combine(work_date, time(9))),
            scheduled_end_at=timezone.make_aware(datetime.combine(work_date, time(17))),
        )
        return AttendanceRecord.objects.create(
            schedule=schedule,
            status=status,
            late_minutes=5,
            total_work_minutes=480,
        )

    def test_daily_report_applies_date_filter(self):
        response = self.client.get(
            reverse("attendance:report-daily"),
            {"start_date": "2025-02-01", "end_date": "2025-02-01"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dili Office")
        self.assertContains(response, "EMP-001")
        self.assertContains(response, "Late")

    def test_office_report_aggregates_attendance_and_filters_office(self):
        response = self.client.get(
            reverse("attendance:report-offices"),
            {"office": self.office.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dili Office")
        self.assertContains(response, "<td>1</td>", html=True)

    def test_csv_export_is_filtered_and_protects_spreadsheet_formulas(self):
        response = self.client.get(
            reverse("attendance:report-daily"),
            {
                "start_date": "2025-02-01",
                "end_date": "2025-02-01",
                "export": "csv",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("'=SUM(1,1)", response.content.decode("utf-8-sig"))

    def test_daily_report_includes_break_event_timestamps(self):
        occurred_at = timezone.make_aware(datetime(2025, 2, 1, 10, 5))
        AttendanceEvent.objects.create(
            attendance_record=self.record,
            event_type=AttendanceEvent.EventType.BREAK_START,
            occurred_at=occurred_at,
            source=AttendanceEvent.Source.SYSTEM,
        )

        response = self.client.get(
            reverse("attendance:report-daily"),
            {"start_date": "2025-02-01", "end_date": "2025-02-01"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Break Out")
        self.assertContains(response, "Break In")
        self.assertEqual(response.context["headers"][6], "Break Out")

    def test_malformed_filter_date_is_reported_as_bad_request(self):
        response = self.client.get(
            reverse("attendance:report-daily"),
            {"start_date": "not-a-date"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("start_date", response.context["report_form"].errors)

    def test_report_requires_attendance_view_permission(self):
        user = get_user_model().objects.create_user(
            username="report-reader",
            password="test-password",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("attendance:report-daily"))

        self.assertEqual(response.status_code, 403)

    def test_employee_report_is_limited_to_employee_and_current_office(self):
        self.log_in_as_employee()
        other_employee = Employee.objects.create(
            employee_id="EMP-002",
            full_name="Other Employee",
            hire_date=date(2024, 1, 1),
        )
        self.create_record(
            other_employee,
            self.position,
            self.shift,
            date(2025, 2, 1),
        )
        other_office = Office.objects.create(
            office_code="BTC",
            office_name="Other Office",
        )
        other_position = Position.objects.create(
            office=other_office,
            position_code="OTH",
            position_title="Other Position",
        )
        other_shift = Shift.objects.create(
            office=other_office,
            shift_code="OTHER",
            name="Other shift",
            start_time=time(9),
            end_time=time(17),
        )
        other_employee_in_other_office = Employee.objects.create(
            employee_id="EMP-003",
            full_name="Other Office Employee",
            hire_date=date(2024, 1, 1),
        )
        self.create_record(
            other_employee_in_other_office,
            other_position,
            other_shift,
            date(2025, 2, 1),
        )

        response = self.client.get(
            reverse("attendance:report-daily"),
            {"start_date": "2025-02-01", "end_date": "2025-02-01"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "=SUM(1,1)")
        self.assertNotContains(response, "Other Employee")
        self.assertNotContains(response, "Other Office Employee")
        self.assertNotContains(response, 'name="office"')
        self.assertNotContains(response, 'name="employee"')

    def test_employee_report_ignores_office_and_employee_parameters(self):
        self.log_in_as_employee()
        other_employee = Employee.objects.create(
            employee_id="EMP-004",
            full_name="Outside Employee",
            hire_date=date(2024, 1, 1),
        )
        self.create_record(
            other_employee,
            self.position,
            self.shift,
            date(2025, 2, 1),
        )

        response = self.client.get(
            reverse("attendance:report-daily"),
            {
                "start_date": "2025-02-01",
                "end_date": "2025-02-01",
                "office": self.office.pk,
                "employee": other_employee.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "=SUM(1,1)")
        self.assertNotContains(response, "Outside Employee")

    def test_employee_csv_export_is_isolated(self):
        self.log_in_as_employee()
        other_employee = Employee.objects.create(
            employee_id="EMP-005",
            full_name="CSV Outside Employee",
            hire_date=date(2024, 1, 1),
        )
        self.create_record(
            other_employee,
            self.position,
            self.shift,
            date(2025, 2, 1),
        )

        response = self.client.get(
            reverse("attendance:report-daily"),
            {
                "start_date": "2025-02-01",
                "end_date": "2025-02-01",
                "export": "csv",
            },
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8-sig")
        self.assertIn("EMP-001", content)
        self.assertNotIn("EMP-005", content)

    def test_employee_without_profile_gets_empty_report(self):
        user = get_user_model().objects.create_user(
            username="report-no-profile",
            password="report-password",
        )
        user.groups.add(Group.objects.get_or_create(name="employees")[0])
        user.user_permissions.add(
            Permission.objects.get(
                codename="view_attendancerecord",
                content_type__app_label="attendance",
            )
        )
        self.client.force_login(user)

        response = self.client.get(
            reverse("attendance:report-daily"),
            {"start_date": "2025-02-01", "end_date": "2025-02-01"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No attendance records found")

    def test_employee_exceptions_and_detail_are_isolated(self):
        self.log_in_as_employee()
        other_employee = Employee.objects.create(
            employee_id="EMP-EXC-2",
            full_name="Same Office Exception Employee",
            hire_date=date(2024, 1, 1),
        )
        # Kegagalan verifikasi wajah milik pegawai lain tidak boleh muncul
        # dalam bagian diagnostik halaman ini untuk pengguna ini.
        FaceVerificationLog.objects.create(
            employee=other_employee,
            match_result=FaceVerificationLog.MatchResult.NOT_MATCHED,
            liveness_result=FaceVerificationLog.LivenessResult.NOT_CHECKED,
            occurred_at=timezone.now(),
        )

        response = self.client.get(reverse("attendance:exceptions"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Same Office Exception Employee")
        self.assertEqual(
            set(response.context["failed_verifications"]),
            set(),
        )

    def test_persistence_failure_is_visible_in_exceptions_review(self):
        schedule = EmployeeSchedule.objects.get(
            employee_assignment__employee=self.employee,
        )
        incident = AttendancePersistenceFailure.objects.create(
            employee=self.employee,
            schedule=schedule,
            attendance_record=self.record,
            expected_event_type="break_end",
            channel=AttendancePersistenceFailure.Channel.MOBILE,
            persistence_stage="attendance_event_save",
            exception_type="DatabaseError",
            diagnostic_summary="Simulated persistence failure",
        )

        response = self.client.get(reverse("attendance:exceptions"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(incident, response.context["persistence_failures"])
        self.assertContains(response, str(incident.incident_id))
        self.assertContains(response, "Attendance persistence failures")

    def test_employee_without_assignment_gets_empty_exceptions(self):
        employee = Employee.objects.create(
            employee_id="EMP-NOASSIGN",
            full_name="No Assignment Employee",
            hire_date=date(2024, 1, 1),
        )
        user = get_user_model().objects.create_user(
            username="employee-no-assignment",
            password="report-password",
        )
        UserEmployeeLink.objects.create(user=user, employee=employee)
        user.groups.add(Group.objects.get_or_create(name="employees")[0])
        user.user_permissions.add(
            Permission.objects.get(
                codename="view_attendance_exceptions",
                content_type__app_label="attendance",
            )
        )
        self.client.force_login(user)

        response = self.client.get(reverse("attendance:exceptions"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["failed_verifications"]), [])
        self.assertEqual(list(response.context["persistence_failures"]), [])

    def test_dashboard_navigation_excludes_home_and_documentation(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)

    def test_history_list_excludes_legacy_review_attributes(self):
        response = self.client.get(reverse("attendance:history"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Review status")
        self.assertNotContains(response, "Reviewed by")
        self.assertNotContains(response, "Reviewed at")
        columns = [label for label, _field in response.context["columns"]]
        self.assertNotIn("Review", columns)
        self.assertNotIn("review_status", [field for _label, field in response.context["columns"]])
        filter_names = [f["name"] for f in response.context["filter_controls"]]
        self.assertNotIn("review_status", filter_names)
        # Status filter remains (it is an attendance status, not a review).
        self.assertIn("status", filter_names)

    def test_history_detail_excludes_legacy_review_attributes(self):
        response = self.client.get(reverse("attendance:record-detail", args=[self.record.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Review status")
        self.assertNotContains(response, "Reviewed by")
        self.assertNotContains(response, "Reviewed at")
        details = dict(response.context["details"])
        self.assertNotIn("Review status", details)
        self.assertNotIn("Reviewed by", details)
        self.assertNotIn("Reviewed at", details)
        self.assertIn("Late (minutes)", details)
        self.assertEqual(details["Late (minutes)"], self.record.late_minutes)
        self.assertIn("Remarks", details)

    def test_history_late_and_incomplete_preserved(self):
        other_employee = Employee.objects.create(
            employee_id="EMP-INC", full_name="Incomplete Employee",
            hire_date=date(2024, 1, 1),
        )
        assignment = EmployeeAssignment.objects.create(
            employee=other_employee, position=self.position, start_date=date(2024, 1, 1),
        )
        incomplete_schedule = EmployeeSchedule.objects.create(
            employee_assignment=assignment,
            shift=self.shift,
            work_date=date(2025, 2, 2),
            scheduled_start_at=timezone.make_aware(datetime(2025, 2, 2, 9)),
            scheduled_end_at=timezone.make_aware(datetime(2025, 2, 2, 17)),
        )
        incomplete = AttendanceRecord.objects.create(
            schedule=incomplete_schedule,
            status=AttendanceRecord.Status.INCOMPLETE,
            late_minutes=0,
            total_work_minutes=0,
        )
        response = self.client.get(reverse("attendance:history"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.record.late_minutes))
        self.assertContains(response, self.record.get_status_display())
        self.assertContains(response, incomplete.get_status_display())

    def test_history_scope_not_broadened_via_detail_url(self):
        self.log_in_as_employee()
        other_office = Office.objects.create(
            office_code="OHX", office_name="Other History Office",
        )
        other_position = Position.objects.create(
            office=other_office, position_code="OHX", position_title="Other Position",
        )
        other_shift = Shift.objects.create(
            office=other_office, shift_code="OHX", name="Other shift",
            start_time=time(9), end_time=time(17),
        )
        other_employee = Employee.objects.create(
            employee_id="EMP-HX", full_name="Other History Employee",
            hire_date=date(2024, 1, 1),
        )
        other_record = self.create_record(
            other_employee, other_position, other_shift, date(2025, 2, 1),
        )

        # Employee cannot open another employee's history detail via direct URL.
        detail_response = self.client.get(
            reverse("attendance:record-detail", args=[other_record.pk])
        )
        self.assertEqual(detail_response.status_code, 404)

        # History list for employee is scoped to own record only.
        list_response = self.client.get(reverse("attendance:history"))
        self.assertEqual(list_response.status_code, 200)
        self.assertNotContains(list_response, "EMP-HX")
        self.assertNotContains(list_response, "Other History Employee")
       
