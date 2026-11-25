"""Tests for Shifts, Employee Schedules, Holidays, Attendance Policies and the four-event flow."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee, EmployeeAssignment
from organization.models import Office, Position

from .forms import EmployeeScheduleForm, ShiftForm
from .models import (
    AttendanceEvent,
    AttendancePolicy,
    AttendanceRecord,
    EmployeeSchedule,
    Holiday,
    Shift,
)
from .services.scheduling import build_schedule_snapshot, holidays_on, policy_for
from .services.workflow import (
    BREAK_IN,
    BREAK_OUT,
    CHECK_IN,
    CHECK_OUT,
    EventRejected,
    apply_event_to_record,
    decide_next_event,
    mark_incomplete_sessions,
    next_event_type,
    recalculate_record,
)
from .test_kiosk import KioskFixtureMixin

DILI = ZoneInfo("Asia/Dili")
WORK_DATE = date(2026, 3, 2)


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=DILI)


class ScheduleFixture:
    def make_base(self):
        self.office = Office.objects.create(office_code="DIL", office_name="Dili Office")
        self.other_office = Office.objects.create(office_code="BAU", office_name="Baucau Office")
        self.employee = Employee.objects.create(
            employee_id="EMP-001", full_name="Maria Soares", hire_date=date(2024, 1, 1),
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        self.position = Position.objects.create(office=self.office, position_code="ADM", position_title="Admin")
        self.other_position = Position.objects.create(office=self.other_office, position_code="BAU-ADM", position_title="Admin")
        self.assignment = EmployeeAssignment.objects.create(
            employee=self.employee, position=self.position, start_date=date(2024, 1, 1)
        )
        self.morning = Shift.objects.create(
            office=self.office, shift_code="MORNING", name="Morning Shift",
            start_time=time(8, 30), end_time=time(17, 30),
            break_start_time=time(12), break_end_time=time(14),
            grace_period_minutes=10, early_checkin_window_minutes=30,
        )
        self.night = Shift.objects.create(
            office=self.office, shift_code="NIGHT", name="Night Shift",
            start_time=time(22), end_time=time(6),
            break_start_time=time(1), break_end_time=time(2),
        )

    def make_schedule(self, shift=None, work_date=WORK_DATE, assignment=None):
        shift = shift or self.morning
        schedule = EmployeeSchedule(
            employee_assignment=assignment or self.assignment, shift=shift, work_date=work_date,
            status=EmployeeSchedule.Status.SCHEDULED,
        )
        schedule.apply_snapshot(build_schedule_snapshot(shift, work_date))
        schedule.full_clean()
        schedule.save()
        return schedule


class ShiftTests(ScheduleFixture, TestCase):
    def setUp(self):
        self.make_base()

    def test_morning_shift_derived_values(self):
        self.assertEqual(self.morning.break_duration_minutes, 120)
        self.assertEqual(self.morning.scheduled_work_minutes, 420)  # 9h span - 2h break
        self.assertFalse(self.morning.crosses_midnight)
        info = self.morning.summary
        self.assertEqual(info["time_range"], "08:30\u201317:30")
        self.assertEqual(info["break"], "12:00\u201314:00 (120 minutes)")
        self.assertEqual(info["work_duration"], "7 hours")
        self.assertEqual(info["required_events"], 4)

    def test_night_shift_crosses_midnight_automatically(self):
        self.assertTrue(self.night.crosses_midnight)
        self.assertEqual(self.night.break_duration_minutes, 60)
        self.assertEqual(self.night.scheduled_work_minutes, 7 * 60)

    def test_inconsistent_times_are_rejected(self):
        base = dict(office=self.office, shift_code="X", name="X")
        bad_cases = {
            "break outside shift": dict(start_time=time(8), end_time=time(16), break_start_time=time(18), break_end_time=time(19)),
            "break ends before it starts": dict(start_time=time(8), end_time=time(16), break_start_time=time(13), break_end_time=time(12)),
            "break at the very start": dict(start_time=time(8), end_time=time(16), break_start_time=time(8), break_end_time=time(9)),
            "break reaches shift end": dict(start_time=time(8), end_time=time(16), break_start_time=time(12), break_end_time=time(16)),
            "shift too long": dict(start_time=time(6), end_time=time(23), break_start_time=time(12), break_end_time=time(13)),
            "same start and end": dict(start_time=time(8), end_time=time(8), break_start_time=time(12), break_end_time=time(13)),
            "break times missing": dict(start_time=time(8), end_time=time(16)),
        }
        for label, values in bad_cases.items():
            with self.subTest(label), self.assertRaises(ValidationError):
                Shift(**base, **values).full_clean()

    def test_form_does_not_expose_derived_fields(self):
        form = ShiftForm()
        self.assertNotIn("crosses_midnight", form.fields)
        self.assertNotIn("break_duration_minutes", form.fields)

    def test_shift_in_use_cannot_be_deleted_and_office_is_locked(self):
        self.make_schedule()
        admin = get_user_model().objects.create_superuser("root", "root@example.com", "pw")
        self.client.force_login(admin)
        self.client.post(reverse("attendance:shift-delete", args=[self.morning.pk]))
        self.assertTrue(Shift.objects.filter(pk=self.morning.pk).exists())
        self.assertTrue(ShiftForm(instance=self.morning).fields["office"].disabled)

    def test_deactivated_shift_is_not_offered_for_new_schedules(self):
        self.morning.is_active = False
        self.morning.save()
        form = EmployeeScheduleForm()
        self.assertNotIn(self.morning, form.fields["shift"].queryset)
        self.assertIn(self.night, form.fields["shift"].queryset)


class ScheduleSnapshotTests(ScheduleFixture, TestCase):
    def setUp(self):
        self.make_base()

    def test_snapshot_uses_asia_dili_timezone(self):
        schedule = self.make_schedule()
        self.assertEqual(timezone.localtime(schedule.scheduled_start_at).strftime("%H:%M"), "08:30")
        self.assertEqual(schedule.scheduled_start_at.astimezone(ZoneInfo("UTC")).strftime("%d %H:%M"), "01 23:30")
        self.assertEqual(timezone.localtime(schedule.scheduled_break_start_at).strftime("%H:%M"), "12:00")
        self.assertEqual(timezone.localtime(schedule.scheduled_break_end_at).strftime("%H:%M"), "14:00")
        self.assertEqual(timezone.localtime(schedule.scheduled_end_at).strftime("%H:%M"), "17:30")
        self.assertEqual(schedule.scheduled_work_minutes, 420)

    def test_overnight_snapshot_dates_roll_to_next_day(self):
        schedule = self.make_schedule(shift=self.night)
        self.assertEqual(timezone.localtime(schedule.scheduled_start_at), at(WORK_DATE, 22))
        next_day = WORK_DATE + timedelta(days=1)
        self.assertEqual(timezone.localtime(schedule.scheduled_break_start_at), at(next_day, 1))
        self.assertEqual(timezone.localtime(schedule.scheduled_break_end_at), at(next_day, 2))
        self.assertEqual(timezone.localtime(schedule.scheduled_end_at), at(next_day, 6))

    def test_editing_the_shift_template_does_not_change_published_schedules(self):
        schedule = self.make_schedule()
        before = (schedule.scheduled_start_at, schedule.scheduled_end_at, schedule.scheduled_break_start_at)
        self.morning.start_time = time(9)
        self.morning.end_time = time(18)
        self.morning.break_start_time = time(12, 30)
        self.morning.break_end_time = time(13, 30)
        self.morning.grace_period_minutes = 0
        self.morning.save()
        schedule.refresh_from_db()
        self.assertEqual(before, (schedule.scheduled_start_at, schedule.scheduled_end_at, schedule.scheduled_break_start_at))
        self.assertEqual(schedule.effective_grace_minutes, 10)  # frozen value, not the new shift value

    def test_schedule_form_rejects_assignment_from_another_office(self):
        outsider = Employee.objects.create(employee_id="EMP-002", full_name="Joao", hire_date=date(2024, 1, 1))
        foreign = EmployeeAssignment.objects.create(employee=outsider, position=self.other_position, start_date=date(2024, 1, 1))
        form = EmployeeScheduleForm(data={
            "office": self.office.pk, "employee_assignment": foreign.pk, "shift": self.morning.pk,
            "work_date": WORK_DATE.isoformat(), "status": "scheduled",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("employee_assignment", form.errors)

    def test_overlapping_schedule_for_same_employee_is_rejected(self):
        self.make_schedule()
        form = EmployeeScheduleForm(data={
            "office": self.office.pk, "employee_assignment": self.assignment.pk, "shift": self.morning.pk,
            "work_date": WORK_DATE.isoformat(), "status": "scheduled",
        })
        self.assertFalse(form.is_valid())
        self.assertTrue(form.errors)

    def test_cancelled_schedule_does_not_block_a_new_one(self):
        old = self.make_schedule()
        old.status = EmployeeSchedule.Status.CANCELLED
        old.save()
        self.assertIsNotNone(self.make_schedule())

    def test_assignment_not_active_on_work_date_is_rejected(self):
        self.assignment.end_date = date(2026, 1, 31)
        self.assignment.save()
        schedule = EmployeeSchedule(employee_assignment=self.assignment, shift=self.morning, work_date=WORK_DATE)
        schedule.apply_snapshot(build_schedule_snapshot(self.morning, WORK_DATE))
        with self.assertRaises(ValidationError):
            schedule.full_clean()

    def test_schedule_form_summary_and_preview_does_not_save(self):
        admin = get_user_model().objects.create_superuser("root", "root@example.com", "pw")
        self.client.force_login(admin)
        data = {
            "office": self.office.pk, "employee_assignment": self.assignment.pk, "shift": self.morning.pk,
            "work_date": WORK_DATE.isoformat(), "status": "scheduled", "notes": "", "action": "preview",
        }
        response = self.client.post(reverse("attendance:schedule-create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "nothing is saved yet")
        self.assertContains(response, "08:30")
        self.assertContains(response, "Check In")
        self.assertFalse(EmployeeSchedule.objects.exists())
        data["action"] = "save"
        self.assertEqual(self.client.post(reverse("attendance:schedule-create"), data).status_code, 302)
        self.assertEqual(EmployeeSchedule.objects.count(), 1)


class HolidayTests(ScheduleFixture, TestCase):
    def setUp(self):
        self.make_base()
        self.day = WORK_DATE

    def test_global_vs_office_specific_scope(self):
        Holiday.objects.create(name="National Day", holiday_type="national", start_date=self.day, end_date=self.day)
        Holiday.objects.create(name="Dili Fair", holiday_type="office", office=self.office, start_date=self.day, end_date=self.day)
        self.assertEqual({h.name for h in holidays_on(self.office, self.day)}, {"National Day", "Dili Fair"})
        self.assertEqual({h.name for h in holidays_on(self.other_office, self.day)}, {"National Day"})
        self.assertFalse(holidays_on(self.office, self.day + timedelta(days=1)).exists())

    def test_scope_rules_are_validated(self):
        with self.assertRaises(ValidationError):  # office-specific without office
            Holiday(name="A", holiday_type="office", start_date=self.day, end_date=self.day).full_clean()
        with self.assertRaises(ValidationError):  # national holiday naming an office
            Holiday(name="B", holiday_type="national", office=self.office, start_date=self.day, end_date=self.day).full_clean()
        with self.assertRaises(ValidationError):  # end before start
            Holiday(name="C", holiday_type="national", start_date=self.day, end_date=self.day - timedelta(days=1)).full_clean()

    def test_name_alone_does_not_decide_scope(self):
        holiday = Holiday.objects.create(name="Office National Day", holiday_type="institution", start_date=self.day, end_date=self.day)
        self.assertTrue(holiday.covers_all_offices)

    def test_duplicate_holiday_is_rejected(self):
        Holiday.objects.create(name="National Day", holiday_type="national", start_date=self.day, end_date=self.day)
        with self.assertRaises(ValidationError):
            Holiday(name="national day", holiday_type="national", start_date=self.day, end_date=self.day).full_clean()

    def test_scheduling_on_a_holiday_is_allowed_with_a_warning(self):
        Holiday.objects.create(name="National Day", holiday_type="national", start_date=self.day, end_date=self.day)
        form = EmployeeScheduleForm(data={
            "office": self.office.pk, "employee_assignment": self.assignment.pk, "shift": self.morning.pk,
            "work_date": self.day.isoformat(), "status": "scheduled",
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual([h.name for h in form.holiday_notice], ["National Day"])
        admin = get_user_model().objects.create_superuser("root", "root@example.com", "pw")
        self.client.force_login(admin)
        response = self.client.post(reverse("attendance:schedule-create"), {
            "office": self.office.pk, "employee_assignment": self.assignment.pk, "shift": self.morning.pk,
            "work_date": self.day.isoformat(), "status": "scheduled", "action": "save",
        }, follow=True)
        self.assertContains(response, "is a holiday")
        self.assertEqual(EmployeeSchedule.objects.count(), 1)  # specially scheduled work on a holiday is kept


class PolicyTests(ScheduleFixture, TestCase):
    def setUp(self):
        self.make_base()

    def _policy(self, **kwargs):
        defaults = dict(policy_name="P", effective_from=date(2026, 1, 1), require_liveness=True)
        defaults.update(kwargs)
        return AttendancePolicy(**defaults)

    def test_effective_dates_select_the_policy_for_the_event_date(self):
        old = self._policy(policy_name="Old", office=self.office, effective_until=date(2026, 2, 28))
        old.full_clean(); old.save()
        new = self._policy(policy_name="New", office=self.office, effective_from=date(2026, 3, 1))
        new.full_clean(); new.save()
        self.assertEqual(policy_for(self.office, date(2026, 2, 15)), old)
        self.assertEqual(policy_for(self.office, date(2026, 3, 15)), new)
        self.assertIsNone(policy_for(self.office, date(2025, 12, 31)))

    def test_office_policy_overrides_institution_wide_policy(self):
        general = self._policy(policy_name="General"); general.save()
        local = self._policy(policy_name="Local", office=self.office); local.save()
        self.assertEqual(policy_for(self.office, date(2026, 3, 2)), local)
        self.assertEqual(policy_for(self.other_office, date(2026, 3, 2)), general)

    def test_overlapping_active_policies_of_same_scope_are_rejected(self):
        self._policy(office=self.office).save()
        with self.assertRaises(ValidationError):
            self._policy(policy_name="Second", office=self.office, effective_from=date(2026, 6, 1)).full_clean()
        self._policy(policy_name="Other office", office=self.other_office, effective_from=date(2026, 6, 1)).full_clean()

    def test_invalid_period_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._policy(effective_until=date(2025, 1, 1)).full_clean()

    def test_policy_does_not_carry_its_own_grace_or_shift_times(self):
        names = {f.name for f in AttendancePolicy._meta.get_fields()}
        self.assertNotIn("grace_period_minutes", names)
        self.assertNotIn("start_time", names)


class FourEventCalculationTests(ScheduleFixture, TestCase):
    """Service-level checks of ordering, work duration and incomplete sessions."""

    def setUp(self):
        self.make_base()
        self.policy = AttendancePolicy.objects.create(
            policy_name="P", office=self.office, effective_from=date(2026, 1, 1), absent_after_minutes=180,
            require_liveness=True,
        )

    def _store(self, schedule, kind, when, policy=None):
        record = AttendanceRecord.objects.filter(schedule=schedule).first() or AttendanceRecord.objects.create(schedule=schedule)
        event = AttendanceEvent(
            attendance_record=record, event_type=kind, occurred_at=when,
            source=AttendanceEvent.Source.SYSTEM,
        )
        event.full_clean()
        event.save()
        apply_event_to_record(record, schedule, kind, when, policy or self.policy)
        recalculate_record(record, schedule)
        record.save()
        return record

    def test_on_time_example_gives_seven_hours_and_no_late(self):
        schedule = self.make_schedule()
        for kind, hour, minute in ((CHECK_IN, 8, 30), (BREAK_OUT, 12, 0), (BREAK_IN, 14, 0), (CHECK_OUT, 17, 30)):
            record = self._store(schedule, kind, at(WORK_DATE, hour, minute))
        self.assertEqual(record.total_work_minutes, 420)
        self.assertEqual(record.late_minutes, 0)
        self.assertEqual(record.early_leave_minutes, 0)
        self.assertEqual(record.status, AttendanceRecord.Status.PRESENT)

    def test_break_time_is_not_counted_as_work(self):
        schedule = self.make_schedule()
        for kind, hour, minute in ((CHECK_IN, 8, 30), (BREAK_OUT, 12, 0), (BREAK_IN, 15, 0), (CHECK_OUT, 17, 30)):
            record = self._store(schedule, kind, at(WORK_DATE, hour, minute))
        self.assertEqual(record.total_work_minutes, 210 + 150)  # 3h30 + 2h30, the 3h break is excluded

    def test_late_depends_on_check_in_and_grace_only(self):
        schedule = self.make_schedule()
        record = self._store(schedule, CHECK_IN, at(WORK_DATE, 8, 38))  # inside the 10 minute grace
        self.assertEqual(record.status, AttendanceRecord.Status.PRESENT)
        other = self.make_schedule(work_date=WORK_DATE + timedelta(days=1))
        late = self._store(other, CHECK_IN, at(WORK_DATE + timedelta(days=1), 8, 50))
        self.assertEqual(late.status, AttendanceRecord.Status.LATE)
        self.assertEqual(late.late_minutes, 10)
        for kind, hour, minute in ((BREAK_OUT, 12, 0), (BREAK_IN, 14, 30), (CHECK_OUT, 17, 30)):
            late = self._store(other, kind, at(WORK_DATE + timedelta(days=1), hour, minute))
        self.assertEqual(late.late_minutes, 10)  # a late Break In never changes morning lateness
        self.assertEqual(late.status, AttendanceRecord.Status.LATE)

    def test_grace_period_30_minutes_check_in_0830_is_on_time(self):
        shift = Shift.objects.create(
            office=self.office, shift_code="GRACE30", name="Grace 30",
            start_time=time(8, 30), end_time=time(17, 30),
            break_start_time=time(12), break_end_time=time(14),
            grace_period_minutes=30, early_checkin_window_minutes=30,
        )
        schedule = self.make_schedule(shift=shift, work_date=WORK_DATE + timedelta(days=10))
        record = self._store(schedule, CHECK_IN, at(WORK_DATE + timedelta(days=10), 8, 30))
        self.assertEqual(record.status, AttendanceRecord.Status.PRESENT)
        self.assertEqual(record.late_minutes, 0)

    def test_grace_period_30_minutes_check_in_0845_is_on_time(self):
        shift = Shift.objects.create(
            office=self.office, shift_code="GRACE30B", name="Grace 30 B",
            start_time=time(8, 30), end_time=time(17, 30),
            break_start_time=time(12), break_end_time=time(14),
            grace_period_minutes=30, early_checkin_window_minutes=30,
        )
        schedule = self.make_schedule(shift=shift, work_date=WORK_DATE + timedelta(days=11))
        record = self._store(schedule, CHECK_IN, at(WORK_DATE + timedelta(days=11), 8, 45))
        self.assertEqual(record.status, AttendanceRecord.Status.PRESENT)
        self.assertEqual(record.late_minutes, 0)

    def test_grace_period_30_minutes_check_in_0859_is_on_time(self):
        shift = Shift.objects.create(
            office=self.office, shift_code="GRACE30C", name="Grace 30 C",
            start_time=time(8, 30), end_time=time(17, 30),
            break_start_time=time(12), break_end_time=time(14),
            grace_period_minutes=30, early_checkin_window_minutes=30,
        )
        schedule = self.make_schedule(shift=shift, work_date=WORK_DATE + timedelta(days=12))
        record = self._store(schedule, CHECK_IN, at(WORK_DATE + timedelta(days=12), 8, 59))
        self.assertEqual(record.status, AttendanceRecord.Status.PRESENT)
        self.assertEqual(record.late_minutes, 0)

    def test_grace_period_30_minutes_check_in_0900_is_on_time(self):
        shift = Shift.objects.create(
            office=self.office, shift_code="GRACE30D", name="Grace 30 D",
            start_time=time(8, 30), end_time=time(17, 30),
            break_start_time=time(12), break_end_time=time(14),
            grace_period_minutes=30, early_checkin_window_minutes=30,
        )
        schedule = self.make_schedule(shift=shift, work_date=WORK_DATE + timedelta(days=13))
        record = self._store(schedule, CHECK_IN, at(WORK_DATE + timedelta(days=13), 9, 0))
        self.assertEqual(record.status, AttendanceRecord.Status.PRESENT)
        self.assertEqual(record.late_minutes, 0)

    def test_grace_period_30_minutes_check_in_0901_is_late(self):
        shift = Shift.objects.create(
            office=self.office, shift_code="GRACE30E", name="Grace 30 E",
            start_time=time(8, 30), end_time=time(17, 30),
            break_start_time=time(12), break_end_time=time(14),
            grace_period_minutes=30, early_checkin_window_minutes=30,
        )
        schedule = self.make_schedule(shift=shift, work_date=WORK_DATE + timedelta(days=14))
        record = self._store(schedule, CHECK_IN, at(WORK_DATE + timedelta(days=14), 9, 1))
        self.assertEqual(record.status, AttendanceRecord.Status.LATE)
        self.assertEqual(record.late_minutes, 1)

    def test_grace_period_check_in_stored_even_after_grace_expires(self):
        shift = Shift.objects.create(
            office=self.office, shift_code="GRACE30F", name="Grace 30 F",
            start_time=time(8, 30), end_time=time(17, 30),
            break_start_time=time(12), break_end_time=time(14),
            grace_period_minutes=30, early_checkin_window_minutes=30,
        )
        schedule = self.make_schedule(shift=shift, work_date=WORK_DATE + timedelta(days=15))
        record = self._store(schedule, CHECK_IN, at(WORK_DATE + timedelta(days=15), 10, 0))
        self.assertEqual(record.status, AttendanceRecord.Status.LATE)
        self.assertEqual(record.late_minutes, 60)

    def test_shift_with_different_grace_period_uses_its_own_value(self):
        shift_short = Shift.objects.create(
            office=self.office, shift_code="SHORT", name="Short Grace",
            start_time=time(8, 30), end_time=time(17, 30),
            break_start_time=time(12), break_end_time=time(14),
            grace_period_minutes=5, early_checkin_window_minutes=30,
        )
        shift_long = Shift.objects.create(
            office=self.office, shift_code="LONG", name="Long Grace",
            start_time=time(8, 30), end_time=time(17, 30),
            break_start_time=time(12), break_end_time=time(14),
            grace_period_minutes=60, early_checkin_window_minutes=30,
        )
        schedule_short = self.make_schedule(shift=shift_short, work_date=WORK_DATE + timedelta(days=16))
        record_short = self._store(schedule_short, CHECK_IN, at(WORK_DATE + timedelta(days=16), 8, 50))
        self.assertEqual(record_short.status, AttendanceRecord.Status.LATE)
        self.assertEqual(record_short.late_minutes, 15)

        schedule_long = self.make_schedule(shift=shift_long, work_date=WORK_DATE + timedelta(days=17))
        record_long = self._store(schedule_long, CHECK_IN, at(WORK_DATE + timedelta(days=17), 8, 50))
        self.assertEqual(record_long.status, AttendanceRecord.Status.PRESENT)
        self.assertEqual(record_long.late_minutes, 0)

    def test_effective_grace_falls_back_to_shift_when_schedule_value_is_none(self):
        shift = Shift.objects.create(
            office=self.office, shift_code="FALLBACK", name="Fallback Grace",
            start_time=time(8, 30), end_time=time(17, 30),
            break_start_time=time(12), break_end_time=time(14),
            grace_period_minutes=45, early_checkin_window_minutes=30,
        )
        # Create schedule without apply_snapshot so grace_period_minutes is None
        schedule = EmployeeSchedule(
            employee_assignment=self.assignment, shift=shift,
            work_date=WORK_DATE + timedelta(days=18),
            status=EmployeeSchedule.Status.SCHEDULED,
            scheduled_start_at=at(WORK_DATE + timedelta(days=18), 8, 30),
            scheduled_end_at=at(WORK_DATE + timedelta(days=18), 17, 30),
        )
        # grace_period_minutes is None because apply_snapshot was not called
        self.assertIsNone(schedule.grace_period_minutes)
        self.assertEqual(schedule.effective_grace_minutes, 45)

    def test_overnight_calculation(self):
        schedule = self.make_schedule(shift=self.night)
        nxt = WORK_DATE + timedelta(days=1)
        for kind, day, hour in ((CHECK_IN, WORK_DATE, 22), (BREAK_OUT, nxt, 1), (BREAK_IN, nxt, 2), (CHECK_OUT, nxt, 6)):
            record = self._store(schedule, kind, at(day, hour))
        self.assertEqual(record.total_work_minutes, 180 + 240)
        self.assertEqual(record.status, AttendanceRecord.Status.PRESENT)

    def test_events_must_follow_the_required_order(self):
        schedule = self.make_schedule()
        record = AttendanceRecord.objects.create(schedule=schedule)
        for wrong in (BREAK_OUT, BREAK_IN, CHECK_OUT):
            with self.subTest(wrong=wrong), self.assertRaises(ValidationError):
                AttendanceEvent(
                    attendance_record=record, event_type=wrong, occurred_at=at(WORK_DATE, 9),
                    source=AttendanceEvent.Source.SYSTEM,
                ).full_clean()
        self._store(schedule, CHECK_IN, at(WORK_DATE, 8, 30))
        with self.assertRaises(ValidationError):  # Break In before Break Out
            AttendanceEvent(
                attendance_record=record, event_type=BREAK_IN, occurred_at=at(WORK_DATE, 13),
                source=AttendanceEvent.Source.SYSTEM,
            ).full_clean()
        self._store(schedule, BREAK_OUT, at(WORK_DATE, 12))
        # Check Out setelah Check In (break events masih kosong) diizinkan;
        # kejadian yang hilang ditandai pada finalisasi, bukan dengan
        # memblokir Check Out.
        AttendanceEvent(
            attendance_record=record, event_type=CHECK_OUT, occurred_at=at(WORK_DATE, 13),
            source=AttendanceEvent.Source.SYSTEM,
        ).full_clean()
        self.assertEqual(next_event_type(record), BREAK_IN)

    def test_duplicate_required_event_is_blocked_by_the_database(self):
        schedule = self.make_schedule()
        record = self._store(schedule, CHECK_IN, at(WORK_DATE, 8, 30))
        with self.assertRaises(IntegrityError), transaction.atomic():
            AttendanceEvent.objects.create(
                attendance_record=record, event_type=CHECK_IN, occurred_at=at(WORK_DATE, 8, 31),
                source=AttendanceEvent.Source.SYSTEM,
            )
        self.assertEqual(AttendanceEvent.objects.count(), 1)

    def test_decide_next_event_follows_sequence_and_rejects_double_scan(self):
        schedule = self.make_schedule()
        self.assertEqual(decide_next_event(schedule, None, at(WORK_DATE, 8, 30), self.policy), CHECK_IN)
        record = self._store(schedule, CHECK_IN, at(WORK_DATE, 8, 30))
        with self.assertRaises(EventRejected) as caught:
            decide_next_event(schedule, record, at(WORK_DATE, 8, 30) + timedelta(seconds=5), self.policy)
        self.assertEqual(caught.exception.stage, "duplicate")
        self.assertEqual(decide_next_event(schedule, record, at(WORK_DATE, 12), self.policy), BREAK_OUT)

    def test_check_in_window_and_absent_threshold(self):
        schedule = self.make_schedule()
        with self.assertRaises(EventRejected):  # earlier than the 30 minute early check-in window
            decide_next_event(schedule, None, at(WORK_DATE, 7, 45), self.policy)
        # The absent threshold no longer rejects the check-in: an arrival after it
        # but while the schedule is still open is recorded as a late check-in.
        self.assertEqual(decide_next_event(schedule, None, at(WORK_DATE, 11, 31), self.policy), CHECK_IN)  # late, not absent
        self.assertEqual(decide_next_event(schedule, None, at(WORK_DATE, 9, 45), self.policy), CHECK_IN)  # late, not absent

    def test_missing_break_out_does_not_block_check_out(self):
        # Break Out dilewatkan, tetapi pegawai tetap dapat mencatat Check Out
        # pada jendela yang valid. Break Out/Break In tetap kosong dan
        # ditandai Incomplete pada finalisasi, bukan mengeperlakukan pegawai.
        schedule = self.make_schedule()
        record = self._store(schedule, CHECK_IN, at(WORK_DATE, 8, 30))
        # Break Out window sudah lewat (14:00), scan pukul 14:05: Break Out
        # masih diperbolehkan (lewat, tidak ditolak).
        self.assertEqual(
            decide_next_event(schedule, record, at(WORK_DATE, 14, 5), self.policy), BREAK_OUT
        )
        # Check Out pada jendela valid (setelah shift selesai 17:30, sebelum
        # tutup operasional 19:30) diperbolehkan meskipun Break Out/In masih kosong.
        self.assertEqual(
            decide_next_event(schedule, record, at(WORK_DATE, 18, 0), self.policy), CHECK_OUT
        )
        # Menyimpan CHECK_OUT aktual: BREAK_OUT dan BREAK_IN tetap kosong.
        self._store(schedule, CHECK_OUT, at(WORK_DATE, 18, 0))
        record.refresh_from_db()
        # Check-in tepat waktu → status PRESENT (LATE hanya ditetapkan saat
        # check-in melewati grace period, bukan pada check-out).
        self.assertEqual(record.status, AttendanceRecord.Status.PRESENT)
        self.assertEqual(record.late_minutes, 0)  # check-in tepat waktu
        self.assertEqual(AttendanceEvent.objects.count(), 2)  # Check In + Check Out only

    def test_incomplete_session_is_not_final_and_not_absent(self):
        schedule = self.make_schedule()
        record = self._store(schedule, CHECK_IN, at(WORK_DATE, 8, 30))
        self._store(schedule, BREAK_OUT, at(WORK_DATE, 12))
        record.refresh_from_db()
        self.assertEqual(record.total_work_minutes, 0)  # not final until all four events exist
        self.assertNotEqual(record.status, AttendanceRecord.Status.ABSENT)
        flagged = mark_incomplete_sessions(now=at(WORK_DATE, 17, 30) + timedelta(hours=3))
        record.refresh_from_db()
        self.assertEqual(flagged, 1)
        self.assertEqual(record.status, AttendanceRecord.Status.INCOMPLETE)
        self.assertEqual(record.review_status, AttendanceRecord.ReviewStatus.PENDING)
        self.assertIn("Break In", record.remarks)
        self.assertEqual(AttendanceEvent.objects.count(), 2)  # no invented events


class KioskFourEventTests(KioskFixtureMixin, TestCase):
    """End-to-end through the kiosk endpoint, reusing the face-recognition test fixtures."""

    def _snapshot_schedule(self):
        start = self.now - timedelta(minutes=1)
        EmployeeSchedule.objects.filter(pk=self.schedule.pk).update(
            scheduled_start_at=start,
            scheduled_break_start_at=start + timedelta(hours=2),
            scheduled_break_end_at=start + timedelta(hours=4),
            scheduled_end_at=start + timedelta(hours=8),
            grace_period_minutes=5, early_checkin_window_minutes=30,
        )

    def test_double_click_does_not_store_two_events(self):
        first = self._post_capture(now=self.now)
        again = self._post_capture(now=self.now + timedelta(seconds=3))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["stage"], "duplicate")
        self.assertEqual(AttendanceEvent.objects.count(), 1)
        self.assertEqual(again.json()["next_action"], "break_start")

    def test_response_reports_next_action_and_last_event_time(self):
        response = self._post_capture(now=self.now).json()
        self.assertEqual(response["action_label"], "Check In")
        self.assertEqual(response["next_action_label"], "Break Out")
        self.assertIn("Next action: Break Out", response["message"])
        self.assertTrue(response["last_event_at"])

    def test_check_out_allowed_with_missing_break_events(self):
        # Check In saja, lalu Check Out pada jendela valid meskipun
        # Break Out/Break In belum dicatat. Tidak ada event palsu,
        # tidak ada Pending review selama scan.
        self._snapshot_schedule()
        self._post_capture(now=self.now)  # Check In
        # Check Out pada jendela valid (setelah shift selesai, sebelum tutup operasional)
        # Break Out/Break In masih kosong, tapi Check Out diperbolehkan.
        response = self._post_capture(now=self.now + timedelta(hours=8, minutes=5))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], CHECK_OUT)
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        self.assertEqual(AttendanceEvent.objects.count(), 2)  # Check In + Check Out only
        # Status belum final pada saat scan; Incomplete ditetapkan setelah penutupan.
        self.assertNotEqual(record.status, AttendanceRecord.Status.INCOMPLETE)
        self.assertEqual(record.review_status, AttendanceRecord.ReviewStatus.NOT_REQUIRED)

    def test_unpublished_schedule_cannot_receive_attendance(self):
        EmployeeSchedule.objects.filter(pk=self.schedule.pk).update(status=EmployeeSchedule.Status.PLANNED)
        response = self._post_capture(now=self.now)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["stage"], "schedule")
        self.assertFalse(AttendanceEvent.objects.exists())

    def test_record_keeps_the_policy_it_started_under(self):
        self._post_capture(now=self.now)
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        policy = record.attendance_policy
        self.assertIsNotNone(policy)
        AttendancePolicy.objects.filter(pk=policy.pk).update(absent_after_minutes=1)
        record.refresh_from_db()
        self.assertEqual(record.attendance_policy_id, policy.pk)

    def test_check_in_rejected_without_policy(self):
        AttendancePolicy.objects.all().delete()
        response = self._post_capture(now=self.now)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["stage"], "policy")
        self.assertFalse(AttendanceEvent.objects.exists())


class PermissionTests(ScheduleFixture, TestCase):
    def setUp(self):
        self.make_base()
        self.viewer = get_user_model().objects.create_user("viewer", password="pw")
        for codename in ("view_shift", "view_employeeschedule", "view_holiday", "view_attendancepolicy"):
            self.viewer.user_permissions.add(Permission.objects.get(codename=codename, content_type__app_label="attendance"))

    def test_anonymous_and_unprivileged_users_are_blocked(self):
        urls = [reverse(f"attendance:{n}") for n in ("shift-list", "schedule-list", "holiday-list", "policy-list")]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(get_user_model().objects.create_user("nobody", password="pw"))
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403)

    def test_view_permission_does_not_allow_creating(self):
        self.client.force_login(self.viewer)
        for name in ("shift-list", "schedule-list", "holiday-list", "policy-list"):
            self.assertEqual(self.client.get(reverse(f"attendance:{name}")).status_code, 200)
        for name in ("shift-create", "schedule-create", "holiday-create", "policy-create"):
            self.assertEqual(self.client.get(reverse(f"attendance:{name}")).status_code, 403)
            self.assertEqual(self.client.post(reverse(f"attendance:{name}"), {}).status_code, 403)
        self.assertFalse(Shift.objects.filter(shift_code="HACK").exists())

    def test_schedule_form_only_offers_active_assigned_employees_of_active_offices(self):
        self.other_office.is_active = False
        self.other_office.save()
        inactive = Employee.objects.create(
            employee_id="EMP-9", full_name="Gone", hire_date=date(2024, 1, 1),
            employment_status=Employee.EmploymentStatus.INACTIVE,
        )
        EmployeeAssignment.objects.create(employee=inactive, position=self.position, start_date=date(2024, 1, 1))
        admin = get_user_model().objects.create_superuser("root", "root@example.com", "pw")
        form = EmployeeScheduleForm(user=admin)
        self.assertEqual(list(form.fields["employee_assignment"].queryset), [self.assignment])
        self.assertNotIn(self.other_office, form.fields["office"].queryset)


class ScheduleOutcomeTests(ScheduleFixture, TestCase):
    """Absent / Incomplete finalisasi berdasarkan jadwal, shift dan policy."""

    def setUp(self):
        self.make_base()
        self.policy = AttendancePolicy.objects.create(
            policy_name="P", office=self.office, effective_from=date(2026, 1, 1), absent_after_minutes=180,
            require_liveness=True,
        )
        self.work_date = WORK_DATE
        self.schedule = self.make_schedule(work_date=self.work_date)
        # Cutoff = scheduled_end + CHECKOUT_GRACE_MINUTES (120 menit); 17:30 + 120m = 19:30.
        self.closing_now = at(self.work_date, 19, 31)

    def _store(self, schedule, kind, when, policy=None):
        record = AttendanceRecord.objects.filter(schedule=schedule).first() or AttendanceRecord.objects.create(schedule=schedule)
        event = AttendanceEvent(
            attendance_record=record, event_type=kind, occurred_at=when,
            source=AttendanceEvent.Source.SYSTEM,
        )
        event.full_clean()
        event.save()
        apply_event_to_record(record, schedule, kind, when, policy or self.policy)
        recalculate_record(record, schedule)
        record.save()
        return record

    def _run_reconciliation(self, now=None, dry_run=False, start_date=None, end_date=None):
        return mark_incomplete_sessions(
            now=now or self.closing_now, dry_run=dry_run,
            start_date=start_date, end_date=end_date,
        )

    def test_schedule_not_yet_started_is_not_absent(self):
        future_schedule = self.make_schedule(work_date=self.work_date + timedelta(days=5))
        self._run_reconciliation(now=at(self.work_date, 8, 0))
        future_schedule.refresh_from_db()
        self.assertFalse(AttendanceRecord.objects.filter(schedule=future_schedule).exists())

    def test_schedule_in_progress_without_scan_is_not_final(self):
        # Schedule started (08:30) but not ended (17:30); now is 12:00, before cutoff.
        self._run_reconciliation(now=at(self.work_date, 12, 0))
        self.assertFalse(AttendanceRecord.objects.filter(schedule=self.schedule).exists())

    def test_schedule_closed_without_events_is_absent(self):
        flagged = self._run_reconciliation()
        self.assertEqual(flagged, 1)
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        self.assertEqual(record.status, AttendanceRecord.Status.ABSENT)
        self.assertEqual(record.review_status, AttendanceRecord.ReviewStatus.NOT_REQUIRED)
        self.assertIn("no check-in recorded", record.remarks)

    def test_check_in_only_becomes_incomplete_after_closing(self):
        record = self._store(self.schedule, CHECK_IN, at(self.work_date, 8, 30))
        flagged = self._run_reconciliation()
        self.assertEqual(flagged, 1)
        record.refresh_from_db()
        self.assertEqual(record.status, AttendanceRecord.Status.INCOMPLETE)
        self.assertEqual(record.review_status, AttendanceRecord.ReviewStatus.PENDING)
        self.assertIn("Break Out", record.remarks)
        self.assertEqual(AttendanceEvent.objects.count(), 1)

    def test_missing_break_out_becomes_incomplete(self):
        self._store(self.schedule, CHECK_IN, at(self.work_date, 8, 30))
        self._store(self.schedule, BREAK_OUT, at(self.work_date, 12, 0))
        # Break In and Check Out missing.
        flagged = self._run_reconciliation()
        self.assertEqual(flagged, 1)
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        self.assertEqual(record.status, AttendanceRecord.Status.INCOMPLETE)
        self.assertIn("Break In", record.remarks)

    def test_missing_break_in_becomes_incomplete(self):
        self._store(self.schedule, CHECK_IN, at(self.work_date, 8, 30))
        self._store(self.schedule, BREAK_OUT, at(self.work_date, 12, 0))
        self._store(self.schedule, BREAK_IN, at(self.work_date, 14, 0))
        # Check Out missing.
        flagged = self._run_reconciliation()
        self.assertEqual(flagged, 1)
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        self.assertEqual(record.status, AttendanceRecord.Status.INCOMPLETE)
        self.assertIn("Check Out", record.remarks)

    def test_missing_check_out_becomes_incomplete(self):
        self._store(self.schedule, CHECK_IN, at(self.work_date, 8, 30))
        self._store(self.schedule, BREAK_OUT, at(self.work_date, 12, 0))
        self._store(self.schedule, BREAK_IN, at(self.work_date, 14, 0))
        flagged = self._run_reconciliation()
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        self.assertEqual(record.status, AttendanceRecord.Status.INCOMPLETE)
        self.assertIn("Check Out", record.remarks)

    def test_four_events_on_time_is_present(self):
        for kind, hour, minute in ((CHECK_IN, 8, 30), (BREAK_OUT, 12, 0), (BREAK_IN, 14, 0), (CHECK_OUT, 17, 30)):
            self._store(self.schedule, kind, at(self.work_date, hour, minute))
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        self.assertEqual(record.status, AttendanceRecord.Status.PRESENT)
        self.assertEqual(record.late_minutes, 0)
        self.assertEqual(record.total_work_minutes, 420)

    def test_four_events_late_is_late_with_minutes(self):
        # Check-in 08:45 = 15 min after start, grace 10 min -> 5 late minutes.
        self._store(self.schedule, CHECK_IN, at(self.work_date, 8, 45))
        for kind, hour, minute in ((BREAK_OUT, 12, 0), (BREAK_IN, 14, 0), (CHECK_OUT, 17, 30)):
            self._store(self.schedule, kind, at(self.work_date, hour, minute))
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        self.assertEqual(record.status, AttendanceRecord.Status.LATE)
        self.assertEqual(record.late_minutes, 5)

    def test_late_plus_missing_events_stays_incomplete_with_late_minutes(self):
        # Check-in terlambat, kemudian Break Out tersimpan tapi sisanya kurang.
        self._store(self.schedule, CHECK_IN, at(self.work_date, 8, 45))
        self._store(self.schedule, BREAK_OUT, at(self.work_date, 12, 0))
        flagged = self._run_reconciliation()
        self.assertEqual(flagged, 1)
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        self.assertEqual(record.status, AttendanceRecord.Status.INCOMPLETE)
        self.assertEqual(record.late_minutes, 5)
        self.assertIn("Break In", record.remarks)
        self.assertIn("Check Out", record.remarks)

    def test_midnight_shift_not_finalized_too_early(self):
        night = self.make_schedule(shift=self.night, work_date=self.work_date)
        # Jam 17:30 di hari kerja: shift malam (22:00-06:00 besok) belum dimulai.
        self._run_reconciliation(now=at(self.work_date, 17, 30))
        self.assertFalse(AttendanceRecord.objects.filter(schedule=night).exists())
        # Bahkan setelah shift seharusnya berjalan tapi belum tutup jendela operasional.
        self._run_reconciliation(now=at(self.work_date, 21, 0))
        self.assertFalse(AttendanceRecord.objects.filter(schedule=night).exists())

    def test_cancelled_schedule_is_not_absent(self):
        self.schedule.status = EmployeeSchedule.Status.CANCELLED
        self.schedule.save()
        flagged = self._run_reconciliation()
        self.assertEqual(flagged, 0)
        self.assertFalse(AttendanceRecord.objects.filter(schedule=self.schedule).exists())

    def test_holiday_schedule_is_not_absent(self):
        # Jadwal SCHEDULED pada tanggal hari libur (tanpa cancelled_by_holiday)
        # adalah jadwal kerja VALID: pegawai dijanjikan bekerja hari itu.
        # Kebijakan baru: tidak di-skip secara buta karena libur — diproses
        # seperti jadwal biasa, jadi Absent jika tidak ada check-in.
        Holiday.objects.create(
            office=self.office, name="Libur", start_date=self.work_date, end_date=self.work_date,
            is_active=True,
        )
        flagged = self._run_reconciliation()
        self.assertEqual(flagged, 1)
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        self.assertEqual(record.status, AttendanceRecord.Status.ABSENT)

    def test_on_leave_record_is_not_absent(self):
        record = AttendanceRecord.objects.create(schedule=self.schedule)
        record.status = AttendanceRecord.Status.ON_LEAVE
        record.review_status = AttendanceRecord.ReviewStatus.NOT_REQUIRED
        record.remarks = "Cuti disetujui."
        record.save()
        flagged = self._run_reconciliation()
        self.assertEqual(flagged, 0)
        record.refresh_from_db()
        self.assertEqual(record.status, AttendanceRecord.Status.ON_LEAVE)

    def test_policy_selected_by_office_and_date(self):
        from attendance.services.scheduling import policy_for
        # Policy kantor Dili pada 2026-03-02.
        policy = policy_for(self.office, self.work_date)
        self.assertIsNotNone(policy)
        self.assertEqual(policy.policy_name, "P")
        # Kantor lain tanpa policy sendiri -> institution-wide (tidak ada di sini).
        self.assertIsNone(policy_for(self.other_office, self.work_date))

    def test_reconciliation_is_idempotent(self):
        # Tidak ada event -> Absent dibuat sekali.
        first = self._run_reconciliation()
        self.assertEqual(first, 1)
        # Kedua kali: record sudah ABSENT, dilewati.
        second = self._run_reconciliation()
        self.assertEqual(second, 0)
        self.assertEqual(AttendanceRecord.objects.filter(schedule=self.schedule).count(), 1)

    def test_dry_run_does_not_write(self):
        # Tidak ada record sebelumnya.
        self.assertFalse(AttendanceRecord.objects.filter(schedule=self.schedule).exists())
        would_flag = self._run_reconciliation(dry_run=True)
        self.assertEqual(would_flag, 1)
        # Tidak ada tulisan ke database.
        self.assertFalse(AttendanceRecord.objects.filter(schedule=self.schedule).exists())
        # Jalankan untuk nyata: record dibuat.
        real = self._run_reconciliation()
        self.assertEqual(real, 1)
        self.assertTrue(AttendanceRecord.objects.filter(schedule=self.schedule).exists())

    def test_catch_up_date_range_limits_schedules(self):
        # Jadwal di luar rentang tidak diproses.
        outside = self.make_schedule(work_date=self.work_date + timedelta(days=10))
        self._run_reconciliation(start_date=self.work_date, end_date=self.work_date)
        self.assertTrue(AttendanceRecord.objects.filter(schedule=self.schedule, status=AttendanceRecord.Status.ABSENT).exists())
        self.assertFalse(AttendanceRecord.objects.filter(schedule=outside).exists())

    def test_holiday_schedule_is_valid_work_not_automatically_skipped(self):
        # Jadwal SCHEDULED pada tanggal hari libur (tanpa cancelled_by_holiday)
        # adalah jadwal kerja valid: pegawai dijanjikan bekerja, jadi jika tidak
        # absen tetap menjadi Absent — tidak di-skip secara buta karena libur.
        Holiday.objects.create(
            office=self.office, name="Libur Nasional", start_date=self.work_date,
            end_date=self.work_date, is_active=True,
        )
        flagged = self._run_reconciliation()
        self.assertEqual(flagged, 1)
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        self.assertEqual(record.status, AttendanceRecord.Status.ABSENT)

    def test_holiday_cancelled_schedule_is_excluded(self):
        # Jadwal yang sudah dibatalkan karena libur (status CANCELLED,
        # cancelled_by_holiday diisi) tidak diproses oleh filter SCHEDULED.
        # Gunakan tanggal lain agar tidak bentrok dengan self.schedule.
        cancel_date = self.work_date + timedelta(days=1)
        schedule = self.make_schedule(work_date=cancel_date)
        holiday = Holiday.objects.create(
            office=self.office, name="Libur", start_date=cancel_date,
            end_date=cancel_date, is_active=True,
        )
        schedule.status = EmployeeSchedule.Status.CANCELLED
        schedule.cancelled_by_holiday = holiday
        schedule.save()
        flagged = self._run_reconciliation(start_date=cancel_date, end_date=cancel_date)
        self.assertEqual(flagged, 0)
        self.assertFalse(AttendanceRecord.objects.filter(schedule=schedule).exists())

    def test_shift_without_breaks_is_rejected_by_validation(self):
        # Semua shift harus memiliki waktu istirahat (compute_shift_metrics
        # menolak break_times kosong). Tidak ada shift yang bisa disimpan tanpa
        # kewajiban break, sehingga tidak ada shift yang salah ditandai
        # Incomplete karena "break events yang kurang".
        shift_no_break = Shift.objects.create(
            office=self.office, shift_code="NOBRK", name="No Break",
            start_time=time(8, 0), end_time=time(16, 0),
            break_start_time=None, break_end_time=None,
        )
        with self.assertRaises(ValidationError):
            shift_no_break.full_clean()

    def test_break_out_missed_does_not_trap_employee(self):
        # Break Out dilewatkan, tetapi pegawai tetap dapat mencatat Check Out
        # pada jendela valid. Tidak ada Pending review selama scan.
        record = self._store(self.schedule, CHECK_IN, at(self.work_date, 8, 30))
        # Break Out pada pukul 14:05 (setelah jendela istirahat berakhir 14:00)
        # masih diperbolehkan — tidak ditolak, tidak ada flag_anomaly.
        self.assertEqual(
            decide_next_event(self.schedule, record, at(self.work_date, 14, 5), self.policy), BREAK_OUT
        )
        self._store(self.schedule, BREAK_OUT, at(self.work_date, 14, 5))
        self.assertEqual(AttendanceEvent.objects.count(), 2)
        record.refresh_from_db()
        self.assertEqual(record.review_status, AttendanceRecord.ReviewStatus.NOT_REQUIRED)

        # Check Out pada jendela valid (17:30 = shift end) meskipun Break In masih kosong.
        self.assertEqual(
            decide_next_event(self.schedule, record, at(self.work_date, 17, 30), self.policy), CHECK_OUT
        )
        self._store(self.schedule, CHECK_OUT, at(self.work_date, 17, 30))
        self.assertEqual(AttendanceEvent.objects.count(), 3)  # Check In + Break Out + Check Out
        record.refresh_from_db()
        # Break In masih kosong, status belum final (Incomplete ditetapkan setelah penutupan).
        self.assertNotEqual(record.status, AttendanceRecord.Status.INCOMPLETE)
        self.assertEqual(record.review_status, AttendanceRecord.ReviewStatus.NOT_REQUIRED)

        # Setelah penutupan, finalisasi menjadi Incomplete dengan alasan Break In yang hilang,
        # late_minutes tetap tersimpan (check-in tepat waktu → 0).
        flagged = mark_incomplete_sessions(now=at(self.work_date, 19, 31))
        self.assertEqual(flagged, 1)
        record.refresh_from_db()
        self.assertEqual(record.status, AttendanceRecord.Status.INCOMPLETE)
        self.assertIn("Break In", record.remarks)
        self.assertEqual(record.late_minutes, 0)

    def test_reconciliation_rescans_after_anomaly_flag(self):
        # Setelah flag_anomaly (INCOMPLETE+PENDING), mark_incomplete_sessions
        # tidak menandainya lagi (idempotent) dan tidak mengubahnya jadi Absent.
        record = self._store(self.schedule, CHECK_IN, at(self.work_date, 8, 30))
        self._store(self.schedule, BREAK_OUT, at(self.work_date, 12, 0))
        first = self._run_reconciliation()
        self.assertEqual(first, 1)
        record.refresh_from_db()
        self.assertEqual(record.status, AttendanceRecord.Status.INCOMPLETE)
        second = self._run_reconciliation()
        self.assertEqual(second, 0)
        record.refresh_from_db()
        self.assertEqual(record.status, AttendanceRecord.Status.INCOMPLETE)


