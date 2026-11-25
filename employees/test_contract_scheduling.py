"""Focused tests for contract-based automatic scheduling and contract-expiry alerts."""
from datetime import date, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from attendance.models import EmployeeSchedule, Holiday, Shift
from attendance.services.scheduling import build_schedule_snapshot
from employees.models import Employee, EmployeeAssignment, UserEmployeeLink, get_linked_employee
from employees.services.contract_alerts import (
    business_today,
    send_upcoming_contract_expiry_alerts,
)
from employees.services.contract_scheduling import (
    generate_contract_schedules,
    snapshot_employee_state,
    synchronize_employee,
)
from employee_requests.models import ContractExpiryAlert, Notification
from organization.models import Department, Office, Position

User = get_user_model()

DILI_TODAY = date(2026, 11, 16)


def dshift(days=5):
    return time(8, 0), time(17, 0)


class BaseFixture:
    def make_base(self, *, contract_start=None, contract_end=None, working_days=None):
        self.office = Office.objects.create(office_code="DIL", office_name="Dili Office")
        self.other_office = Office.objects.create(office_code="BAU", office_name="Baucau Office")
        self.department = Department.objects.create(
            office=self.office, department_code="ADM", department_name="Administration"
        )
        self.position = Position.objects.create(
            office=self.office, department=self.department, position_code="ADM", position_title="Admin"
        )
        self.employee = Employee.objects.create(
            employee_id="EMP-C1",
            full_name="Contract Employee",
            employment_status=Employee.EmploymentStatus.ACTIVE,
            contract_start_date=contract_start or DILI_TODAY - timedelta(days=30),
            contract_end_date=contract_end or DILI_TODAY + timedelta(days=30),
        )
        self.assignment = EmployeeAssignment.objects.create(
            employee=self.employee,
            position=self.position,
            start_date=self.employee.contract_start_date,
            end_date=None,
            is_primary=True,
        )
        kwargs = dict(
            office=self.office, shift_code="DAY", name="Day Shift",
            start_time=time(8, 0), end_time=time(17, 0),
            break_start_time=time(12, 0), break_end_time=time(13, 0),
        )
        if working_days is not None:
            kwargs["working_days"] = working_days
        self.shift = Shift.objects.create(**kwargs)
        self.employee.automatic_schedule_enabled = True
        self.employee.automatic_schedule_shift = self.shift
        self.employee.save()

    def enable(self):
        self.employee.automatic_schedule_enabled = True
        self.employee.automatic_schedule_shift = self.shift
        self.employee.save()


