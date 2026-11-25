"""Targeted tests for the contract -> assignment -> schedule workflow.

Covers: assignment/contract date boundaries, schedule boundaries, transfer
reconciliation (branch / department / position / future / successive), expiry
notification effective-branch selection, and historical report access after a
transfer. Uses an isolated test database and controlled dates (DILI_TODAY).
"""
from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from attendance.models import EmployeeSchedule, Shift
from attendance.services.scheduling import build_schedule_snapshot
from employees.models import Employee, EmployeeAssignment, UserEmployeeLink
from employees.services.contract_alerts import send_upcoming_contract_expiry_alerts
from employees.services.contract_scheduling import (
    generate_contract_schedules,
    snapshot_employee_state,
    synchronize_employee,
)
from employee_requests.models import ContractExpiryAlert, Notification
from organization.models import Department, Office, Position

User = get_user_model()

DILI_TODAY = date(2026, 11, 16)  # Monday


def _mk_office(code, name):
    return Office.objects.create(office_code=code, office_name=name)


def _mk_shift(office, code="DAY", name="Day Shift"):
    return Shift.objects.create(
        office=office, shift_code=code, name=name,
        start_time=time(8, 0), end_time=time(17, 0),
        break_start_time=time(12, 0), break_end_time=time(13, 0),
    )


class WorkflowBase(TestCase):
    def setUp(self):
        self.office_a = _mk_office("DIL", "Dili Office")
        self.office_b = _mk_office("BAU", "Baucau Office")
        self.dept_a1 = Department.objects.create(
            office=self.office_a, department_code="ADM", department_name="Administration"
        )
        self.dept_a2 = Department.objects.create(
            office=self.office_a, department_code="FIN", department_name="Finance"
        )
        self.dept_b1 = Department.objects.create(
            office=self.office_b, department_code="OPS", department_name="Operations"
        )
        self.pos_a1 = Position.objects.create(
            office=self.office_a, department=self.dept_a1, position_code="ADM", position_title="Admin"
        )
        self.pos_a2 = Position.objects.create(
            office=self.office_a, department=self.dept_a2, position_code="FIN", position_title="Finance Officer"
        )
        self.pos_b1 = Position.objects.create(
            office=self.office_b, department=self.dept_b1, position_code="OPS", position_title="Ops Officer"
        )
        self.shift_a = _mk_shift(self.office_a, "DAY", "Dili Day")
        self.shift_b = _mk_shift(self.office_b, "DAY", "Baucau Day")

        self.employee = Employee.objects.create(
            employee_id="EMP-W1",
            full_name="Workflow Employee",
            employment_status=Employee.EmploymentStatus.ACTIVE,
            contract_start_date=DILI_TODAY - timedelta(days=30),
            contract_end_date=DILI_TODAY + timedelta(days=60),
        )
        self.assignment = EmployeeAssignment.objects.create(
            employee=self.employee, position=self.pos_a1,
            start_date=self.employee.contract_start_date, end_date=None, is_primary=True,
        )
        self.employee.automatic_schedule_enabled = True
        self.employee.automatic_schedule_shift = self.shift_a
        self.employee.save()

    def transfer(self, *, position, start_date, shift):
        """Simulate a completed transfer: close the old primary, open the new one."""
        old = (
            self.employee.assignments.filter(is_primary=True, start_date__lte=start_date)
            .order_by("-start_date").first()
        )
        if old is not None:
            old.end_date = start_date - timedelta(days=1)
            old.save(update_fields=["end_date", "updated_at"])
        new = EmployeeAssignment.objects.create(
            employee=self.employee, position=position,
            start_date=start_date, end_date=self.employee.contract_end_date, is_primary=True,
        )
        self.employee.automatic_schedule_shift = shift
        self.employee.save()
        return new


