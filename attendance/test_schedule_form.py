"""Employee Schedule Add / Edit / View / List and the Office -> Employee/Shift dropdowns."""
import re
from datetime import date, time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse

from employees.models import Employee, EmployeeAssignment, UserEmployeeLink
from organization.models import Position

from .forms import EmployeeScheduleForm
from .models import AttendanceEvent, AttendanceRecord, EmployeeSchedule, Holiday, Shift
from .test_work_schedules import WORK_DATE, ScheduleFixture


def select_html(html, field_id):
    return re.search(r'<select[^>]*id="%s".*?</select>' % field_id, html, re.S).group(0)


def option_count(select):
    return len([o for o in re.findall(r"<option[^>]*>", select) if 'value=""' not in o])


class ScheduleFormTestBase(ScheduleFixture, TestCase):
    def setUp(self):
        self.make_base()
        self.admin = get_user_model().objects.create_superuser("root", "root@example.com", "pw")
        self.client.force_login(self.admin)
        # A second office with its own employee and shift.
        self.other_employee = Employee.objects.create(
            employee_id="EMP-002", full_name="Jose Ximenes", hire_date=date(2024, 1, 1),
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        self.other_assignment = EmployeeAssignment.objects.create(
            employee=self.other_employee, position=self.other_position, start_date=date(2024, 1, 1)
        )
        self.other_shift = Shift.objects.create(
            office=self.other_office, shift_code="DAY", name="Baucau Day",
            start_time=time(9), end_time=time(17), break_start_time=time(12), break_end_time=time(13),
        )

    def user_with(self, *codenames):
        self._user_count = getattr(self, "_user_count", 0) + 1
        user = get_user_model().objects.create_user("limited%d" % self._user_count, password="pw")
        user.user_permissions.set(Permission.objects.filter(codename__in=codenames))
        self.client.force_login(user)
        return user


class AddFormDropdownTests(ScheduleFormTestBase):
    def test_add_form_locks_employee_and_shift_until_office_is_chosen(self):
        html = self.client.get(reverse("attendance:schedule-create")).content.decode()
        for field_id in ("id_employee_assignment", "id_shift"):
            tag = select_html(html, field_id).split(">")[0]
            self.assertIn("disabled", tag)
        self.assertRegex(select_html(html, "id_employee_assignment"), r'<option value="" selected>Select an office first</option>')
        self.assertRegex(select_html(html, "id_shift"), r'<option value="" selected>Select an office first</option>')
        self.assertIn("Choose the office first", html)

    def test_dropdowns_are_populated_not_empty(self):
        """Regression: replacing the widget after setting the queryset left both selects with no <option>."""
        html = self.client.get(reverse("attendance:schedule-create")).content.decode()
        self.assertEqual(option_count(select_html(html, "id_employee_assignment")), 2)
        self.assertEqual(option_count(select_html(html, "id_shift")), 3)

    def test_options_carry_their_office_so_choosing_an_office_filters_them(self):
        html = self.client.get(reverse("attendance:schedule-create")).content.decode()
        employees = select_html(html, "id_employee_assignment")
        self.assertRegex(employees, r'value="%d"[^>]*data-office="%d"' % (self.assignment.pk, self.office.pk))
        self.assertRegex(employees, r'value="%d"[^>]*data-office="%d"' % (self.other_assignment.pk, self.other_office.pk))
        shifts = select_html(html, "id_shift")
        self.assertRegex(shifts, r'value="%d"[^>]*data-office="%d"' % (self.morning.pk, self.office.pk))
        self.assertRegex(shifts, r'value="%d"[^>]*data-office="%d"' % (self.other_shift.pk, self.other_office.pk))

    def test_posted_office_unlocks_the_selects(self):
        response = self.client.post(reverse("attendance:schedule-create"), {"office": self.office.pk, "action": "save"})
        html = response.content.decode()
        for field_id in ("id_employee_assignment", "id_shift"):
            self.assertNotIn("disabled", select_html(html, field_id).split(">")[0])
        self.assertIn("Select an employee", html)

    def test_office_without_employees_or_shifts_gets_a_clear_message(self):
        from organization.models import Office
        empty = Office.objects.create(office_code="EMP", office_name="Empty Office")
        form = EmployeeScheduleForm(data={"office": empty.pk})
        self.assertFalse(form.fields["employee_assignment"].queryset.filter(position__office=empty).exists())
        self.assertFalse(form.fields["shift"].queryset.filter(office=empty).exists())
        html = self.client.get(reverse("attendance:schedule-create")).content.decode()
        self.assertIn("No employees with an active assignment were found for this office.", html)
        self.assertIn("No active shifts were found for this office.", html)

    def test_only_active_employees_and_active_shifts_are_offered(self):
        gone = Employee.objects.create(
            employee_id="EMP-9", full_name="Gone", hire_date=date(2024, 1, 1),
            employment_status=Employee.EmploymentStatus.INACTIVE,
        )
        EmployeeAssignment.objects.create(employee=gone, position=self.position, start_date=date(2024, 1, 1))
        self.night.is_active = False
        self.night.save()
        html = self.client.get(reverse("attendance:schedule-create")).content.decode()
        self.assertNotIn("EMP-9", html)
        self.assertNotIn("Night Shift", html)

    def test_shift_without_break_times_is_visible_but_cannot_be_chosen(self):
        legacy = Shift.objects.create(
            office=self.office, shift_code="OLD", name="Legacy Shift", start_time=time(8), end_time=time(16)
        )
        html = self.client.get(reverse("attendance:schedule-create")).content.decode()
        self.assertRegex(select_html(html, "id_shift"), r'<option value="%d"[^>]*disabled' % legacy.pk)
        self.assertContains(self.client.get(reverse("attendance:schedule-create")), "add break times to this shift first")

    def test_backend_rejects_employee_or_shift_from_another_office(self):
        response = self.client.post(reverse("attendance:schedule-create"), {
            "office": self.office.pk, "employee_assignment": self.other_assignment.pk,
            "shift": self.other_shift.pk, "work_date": "2026-03-03", "status": "scheduled", "action": "save",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not assigned to the selected office")
        self.assertContains(response, "different office")
        self.assertFalse(EmployeeSchedule.objects.exists())

    def test_creating_a_schedule_creates_no_attendance_data(self):
        response = self.client.post(reverse("attendance:schedule-create"), {
            "office": self.office.pk, "employee_assignment": self.assignment.pk, "shift": self.morning.pk,
            "work_date": "2026-03-03", "status": "scheduled", "notes": "", "action": "save",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(EmployeeSchedule.objects.count(), 1)
        self.assertEqual(AttendanceEvent.objects.count(), 0)
        self.assertEqual(AttendanceRecord.objects.count(), 0)


class EditFormTests(ScheduleFormTestBase):
    def setUp(self):
        super().setUp()
        self.schedule = self.make_schedule()
        self.schedule.notes = "Cover for training"
        self.schedule.save()

    def edit_html(self):
        return self.client.get(reverse("attendance:schedule-update", args=[self.schedule.pk])).content.decode()

    def test_edit_shows_every_saved_value(self):
        html = self.edit_html()
        self.assertRegex(select_html(html, "id_office"), r'<option value="%d" selected' % self.office.pk)
        self.assertRegex(select_html(html, "id_employee_assignment"), r'<option value="%d" selected' % self.assignment.pk)
        self.assertRegex(select_html(html, "id_shift"), r'<option value="%d" selected' % self.morning.pk)
        self.assertIn('value="2026-03-02"', html)
        self.assertRegex(select_html(html, "id_status"), r'<option value="scheduled" selected')
        self.assertIn("Cover for training", html)
        self.assertNotIn("disabled", select_html(html, "id_employee_assignment").split(">")[0])

    def test_edit_keeps_inactive_shift_and_ended_employee_for_history(self):
        self.morning.is_active = False
        self.morning.save()
        self.assignment.end_date = date(2026, 3, 1)
        self.assignment.save()
        html = self.edit_html()
        self.assertRegex(select_html(html, "id_shift"), r'<option value="%d" selected[^>]*>[^<]*inactive \(kept for history\)' % self.morning.pk)
        self.assertRegex(select_html(html, "id_employee_assignment"), r'<option value="%d" selected' % self.assignment.pk)
        self.assertIn("no longer available for new schedules", html)
        self.assertIn("The saved shift is inactive", html)

    def test_edit_cannot_switch_to_an_unavailable_shift(self):
        inactive = Shift.objects.create(
            office=self.office, shift_code="OFF", name="Retired", start_time=time(9), end_time=time(17),
            break_start_time=time(12), break_end_time=time(13), is_active=False,
        )
        response = self.client.post(reverse("attendance:schedule-update", args=[self.schedule.pk]), {
            "office": self.office.pk, "employee_assignment": self.assignment.pk, "shift": inactive.pk,
            "work_date": "2026-03-02", "status": "scheduled", "notes": "", "action": "save",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("shift", response.context["form"].errors)  # not a valid choice (message follows the site locale)
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.shift_id, self.morning.pk)

    def test_edit_with_inactive_saved_shift_still_saves_notes(self):
        self.morning.is_active = False
        self.morning.save()
        response = self.client.post(reverse("attendance:schedule-update", args=[self.schedule.pk]), {
            "office": self.office.pk, "employee_assignment": self.assignment.pk, "shift": self.morning.pk,
            "work_date": "2026-03-02", "status": "scheduled", "notes": "Updated", "action": "save",
        })
        self.assertEqual(response.status_code, 302)
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.notes, "Updated")

    def test_legacy_schedule_on_shift_without_breaks_can_still_be_edited(self):
        legacy = Shift.objects.create(
            office=self.office, shift_code="OLD", name="Legacy Shift", start_time=time(8), end_time=time(16)
        )
        schedule = self.make_schedule(shift=self.morning, work_date=date(2026, 3, 9))
        EmployeeSchedule.objects.filter(pk=schedule.pk).update(shift=legacy, scheduled_break_start_at=None, scheduled_break_end_at=None)
        response = self.client.post(reverse("attendance:schedule-update", args=[schedule.pk]), {
            "office": self.office.pk, "employee_assignment": self.assignment.pk, "shift": legacy.pk,
            "work_date": "2026-03-09", "status": "scheduled", "notes": "note only", "action": "save",
        })
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["form"].errors)
        schedule.refresh_from_db()
        self.assertEqual(schedule.notes, "note only")

    def test_shift_edit_does_not_change_the_saved_snapshot(self):
        before = (self.schedule.scheduled_start_at, self.schedule.scheduled_end_at)
        self.morning.start_time = time(9)
        self.morning.save()
        self.schedule.refresh_from_db()
        self.assertEqual((self.schedule.scheduled_start_at, self.schedule.scheduled_end_at), before)


class PreviewTests(ScheduleFormTestBase):
    def preview(self, **overrides):
        params = {
            "office": self.office.pk, "employee_assignment": self.assignment.pk,
            "shift": self.morning.pk, "work_date": "2026-03-02",
        }
        params.update(overrides)
        return self.client.get(reverse("attendance:schedule-preview"), params).json()

    def test_preview_shows_times_and_break_from_the_chosen_shift(self):
        data = self.preview()
        self.assertTrue(data["ok"])
        p = data["preview"]
        self.assertEqual(p["employee_id"], "EMP-001")
        self.assertEqual(p["employee_name"], "Maria Soares")
        self.assertEqual(p["office"], "Dili Office")
        self.assertEqual(p["position"], "Admin \u00b7 No department \u00b7 primary assignment")
        self.assertEqual(p["scheduled_start"], "2026-03-02 08:30")
        self.assertEqual(p["scheduled_break_start"], "2026-03-02 12:00")
        self.assertEqual(p["scheduled_break_end"], "2026-03-02 14:00")
        self.assertEqual(p["scheduled_end"], "2026-03-02 17:30")
        self.assertEqual(p["break_duration"], "120 minutes")
        self.assertEqual(p["work_duration"], "7 hours (break excluded)")
        self.assertEqual(p["sequence"], ["CHECK_IN", "BREAK_OUT", "BREAK_IN", "CHECK_OUT"])
        self.assertEqual(data["source"], "shift")

    def test_preview_changes_with_shift_and_date(self):
        data = self.preview(shift=self.night.pk, work_date="2026-03-05")
        p = data["preview"]
        self.assertEqual(p["scheduled_start"], "2026-03-05 22:00")
        self.assertEqual(p["scheduled_break_start"], "2026-03-06 01:00 (+1 day)")
        self.assertEqual(p["scheduled_end"], "2026-03-06 06:00 (+1 day)")
        self.assertEqual(p["work_duration"], "7 hours (break excluded)")

    def test_preview_warns_about_holidays_without_refusing(self):
        Holiday.objects.create(
            holiday_type=Holiday.HolidayType.NATIONAL, name="National Day",
            start_date=date(2026, 3, 2), end_date=date(2026, 3, 2),
        )
        data = self.preview()
        self.assertTrue(data["ok"])
        self.assertEqual([h["name"] for h in data["holidays"]], ["National Day"])

    def test_preview_warns_about_overlapping_schedule(self):
        self.make_schedule()
        data = self.preview()
        self.assertTrue(data["ok"])
        self.assertIn("overlapping schedule", data["warnings"][0])

    def test_preview_of_saved_schedule_uses_the_frozen_snapshot(self):
        schedule = self.make_schedule()
        self.morning.start_time = time(9)
        self.morning.save()
        data = self.preview(schedule=schedule.pk)
        self.assertEqual(data["source"], "saved")
        self.assertEqual(data["preview"]["scheduled_start"], "2026-03-02 08:30")

    def test_preview_rejects_mismatched_or_incomplete_choices(self):
        self.assertIn("different office", self.preview(shift=self.other_shift.pk)["errors"][0])
        self.assertIn("not assigned", self.preview(employee_assignment=self.other_assignment.pk)["errors"][0])
        self.assertTrue(self.client.get(reverse("attendance:schedule-preview"), {"office": self.office.pk}).json()["incomplete"])

    def test_preview_refuses_shift_without_break_times(self):
        legacy = Shift.objects.create(
            office=self.office, shift_code="OLD", name="Legacy Shift", start_time=time(8), end_time=time(16)
        )
        self.assertFalse(self.preview(shift=legacy.pk)["ok"])

    def test_preview_requires_login_and_schedule_permission(self):
        url = reverse("attendance:schedule-preview")
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 403)  # project uses raise_exception for access mixins
        self.user_with("view_employeeschedule")
        self.assertEqual(self.client.get(url).status_code, 403)  # viewing is not enough to preview a new schedule
        self.user_with("add_employeeschedule")
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_preview_does_not_expose_inactive_office(self):
        self.office.is_active = False
        self.office.save()
        self.assertFalse(self.preview()["ok"])


class DetailAndListTests(ScheduleFormTestBase):
    def setUp(self):
        super().setUp()
        self.schedule = self.make_schedule()

    def test_detail_shows_all_planned_fields_and_the_expected_sequence(self):
        response = self.client.get(reverse("attendance:schedule-detail", args=[self.schedule.pk]))
        for expected in (
            "EMP-001", "Maria Soares", "Dili Office", "Admin", "Morning Shift (MORNING)",
            "2026-03-02 08:30", "2026-03-02 12:00", "2026-03-02 14:00", "2026-03-02 17:30",
            "120 minutes", "7 hours (break excluded)", "Published", "Back to List", "Edit",
            "Check In", "Break Out", "Break In", "Check Out", "planned 2026-03-02 12:00",
        ):
            self.assertContains(response, expected)
        self.assertContains(response, "No attendance events have been recorded")
        self.assertNotContains(response, '<form method="post" novalidate>')

    def test_detail_lists_actual_events_separately_with_verification(self):
        record = AttendanceRecord.objects.create(schedule=self.schedule)
        AttendanceEvent.objects.create(
            attendance_record=record, event_type="check_in", occurred_at=self.schedule.scheduled_start_at,
            source=AttendanceEvent.Source.SYSTEM,
        )
        response = self.client.get(reverse("attendance:schedule-detail", args=[self.schedule.pk]))
        self.assertContains(response, "Recorded attendance events")
        self.assertNotContains(response, "No attendance events have been recorded")
        self.assertContains(response, "System generated")

    def test_detail_hides_edit_without_change_permission(self):
        self.user_with("view_employeeschedule")
        response = self.client.get(reverse("attendance:schedule-detail", args=[self.schedule.pk]))
        self.assertContains(response, "Back to List")
        self.assertNotContains(response, reverse("attendance:schedule-update", args=[self.schedule.pk]))

    def test_list_shows_planned_times_break_and_working_actions(self):
        response = self.client.get(reverse("attendance:schedule-list"))
        for expected in ("EMP-001 \u2014 Maria Soares", "2026-03-02 08:30", "2026-03-02 17:30", "12:00\u201314:00 (120 min)", "Actions"):
            self.assertContains(response, expected)
        self.assertContains(response, 'href="%s"' % reverse("attendance:schedule-detail", args=[self.schedule.pk]))
        self.assertContains(response, 'href="%s"' % reverse("attendance:schedule-update", args=[self.schedule.pk]))

    def test_list_hides_edit_button_without_change_permission(self):
        self.user_with("view_employeeschedule")
        response = self.client.get(reverse("attendance:schedule-list"))
        self.assertContains(response, reverse("attendance:schedule-detail", args=[self.schedule.pk]))
        self.assertNotContains(response, reverse("attendance:schedule-update", args=[self.schedule.pk]))

    def test_backend_blocks_edit_and_create_without_permission(self):
        self.user_with("view_employeeschedule")
        self.assertEqual(self.client.get(reverse("attendance:schedule-update", args=[self.schedule.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("attendance:schedule-create")).status_code, 403)


class DirectorScheduleScopeTests(ScheduleFixture, TestCase):
    def setUp(self):
        self.make_base()
        self.director_employee = Employee.objects.create(
            employee_id="DIR-001",
            full_name="Director Without Department",
            hire_date=date(2024, 1, 1),
            user=None,
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        self.director_assignment = EmployeeAssignment.objects.create(
            employee=self.director_employee,
            position=self.position,
            start_date=date(2024, 1, 1),
        )
        self.director = get_user_model().objects.create_user("director", password="pw")
        UserEmployeeLink.objects.create(user=self.director, employee=self.director_employee)
        director_group = Group.objects.create(name="Director")
        self.director.groups.add(director_group)
        self.director.user_permissions.add(
            Permission.objects.get(codename="view_employeeschedule"),
            Permission.objects.get(codename="view_employeerequest"),
            Permission.objects.get(codename="view_requestapproval"),
        )
        self.other_employee = Employee.objects.create(
            employee_id="EMP-002",
            full_name="Other Branch Employee",
            hire_date=date(2024, 1, 1),
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        self.other_assignment = EmployeeAssignment.objects.create(
            employee=self.other_employee,
            position=self.other_position,
            start_date=date(2024, 1, 1),
        )
        self.own_schedule = self.make_schedule()
        self.other_shift = Shift.objects.create(
            office=self.other_office,
            shift_code="OTHER",
            name="Other Branch Shift",
            start_time=time(9),
            end_time=time(17),
            break_start_time=time(12),
            break_end_time=time(13),
        )
        self.other_schedule = self.make_schedule(
            shift=self.other_shift,
            assignment=self.other_assignment,
        )
        self.client.force_login(self.director)

    def test_director_sees_only_own_branch_schedules(self):
        response = self.client.get(reverse("attendance:schedule-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.own_schedule.employee.full_name)
        self.assertNotContains(response, self.other_schedule.employee.full_name)
        self.assertContains(response, reverse("attendance:schedule-detail", args=[self.own_schedule.pk]))

        response = self.client.get(
            reverse("attendance:schedule-detail", args=[self.other_schedule.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_director_sidebar_hides_only_my_requests(self):
        response = self.client.get(reverse("attendance:schedule-list"))
        self.assertNotContains(response, "My Requests")
        self.assertContains(response, "Approval Inbox")
        self.assertContains(response, "All Requests")
