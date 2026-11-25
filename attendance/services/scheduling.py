"""Scheduling helpers shared by models, forms, views and the kiosk workflow.

Everything here is pure scheduling math or small lookups. A Shift is only a
*template* of times of day; a schedule snapshot turns that template into real
aware datetimes for one work date, in the office timezone (Asia/Dili by default).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

REQUIRED_EVENT_COUNT = 4
MIN_SHIFT_MINUTES = 60
MAX_SHIFT_MINUTES = 16 * 60
MIN_BREAK_MINUTES = 5
MAX_BREAK_MINUTES = 4 * 60
MIN_WORK_MINUTES = 60
_DAY_SECONDS = 24 * 3600

WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
DEFAULT_WORKING_DAYS = [0, 1, 2, 3, 4]



class ShiftTimingError(ValueError):
    """Inconsistent shift times. ``field`` names the form field to attach the error to."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class ShiftMetrics:
    span_minutes: int
    break_minutes: int
    work_minutes: int
    crosses_midnight: bool


@dataclass(frozen=True)
class ScheduleSnapshot:
    start: datetime
    break_start: datetime
    break_end: datetime
    end: datetime
    grace_minutes: int
    early_checkin_minutes: int


def _seconds(value: time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def _offset(value: time, start: time) -> int:
    """Seconds from ``start`` to ``value`` going forward (wraps past midnight)."""
    return (_seconds(value) - _seconds(start)) % _DAY_SECONDS


def compute_shift_metrics(start_time, end_time, break_start_time, break_end_time) -> ShiftMetrics:
    """Validate shift/break times and derive span, break and working minutes.

    Night shifts work naturally: offsets are measured forward from the start time,
    so 22:00-06:00 with a 01:00-02:00 break is an 8 hour span with a 60 minute break.
    """
    if start_time == end_time:
        raise ShiftTimingError("end_time", "Shift start and end times must differ.")
    span = _offset(end_time, start_time)
    if span < MIN_SHIFT_MINUTES * 60:
        raise ShiftTimingError("end_time", f"A shift must last at least {MIN_SHIFT_MINUTES} minutes.")
    if span > MAX_SHIFT_MINUTES * 60:
        raise ShiftTimingError("end_time", f"A shift cannot be longer than {MAX_SHIFT_MINUTES // 60} hours.")
    if break_start_time is None or break_end_time is None:
        raise ShiftTimingError(
            "break_start_time",
            "Break start and break end are required: every shift records four attendance events.",
        )
    break_from = _offset(break_start_time, start_time)
    break_to = _offset(break_end_time, start_time)
    if break_from == 0 or break_from >= span:
        raise ShiftTimingError("break_start_time", "Break start must fall inside the shift, after it starts.")
    if break_to <= break_from:
        raise ShiftTimingError("break_end_time", "Break end must be later than break start.")
    if break_to >= span:
        raise ShiftTimingError("break_end_time", "Break must end before the shift ends.")
    break_minutes = (break_to - break_from) // 60
    if break_minutes < MIN_BREAK_MINUTES or break_minutes > MAX_BREAK_MINUTES:
        raise ShiftTimingError(
            "break_end_time",
            f"Break must last between {MIN_BREAK_MINUTES} minutes and {MAX_BREAK_MINUTES // 60} hours.",
        )
    work_minutes = span // 60 - break_minutes
    if work_minutes < MIN_WORK_MINUTES:
        raise ShiftTimingError("break_end_time", "The break leaves less than one hour of scheduled work.")
    return ShiftMetrics(
        span_minutes=span // 60,
        break_minutes=break_minutes,
        work_minutes=work_minutes,
        crosses_midnight=end_time < start_time,
    )


def office_timezone(office=None):
    name = getattr(office, "timezone", "") or ""
    try:
        return ZoneInfo(name) if name else timezone.get_current_timezone()
    except ZoneInfoNotFoundError:
        return timezone.get_current_timezone()


def build_timeline(*, start_time, end_time, break_start_time, break_end_time, work_date: date, tz):
    """Real datetimes (start, break_start, break_end, end) for one work date."""
    base = datetime.combine(work_date, start_time, tzinfo=tz)
    return (
        base,
        base + timedelta(seconds=_offset(break_start_time, start_time)),
        base + timedelta(seconds=_offset(break_end_time, start_time)),
        base + timedelta(seconds=_offset(end_time, start_time)),
    )


def build_schedule_snapshot(shift, work_date: date) -> ScheduleSnapshot:
    """Freeze a shift template into concrete times for ``work_date``."""
    compute_shift_metrics(shift.start_time, shift.end_time, shift.break_start_time, shift.break_end_time)
    start, break_start, break_end, end = build_timeline(
        start_time=shift.start_time,
        end_time=shift.end_time,
        break_start_time=shift.break_start_time,
        break_end_time=shift.break_end_time,
        work_date=work_date,
        tz=office_timezone(shift.office),
    )
    return ScheduleSnapshot(
        start=start,
        break_start=break_start,
        break_end=break_end,
        end=end,
        grace_minutes=shift.grace_period_minutes,
        early_checkin_minutes=shift.early_checkin_window_minutes,
    )


def holidays_on(office, day: date):
    """Active holidays that cover ``day`` for ``office`` (institution-wide + office-specific)."""
    from attendance.models import Holiday

    scope = Q(office__isnull=True)
    if office is not None:
        scope |= Q(office=office)
    return Holiday.objects.filter(scope, is_active=True, start_date__lte=day, end_date__gte=day)


def policy_for(office, day: date):
    """Policy in force for ``office`` on ``day``. Office-specific beats institution-wide."""
    from attendance.models import AttendancePolicy

    applicable = AttendancePolicy.objects.filter(is_active=True, effective_from__lte=day).filter(
        Q(effective_until__isnull=True) | Q(effective_until__gte=day)
    )
    policy = applicable.filter(office=office).order_by("-effective_from").first() if office is not None else None
    if policy is not None:
        return policy
    return applicable.filter(office__isnull=True).order_by("-effective_from").first()


def overlapping_schedules(employee_id, start_at, end_at, exclude_pk=None, exclude_pk__in=None):
    """Non-cancelled schedules of the same employee whose time range intersects the given one."""
    from attendance.models import EmployeeSchedule

    queryset = EmployeeSchedule.objects.filter(
        employee_assignment__employee_id=employee_id,
        scheduled_start_at__lt=end_at,
        scheduled_end_at__gt=start_at,
    ).exclude(status=EmployeeSchedule.Status.CANCELLED)
    if exclude_pk:
        queryset = queryset.exclude(pk=exclude_pk)
    if exclude_pk__in:
        queryset = queryset.exclude(pk__in=list(exclude_pk__in))
    return queryset


def assignment_position_label(assignment) -> str:
    """``Position · Department · primary assignment`` for an EmployeeAssignment."""
    position = assignment.position
    department = position.department.department_name if position.department_id else "No department"
    kind = "primary" if assignment.is_primary else assignment.get_assignment_type_display().lower()
    return f"{position.position_title} \u00b7 {department} \u00b7 {kind} assignment"


def format_local(value, work_date=None) -> str:
    """Local ``YYYY-MM-DD HH:MM`` text; adds ``(+1 day)`` when it falls after the work date (night shifts)."""
    local = timezone.localtime(value)
    text = f"{local:%Y-%m-%d %H:%M}"
    if work_date is not None and local.date() > work_date:
        days = (local.date() - work_date).days
        text += f" (+{days} day{'s' if days != 1 else ''})"
    return text


def format_minutes(minutes: int) -> str:
    hours, rest = divmod(int(minutes), 60)
    if hours and rest:
        return f"{hours} h {rest} min"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{rest} min"


def format_working_days(days: list[int] | None) -> str:
    if not days:
        days = DEFAULT_WORKING_DAYS
    sorted_days = sorted(set(int(d) for d in days if 0 <= int(d) <= 6))
    if not sorted_days:
        return "None"
    if sorted_days == [0, 1, 2, 3, 4]:
        return "Monday–Friday"
    if sorted_days == [0, 1, 2, 3, 4, 5]:
        return "Monday–Saturday"
    if sorted_days == [0, 1, 2, 3, 4, 5, 6]:
        return "Every day (Monday–Sunday)"
    return ", ".join(WEEKDAY_NAMES[d] for d in sorted_days)


@dataclass
class GenerationResult:
    created_count: int = 0
    skipped_existing_count: int = 0
    skipped_holiday_count: int = 0
    skipped_non_working_day_count: int = 0
    skipped_inactive_assignment_count: int = 0
    conflict_count: int = 0
    conflicts: list[dict] = field(default_factory=list)
    cancelled_count: int = 0
    restored_count: int = 0


def get_eligible_assignments_for_shift(shift, target_date: date | None = None):
    """Active assignments for shift's office, respecting target_assignment_mode and target_date."""
    from employees.models import Employee, EmployeeAssignment

    qs = EmployeeAssignment.objects.filter(
        position__office_id=shift.office_id,
        employee__employment_status=Employee.EmploymentStatus.ACTIVE,
    )
    if getattr(shift, "target_assignment_mode", "all") == "specific":
        qs = qs.filter(pk__in=shift.target_assignments.values_list("pk", flat=True))
    if target_date is not None:
        qs = qs.filter(
            start_date__lte=target_date,
        ).filter(Q(end_date__isnull=True) | Q(end_date__gte=target_date))
    return qs.select_related("employee", "position__office", "position__department")


def generate_schedules_for_shift(
    shift,
    actor_user=None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> GenerationResult:
    """Generate routine EmployeeSchedule instances from a Shift's General Schedule configuration.

    Idempotent: running multiple times never duplicates schedules.
    Skips non-working days and active holidays.
    Freezes snapshot times at creation.
    Reports conflicts without creating overlapping schedules.
    """
    from attendance.models import EmployeeSchedule

    result = GenerationResult()
    if not shift.is_active or not getattr(shift, "recurring_schedule_enabled", False):
        return result
    if not shift.has_break_times:
        return result

    start = start_date or shift.recurrence_start_date
    end = end_date or shift.recurrence_end_date
    if not start or not end or end < start:
        return result

    working_days = set(shift.working_days_list)
    current_date = start
    while current_date <= end:
        weekday = current_date.weekday()
        if weekday not in working_days:
            result.skipped_non_working_day_count += 1
            current_date += timedelta(days=1)
            continue

        holidays = holidays_on(shift.office, current_date)
        if holidays.exists():
            result.skipped_holiday_count += 1
            current_date += timedelta(days=1)
            continue

        assignments = list(get_eligible_assignments_for_shift(shift, target_date=current_date))
        if not assignments:
            current_date += timedelta(days=1)
            continue

        snapshot = build_schedule_snapshot(shift, current_date)

        for assignment in assignments:
            # Check existing schedules for this assignment and date
            existing_schedules = EmployeeSchedule.objects.filter(
                employee_assignment=assignment,
                work_date=current_date,
            )

            # Check if there is an active/live or completed schedule, or a schedule with attendance
            live_or_attended = existing_schedules.filter(
                Q(status__in=EmployeeSchedule.LIVE_STATUSES)
                | Q(status=EmployeeSchedule.Status.COMPLETED)
                | Q(attendance_record__isnull=False)
            ).first()
            if live_or_attended is not None:
                result.skipped_existing_count += 1
                continue

            # Check if there was a cancelled schedule that was cancelled by a holiday:
            # If so, and holiday no longer applies, we can restore it!
            cancelled_by_hol = existing_schedules.filter(
                status=EmployeeSchedule.Status.CANCELLED,
                cancelled_by_holiday__isnull=False,
            ).first()
            if cancelled_by_hol is not None:
                # Check for overlap before restoring
                clash = overlapping_schedules(
                    assignment.employee_id, snapshot.start, snapshot.end, exclude_pk=cancelled_by_hol.pk
                ).first()
                if clash is not None:
                    result.conflict_count += 1
                    result.conflicts.append({
                        "employee": assignment.employee.employee_id,
                        "date": current_date,
                        "reason": f"Overlap with schedule #{clash.pk}",
                    })
                    continue
                cancelled_by_hol.status = EmployeeSchedule.Status.SCHEDULED
                cancelled_by_hol.cancelled_by_holiday = None
                cancelled_by_hol.apply_snapshot(snapshot)
                cancelled_by_hol.notes = (
                    f"{cancelled_by_hol.notes}\nRestored by General Schedule generator on {timezone.localdate()}."
                ).strip()
                cancelled_by_hol.save(
                    update_fields=[
                        "status", "cancelled_by_holiday", "scheduled_start_at",
                        "scheduled_break_start_at", "scheduled_break_end_at",
                        "scheduled_end_at", "grace_period_minutes",
                        "early_checkin_window_minutes", "notes", "updated_at",
                    ]
                )
                result.restored_count += 1
                continue

            # If there is a manually cancelled schedule (cancelled_by_holiday is null), do not overwrite it!
            manually_cancelled = existing_schedules.filter(
                status=EmployeeSchedule.Status.CANCELLED,
                cancelled_by_holiday__isnull=True,
            ).first()
            if manually_cancelled is not None:
                result.skipped_existing_count += 1
                continue

            # Check for overlap with other schedules of the employee
            clash = overlapping_schedules(assignment.employee_id, snapshot.start, snapshot.end).first()
            if clash is not None:
                result.conflict_count += 1
                result.conflicts.append({
                    "employee": assignment.employee.employee_id,
                    "date": current_date,
                    "reason": f"Overlap with schedule #{clash.pk} ({clash.work_date})",
                })
                continue

            # Create new schedule
            schedule = EmployeeSchedule(
                employee_assignment=assignment,
                shift=shift,
                work_date=current_date,
                status=EmployeeSchedule.Status.SCHEDULED,
                schedule_source=EmployeeSchedule.ScheduleSource.GENERATED,
                assigned_by=actor_user,
            )
            schedule.apply_snapshot(snapshot)
            schedule.full_clean()
            schedule.save()
            result.created_count += 1

        current_date += timedelta(days=1        )

    return result


def sync_shift_general_schedule_changes(shift, actor_user=None) -> GenerationResult:
    """Synchronize schedules when a Shift's General Schedule settings are modified.

    Never modifies past schedules (< today) or schedules with attendance records/events.
    Cancels future generated schedules that no longer match the recurrence pattern.
    Generates new schedules for the updated pattern.
    """
    from attendance.models import EmployeeSchedule

    today = timezone.localdate()
    result = GenerationResult()

    # Query future generated schedules for this shift without attendance records.
    # CONTRACT-sourced schedules are owned by the per-employee contract feature and must
    # never be cancelled or recreated by the branch-wide Shift General Schedule sync.
    future_generated = EmployeeSchedule.objects.filter(
        shift=shift,
        work_date__gte=today,
        schedule_source=EmployeeSchedule.ScheduleSource.GENERATED,
        attendance_record__isnull=True,
    )

    if not shift.is_active or not getattr(shift, "recurring_schedule_enabled", False):
        # Cancel all future live generated schedules
        to_cancel = future_generated.filter(status__in=EmployeeSchedule.LIVE_STATUSES)
        count = to_cancel.update(
            status=EmployeeSchedule.Status.CANCELLED,
            notes="Cancelled: Shift General Schedule was deactivated or recurrence disabled.",
        )
        result.cancelled_count += count
        return result

    # Recurring is enabled: cancel future schedules outside the new parameters
    working_days = set(shift.working_days_list)
    rec_start = shift.recurrence_start_date or today
    rec_end = shift.recurrence_end_date or today

    # Check each future live generated schedule
    for sched in future_generated.filter(status__in=EmployeeSchedule.LIVE_STATUSES):
        should_cancel = False
        reason = ""
        if sched.work_date < rec_start or sched.work_date > rec_end:
            should_cancel = True
            reason = "Date outside new recurrence period."
        elif sched.work_date.weekday() not in working_days:
            should_cancel = True
            reason = "Day of week no longer in working days."
        elif holidays_on(shift.office, sched.work_date).exists():
            should_cancel = True
            reason = "Date is a holiday."
        elif shift.target_assignment_mode == "specific":
            if not shift.target_assignments.filter(pk=sched.employee_assignment_id).exists():
                should_cancel = True
                reason = "Employee assignment removed from specific targets."

        if should_cancel:
            sched.status = EmployeeSchedule.Status.CANCELLED
            sched.notes = f"{sched.notes}\nCancelled: {reason}".strip()
            sched.save(update_fields=["status", "notes", "updated_at"])
            result.cancelled_count += 1

    # Now generate schedules for the valid period (from max(today, rec_start) to rec_end)
    gen_start = max(today, rec_start)
    if gen_start <= rec_end:
        gen_res = generate_schedules_for_shift(
            shift,
            actor_user=actor_user,
            start_date=gen_start,
            end_date=rec_end,
        )
        result.created_count += gen_res.created_count
        result.skipped_existing_count += gen_res.skipped_existing_count
        result.skipped_holiday_count += gen_res.skipped_holiday_count
        result.skipped_non_working_day_count += gen_res.skipped_non_working_day_count
        result.conflict_count += gen_res.conflict_count
        result.conflicts.extend(gen_res.conflicts)
        result.restored_count += gen_res.restored_count

    return result


def sync_holiday_schedules(holiday=None, office=None, actor_user=None) -> GenerationResult:
    """Synchronize future schedules affected by Holiday additions, changes, or removals.

    CONTRACT schedules use the per-employee contract lifecycle and are never
    cancelled or restored by this branch-wide holiday synchronization. Generated
    Shift General Schedules retain the existing holiday behavior, including
    preserving worked, historical, manual, and manually cancelled rows.
    """
    # Contract schedules are owned by employees.services.contract_scheduling. Only
    # generated Shift General Schedules are changed by this branch-wide sync.
    from attendance.models import EmployeeSchedule, Holiday, Shift
    from django.utils import timezone

    today = timezone.localdate()
    result = GenerationResult()

    # Contract schedules are owned by employees.services.contract_scheduling. Only
    # generated Shift General Schedules are changed by this branch-wide sync.
    from employees.models import Employee
    from employees.services.contract_scheduling import sync_contract_holiday_schedules

    holidays_qs = Holiday.objects.filter(is_active=True, end_date__gte=today)
    if holiday is not None and holiday.pk and holiday.is_active:
        holidays_qs = holidays_qs.filter(pk=holiday.pk)
    elif office is not None:
        holidays_qs = holidays_qs.filter(Q(office__isnull=True) | Q(office=office))

    for hol in holidays_qs:
        sched_scope = Q(shift__office__isnull=False)
        if hol.office_id is not None:
            sched_scope = Q(shift__office_id=hol.office_id)

        target_schedules = EmployeeSchedule.objects.filter(
            sched_scope,
            work_date__gte=max(today, hol.start_date),
            work_date__lte=hol.end_date,
            schedule_source=EmployeeSchedule.ScheduleSource.GENERATED,
            status__in=EmployeeSchedule.LIVE_STATUSES,
            attendance_record__isnull=True,
        )
        for sched in target_schedules:
            sched.status = EmployeeSchedule.Status.CANCELLED
            sched.cancelled_by_holiday = hol
            sched.notes = f"{sched.notes}\nAutomatically cancelled due to holiday: {hol.name} ({hol.scope_label})".strip()
            sched.save(update_fields=["status", "cancelled_by_holiday", "notes", "updated_at"])
            result.cancelled_count += 1

    holiday_cancelled_qs = EmployeeSchedule.objects.filter(
        work_date__gte=today,
        schedule_source=EmployeeSchedule.ScheduleSource.GENERATED,
        status=EmployeeSchedule.Status.CANCELLED,
        cancelled_by_holiday__isnull=False,
    )
    if office is not None:
        holiday_cancelled_qs = holiday_cancelled_qs.filter(shift__office=office)

    for sched in holiday_cancelled_qs.select_related("shift", "cancelled_by_holiday", "employee_assignment__employee"):
        active_hols = holidays_on(sched.shift.office, sched.work_date)
        if active_hols.exists():
            current_hol = active_hols.first()
            if sched.cancelled_by_holiday_id != current_hol.pk:
                sched.cancelled_by_holiday = current_hol
                sched.save(update_fields=["cancelled_by_holiday", "updated_at"])
            continue

        shift = sched.shift
        if (
            shift.is_active
            and shift.recurring_schedule_enabled
            and shift.recurrence_start_date
            and shift.recurrence_end_date
            and shift.recurrence_start_date <= sched.work_date <= shift.recurrence_end_date
            and sched.work_date.weekday() in shift.working_days_list
        ):
            assignment = sched.employee_assignment
            if assignment.employee.employment_status == "active":
                if shift.target_assignment_mode == "specific" and not shift.target_assignments.filter(pk=assignment.pk).exists():
                    continue

                snapshot = build_schedule_snapshot(shift, sched.work_date)
                clash = overlapping_schedules(
                    assignment.employee_id, snapshot.start, snapshot.end, exclude_pk=sched.pk
                ).first()
                if clash is not None:
                    result.conflict_count += 1
                    continue

                sched.status = EmployeeSchedule.Status.SCHEDULED
                sched.cancelled_by_holiday = None
                sched.apply_snapshot(snapshot)
                sched.notes = f"{sched.notes}\nRestored: Holiday is no longer active.".strip()
                sched.save(
                    update_fields=[
                        "status", "cancelled_by_holiday", "scheduled_start_at",
                        "scheduled_break_start_at", "scheduled_break_end_at",
                        "scheduled_end_at", "grace_period_minutes",
                        "early_checkin_window_minutes", "notes", "updated_at",
                    ]
                )
                result.restored_count += 1

    contract_schedules = EmployeeSchedule.objects.filter(
        schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        work_date__gte=today,
    )
    if holiday is not None and holiday.pk:
        contract_schedules = contract_schedules.filter(
            Q(shift__office_id=holiday.office_id) | Q(cancelled_by_holiday=holiday)
        )
    elif office is not None:
        contract_schedules = contract_schedules.filter(shift__office=office)
    employee_ids = contract_schedules.values_list(
        "employee_assignment__employee_id", flat=True
    ).distinct()
    for employee_id in employee_ids:
        employee = Employee.objects.filter(pk=employee_id).first()
        if employee is None:
            continue
        contract_result = sync_contract_holiday_schedules(
            employee, today=today, holiday=holiday, office=office
        )
        result.cancelled_count += contract_result.cancelled_count
        result.restored_count += contract_result.restored_count
        result.conflict_count += contract_result.conflict_count
        result.conflicts.extend(contract_result.conflicts)

    return result