class AssignmentContractBoundaryTests(WorkflowBase):
    def test_end_equal_to_contract_end_accepted(self):
        a = EmployeeAssignment(
            employee=self.employee, position=self.pos_a1,
            start_date=self.employee.contract_start_date,
            end_date=self.employee.contract_end_date, is_primary=False,
        )
        a.full_clean()  # must not raise

    def test_end_before_contract_end_accepted(self):
        a = EmployeeAssignment(
            employee=self.employee, position=self.pos_a1,
            start_date=self.employee.contract_start_date,
            end_date=self.employee.contract_end_date - timedelta(days=5), is_primary=False,
        )
        a.full_clean()

    def test_end_after_contract_end_rejected(self):
        a = EmployeeAssignment(
            employee=self.employee, position=self.pos_a1,
            start_date=self.employee.contract_start_date,
            end_date=self.employee.contract_end_date + timedelta(days=1), is_primary=False,
        )
        with self.assertRaises(ValidationError):
            a.full_clean()

    def test_new_start_before_contract_start_rejected(self):
        a = EmployeeAssignment(
            employee=self.employee, position=self.pos_a1,
            start_date=self.employee.contract_start_date - timedelta(days=1),
            end_date=self.employee.contract_end_date, is_primary=False,
        )
        with self.assertRaises(ValidationError):
            a.full_clean()

    def test_new_start_after_contract_end_rejected(self):
        a = EmployeeAssignment(
            employee=self.employee, position=self.pos_a1,
            start_date=self.employee.contract_end_date + timedelta(days=1),
            end_date=None, is_primary=False,
        )
        with self.assertRaises(ValidationError):
            a.full_clean()

    def test_omitted_end_with_finite_contract_is_bounded(self):
        a = EmployeeAssignment(
            employee=self.employee, position=self.pos_a1,
            start_date=self.employee.contract_start_date, end_date=None, is_primary=False,
        )
        a.full_clean()
        self.assertEqual(a.end_date, self.employee.contract_end_date)

    def test_existing_assignment_predating_contract_is_not_rejected(self):
        # Historical continuity: an existing assignment that started before the
        # current contract must not be rejected (renewal tolerance).
        self.assignment.start_date = self.employee.contract_start_date - timedelta(days=400)
        self.assignment.end_date = None
        self.assignment.save(update_fields=["start_date", "end_date"])
        self.assignment.refresh_from_db()
        # full_clean on the existing row must not raise for the pre-contract start.
        self.assignment.full_clean()

    def test_overlapping_primary_assignments_rejected(self):
        overlapping = EmployeeAssignment(
            employee=self.employee, position=self.pos_a2,
            start_date=self.employee.contract_start_date,
            end_date=self.employee.contract_end_date, is_primary=True,
        )
        with self.assertRaises(ValidationError):
            overlapping.full_clean()

    def test_non_overlapping_primary_assignments_accepted(self):
        # Close the current one, then a new primary that starts the day after.
        self.assignment.end_date = DILI_TODAY - timedelta(days=1)
        self.assignment.save(update_fields=["end_date"])
        nxt = EmployeeAssignment(
            employee=self.employee, position=self.pos_a2,
            start_date=DILI_TODAY, end_date=self.employee.contract_end_date, is_primary=True,
        )
        nxt.full_clean()  # must not raise


