"""Contract-based automatic scheduling services.

Business services for the opt-in "Automatic work shift" feature:

* generation of :class:`attendance.models.EmployeeSchedule` rows for one employee
  and one eligible assignment, bounded by the employee's contract period;
* safe reconciliation when the contract is extended or shortened, when the shift
  changes, when automatic scheduling is disabled, or when the employee is
  deactivated.

Every function here is scoped to a single employee/assignment. The branch-wide
Shift General Schedule generator (``attendance.services.scheduling``) is never
called from this module.

DB access is intentionally deferred to call time (function-level imports) so the
module can be imported from ``employees.models`` without a circular import.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import logging

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

PENDING_REASONS = {
    "disabled": "Automatic scheduling is turned off for this employee.",
    "no_shift": "Select an automatic work shift to enable contract-based scheduling.",
    "inactive_shift": "The selected automatic work shift is inactive.",
    "shift_office_mismatch": "The selected automatic work shift does not belong to the assignment's office.",
    "shift_timing": "The selected automatic work shift has no valid break times yet.",
    "inactive_employee": "The employee is not active.",
    "no_assignment": "No eligible primary assignment was found for automatic scheduling.",
    "assignment_office_mismatch": "The assignment's office does not match the selected shift.",
    "no_contract_start": "A contract start date is required for contract-based scheduling.",
    "no_contract_end": (
        "A contract end date is required: contract-based automatic scheduling cannot run for an "
        "open-ended contract. Use the manual or Shift General Schedule process instead."
    ),
    "contract_not_started": "The contract has not started yet, so there is nothing to generate.",
    "period_over": "The contract/assignment period has already ended.",
    "ambiguous_assignment": (
        "More than one eligible primary assignment was found for automatic scheduling."
    ),
}

HOLIDAY_CANCELLED_NOTE = "Cancelled: contract-based automatic scheduling holiday reconciliation."


@dataclass
class ContractScheduleResult:
    """Reported honestly: created / skipped / conflicted counts and pending reasons."""

    employee_id: str = ""
    eligible: bool = False
    pending_reasons: list[str] = field(default_factory=list)
    start_date: date | None = None
    end_date: date | None = None
    created_count: int = 0
    skipped_existing_count: int = 0
    skipped_holiday_count: int = 0
    skipped_non_working_day_count: int = 0
    skipped_inactive_assignment_count: int = 0
    skipped_cancelled_count: int = 0
    conflict_count: int = 0
    conflicts: list[dict] = field(default_factory=list)
    restored_count: int = 0
    cancelled_count: int = 0
    blocking_conflicts: list[str] = field(default_factory=list)

    @property
    def pending_reason(self) -> str:
        return " ".join(self.pending_reasons)

    def summary(self) -> str:
        return (
            f"{self.created_count} created, {self.skipped_existing_count} already present, "
            f"{self.skipped_holiday_count} holidays skipped, "
            f"{self.skipped_non_working_day_count} non-working days skipped, "
            f"{self.conflict_count} conflicts, {self.restored_count} restored, "
            f"{self.cancelled_count} cancelled."
        )


def _empty_result(employee) -> ContractScheduleResult:
    return ContractScheduleResult(employee_id=getattr(employee, "employee_id", "") or "")


def _add_pending(result: ContractScheduleResult, reason: str):
    if reason not in result.pending_reasons:
        result.pending_reasons.append(reason)


def resolve_auto_schedule_shift(employee, assignment=None):
    """Return (shift, error_reason) for this employee's automatic scheduling setup.

    Never falls back to another shift and never picks "the first" shift.
    """
    if not getattr(employee, "automatic_schedule_enabled", False):
        return None, PENDING_REASONS["disabled"]
    shift = getattr(employee, "automatic_schedule_shift", None)
    if shift is None:
        return None, PENDING_REASONS["no_shift"]
    if not shift.is_active:
        return None, PENDING_REASONS["inactive_shift"]
    if not shift.has_break_times:
        return None, PENDING_REASONS["shift_timing"]
    if assignment is not None and shift.office_id != assignment.position.office_id:
        return None, PENDING_REASONS["shift_office_mismatch"]
    return shift, ""


def _eligible_assignment_q(on_date):
    """Assignment queryset predicate that includes the contract-bounded end date."""
    return (
        Q(employee__contract_end_date__isnull=True)
        | Q(employee__contract_end_date__gte=on_date)
    )


def resolve_auto_schedule_assignment(employee, on_date=None, *, allow_future=False):
    """Resolve (status, assignment) for the employee's contract scheduling.

    Status is one of ``single``, ``missing``, ``ambiguous`` or ``no_position``.
    Missing and ambiguous assignments fail closed and never choose an arbitrary row.
    By default only an assignment that is currently valid is returned. When
    ``allow_future`` is set, a future-dated primary assignment is also accepted
    (used while reconciling before the assignment starts).
    """
    from employees.models import EmployeeAssignment

    on_date = on_date or timezone.localdate()
    queryset = EmployeeAssignment.objects.filter(employee=employee, is_primary=True)
    if allow_future:
        queryset = queryset.filter(
            Q(end_date__isnull=True) | Q(end_date__gte=on_date)
        ).filter(_eligible_assignment_q(on_date))
    else:
        queryset = queryset.filter(start_date__lte=on_date).filter(
            Q(end_date__isnull=True) | Q(end_date__gte=on_date)
        ).filter(_eligible_assignment_q(on_date))
    candidates = list(queryset.select_related("position__office").order_by("-start_date"))
    if not candidates:
        return "missing", None
    if len(candidates) > 1:
        return "ambiguous", None
    assignment = candidates[0]
    if assignment.position is None or assignment.position.office_id is None:
        return "no_position", None
    return "single", assignment


def contract_period(employee, assignment, *, today=None, require_end=True):
    """(start, end, reason) for automatic scheduling.

    start = latest of today, contract start and assignment start;
    end   = earliest of contract end and assignment end (assignment end may be blank).
    """
    today = today or timezone.localdate()
    contract_start = getattr(employee, "contract_start_date", None)
    contract_end = getattr(employee, "contract_end_date", None)
    if not contract_start:
        return None, None, PENDING_REASONS["no_contract_start"]
    if require_end and not contract_end:
        return None, None, PENDING_REASONS["no_contract_end"]
    if assignment is None:
        return None, None, PENDING_REASONS["no_assignment"]

    start = max(today, contract_start, assignment.start_date)
    ends = [value for value in (contract_end, assignment.end_date) if value is not None]
    if not ends:
        return None, None, PENDING_REASONS["no_contract_end"]
    end = min(ends)
    if end < start:
        return start, end, ""
    return start, end, ""


def _eligible_generation_dates(shift, start, end, result: ContractScheduleResult):
    """Yield work dates inside [start, end] that pass working-day and holiday rules.

    Existing contract schedules cancelled for an active holiday are excluded here
    and are owned by the contract holiday reconciliation flow.
    """
    working_days = set(shift.working_days_list)
    current = start
    while current <= end:
        if current.weekday() not in working_days:
            result.skipped_non_working_day_count += 1
        elif _holidays_on(shift.office, current):
            result.skipped_holiday_count += 1
        else:
            yield current
        current += timedelta(days=1)


def _holidays_on(office, day):
    from attendance.services.scheduling import holidays_on

    return list(holidays_on(office, day))


def _create_contract_schedule(assignment, shift, work_date, snapshot, actor_user):
    from attendance.models import EmployeeSchedule

    schedule = EmployeeSchedule(
        employee_assignment=assignment,
        shift=shift,
        work_date=work_date,
        status=EmployeeSchedule.Status.SCHEDULED,
        schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        assigned_by=actor_user,
    )
    schedule.apply_snapshot(snapshot)
    schedule.full_clean()
    with transaction.atomic():
        schedule.save()
    return schedule


def _log_contract_event(employee, action, result: ContractScheduleResult, reason=""):
    try:
        from audittrail.models import AuditLog
        from audittrail.services import log_action

        assignment = getattr(employee, "_auto_schedule_assignment", None)
        log_action(
            actor_user=None,
            office=getattr(getattr(assignment, "position", None), "office", None),
            action=action,
            entity_type="Employee",
            object_id=employee.pk,
            after_data={
                "automatic_schedule_enabled": bool(getattr(employee, "automatic_schedule_enabled", False)),
                "automatic_schedule_shift_id": getattr(getattr(employee, "automatic_schedule_shift", None), "pk", None),
                "contract_start_date": str(getattr(employee, "contract_start_date", "") or ""),
                "contract_end_date": str(getattr(employee, "contract_end_date", "") or ""),
                "created": result.created_count,
                "cancelled": result.cancelled_count,
                "conflicts": result.conflict_count,
            },
            reason=reason or "Contract-based automatic scheduling synchronization.",
        )
    except Exception:  # pragma: no cover - audit failures must never block scheduling
        logger.exception("Could not record contract scheduling audit entry for employee %s", getattr(employee, "pk", None))


def generate_contract_schedules(
    employee,
    *,
    actor_user=None,
    today=None,
    assignment=None,
    ignore_cancelled_dates=None,
    restore_holiday_cancelled=False,
) -> ContractScheduleResult:
    """Generate contract-bounded schedules for ONE employee and ONE assignment.

    Idempotent: existing schedules (including cancelled ones) are never recreated,
    manual schedules are preserved, and the unique database constraint plus an
    IntegrityError guard make concurrent runs safe.

    ``ignore_cancelled_dates`` lets the reconciliation flow recreate a slot it just
    cancelled itself (e.g. after a shift change) without resurrecting any other
    cancelled schedule. ``restore_holiday_cancelled`` is reserved for the explicit
    contract holiday synchronization path.
    """
    from attendance.models import EmployeeSchedule
    from attendance.services.scheduling import build_schedule_snapshot, holidays_on, overlapping_schedules

    today = today or timezone.localdate()
    ignore_cancelled_dates = set(ignore_cancelled_dates or ())
    result = _empty_result(employee)
    if getattr(employee, "employment_status", None) != "active":
        _add_pending(result, PENDING_REASONS["inactive_employee"])
        return result

    if assignment is None:
        assignment_status, resolved_assignment = resolve_auto_schedule_assignment(
            employee, today, allow_future=True
        )
        if assignment_status == "ambiguous":
            _add_pending(result, PENDING_REASONS["ambiguous_assignment"])
            return result
        if resolved_assignment is None:
            _add_pending(result, PENDING_REASONS["no_assignment"])
            return result
        assignment = resolved_assignment
    employee._auto_schedule_assignment = assignment

    shift, reason = resolve_auto_schedule_shift(employee, assignment)
    if shift is None:
        _add_pending(result, reason)
        return result

    start, end, reason = contract_period(employee, assignment, today=today)
    if reason:
        _add_pending(result, reason)
        return result
    result.start_date, result.end_date = start, end
    if end < start:
        return result

    result.eligible = True
    holiday_cancelled_qs = EmployeeSchedule.objects.filter(
        employee_assignment__employee=employee,
        schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        status=EmployeeSchedule.Status.CANCELLED,
        cancelled_by_holiday__isnull=False,
        work_date__gte=start,
        work_date__lte=end,
    )

    # Restore only when an explicit holiday synchronization calls this path.
    if restore_holiday_cancelled:
        for schedule in holiday_cancelled_qs:
            active_hols = holidays_on(schedule.shift.office, schedule.work_date)
            if not active_hols:
                snapshot = build_schedule_snapshot(schedule.shift, schedule.work_date)
                clash = overlapping_schedules(
                    employee.id, snapshot.start, snapshot.end,
                    exclude_pk=schedule.pk
                ).first()
                if clash is not None:
                    result.conflict_count += 1
                    continue
                schedule.status = EmployeeSchedule.Status.SCHEDULED
                schedule.cancelled_by_holiday = None
                schedule.apply_snapshot(snapshot)
                schedule.notes = f"{schedule.notes}\nRestored: Holiday is no longer active.".strip()
                schedule.save(
                    update_fields=[
                        "status", "cancelled_by_holiday", "scheduled_start_at",
                        "scheduled_break_start_at", "scheduled_break_end_at",
                        "scheduled_end_at", "grace_period_minutes",
                        "early_checkin_window_minutes", "notes", "updated_at",
                    ]
                )
                result.restored_count += 1
                continue

            if hasattr(schedule, "attendance_record"):
                continue
            schedule.cancelled_by_holiday = active_hols[0]
            schedule.notes = (
                f"{schedule.notes}\nAutomatically cancelled due to holiday: "
                f"{active_hols[0].name} ({active_hols[0].scope_label})"
            ).strip()
            schedule.save(update_fields=["cancelled_by_holiday", "notes", "updated_at"])

    for work_date in _eligible_generation_dates(shift, start, end, result):
        try:
            snapshot = build_schedule_snapshot(shift, work_date)
        except Exception:
            _add_pending(result, PENDING_REASONS["shift_timing"])
            break

        existing = list(
            EmployeeSchedule.objects.filter(employee_assignment=assignment, work_date=work_date)
        )
        if any(
            item.status in EmployeeSchedule.LIVE_STATUSES
            or item.status == EmployeeSchedule.Status.COMPLETED
            or hasattr(item, "attendance_record")
            for item in existing
        ):
            result.skipped_existing_count += 1
            continue
        if any(item.status == EmployeeSchedule.Status.CANCELLED for item in existing):
            if work_date not in ignore_cancelled_dates:
                result.skipped_cancelled_count += 1
                continue

        clash = overlapping_schedules(
            assignment.employee_id, snapshot.start, snapshot.end,
            exclude_pk__in=[item.pk for item in existing if item.status == EmployeeSchedule.Status.CANCELLED],
        ).first()
        if clash is not None:
            result.conflict_count += 1
            result.conflicts.append({
                "employee": employee.employee_id,
                "date": str(work_date),
                "reason": f"Overlap with schedule #{clash.pk} ({clash.work_date})",
            })
            continue

        try:
            _create_contract_schedule(assignment, shift, work_date, snapshot, actor_user)
        except IntegrityError:
            result.skipped_existing_count += 1
        except Exception as exc:
            result.conflict_count += 1
            result.conflicts.append({"employee": employee.employee_id, "date": str(work_date), "reason": str(exc)})
        else:
            result.created_count += 1

    return result


def _cancel_future_contract_schedules(
    employee, *, today, keep_shift_id, keep_dates=None, exclude_assignment=None, reason=""
):
    """Cancel future, unworked schedules owned by this feature.

    Returns ``(count, blockers, cancelled_dates)``.
    """
    from attendance.models import EmployeeSchedule

    queryset = EmployeeSchedule.objects.filter(
        employee_assignment__employee=employee,
        work_date__gte=today,
        schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        status__in=EmployeeSchedule.LIVE_STATUSES,
    ).select_related("attendance_record", "employee_assignment")
    if keep_shift_id is not None:
        queryset = queryset.filter(shift_id=keep_shift_id)
    if keep_dates is not None:
        queryset = queryset.exclude(work_date__in=keep_dates)
    if exclude_assignment is not None:
        queryset = queryset.exclude(employee_assignment=exclude_assignment)

    cancelled = 0
    cancelled_dates = set()
    blockers = []
    for schedule in queryset:
        if hasattr(schedule, "attendance_record"):
            blockers.append(
                f"Schedule #{schedule.pk} on {schedule.work_date} already has attendance and cannot be reconciled automatically."
            )
            continue
        schedule.status = EmployeeSchedule.Status.CANCELLED
        schedule.cancelled_by_holiday = None
        note = reason or "Cancelled by contract-based automatic scheduling reconciliation."
        schedule.notes = f"{schedule.notes}\n{note}".strip()
        schedule.save(update_fields=["status", "cancelled_by_holiday", "notes", "updated_at"])
        cancelled += 1
        cancelled_dates.add(schedule.work_date)
    return cancelled, blockers, cancelled_dates


def reconcile_contract_schedules(
    employee,
    *,
    actor_user=None,
    today=None,
    previous_shift_id=None,
    contract_changed=False,
    force=False,
) -> ContractScheduleResult:
    """Reconcile future, unworked schedules owned by this feature.

    * extension -> only missing eligible schedules are added;
    * shortening, shift change, disabled automatic scheduling or employee
      deactivation -> only future, unworked contract schedules are cancelled.

    Manual schedules, historical/attended schedules and schedules cancelled by
    approved leave or manual action are never modified.
    """
    from attendance.models import EmployeeSchedule

    today = today or timezone.localdate()
    result = _empty_result(employee)

    shift, reason = resolve_auto_schedule_shift(employee, None)
    active = getattr(employee, "employment_status", None) == "active"
    disabled_or_inactive = (not getattr(employee, "automatic_schedule_enabled", False)) or (not active)
    shift_changed = previous_shift_id is not None and (
        shift is None or shift.pk != previous_shift_id
    )

    assignment_status, assignment = resolve_auto_schedule_assignment(
        employee, today, allow_future=True
    )
    if assignment_status == "ambiguous":
        _add_pending(result, PENDING_REASONS["ambiguous_assignment"])
    elif assignment is not None:
        employee._auto_schedule_assignment = assignment

    if disabled_or_inactive:
        result.cancelled_count, result.blocking_conflicts, _ = _cancel_future_contract_schedules(
            employee,
            today=today,
            keep_shift_id=None,
            reason="Cancelled: automatic scheduling was disabled or the employee is no longer active.",
        )
        if result.cancelled_count or result.blocking_conflicts:
            _log_contract_event(
                employee, "update", result,
                reason="Automatic contract scheduling disabled or employee inactive.",
            )
        if not getattr(employee, "automatic_schedule_enabled", False):
            _add_pending(result, PENDING_REASONS["disabled"])
        if not active:
            _add_pending(result, PENDING_REASONS["inactive_employee"])
        return result

    # Assignment change (e.g. branch/department transfer or position change): cancel
    # future contract schedules that still belong to a different, now-ended assignment.
    # Historical, attended, manual and holiday-cancelled rows are preserved by
    # _cancel_future_contract_schedules. The current assignment's own schedules are
    # reconciled below and are never touched here.
    if assignment is not None:
        stale_cancelled, stale_blockers, _ = _cancel_future_contract_schedules(
            employee,
            today=today,
            keep_shift_id=None,
            exclude_assignment=assignment,
            reason="Cancelled: the employee moved to a different assignment.",
        )
        result.cancelled_count += stale_cancelled
        result.blocking_conflicts.extend(stale_blockers)
        if stale_cancelled:
            _log_contract_event(employee, "update", result, reason="Assignment change reconciliation.")

    ignore_cancelled_dates = set()
    if shift_changed:
        # Cancel the previous shift's future contract schedules, then regenerate below.
        keep_shift_id = previous_shift_id
        reason = (
            "Cancelled: the automatic work shift changed."
            if keep_shift_id is not None
            else "Cancelled: automatic scheduling was disabled."
        )
        cancelled, blockers, cancelled_dates = _cancel_future_contract_schedules(
            employee,
            today=today,
            keep_shift_id=keep_shift_id,
            reason=reason,
        )
        result.cancelled_count += cancelled
        result.blocking_conflicts.extend(blockers)
        ignore_cancelled_dates |= cancelled_dates

    generation = generate_contract_schedules(
        employee,
        actor_user=actor_user,
        today=today,
        assignment=assignment,
        ignore_cancelled_dates=ignore_cancelled_dates,
    )
    result.eligible = generation.eligible
    result.pending_reasons = generation.pending_reasons
    result.start_date, result.end_date = generation.start_date, generation.end_date
    result.created_count = generation.created_count
    result.skipped_existing_count = generation.skipped_existing_count
    result.skipped_holiday_count = generation.skipped_holiday_count
    result.skipped_non_working_day_count = generation.skipped_non_working_day_count
    result.skipped_inactive_assignment_count = generation.skipped_inactive_assignment_count
    result.skipped_cancelled_count = generation.skipped_cancelled_count
    result.conflict_count = generation.conflict_count
    result.conflicts = generation.conflicts
    result.restored_count = generation.restored_count

    if contract_changed and generation.eligible and generation.end_date is not None:
        # Shortening: future contract schedules past the new contract end are cancelled.
        cancelled, blockers = cancel_future_contract_schedules_after(
            employee,
            end_date=generation.end_date,
            today=today,
        )
        result.cancelled_count += cancelled
        result.blocking_conflicts.extend(blockers)
    return result


def _holiday_scope(holiday):
    if holiday is None or holiday.office_id is None:
        return Q()
    return Q(shift__office_id=holiday.office_id)


def sync_contract_holiday_schedules(
    employee, *, today=None, holiday=None, office=None
) -> ContractScheduleResult:
    """Reconcile contract schedules for a holiday without touching other lifecycle cancellations."""
    from attendance.models import EmployeeSchedule
    from attendance.services.scheduling import build_schedule_snapshot, holidays_on, overlapping_schedules

    today = today or timezone.localdate()
    result = _empty_result(employee)
    assignment_status, assignment = resolve_auto_schedule_assignment(
        employee, today, allow_future=True
    )
    if assignment_status == "ambiguous":
        _add_pending(result, PENDING_REASONS["ambiguous_assignment"])
        return result
    if assignment is None:
        _add_pending(result, PENDING_REASONS["no_assignment"])
        return result
    employee._auto_schedule_assignment = assignment

    contract_qs = EmployeeSchedule.objects.filter(
        employee_assignment__employee=employee,
        schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        work_date__gte=today,
    ).select_related("shift", "employee_assignment", "attendance_record")
    if holiday is not None and holiday.pk:
        contract_qs = contract_qs.filter(_holiday_scope(holiday))
    elif office is not None:
        contract_qs = contract_qs.filter(shift__office=office)

    for schedule in contract_qs.filter(status__in=EmployeeSchedule.LIVE_STATUSES):
        active_hols = holidays_on(schedule.shift.office, schedule.work_date)
        if active_hols.exists():
            if holiday is not None and holiday.pk and not active_hols.filter(pk=holiday.pk).exists():
                continue
            schedule.status = EmployeeSchedule.Status.CANCELLED
            schedule.cancelled_by_holiday = active_hols.first()
            schedule.notes = (
                f"{schedule.notes}\nAutomatically cancelled due to holiday: "
                f"{active_hols.first().name} ({active_hols.first().scope_label})"
            ).strip()
            schedule.save(update_fields=["status", "cancelled_by_holiday", "notes", "updated_at"])
            result.cancelled_count += 1

    holiday_cancelled_qs = EmployeeSchedule.objects.filter(
        employee_assignment__employee=employee,
        schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        work_date__gte=today,
        status=EmployeeSchedule.Status.CANCELLED,
        cancelled_by_holiday__isnull=False,
        attendance_record__isnull=True,
    ).select_related("shift", "employee_assignment", "cancelled_by_holiday")
    if holiday is not None and holiday.pk:
        holiday_cancelled_qs = holiday_cancelled_qs.filter(cancelled_by_holiday=holiday)
    elif office is not None:
        holiday_cancelled_qs = holiday_cancelled_qs.filter(shift__office=office)

    for schedule in holiday_cancelled_qs:
        active_hols = holidays_on(schedule.shift.office, schedule.work_date)
        if active_hols.exists():
            if holiday is not None and holiday.pk and not active_hols.filter(pk=holiday.pk).exists():
                continue
            schedule.cancelled_by_holiday = active_hols.first()
            schedule.notes = (
                f"{schedule.notes}\nAutomatically cancelled due to holiday: "
                f"{active_hols.first().name} ({active_hols.first().scope_label})"
            ).strip()
            schedule.save(update_fields=["cancelled_by_holiday", "notes", "updated_at"])
            continue

        assignment = schedule.employee_assignment
        if (
            assignment is None
            or assignment.employee.employment_status != "active"
            or not assignment.is_effective_active_on(schedule.work_date)
            or schedule.shift is None
            or not schedule.shift.is_active
            or schedule.shift.office_id != assignment.position.office_id
        ):
            continue

        snapshot = build_schedule_snapshot(schedule.shift, schedule.work_date)
        clash = overlapping_schedules(
            employee.id, snapshot.start, snapshot.end, exclude_pk=schedule.pk
        ).first()
        if clash is not None:
            result.conflict_count += 1
            continue

        schedule.status = EmployeeSchedule.Status.SCHEDULED
        schedule.cancelled_by_holiday = None
        schedule.apply_snapshot(snapshot)
        schedule.notes = f"{schedule.notes}\nRestored: Holiday is no longer active.".strip()
        schedule.save(
            update_fields=[
                "status", "cancelled_by_holiday", "scheduled_start_at",
                "scheduled_break_start_at", "scheduled_break_end_at",
                "scheduled_end_at", "grace_period_minutes",
                "early_checkin_window_minutes", "notes", "updated_at",
            ]
        )
        result.restored_count += 1

    return result


def cancel_future_contract_schedules_after(employee, *, end_date, today=None, reason=""):
    """Cancel future contract schedules that fall after ``end_date`` (contract shortening)."""
    from attendance.models import EmployeeSchedule

    today = today or timezone.localdate()
    queryset = EmployeeSchedule.objects.filter(
        employee_assignment__employee=employee,
        work_date__gte=today,
        work_date__gt=end_date,
        schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
        status__in=EmployeeSchedule.LIVE_STATUSES,
    ).select_related("attendance_record")
    cancelled = 0
    blockers = []
    for schedule in queryset:
        if hasattr(schedule, "attendance_record"):
            blockers.append(f"Schedule #{schedule.pk} on {schedule.work_date} has attendance and was left unchanged.")
            continue
        schedule.status = EmployeeSchedule.Status.CANCELLED
        schedule.cancelled_by_holiday = None
        note = reason or "Cancelled: the contract period was shortened."
        schedule.notes = f"{schedule.notes}\n{note}".strip()
        schedule.save(update_fields=["status", "cancelled_by_holiday", "notes", "updated_at"])
        cancelled += 1
    return cancelled, blockers


def synchronize_employee(employee, *, actor_user=None, previous=None, today=None) -> ContractScheduleResult:
    """Single entry point used by every employee/assignment save path.

    ``previous`` is a snapshot dict of the values before this save (contract dates,
    shift id, employment status, automatic-scheduling flag). It is optional: when
    omitted the reconciliation simply generates whatever is missing.
    """
    today = today or timezone.localdate()
    previous = previous or {}
    contract_changed = any(
        key in previous and previous[key] != getattr(employee, key, None)
        for key in ("contract_start_date", "contract_end_date")
    )
    previous_shift_id = previous.get("automatic_schedule_shift_id")

    result = reconcile_contract_schedules(
        employee,
        actor_user=actor_user,
        today=today,
        previous_shift_id=previous_shift_id,
        contract_changed=contract_changed,
    )

    # Contract shortening: drop future contract schedules past the new end date.
    new_end = getattr(employee, "contract_end_date", None)
    old_end = previous.get("contract_end_date")
    if new_end is not None and old_end is not None and new_end < old_end:
        cancelled, blockers = cancel_future_contract_schedules_after(
            employee, end_date=new_end, today=today,
        )
        result.cancelled_count += cancelled
        result.blocking_conflicts.extend(blockers)
        if cancelled:
            _log_contract_event(employee, "update", result, reason="Contract end date shortened.")
    return result


def snapshot_employee_state(employee) -> dict:
    return {
        "contract_start_date": getattr(employee, "contract_start_date", None),
        "contract_end_date": getattr(employee, "contract_end_date", None),
        "automatic_schedule_shift_id": getattr(
            getattr(employee, "automatic_schedule_shift", None), "pk", None
        ),
        "automatic_schedule_enabled": bool(getattr(employee, "automatic_schedule_enabled", False)),
        "employment_status": getattr(employee, "employment_status", None),
    }