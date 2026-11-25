from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from django.utils import timezone

from audittrail.models import AuditLog
from audittrail.services import log_action
from employee_requests.models import EmployeeRequest

try:
    from attendance.models import AttendanceEvent, AttendanceRecord, EmployeeSchedule
except Exception:  # pragma: no cover
    AttendanceEvent = AttendanceRecord = EmployeeSchedule = None

LEAVE_CANCEL_NOTE_PREFIX = "Cancelled - Approved leave (Request "


@dataclass
class ScheduleProcessingTarget:
    schedule_id: int
    work_date: str
    schedule_status: str
    schedule_source: str
    shift: str
    has_actual_attendance: bool
    existing_cancel_note: str
    action: str
    reason: str


@dataclass
class ScheduleProcessingPreview:
    request_id: int
    request_number: str
    supported: bool
    can_apply: bool
    block_reason: str = ""
    warning: str = ""
    targets: list[ScheduleProcessingTarget] = field(default_factory=list)
    fingerprint: str = ""
    request_status: str = ""
    request_type: str = ""
    duration_type: str = ""
    start_date: str = ""
    end_date: str = ""

    @property
    def affected_ids(self):
        return [t.schedule_id for t in self.targets if t.action == "cancel"]

    @property
    def zero_matches(self):
        return self.supported and len(self.targets) == 0 and not self.block_reason


def _fingerprint(request_record, targets):
    parts = [
        str(request_record.pk),
        request_record.request_number,
        request_record.status,
        request_record.request_type,
        request_record.duration_type or "",
        str(request_record.start_date),
        str(request_record.end_date),
        str(request_record.branch_id or ""),
        str(request_record.employee_assignment_id or ""),
        str(request_record.schedule_processed_at or ""),
        str(request_record.updated_at or ""),
    ]
    for t in targets:
        parts.extend([str(t.schedule_id), str(t.work_date), t.schedule_status, t.existing_cancel_note, str(t.has_actual_attendance)])
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _schedule_has_actual_attendance(schedule):
    record = getattr(schedule, "attendance_record", None)
    if record is not None:
        if getattr(record, "first_check_in_at", None) or getattr(record, "last_check_out_at", None):
            return True
        if getattr(record, "status", "") in {"present", "late", "incomplete", "pending_review"}:
            return True
        if AttendanceEvent is not None and AttendanceEvent.objects.filter(attendance_record=record).exclude(event_type="correction").exists():
            return True
    if AttendanceEvent is not None and AttendanceEvent.objects.filter(attendance_record__schedule=schedule).exclude(event_type="correction").exists():
        return True
    return False


def build_schedule_processing_preview(request_record, *, actor_user=None):
    request_record = EmployeeRequest.objects.select_related(
        "employee_assignment__position__office",
        "employee_assignment__position__department",
        "employee_assignment__employee",
        "branch",
    ).get(pk=request_record.pk)
    preview = ScheduleProcessingPreview(
        request_id=request_record.pk,
        request_number=request_record.request_number,
        supported=False,
        can_apply=False,
        request_status=request_record.status,
        request_type=request_record.request_type,
        duration_type=request_record.duration_type or EmployeeRequest.DurationType.FULL_DAY,
        start_date=str(request_record.start_date),
        end_date=str(request_record.end_date),
    )

    if request_record.status != EmployeeRequest.Status.APPROVED:
        preview.block_reason = "Only Director-approved requests can be processed by Staff HR."
        return preview
    if request_record.schedule_processed_at is not None:
        preview.supported = True
        preview.can_apply = False
        preview.block_reason = "Schedule processing has already been recorded for this request."
        preview.fingerprint = _fingerprint(request_record, [])
        return preview

    assignment = request_record.employee_assignment
    position = getattr(assignment, "position", None)
    if position is None or getattr(position, "office_id", None) is None:
        preview.block_reason = "The request's employee assignment/branch is missing or invalid; processing is blocked."
        return preview
    if request_record.branch_id and request_record.branch_id != position.office_id:
        preview.block_reason = "The request branch does not match the employee assignment office; processing is blocked."
        return preview

    duration = request_record.duration_type or EmployeeRequest.DurationType.FULL_DAY
    if request_record.request_type != EmployeeRequest.RequestType.LEAVE:
        preview.block_reason = (
            f"Request type '{request_record.get_request_type_display()}' is not supported for automatic schedule processing. "
            "Process this request manually with an auditable HR action if policy allows."
        )
        return preview
    if duration != EmployeeRequest.DurationType.FULL_DAY:
        preview.block_reason = (
            f"{request_record.get_duration_type_display()} leave cannot be safely applied to full-day schedules without a verified target interval. "
            "Processing is blocked; no schedules were changed."
        )
        return preview

    if EmployeeSchedule is None:
        preview.block_reason = "Schedule model is unavailable; processing is blocked."
        return preview

    schedules = list(EmployeeSchedule.objects.select_related(
        "shift", "attendance_record",
    ).filter(
        employee_assignment=request_record.employee_assignment,
        work_date__range=(request_record.start_date, request_record.end_date),
    ).order_by("work_date", "pk"))

    this_note = f"{LEAVE_CANCEL_NOTE_PREFIX}{request_record.request_number})"
    targets = []
    blockers = []
    for sch in schedules:
        actual = _schedule_has_actual_attendance(sch)
        note = (sch.notes or "").strip()
        already_this = sch.status == EmployeeSchedule.Status.CANCELLED and this_note in note
        if sch.status == EmployeeSchedule.Status.COMPLETED:
            blockers.append(f"Schedule #{sch.pk} ({sch.work_date}) is completed and cannot be cancelled.")
            action, reason = "blocked", "Completed schedule"
        elif sch.status == EmployeeSchedule.Status.CANCELLED and already_this:
            action, reason = "noop", "Already cancelled by this approved request"
        elif sch.status == EmployeeSchedule.Status.CANCELLED:
            blockers.append(f"Schedule #{sch.pk} ({sch.work_date}) is already cancelled for another reason.")
            action, reason = "blocked", "Cancelled for another reason"
        elif actual:
            blockers.append(f"Schedule #{sch.pk} ({sch.work_date}) has actual attendance/conflict evidence; cannot auto-cancel.")
            action, reason = "blocked", "Actual attendance/conflict"
        elif sch.status in {EmployeeSchedule.Status.PLANNED, EmployeeSchedule.Status.SCHEDULED}:
            action, reason = "cancel", "Will cancel (Approved leave)"
        else:
            blockers.append(f"Schedule #{sch.pk} ({sch.work_date}) has unsupported status '{sch.status}'.")
            action, reason = "blocked", f"Unsupported status {sch.status}"
        targets.append(ScheduleProcessingTarget(
            schedule_id=sch.pk, work_date=str(sch.work_date), schedule_status=sch.status,
            schedule_source=getattr(sch, "schedule_source", ""), shift=str(getattr(sch, "shift", "")),
            has_actual_attendance=actual, existing_cancel_note=note, action=action, reason=reason,
        ))

    preview.supported = True
    preview.targets = targets
    if not targets:
        preview.block_reason = "No matching schedules were found for the approved request period. No schedules were changed."
        preview.can_apply = False
    elif blockers:
        preview.block_reason = "Processing blocked: " + " ".join(blockers[:6]) + (" ..." if len(blockers) > 6 else "")
        preview.can_apply = False
    else:
        preview.can_apply = True
        preview.warning = "Review affected schedules before confirming. Confirmation records an auditable Staff HR processing action."
    preview.fingerprint = _fingerprint(request_record, targets)
    return preview