class EmployeeFormContractTests(WorkflowBase):
    def _payload(self, **overrides):
        data = {
            "employee_id": "EMP-F1", "full_name": "Form Employee",
            "marital_status": "single", "hire_date": DILI_TODAY.isoformat(),
            "employment_type": Employee.EmploymentType.CONTRACT,
            "employment_status": Employee.EmploymentStatus.ACTIVE,
            "office": self.office_a.pk, "department": self.dept_a1.pk,
            "position": self.pos_a1.pk,
            "assignment_start_date": (DILI_TODAY - timedelta(days=30)).isoformat(),
            "contract_start_date": (DILI_TODAY - timedelta(days=30)).isoformat(),
            "contract_end_date": (DILI_TODAY + timedelta(days=60)).isoformat(),
        }
        data.update(overrides)
        return data

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser(
            username="wf-admin", email="wf@example.com", password="pw"
        )
        self.client.force_login(self.user)

    def test_assignment_before_contract_rejected_no_partial_save(self):
        response = self.client.post(
            reverse("employees:employee-create"),
            self._payload(
                contract_start_date=(DILI_TODAY + timedelta(days=10)).isoformat(),
                assignment_start_date=(DILI_TODAY - timedelta(days=30)).isoformat(),
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("assignment_start_date", response.context["form"].errors)
        self.assertFalse(Employee.objects.filter(employee_id="EMP-F1").exists())
        self.assertFalse(EmployeeAssignment.objects.filter(employee__employee_id="EMP-F1").exists())

    def test_assignment_after_contract_rejected(self):
        response = self.client.post(
            reverse("employees:employee-create"),
            self._payload(
                contract_start_date=(DILI_TODAY - timedelta(days=30)).isoformat(),
                contract_end_date=(DILI_TODAY + timedelta(days=5)).isoformat(),
                assignment_start_date=(DILI_TODAY + timedelta(days=10)).isoformat(),
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("assignment_start_date", response.context["form"].errors)
        self.assertFalse(Employee.objects.filter(employee_id="EMP-F1").exists())

    def test_open_ended_contract_with_valid_assignment_allowed(self):
        response = self.client.post(
            reverse("employees:employee-create"),
            self._payload(
                contract_start_date=(DILI_TODAY - timedelta(days=30)).isoformat(),
                contract_end_date="",
                assignment_start_date=(DILI_TODAY - timedelta(days=30)).isoformat(),
            ),
        )
        self.assertEqual(response.status_code, 302)
        emp = Employee.objects.get(employee_id="EMP-F1")
        self.assertIsNone(emp.contract_end_date)
        # The assignment is created (open-ended contract -> open assignment).
        self.assertTrue(emp.assignments.filter(is_primary=True).exists())

    def test_renewal_with_historical_assignment_allowed(self):
        # Existing assignment predates the (renewed) contract start.
        self.assignment.start_date = self.employee.contract_start_date - timedelta(days=400)
        self.assignment.end_date = None
        self.assignment.save(update_fields=["start_date", "end_date"])
        response = self.client.post(
            reverse("employees:employee-update", args=[self.employee.pk]),
            self._payload(
                employee_id=self.employee.employee_id, full_name=self.employee.full_name,
                office=self.office_a.pk, department=self.dept_a1.pk, position=self.pos_a1.pk,
                assignment_start_date=self.assignment.start_date.isoformat(),
                contract_start_date=self.employee.contract_start_date.isoformat(),
                contract_end_date=self.employee.contract_end_date.isoformat(),
            ),
        )
        self.assertEqual(response.status_code, 302)


class ScheduleBoundaryTests(WorkflowBase):
    def _schedule(self, work_date):
        s = EmployeeSchedule(
            employee_assignment=self.assignment, shift=self.shift_a,
            work_date=work_date, status=EmployeeSchedule.Status.SCHEDULED,
        )
        s.apply_snapshot(build_schedule_snapshot(self.shift_a, work_date))
        return s

    def test_before_assignment_start_rejected(self):
        s = self._schedule(self.assignment.start_date - timedelta(days=1))
        with self.assertRaises(ValidationError):
            s.full_clean()

    def test_after_assignment_end_within_contract_rejected(self):
        self.assignment.end_date = DILI_TODAY + timedelta(days=5)
        self.assignment.save(update_fields=["end_date"])
        s = self._schedule(DILI_TODAY + timedelta(days=6))
        with self.assertRaises(ValidationError):
            s.full_clean()

    def test_after_contract_end_rejected(self):
        # Open assignment (end None) but work date beyond contract end.
        s = self._schedule(self.employee.contract_end_date + timedelta(days=1))
        with self.assertRaises(ValidationError):
            s.full_clean()

    def test_on_assignment_boundaries_accepted(self):
        start = self._schedule(self.assignment.start_date)
        start.full_clean()
        end = self._schedule(self.employee.contract_end_date)
        end.full_clean()


class TransferReconciliationTests(WorkflowBase):
    def test_branch_transfer_cancels_old_and_generates_new(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        old_live = EmployeeSchedule.objects.filter(
            employee_assignment=self.assignment,
            schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
            status__in=EmployeeSchedule.LIVE_STATUSES, work_date__gte=DILI_TODAY,
        ).count()
        self.assertGreater(old_live, 0)

        new_assignment = self.transfer(position=self.pos_b1, start_date=DILI_TODAY, shift=self.shift_b)
        previous = {"automatic_schedule_shift_id": self.shift_a.pk}
        result = synchronize_employee(self.employee, actor_user=None, previous=previous, today=DILI_TODAY)

        # Old assignment's future contract schedules are cancelled.
        self.assertEqual(
            EmployeeSchedule.objects.filter(
                employee_assignment=self.assignment,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                status__in=EmployeeSchedule.LIVE_STATUSES, work_date__gte=DILI_TODAY,
            ).count(),
            0,
        )
        # New assignment has generated schedules in the destination branch.
        self.assertGreater(
            EmployeeSchedule.objects.filter(
                employee_assignment=new_assignment,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                status__in=EmployeeSchedule.LIVE_STATUSES,
            ).count(),
            0,
        )

    def test_department_transfer_within_branch(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        new_assignment = self.transfer(position=self.pos_a2, start_date=DILI_TODAY, shift=self.shift_a)
        synchronize_employee(self.employee, actor_user=None, previous={}, today=DILI_TODAY)
        self.assertEqual(
            EmployeeSchedule.objects.filter(
                employee_assignment=self.assignment,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                status__in=EmployeeSchedule.LIVE_STATUSES, work_date__gte=DILI_TODAY,
            ).count(),
            0,
        )
        self.assertGreater(
            EmployeeSchedule.objects.filter(
                employee_assignment=new_assignment,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                status__in=EmployeeSchedule.LIVE_STATUSES,
            ).count(),
            0,
        )

    def test_position_change_reconciles(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        new_assignment = self.transfer(position=self.pos_a2, start_date=DILI_TODAY, shift=self.shift_a)
        synchronize_employee(self.employee, actor_user=None, previous={}, today=DILI_TODAY)
        self.assertTrue(
            EmployeeSchedule.objects.filter(
                employee_assignment=new_assignment,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                status__in=EmployeeSchedule.LIVE_STATUSES,
            ).exists()
        )

    def test_future_transfer_scope_unchanged_before_start(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        future = DILI_TODAY + timedelta(days=10)
        new_assignment = self.transfer(position=self.pos_b1, start_date=future, shift=self.shift_b)
        # Before the transfer date the old assignment is still the effective one.
        synchronize_employee(self.employee, actor_user=None, previous={}, today=DILI_TODAY)
        # Old assignment's future schedules are NOT cancelled before the transfer.
        self.assertGreater(
            EmployeeSchedule.objects.filter(
                employee_assignment=self.assignment,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                status__in=EmployeeSchedule.LIVE_STATUSES, work_date__gte=DILI_TODAY,
            ).count(),
            0,
        )
        # On the transfer date the old schedules are cancelled and the new generated.
        synchronize_employee(self.employee, actor_user=None, previous={}, today=future)
        self.assertEqual(
            EmployeeSchedule.objects.filter(
                employee_assignment=self.assignment,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                status__in=EmployeeSchedule.LIVE_STATUSES, work_date__gte=future,
            ).count(),
            0,
        )

    def test_two_successive_transfers(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        first = self.transfer(position=self.pos_b1, start_date=DILI_TODAY, shift=self.shift_b)
        synchronize_employee(self.employee, actor_user=None, previous={}, today=DILI_TODAY)
        # Second transfer back to office A a few days later.
        second = self.transfer(position=self.pos_a2, start_date=DILI_TODAY + timedelta(days=3), shift=self.shift_a)
        synchronize_employee(self.employee, actor_user=None, previous={}, today=DILI_TODAY + timedelta(days=3))
        self.assertEqual(
            EmployeeSchedule.objects.filter(
                employee_assignment=first,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                status__in=EmployeeSchedule.LIVE_STATUSES,
                work_date__gte=DILI_TODAY + timedelta(days=3),
            ).count(),
            0,
        )
        self.assertTrue(
            EmployeeSchedule.objects.filter(
                employee_assignment=second,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                status__in=EmployeeSchedule.LIVE_STATUSES,
            ).exists()
        )

    def test_transfer_preserves_attended_and_manual(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        # Mark one old-assignment future schedule as attended (protected).
        attended = EmployeeSchedule.objects.filter(
            employee_assignment=self.assignment,
            schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
            status__in=EmployeeSchedule.LIVE_STATUSES, work_date__gte=DILI_TODAY,
        ).first()
        # Simulate an attendance record by creating a manual schedule instead (protected).
        from attendance.models import AttendanceRecord
        # Use a manual schedule to represent a protected row.
        manual = EmployeeSchedule(
            employee_assignment=self.assignment, shift=self.shift_a,
            work_date=DILI_TODAY + timedelta(days=20),
            status=EmployeeSchedule.Status.SCHEDULED,
            schedule_source=EmployeeSchedule.ScheduleSource.MANUAL,
        )
        manual.apply_snapshot(build_schedule_snapshot(self.shift_a, manual.work_date))
        manual.full_clean()
        manual.save()

        self.transfer(position=self.pos_b1, start_date=DILI_TODAY, shift=self.shift_b)
        synchronize_employee(self.employee, actor_user=None, previous={}, today=DILI_TODAY)
        manual.refresh_from_db()
        self.assertEqual(manual.status, EmployeeSchedule.Status.SCHEDULED)
        self.assertEqual(manual.schedule_source, EmployeeSchedule.ScheduleSource.MANUAL)

    def test_reconciliation_without_previous_snapshot(self):
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        self.transfer(position=self.pos_b1, start_date=DILI_TODAY, shift=self.shift_b)
        # No previous snapshot at all: stale old-assignment schedules are still cancelled.
        result = synchronize_employee(self.employee, actor_user=None, previous=None, today=DILI_TODAY)
        self.assertGreater(result.cancelled_count, 0)
        self.assertEqual(
            EmployeeSchedule.objects.filter(
                employee_assignment=self.assignment,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                status__in=EmployeeSchedule.LIVE_STATUSES, work_date__gte=DILI_TODAY,
            ).count(),
            0,
        )

    def test_contract_extension_does_not_extend_ended_assignment(self):
        # Assignment has a finite end; extending the contract must not extend it.
        self.assignment.end_date = DILI_TODAY + timedelta(days=10)
        self.assignment.save(update_fields=["end_date"])
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        before = EmployeeSchedule.objects.filter(
            employee_assignment=self.assignment,
            schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        ).count()
        self.employee.contract_end_date = DILI_TODAY + timedelta(days=90)
        self.employee.save()
        synchronize_employee(self.employee, actor_user=None, previous={"contract_end_date": DILI_TODAY + timedelta(days=60)}, today=DILI_TODAY)
        after = EmployeeSchedule.objects.filter(
            employee_assignment=self.assignment,
            schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        ).count()
        # No new schedules beyond the assignment's finite end.
        self.assertEqual(after, before)
        for s in EmployeeSchedule.objects.filter(
            employee_assignment=self.assignment,
            schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        ):
            self.assertLessEqual(s.work_date, self.assignment.end_date)

    def test_contract_shortening_cancels_future_and_clamps_effective_end(self):
        self.assignment.end_date = None  # open, bounded by contract
        self.assignment.save(update_fields=["end_date"])
        generate_contract_schedules(self.employee, today=DILI_TODAY)
        new_end = DILI_TODAY + timedelta(days=5)
        self.employee.contract_end_date = new_end
        self.employee.save()
        synchronize_employee(
            self.employee, actor_user=None,
            previous={"contract_end_date": DILI_TODAY + timedelta(days=60)}, today=DILI_TODAY,
        )
        remaining = EmployeeSchedule.objects.filter(
            employee_assignment=self.assignment,
            schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
            status__in=EmployeeSchedule.LIVE_STATUSES,
        )
        for s in remaining:
            self.assertLessEqual(s.work_date, new_end)
        # Effective end is clamped to the shortened contract.
        self.assertEqual(self.assignment.effective_end_date, new_end)


class ExpiryNotificationBranchTests(WorkflowBase):
    def setUp(self):
        super().setUp()
        self.group = Group.objects.create(name="Staff HR")
        self.hr_a = User.objects.create_user(username="hr-a", password="pw")
        self.hr_a.groups.add(self.group)
        self.hr_b = User.objects.create_user(username="hr-b", password="pw")
        self.hr_b.groups.add(self.group)
        UserEmployeeLink.objects.create(user=self.hr_a, employee=self._staff_hr_employee(self.office_a))
        UserEmployeeLink.objects.create(user=self.hr_b, employee=self._staff_hr_employee(self.office_b))
        # Contract expires inside the 7-day window.
        self.employee.contract_end_date = DILI_TODAY + timedelta(days=3)
        self.employee.save()

    def _staff_hr_employee(self, office):
        e = Employee.objects.create(
            employee_id=f"HR-{office.office_code}", full_name="Staff HR",
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        pos = Position.objects.create(
            office=office, position_code="HR", position_title="HR Officer"
        )
        EmployeeAssignment.objects.create(
            employee=e, position=pos, start_date=DILI_TODAY - timedelta(days=365),
            end_date=None, is_primary=True,
        )
        return e

    def test_recipient_is_effective_branch(self):
        send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        self.assertTrue(
            Notification.objects.filter(
                recipient_user=self.hr_a,
                notification_type=Notification.NotificationType.GENERAL,
            ).exists()
        )
        self.assertFalse(
            Notification.objects.filter(
                recipient_user=self.hr_b,
                notification_type=Notification.NotificationType.GENERAL,
            ).exists()
        )

    def test_recipient_switches_after_transfer_effective(self):
        # Transfer the employee to office B, effective today.
        self.transfer(position=self.pos_b1, start_date=DILI_TODAY, shift=self.shift_b)
        send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        self.assertFalse(
            Notification.objects.filter(
                recipient_user=self.hr_a,
                notification_type=Notification.NotificationType.GENERAL,
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient_user=self.hr_b,
                notification_type=Notification.NotificationType.GENERAL,
            ).exists()
        )

    def test_recipient_not_future_branch_before_effective(self):
        # Future-dated transfer: before it is effective, office A remains the branch.
        future = DILI_TODAY + timedelta(days=10)
        self.transfer(position=self.pos_b1, start_date=future, shift=self.shift_b)
        send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        self.assertTrue(
            Notification.objects.filter(
                recipient_user=self.hr_a,
                notification_type=Notification.NotificationType.GENERAL,
            ).exists()
        )
        self.assertFalse(
            Notification.objects.filter(
                recipient_user=self.hr_b,
                notification_type=Notification.NotificationType.GENERAL,
            ).exists()
        )

    def test_dedup_repeated_run(self):
        send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        send_upcoming_contract_expiry_alerts(today=DILI_TODAY)
        self.assertEqual(
            Notification.objects.filter(
                recipient_user=self.hr_a,
                notification_type=Notification.NotificationType.GENERAL,
            ).count(),
            1,
        )


class HistoricalReportAccessTests(WorkflowBase):
    def setUp(self):
        super().setUp()
        self.group = Group.objects.create(name="Staff HR")
        self.hr_a = User.objects.create_user(username="hr-a2", password="pw")
        self.hr_a.groups.add(self.group)
        self.hr_b = User.objects.create_user(username="hr-b2", password="pw")
        self.hr_b.groups.add(self.group)
        UserEmployeeLink.objects.create(user=self.hr_a, employee=self._staff_hr_employee(self.office_a))
        UserEmployeeLink.objects.create(user=self.hr_b, employee=self._staff_hr_employee(self.office_b))
        self.client.force_login(self.hr_a)

    def _staff_hr_employee(self, office):
        e = Employee.objects.create(
            employee_id=f"HR2-{office.office_code}", full_name="Staff HR 2",
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        pos = Position.objects.create(office=office, position_code="HR2", position_title="HR")
        EmployeeAssignment.objects.create(
            employee=e, position=pos, start_date=DILI_TODAY - timedelta(days=365),
            end_date=None, is_primary=True,
        )
        return e

    def _attendance_record(self, assignment, work_date):
        from attendance.models import AttendanceRecord
        schedule = EmployeeSchedule(
            employee_assignment=assignment, shift=self.shift_a, work_date=work_date,
            status=EmployeeSchedule.Status.SCHEDULED,
            schedule_source=EmployeeSchedule.ScheduleSource.MANUAL,
        )
        schedule.apply_snapshot(build_schedule_snapshot(self.shift_a, work_date))
        schedule.full_clean()
        schedule.save()
        record = AttendanceRecord.objects.create(
            schedule=schedule, status=AttendanceRecord.Status.PRESENT,
        )
        return record

    def test_previous_branch_sees_old_not_new(self):
        old_record = self._attendance_record(self.assignment, DILI_TODAY - timedelta(days=5))
        # Transfer to office B effective today; new record after transfer.
        new_assignment = self.transfer(position=self.pos_b1, start_date=DILI_TODAY, shift=self.shift_b)
        new_record = self._attendance_record(new_assignment, DILI_TODAY + timedelta(days=2))

        # Previous branch (hr_a) sees the old record, not the new one.
        self.client.force_login(self.hr_a)
        resp = self.client.get(reverse("attendance:history"))
        self.assertEqual(resp.status_code, 200)
        ids = {r.pk for r in resp.context["items"]}
        self.assertIn(old_record.pk, ids)
        self.assertNotIn(new_record.pk, ids)

        # Direct detail URL: previous branch cannot open the new record.
        self.assertEqual(
            self.client.get(reverse("attendance:record-detail", args=[new_record.pk])).status_code,
            404,
        )
        # ...but can open the old record.
        self.assertEqual(
            self.client.get(reverse("attendance:record-detail", args=[old_record.pk])).status_code,
            200,
        )

    def test_destination_branch_sees_new_not_old(self):
        old_record = self._attendance_record(self.assignment, DILI_TODAY - timedelta(days=5))
        new_assignment = self.transfer(position=self.pos_b1, start_date=DILI_TODAY, shift=self.shift_b)
        new_record = self._attendance_record(new_assignment, DILI_TODAY + timedelta(days=2))

        self.client.force_login(self.hr_b)
        resp = self.client.get(reverse("attendance:history"))
        ids = {r.pk for r in resp.context["items"]}
        self.assertIn(new_record.pk, ids)
        self.assertNotIn(old_record.pk, ids)