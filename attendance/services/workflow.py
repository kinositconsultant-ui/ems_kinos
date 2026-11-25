"""Four-event attendance workflow: CHECK_IN -> BREAK_OUT -> BREAK_IN -> CHECK_OUT.

The kiosk never asks the employee which event to record. The next event is derived
from the events already stored for the schedule's AttendanceRecord, the schedule
snapshot, and the policy. Server time is the only time source. Nothing here invents
a missing event or a missing timestamp.

Event type values keep their historical stored names: ``break_start`` is shown as
"Break Out" and ``break_end`` as "Break In".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import BooleanField, Case, Count, F, Q, When
from django.db.models import Value
from django.utils import timezone

CHECK_IN = "check_in"
BREAK_OUT = "break_start"
BREAK_IN = "break_end"
CHECK_OUT = "check_out"
SEQUENCE = (CHECK_IN, BREAK_OUT, BREAK_IN, CHECK_OUT)
LABELS = {
    CHECK_IN: "Check In",
    BREAK_OUT: "Break Out",
    BREAK_IN: "Break In",
    CHECK_OUT: "Check Out",
}

# A second scan this soon after a stored event is treated as a double click / retry and
# rejected; otherwise it would be read as the *next* event in the sequence.
EVENT_COOLDOWN_SECONDS = 60
# How long after the scheduled end an open session may still Check Out.
# TODO(business decision): confirm this value with the institution.
CHECKOUT_GRACE_MINUTES = 120


class EventRejected(Exception):
    """The kiosk must not store an event. ``flag_anomaly`` marks the session for review."""

    def __init__(self, stage, message, status=409, *, flag_anomaly=False):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.status = status
        self.flag_anomaly = flag_anomaly


@dataclass(frozen=True)
class SessionState:
    next_type: str | None
    next_label: str
    last_event_type: str | None
    last_event_label: str
    last_event_at: object
    complete: bool


def recorded_events(record):
    """First stored event of each required type, keyed by type."""
    found = {}
    if record is None or record.pk is None:
        return found
    for event in record.events.filter(event_type__in=SEQUENCE).order_by("occurred_at", "pk"):
        found.setdefault(event.event_type, event)
    return found


def is_session_complete(record):
    if record is None:
        return False
    if record.last_check_out_at is None or record.first_check_in_at is None:
        return False
    events = recorded_events(record)
    return CHECK_IN in events and CHECK_OUT in events


def next_event_type(record):
    events = recorded_events(record)
    for kind in SEQUENCE:
        if kind not in events:
            return kind
    return None


def session_state(record) -> SessionState:
    events = recorded_events(record)
    nxt = next_event_type(record) if record is not None else CHECK_IN
    last = max(events.values(), key=lambda e: (e.occurred_at, e.pk), default=None)
    return SessionState(
        next_type=nxt,
        next_label=LABELS.get(nxt, "Session complete"),
        last_event_type=last.event_type if last else None,
        last_event_label=LABELS.get(last.event_type, "") if last else "",
        last_event_at=last.occurred_at if last else None,
        complete=is_session_complete(record),
    )


def schedule_candidates(employee, device, now):
    """Published schedules for this employee/device whose attendance window includes ``now``."""
    from attendance.models import AttendanceRecord, EmployeeSchedule

    today = timezone.localdate(now)
    office = device.office
    schedules = (
        EmployeeSchedule.objects.filter(
            employee_assignment__employee=employee,
            employee_assignment__position__office=office,
            employee_assignment__start_date__lte=F("work_date"),
            shift__office=office,
            shift__is_active=True,
            work_date__in=(today, today - timedelta(days=1)),
            status=EmployeeSchedule.Status.SCHEDULED,
        )
        .filter(Q(employee_assignment__end_date__isnull=True) | Q(employee_assignment__end_date__gte=F("work_date")))
        .filter(
            Q(employee_assignment__employee__contract_end_date__isnull=True)
            | Q(employee_assignment__employee__contract_end_date__gte=F("work_date"))
        )
        .select_related("employee_assignment__employee", "employee_assignment__position__office", "shift")
        .order_by("scheduled_start_at")
    )
    records = {r.schedule_id: r for r in AttendanceRecord.objects.filter(schedule__in=schedules)}
    open_sessions, completed = [], []
    for schedule in schedules:
        opens = schedule.scheduled_start_at - timedelta(minutes=schedule.effective_early_checkin_minutes)
        closes = schedule.scheduled_end_at + timedelta(minutes=CHECKOUT_GRACE_MINUTES)
        if not (opens <= now <= closes):
            continue
        record = records.get(schedule.pk)
        if record is not None and is_session_complete(record):
            completed.append((schedule, record))
        else:
            open_sessions.append((schedule, record))
    return open_sessions, completed


def decide_next_event(schedule, record, now, policy):
    """Return the event type the kiosk should store now, or raise ``EventRejected``."""
    if policy is None:
        raise EventRejected("policy", "No active attendance policy applies to this office today.")
    events = recorded_events(record)
    state = session_state(record) if record is not None else None

    if CHECK_IN not in events:
        earliest = schedule.scheduled_start_at - timedelta(minutes=schedule.effective_early_checkin_minutes)
        if now < earliest:
            raise EventRejected("schedule", "Check In is not open yet for this schedule.")
        # The absent threshold no longer closes Check In by itself: an employee who
        # arrives after it but while the schedule is still open is recorded as a
        # late check-in (LATE), not rejected as absent. Attendance is only refused
        # once the whole operational window (scheduled end + checkout grace) has
        # ended, which is a correction/exception case rather than ordinary lateness.
        closes = schedule.scheduled_end_at + timedelta(minutes=CHECKOUT_GRACE_MINUTES)
        if now > closes:
            raise EventRejected(
                "schedule",
                "Check In is closed: the operational window for this schedule has ended. Ask HR for a correction.",
            )
        return CHECK_IN

    if state.last_event_at is not None and (now - state.last_event_at).total_seconds() < EVENT_COOLDOWN_SECONDS:
        raise EventRejected(
            "duplicate",
            f"{state.last_event_label} was already recorded at "
            f"{timezone.localtime(state.last_event_at):%H:%M:%S}. Wait before scanning again.",
        )
    if now < state.last_event_at:
        raise EventRejected("duplicate", "The server time is earlier than the last recorded event.")

    nxt = state.next_type
    if nxt is None:
        raise EventRejected("duplicate", "All four attendance events are already recorded for this schedule.")

    closes = schedule.scheduled_end_at + timedelta(minutes=CHECKOUT_GRACE_MINUTES)

    # Operational window has fully ended: no more events can be stored.
    # Finalization (Absent/Incomplete) is handled by mark_incomplete_sessions,
    # not by rejecting the scan.
    if now > closes:
        raise EventRejected("schedule", "The operational window for this schedule has ended.")

    # Allow Check Out even when break events are missing. Missing break events
    # remain missing and are flagged at finalization; they do not trap the
    # employee during the open recording window.
    if CHECK_OUT not in events and now >= schedule.scheduled_end_at:
        return CHECK_OUT

    # Break Out past its own window is still recordable (late Break Out); it is
    # not rejected and not auto-converted to Break In. Missing Break Out is
    # flagged at finalization, not by blocking the scan.
    return nxt


def apply_event_to_record(record, schedule, event_type, now, policy):
    """Update the AttendanceRecord fields that depend on the event that was just stored."""
    from attendance.models import AttendanceRecord

    if event_type == CHECK_IN:
        late_after_start = max(0, int((now - schedule.scheduled_start_at).total_seconds() // 60))
        record.first_check_in_at = now
        grace = schedule.effective_grace_minutes or 0
        # Late minutes are measured after the grace period ends, not after the
        # scheduled start time. A check-in inside the grace window is on time.
        late_minutes = max(0, late_after_start - grace)
        record.late_minutes = late_minutes
        record.attendance_policy = policy
        if late_minutes > 0:
            # Late arrival: the check-in happened after the shift grace period, so the
            # session is Late and must be reviewed by HR. The status is never Absent here.
            record.status = AttendanceRecord.Status.LATE
            record.review_status = AttendanceRecord.ReviewStatus.PENDING
            note = (
                f"Late arrival: {late_minutes} minute(s) after the grace period "
                f"({grace} minute(s)) ended."
            )
            record.remarks = (
                f"{record.remarks}\n{note}".strip() if record.remarks else note
            )
        else:
            record.status = AttendanceRecord.Status.PRESENT
            record.review_status = AttendanceRecord.ReviewStatus.NOT_REQUIRED
    elif event_type == CHECK_OUT:
        # A check-out is only meaningful together with a check-in. If a check-in was
        # never recorded (e.g. a legacy/imported event), the record must not be
        # treated as a completed working session and it is flagged for HR review.
        if record.first_check_in_at is None:
            record.review_status = AttendanceRecord.ReviewStatus.PENDING
            note = "Check-out recorded without a check-in; work duration is not calculated."
            record.remarks = (
                f"{record.remarks}\n{note}".strip() if record.remarks else note
            )
        record.last_check_out_at = now
    return record


def recalculate_record(record, schedule=None):
    """Work minutes from the *stored* events: (check_in..break_out) + (break_in..check_out).

    The break interval is never counted as work. With any required event missing the
    total is not final, so it stays 0 instead of being estimated.
    """
    schedule = schedule or record.schedule
    events = recorded_events(record)
    # A check-in is mandatory for any meaningful work duration; a record with a
    # check-out but no check-in never yields a valid duration.
    if record.first_check_in_at is None or CHECK_IN not in events:
        record.total_work_minutes = 0
        record.early_leave_minutes = 0
        return record
    if all(kind in events for kind in SEQUENCE):
        worked = (events[BREAK_OUT].occurred_at - events[CHECK_IN].occurred_at) + (
            events[CHECK_OUT].occurred_at - events[BREAK_IN].occurred_at
        )
        record.total_work_minutes = max(0, int(worked.total_seconds() // 60))
        record.early_leave_minutes = max(
            0, int((schedule.scheduled_end_at - events[CHECK_OUT].occurred_at).total_seconds() // 60)
        )
    else:
        record.total_work_minutes = 0
        record.early_leave_minutes = 0
    return record


def missing_events(record):
    events = recorded_events(record)
    return [LABELS[kind] for kind in SEQUENCE if kind not in events]


def flag_anomaly(record, reason):
    """Mark a session incomplete and pending review without touching stored events."""
    from attendance.models import AttendanceRecord

    record.status = AttendanceRecord.Status.INCOMPLETE
    record.review_status = AttendanceRecord.ReviewStatus.PENDING
    if reason not in (record.remarks or ""):
        record.remarks = f"{record.remarks}\n{reason}".strip()
    record.save(update_fields=["status", "review_status", "remarks", "updated_at"])
    return record


def present_incomplete_count(records):
    """Count of records that are Present AND have missing required events.

    Used for reporting: Incomplete is a subset of Present, not a separate
    category. This function counts the overlap without double-counting.
    """
    count = 0
    for record in records:
        if record.is_present and record.is_incomplete:
            count += 1
    return count


def present_count(records):
    """Count of records where a Check In was recorded (Present or Late)."""
    count = 0
    for record in records:
        if record.is_present:
            count += 1
    return count


def present_incomplete_count(records):
    """Count of records that are Present AND have missing required events.

    Used for reporting: Incomplete is a subset of Present, not a separate
    category. This function counts the overlap without double-counting.
    """
    count = 0
    for record in records:
        if record.is_present and record.is_incomplete:
            count += 1
    return count


def annotate_incomplete(queryset):
    """Annotate a queryset with an ``is_incomplete`` boolean for filtering.

    A session is incomplete when it has a Check In but is missing one or
    more of the four required events (Break Out, Break In, Check Out).
    This is computed at the database level using EXISTS subqueries so it
    can be used in filter lookups without loading records into Python.
    """
    # A record is incomplete when:
    # - first_check_in_at IS NOT NULL (present)
    # - AND at least one required event type is missing
    has_check_in = Q(first_check_in_at__isnull=False)
    missing_any = Q()
    for event_type in ("break_start", "break_end", "check_out"):
        missing_any &= ~Q(
            attendance_record__events__event_type=event_type
        )
    return queryset.annotate(
        is_incomplete=Case(
            When(has_check_in & missing_any, then=True),
            default=False,
            output_field=BooleanField(),
        )
    )


def present_incomplete_count(records):
    """Count of records that are Present AND have missing required events.

    Used for reporting: Incomplete is a subset of Present, not a separate
    category. This function counts the overlap without double-counting.
    """
    count = 0
    for record in records:
        if record.is_present and record.is_incomplete:
            count += 1
    return count


def present_count(records):
    """Count of records where a Check In was recorded (Present or Late)."""
    count = 0
    for record in records:
        if record.is_present:
            count += 1
    return count


def mark_incomplete_sessions(now=None, dry_run=False, start_date=None, end_date=None):
    """Close out attendance for schedules whose window is over.

    Two outcomes, both only ever produced by this closing process:
      * A session that checked in but missed required events is flagged as
        Incomplete and pending HR review (no events are invented).
      * A published schedule that ended without any check-in at all is marked
        Absent. Records that represent an approved leave, holiday or rest day
        are never touched, and a record that already carries a check-in is
        never turned into an Absent.

    ``dry_run`` performs every selection and counting step without writing to
    the database, so it is safe for preview / catch-up checks.
    ``start_date`` / ``end_date`` (inclusive work dates) limit which schedules
    are considered; useful for catch-up after server downtime.

    Holiday handling: a schedule with ``status=SCHEDULED`` on a holiday date is
    a *valid work schedule* (the employee is expected to work that day) and is
    processed normally. Schedules cancelled because of a holiday already carry
    ``status=CANCELLED`` and ``cancelled_by_holiday`` set, so they are excluded
    by the ``status=SCHEDULED`` filter — they are never blindly skipped merely
    because the calendar date happens to be a holiday.
    """
    from django.db import IntegrityError, transaction
    from attendance.models import AttendanceRecord, EmployeeSchedule

    now = now or timezone.now()
    cutoff = now - timedelta(minutes=CHECKOUT_GRACE_MINUTES)
    flagged = 0
    would_flag = 0

    with transaction.atomic():
        # 1. Sessions that checked in but are missing required events -> Incomplete.
        candidates = (
            AttendanceRecord.objects.filter(
                first_check_in_at__isnull=False,
                schedule__scheduled_end_at__lt=cutoff,
                schedule__status=EmployeeSchedule.Status.SCHEDULED,
            )
            .exclude(status=AttendanceRecord.Status.INCOMPLETE)
            .select_for_update()
            .select_related("schedule", "schedule__shift__office")
        )
        if start_date is not None:
            candidates = candidates.filter(schedule__work_date__gte=start_date)
        if end_date is not None:
            candidates = candidates.filter(schedule__work_date__lte=end_date)
        for record in candidates:
            if not record.events.exists():
                continue  # legacy record created before the four-event workflow
            missing = missing_events(record)
            if missing:
                if dry_run:
                    would_flag += 1
                else:
                    # Re-check inside the transaction: another process may have
                    # completed the session since the queryset was evaluated.
                    record.refresh_from_db(fields=["status", "review_status"])
                    if record.status == AttendanceRecord.Status.INCOMPLETE:
                        continue
                    if missing_events(record):
                        flag_anomaly(
                            record,
                            "Missing required events: " + ", ".join(missing) + ".",
                        )
                        flagged += 1

        # 2. Published schedules that ended without any check-in -> Absent.
        ended_schedules = (
            EmployeeSchedule.objects.filter(
                status=EmployeeSchedule.Status.SCHEDULED,
                scheduled_end_at__lt=cutoff,
            )
            .select_for_update()
            .select_related("employee_assignment__position__office")
        )
        if start_date is not None:
            ended_schedules = ended_schedules.filter(work_date__gte=start_date)
        if end_date is not None:
            ended_schedules = ended_schedules.filter(work_date__lte=end_date)
        for schedule in ended_schedules:
            record = AttendanceRecord.objects.filter(schedule=schedule).first()
            if record is None:
                if dry_run:
                    would_flag += 1
                else:
                    try:
                        new_record, created = AttendanceRecord.objects.get_or_create(
                            schedule=schedule,
                            defaults={
                                "status": AttendanceRecord.Status.ABSENT,
                                "review_status": AttendanceRecord.ReviewStatus.NOT_REQUIRED,
                                "remarks": "Marked absent at attendance closing: no check-in recorded.",
                            },
                        )
                        if created:
                            flagged += 1
                    except IntegrityError:
                        # Another process created the record for the same
                        # schedule (OneToOneField); it is already handled or
                        # will be handled by a subsequent pass.
                        continue
                continue
            if record.first_check_in_at is not None:
                continue  # already attended; handled above or complete
            if record.status in (
                AttendanceRecord.Status.ON_LEAVE,
                AttendanceRecord.Status.HOLIDAY,
                AttendanceRecord.Status.REST_DAY,
                AttendanceRecord.Status.ABSENT,
            ):
                continue
            if dry_run:
                would_flag += 1
            else:
                # Re-check inside the transaction: a scan may have added a
                # check-in since the queryset was evaluated.
                record.refresh_from_db(fields=["status", "review_status", "first_check_in_at", "remarks"])
                if record.first_check_in_at is not None:
                    continue
                if record.status in (
                    AttendanceRecord.Status.ON_LEAVE,
                    AttendanceRecord.Status.HOLIDAY,
                    AttendanceRecord.Status.REST_DAY,
                    AttendanceRecord.Status.ABSENT,
                ):
                    continue
                record.status = AttendanceRecord.Status.ABSENT
                record.review_status = AttendanceRecord.ReviewStatus.NOT_REQUIRED
                note = "Marked absent at attendance closing: no check-in recorded."
                if note not in (record.remarks or ""):
                    record.remarks = (f"{record.remarks}\n{note}").strip() if record.remarks else note
                record.save(update_fields=["status", "review_status", "remarks", "updated_at"])
                flagged += 1
    return would_flag if dry_run else flagged