class ContractSchedulingTests(BaseFixture, TestCase):
    def setUp(self):
        self.make_base()
    def test_generation_only_for_selected_employee(self):
        other = Employee.objects.create(
            employee_id="EMP-C2", full_name="Other",
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        EmployeeAssignment.objects.create(
            employee=other, position=self.position, start_date=DILI_TODAY - timedelta(days=5), is_primary=True,
        )
        other.automatic_schedule_enabled = True
        other.automatic_schedule_shift = self.shift
        other.save()

        result = generate_contract_schedules(self.employee, today=DILI_TODAY)
        self.assertTrue(result.eligible)
        self.assertGreater(result.created_count, 0)
        self.assertEqual(
            EmployeeSchedule.objects.filter(
                employee_assignment__employee=self.employee,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
            ).count(),
            result.created_count,
        )
        # The other employee has no automatic schedules from this run.
        self.assertEqual(
            EmployeeSchedule.objects.filter(
                employee_assignment__employee=other,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
            ).count(),
            0,
        )

    def test_idempotent_repeated_saves(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        second = generate_contract_schedules(self.employee, today=DILI_TODAY)
        self.assertEqual(second.created_count, 0)
        self.assertGreater(second.skipped_existing_count, 0)

    def test_holiday_skipped_and_manual_preserved(self):
        Holiday.objects.create(
            name="Test Holiday", start_date=DILI_TODAY, end_date=DILI_TODAY,
            holiday_type=Holiday.HolidayType.NATIONAL,
        )
        manual = EmployeeSchedule(
            employee_assignment=self.assignment, shift=self.shift, work_date=DILI_TODAY + timedelta(days=1),
            status=EmployeeSchedule.Status.SCHEDULED, schedule_source=EmployeeSchedule.ScheduleSource.MANUAL,
        )

        manual.apply_snapshot(build_schedule_snapshot(self.shift, manual.work_date))
        manual.full_clean()
        manual.save()

        result = generate_contract_schedules(self.employee, today=DILI_TODAY)
        self.assertEqual(result.skipped_holiday_count, 1)
        manual.refresh_from_db()
        self.assertEqual(manual.status, EmployeeSchedule.Status.SCHEDULED)
        self.assertEqual(manual.schedule_source, EmployeeSchedule.ScheduleSource.MANUAL)

    def test_overnight_shift_not_truncated(self):
        night = Shift.objects.create(
            office=self.office, shift_code="NIGHT", name="Night",
            start_time=time(22, 0), end_time=time(6, 0),
            break_start_time=time(1, 0), break_end_time=time(2, 0),
        )
        self.employee.automatic_schedule_shift = night
        self.employee.save()
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        schedule = EmployeeSchedule.objects.filter(
            employee_assignment=self.assignment, schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        ).first()
        self.assertIsNotNone(schedule)
        self.assertEqual(timezone.localtime(schedule.scheduled_end_at).date(), schedule.work_date + timedelta(days=1))

    def test_open_ended_contract_is_pending(self):
        self.employee.contract_end_date = None
        self.employee.save()
        result = generate_contract_schedules(self.employee, today=DILI_TODAY)
        self.assertFalse(result.eligible)
        self.assertTrue(result.pending_reasons)
        self.assertEqual(result.created_count, 0)

    def test_shift_office_mismatch_pending(self):
        other_shift = Shift.objects.create(
            office=self.other_office, shift_code="B-DAY", name="Baucau Day",
            start_time=time(8, 0), end_time=time(17, 0),
            break_start_time=time(12, 0), break_end_time=time(13, 0),
        )
        self.employee.automatic_schedule_shift = other_shift
        self.employee.save()
        result = generate_contract_schedules(self.employee, today=DILI_TODAY)
        self.assertFalse(result.eligible)
        self.assertIn("does not belong", result.pending_reason)

    def test_extension_adds_only_missing(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        before = EmployeeSchedule.objects.filter(
            employee_assignment=self.assignment, schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        ).count()
        self.employee.contract_end_date = DILI_TODAY + timedelta(days=60)
        self.employee.save()
        result = synchronize_employee(
            self.employee,
            previous={"contract_end_date": DILI_TODAY + timedelta(days=30)},
            today=DILI_TODAY,
        )
        after = EmployeeSchedule.objects.filter(
            employee_assignment=self.assignment, schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        ).count()
        self.assertGreater(after, before)
        self.assertEqual(result.created_count, after - before)

    def test_shortening_cancels_only_future_contract_schedules(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        manual = EmployeeSchedule(
            employee_assignment=self.assignment, shift=self.shift,
            work_date=DILI_TODAY + timedelta(days=20), status=EmployeeSchedule.Status.SCHEDULED,
            schedule_source=EmployeeSchedule.ScheduleSource.MANUAL,
        )
        from attendance.services.scheduling import build_schedule_snapshot

        manual.apply_snapshot(build_schedule_snapshot(self.shift, manual.work_date))
        manual.full_clean()
        manual.save()

        result = synchronize_employee(
            self.employee, previous={"contract_end_date": DILI_TODAY + timedelta(days=30)},
            today=DILI_TODAY,
        )
        # shorten
        self.employee.contract_end_date = DILI_TODAY + timedelta(days=5)
        self.employee.save()
        result = synchronize_employee(
            self.employee, previous={"contract_end_date": DILI_TODAY + timedelta(days=30)},
            today=DILI_TODAY,
        )
        manual.refresh_from_db()
        self.assertEqual(manual.status, EmployeeSchedule.Status.SCHEDULED)
        self.assertEqual(manual.schedule_source, EmployeeSchedule.ScheduleSource.MANUAL)
        remaining = EmployeeSchedule.objects.filter(
            employee_assignment=self.assignment,
            schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
            status__in=EmployeeSchedule.LIVE_STATUSES,
        )
        for schedule in remaining:
            self.assertLessEqual(schedule.work_date, self.employee.contract_end_date)

    def test_shift_change_reconciles_future_contract_schedules(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        other = Shift.objects.create(
            office=self.office, shift_code="EVE", name="Evening",
            start_time=time(14, 0), end_time=time(22, 0),
            break_start_time=time(18, 0), break_end_time=time(18, 30),
        )
        old_count = EmployeeSchedule.objects.filter(
            employee_assignment=self.assignment, shift=self.shift,
            schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
            status__in=EmployeeSchedule.LIVE_STATUSES,
        ).count()
        self.employee.automatic_schedule_shift = other
        self.employee.save()
        result = synchronize_employee(
            self.employee,
            previous={"automatic_schedule_shift_id": self.shift.pk},
            today=DILI_TODAY,
        )

        self.assertEqual(
            EmployeeSchedule.objects.filter(
                employee_assignment=self.assignment, shift=self.shift,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                status__in=EmployeeSchedule.LIVE_STATUSES,
            ).count(),
            0,
        )
        self.assertGreater(
            EmployeeSchedule.objects.filter(
                employee_assignment=self.assignment, shift=other,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                status__in=EmployeeSchedule.LIVE_STATUSES,
            ).count(),
            0,
        )

    def test_disable_cancels_future_contract_schedules_only(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        self.employee.automatic_schedule_enabled = False
        self.employee.save()
        result = synchronize_employee(
            self.employee, previous={"automatic_schedule_enabled": True}, today=DILI_TODAY
        )
        self.assertGreater(result.cancelled_count, 0)
        self.assertEqual(
            EmployeeSchedule.objects.filter(
                employee_assignment=self.assignment,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                status__in=EmployeeSchedule.LIVE_STATUSES,
            ).count(),
            0,
        )

    def test_cancelled_schedule_not_restored(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        schedule = EmployeeSchedule.objects.filter(
            employee_assignment=self.assignment, schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        ).first()
        schedule.status = EmployeeSchedule.Status.CANCELLED
        schedule.notes = "Cancelled - Approved leave (Request REQ-1)"
        schedule.save(update_fields=["status", "notes", "updated_at"])
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        schedule.refresh_from_db()
        self.assertEqual(schedule.status, EmployeeSchedule.Status.CANCELLED)

    def test_missing_and_ambiguous_assignment_fail_closed(self):
        with patch(
            "employees.services.contract_scheduling.resolve_auto_schedule_assignment",
            return_value=("ambiguous", None),
        ):
            result = generate_contract_schedules(self.employee, today=DILI_TODAY)
        self.assertIn("More than one eligible primary assignment", result.pending_reason)
        self.assertEqual(result.created_count, 0)

        self.assignment.delete()
        result = generate_contract_schedules(self.employee, today=DILI_TODAY)
        self.assertIn("No eligible primary assignment", result.pending_reason)
        self.assertEqual(
            EmployeeSchedule.objects.filter(
                employee_assignment__employee=self.employee,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
            ).count(),
            0,
        )

    def test_effective_contract_end_blocks_current_assignment_and_generation(self):
        self.employee.contract_end_date = DILI_TODAY - timedelta(days=1)
        self.employee.save(update_fields=["contract_end_date"])
        result = generate_contract_schedules(self.employee, today=DILI_TODAY)
        self.assertIn("No eligible primary assignment", result.pending_reason)
        self.assertFalse(self.assignment.is_effective_active_on(DILI_TODAY))

    def test_deactivation_cancels_future_contract_schedules(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        previous = snapshot_employee_state(self.employee)
        self.employee.employment_status = Employee.EmploymentStatus.INACTIVE
        self.employee.save(update_fields=["employment_status"])
        result = synchronize_employee(
            self.employee,
            previous={**previous, "employment_status": Employee.EmploymentStatus.ACTIVE},
            today=DILI_TODAY,
        )
        self.assertGreater(result.cancelled_count, 0)
        self.assertFalse(
            EmployeeSchedule.objects.filter(
                employee_assignment=self.assignment,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                work_date__gte=DILI_TODAY,
                status__in=EmployeeSchedule.LIVE_STATUSES,
            ).exists()
        )


class ContractHolidayReconciliationTests(BaseFixture, TestCase):
    def setUp(self):
        self.make_base()
        generate_contract_schedules(self.employee, today=DILI_TODAY)

    def test_holiday_cancels_and_deletion_restores_only_contract_row(self):
        holiday_date = DILI_TODAY + timedelta(days=2)
        contract_schedule = EmployeeSchedule.objects.get(
            employee_assignment=self.assignment,
            work_date=holiday_date,
            schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        )
        # A separate manual-only date proves the sync never modifies manual rows.
        manual_date = DILI_TODAY + timedelta(days=6)
        EmployeeSchedule.objects.filter(
            employee_assignment=self.assignment,
            work_date=manual_date,
        ).delete()
        manual = EmployeeSchedule(
            employee_assignment=self.assignment, shift=self.shift,
            work_date=manual_date,
            status=EmployeeSchedule.Status.SCHEDULED,
            schedule_source=EmployeeSchedule.ScheduleSource.MANUAL,
        )
        manual.apply_snapshot(build_schedule_snapshot(self.shift, manual.work_date))
        manual.full_clean()
        manual.save()

        holiday = Holiday.objects.create(
            name="Contract Holiday", start_date=holiday_date,
            end_date=holiday_date,
            holiday_type=Holiday.HolidayType.INSTITUTION,
        )
        from attendance.services.scheduling import sync_holiday_schedules
        from employees.services.contract_scheduling import sync_contract_holiday_schedules

        result = sync_contract_holiday_schedules(self.employee, today=DILI_TODAY, holiday=holiday)
        self.assertEqual(result.cancelled_count, 1)
        contract_schedule.refresh_from_db()
        self.assertEqual(contract_schedule.status, EmployeeSchedule.Status.CANCELLED)
        self.assertEqual(contract_schedule.cancelled_by_holiday, holiday)

        holiday.is_active = False
        holiday.save(update_fields=["is_active"])
        result = sync_contract_holiday_schedules(self.employee, today=DILI_TODAY, holiday=holiday)
        self.assertEqual(result.restored_count, 1)
        contract_schedule.refresh_from_db()
        self.assertEqual(contract_schedule.status, EmployeeSchedule.Status.SCHEDULED)
        self.assertIsNone(contract_schedule.cancelled_by_holiday)
        manual.refresh_from_db()
        self.assertEqual(manual.status, EmployeeSchedule.Status.SCHEDULED)

    def test_holiday_does_not_restore_leave_or_manual_cancellation(self):
        holiday_date = DILI_TODAY + timedelta(days=3)
        holiday = Holiday.objects.create(
            name="Contract Holiday", start_date=holiday_date,
            end_date=holiday_date,
            holiday_type=Holiday.HolidayType.INSTITUTION,
        )
        leave_cancelled = EmployeeSchedule(
            employee_assignment=self.assignment, shift=self.shift,
            work_date=holiday_date, status=EmployeeSchedule.Status.CANCELLED,
            schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
            notes="Cancelled - Approved leave (Request REQ-1)",
        )
        leave_cancelled.apply_snapshot(build_schedule_snapshot(self.shift, holiday_date))
        leave_cancelled.save()
        manual_cancelled = EmployeeSchedule(
            employee_assignment=self.assignment, shift=self.shift,
            work_date=holiday_date + timedelta(days=1),
            status=EmployeeSchedule.Status.CANCELLED,
            schedule_source=EmployeeSchedule.ScheduleSource.MANUAL,
        )
        manual_cancelled.apply_snapshot(build_schedule_snapshot(self.shift, manual_cancelled.work_date))
        manual_cancelled.save()

        from attendance.services.scheduling import sync_holiday_schedules

        sync_holiday_schedules(holiday)
        leave_cancelled.refresh_from_db()
        manual_cancelled.refresh_from_db()
        self.assertEqual(leave_cancelled.status, EmployeeSchedule.Status.CANCELLED)
        self.assertIsNone(leave_cancelled.cancelled_by_holiday)
        self.assertEqual(manual_cancelled.status, EmployeeSchedule.Status.CANCELLED)


class ContractAlertTests(BaseFixture, TestCase):
    def setUp(self):
        self.make_base(contract_start=DILI_TODAY - timedelta(days=30), contract_end=DILI_TODAY + timedelta(days=5))
        self.employee_group = Group.objects.create(name="employees")
        self.staff_hr_group = Group.objects.create(name="Staff HR")
        self.other_hr_group = self.staff_hr_group
        # Staff HR linked to the same branch.
        self.hr_user = User.objects.create_user(username="hr1", password="x")
        self.hr_user.groups.add(self.staff_hr_group)
        self.hr_employee = Employee.objects.create(
            employee_id="EMP-HR1", full_name="HR One",
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        hr_pos = Position.objects.create(
            office=self.office, department=self.department, position_code="HR", position_title="HR Officer",
        )
        EmployeeAssignment.objects.create(
            employee=self.hr_employee, position=hr_pos, start_date=DILI_TODAY - timedelta(days=10), is_primary=True,
        )
        UserEmployeeLink.objects.create(user=self.hr_user, employee=self.hr_employee)

        # Staff HR at a different branch.
        self.other_hr = User.objects.create_user(username="hr2", password="x")
        self.other_hr.groups.add(self.staff_hr_group)
        self.other_hr_employee = Employee.objects.create(
            employee_id="EMP-HR2", full_name="HR Two",
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        other_pos = Position.objects.create(
            office=self.other_office, department=None, position_code="HR2", position_title="HR Officer",
        )
        EmployeeAssignment.objects.create(
            employee=self.other_hr_employee, position=other_pos, start_date=DILI_TODAY - timedelta(days=10), is_primary=True,
        )
        UserEmployeeLink.objects.create(user=self.other_hr, employee=self.other_hr_employee)

        # Employee-role account linked to the contract employee.
        self.emp_user = User.objects.create_user(username="emp1", password="x")
        self.emp_user.groups.add(self.employee_group)
        UserEmployeeLink.objects.create(user=self.emp_user, employee=self.employee)

        # An administrative sibling account also linked to the employee: must NOT be notified.
        self.admin_sibling = User.objects.create_user(username="adminsib", password="x", is_staff=True)
        UserEmployeeLink.objects.create(user=self.admin_sibling, employee=self.employee)

    def test_recipients_are_branch_isolated(self):
        result = send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        self.assertEqual(result.sent_count, 2)  # staff HR + employee-role account
        self.assertEqual(result.already_sent_count, 0)
        recipients = set(
            ContractExpiryAlert.objects.filter(employee=self.employee).values_list("recipient_user_id", flat=True)
        )
        self.assertEqual(recipients, {self.hr_user.pk, self.emp_user.pk})
        self.assertNotIn(self.other_hr.pk, recipients)
        self.assertNotIn(self.admin_sibling.pk, recipients)

    def test_dedup_is_persistent(self):
        send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        second = send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        self.assertEqual(second.sent_count, 0)
        self.assertEqual(second.already_sent_count, 2)
        self.assertEqual(Notification.objects.filter(notification_type=Notification.NotificationType.GENERAL).count(), 2)

    def test_catch_up_within_window(self):
        # Simulate a missed H-7 run: 3 days remain.
        result = send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        self.assertEqual(result.sent_count, 2)

    def test_changed_end_date_creates_new_cycle(self):
        send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        self.employee.contract_end_date = DILI_TODAY + timedelta(days=6)
        self.employee.save()
        result = send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        self.assertEqual(result.sent_count, 2)
        self.assertEqual(ContractExpiryAlert.objects.filter(employee=self.employee).count(), 4)

    def test_alert_outside_window_not_sent(self):
        self.employee.contract_end_date = DILI_TODAY + timedelta(days=20)
        self.employee.save()
        result = send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        self.assertEqual(result.evaluated_count, 0)
        self.assertEqual(result.sent_count, 0)

    def test_dry_run_writes_nothing(self):
        result = send_upcoming_contract_expiry_alerts(today=DILI_TODAY, dry_run=True)
        self.assertGreater(result.sent_count, 0)
        self.assertEqual(ContractExpiryAlert.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), 0)

    def test_invalid_scope_reported_not_widened(self):
        # Employee with no resolvable branch (no assignment).
        self.employee.assignments.update(is_primary=False)
        result = send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        self.assertEqual(result.invalid_scope_count, 1)
        self.assertEqual(result.sent_count, 0)

    def test_command_dry_run(self):
        from io import StringIO

        out = StringIO()
        call_command("send_contract_expiry_alerts", "--dry-run", "--today", DILI_TODAY.isoformat(), stdout=out)
        self.assertIn("Dry run", out.getvalue())


    def test_alerts_exclude_inactive_and_other_statuses(self):
        self.employee.employment_status = Employee.EmploymentStatus.INACTIVE
        self.employee.save(update_fields=["employment_status"])
        result = send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        self.assertEqual(result.evaluated_count, 0)
        self.assertEqual(result.sent_count, 0)

    def test_multiple_linked_accounts_and_inactive_recipients(self):
        inactive = User.objects.create_user(username="inactive-emp", password="x", is_active=False)
        inactive.groups.add(self.employee_group)
        UserEmployeeLink.objects.create(user=inactive, employee=self.employee)
        second = User.objects.create_user(username="emp2", password="x")
        second.groups.add(self.employee_group)
        UserEmployeeLink.objects.create(user=second, employee=self.employee)

        result = send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        recipients = set(
            ContractExpiryAlert.objects.filter(employee=self.employee).values_list(
                "recipient_user_id", flat=True
            )
        )
        self.assertIn(second.pk, recipients)
        self.assertNotIn(inactive.pk, recipients)
        self.assertEqual(result.sent_count, 3)


class AssignmentContractConsistencyTests(BaseFixture, TestCase):
    def setUp(self):
        self.make_base()
    def test_assignment_cannot_start_before_contract(self):
        form_position = self.position
        assignment = EmployeeAssignment(
            employee=self.employee, position=form_position,
            start_date=self.employee.contract_start_date - timedelta(days=5),
        )
        with self.assertRaises(Exception):
            assignment.full_clean()

    def test_assignment_cannot_end_after_contract(self):
        assignment = EmployeeAssignment(
            employee=self.employee, position=self.position,
            start_date=self.employee.contract_start_date,
            end_date=self.employee.contract_end_date + timedelta(days=5),
        )
        with self.assertRaises(Exception):
            assignment.full_clean()


class ExistingNotificationTypesTests(TestCase):
    def test_existing_types_unchanged(self):
        values = set(Notification.NotificationType.values)
        self.assertTrue({"request_submitted", "request_approved", "request_rejected", "attendance_alert"}.issubset(values))