def apply_schedule_processing(request_record, *, actor_user, office, ip_address=None, fingerprint=""):
    from django.db import transaction as db_transaction

    with db_transaction.atomic():
        locked = EmployeeRequest.objects.select_for_update().select_related(
            "employee_assignment__position__office", "employee_assignment__position__department",
            "employee_assignment__employee", "branch",
        ).get(pk=request_record.pk)
        before = {"schedule_processed_at": str(locked.schedule_processed_at) if locked.schedule_processed_at else None}
        if locked.schedule_processed_at is not None:
            return locked, "already_processed", []
        preview = build_schedule_processing_preview(locked, actor_user=actor_user)
        if fingerprint and fingerprint != preview.fingerprint:
            raise ValueError("The request or its target schedules changed since preview. Review the updated preview before confirming.")
        if not preview.can_apply:
            raise ValueError(preview.block_reason or "Schedule processing cannot be applied safely.")

        schedule_qs = EmployeeSchedule.objects.select_for_update().select_related("attendance_record", "shift").filter(
            pk__in=preview.affected_ids, employee_assignment=locked.employee_assignment,
            work_date__range=(locked.start_date, locked.end_date),
        )
        locked_schedules = {s.pk: s for s in schedule_qs}
        if set(locked_schedules) != set(preview.affected_ids):
            raise ValueError("Target schedules changed since preview. Review the updated preview before confirming.")
        this_note = f"{LEAVE_CANCEL_NOTE_PREFIX}{locked.request_number})"
        results = []
        for sid in preview.affected_ids:
            sch = locked_schedules[sid]
            if _schedule_has_actual_attendance(sch):
                raise ValueError(f"Schedule #{sch.pk} now has actual attendance/conflict evidence. Processing blocked.")
            if sch.status not in {EmployeeSchedule.Status.PLANNED, EmployeeSchedule.Status.SCHEDULED}:
                raise ValueError(f"Schedule #{sch.pk} status changed to '{sch.status}'. Processing blocked.")
            sch.status = EmployeeSchedule.Status.CANCELLED
            existing = (sch.notes or "").strip()
            sch.notes = this_note if not existing else f"{existing}\n{this_note}"
            sch.save(update_fields=["status", "notes", "updated_at"])
            results.append({"schedule_id": sch.pk, "work_date": str(sch.work_date), "result": "cancelled"})
        now = timezone.now()
        locked.schedule_processed_at = now
        locked.schedule_processed_by = actor_user
        locked.save(update_fields=["schedule_processed_at", "schedule_processed_by", "updated_at"])
        log_action(
            actor_user=actor_user,
            office=office,
            action=AuditLog.Action.UPDATE,
            entity_type="EmployeeRequest",
            object_id=locked.pk,
            before_data=before,
            after_data={
                "schedule_processed_at": now.isoformat(),
                "schedule_processed_by_id": getattr(actor_user, "pk", None),
                "affected_schedule_ids": preview.affected_ids,
                "results": results,
            },
            reason="Staff HR applied approved leave schedule processing (preview confirmed).",
            ip_address=ip_address,
        )
        return locked, "applied", results
