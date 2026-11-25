from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
import uuid

from .services.scheduling import (
    DEFAULT_WORKING_DAYS,
    REQUIRED_EVENT_COUNT,
    ShiftTimingError,
    build_schedule_snapshot,
    compute_shift_metrics,
    assignment_position_label,
    format_local,
    format_minutes,
    format_working_days,
    overlapping_schedules,
)


def default_working_days():
    return list(DEFAULT_WORKING_DAYS)


class Shift(models.Model):
    """Reusable work-pattern template (times of day only, no dates).

    Break duration and "crosses midnight" are derived from the times; they are never
    entered by hand, so they cannot contradict start/end/break times.
    """

    class TargetAssignmentMode(models.TextChoices):
        ALL_ELIGIBLE = "all", "All active eligible employees in office"
        SPECIFIC = "specific", "Specific employee assignments"

    REQUIRED_EVENT_COUNT = REQUIRED_EVENT_COUNT

    office = models.ForeignKey("organization.Office", on_delete=models.PROTECT, related_name="shifts")
    shift_code = models.CharField(max_length=30, help_text="Unique within the office, e.g. MORNING.")
    name = models.CharField(max_length=100)
    start_time = models.TimeField(help_text="Time employees are expected to start work (Check In).")
    end_time = models.TimeField(help_text="Time the shift ends (Check Out). Earlier than the start means the shift ends the next day.")
    break_start_time = models.TimeField(null=True, blank=True, help_text="Planned start of the break (Break Out).")
    break_end_time = models.TimeField(null=True, blank=True, help_text="Planned end of the break (Break In).")
    crosses_midnight = models.BooleanField(default=False, editable=False)
    break_duration_minutes = models.PositiveSmallIntegerField(default=0, editable=False)
    grace_period_minutes = models.PositiveSmallIntegerField(
        default=0, help_text="Minutes after the start time that are still on time. Arriving later is recorded as Late."
    )
    early_checkin_window_minutes = models.PositiveSmallIntegerField(
        default=60, help_text="How many minutes before the start time an employee may already Check In."
    )
    is_active = models.BooleanField(default=True, help_text="Inactive shifts cannot be used for new schedules.")

    # General Schedule (Recurring Work Schedule) fields:
    recurring_schedule_enabled = models.BooleanField(
        default=False,
        help_text="Generate routine employee schedules automatically from this shift.",
    )
    recurrence_start_date = models.DateField(
        null=True,
        blank=True,
        help_text="Start date for automatic schedule generation.",
    )
    recurrence_end_date = models.DateField(
        null=True,
        blank=True,
        help_text="End date for automatic schedule generation (up to 1 year from start date).",
    )
    working_days = models.JSONField(
        default=default_working_days,
        blank=True,
        help_text="List of working day integers: 0=Monday .. 4=Friday, 5=Saturday, 6=Sunday.",
    )
    target_assignment_mode = models.CharField(
        max_length=20,
        choices=TargetAssignmentMode.choices,
        default=TargetAssignmentMode.ALL_ELIGIBLE,
        help_text="Target all active eligible employees in the shift's office, or choose specific assignments.",
    )
    target_assignments = models.ManyToManyField(
        "employees.EmployeeAssignment",
        blank=True,
        related_name="target_shifts",
        help_text="Specific assignments targeted when mode is specific.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["office__office_name", "start_time"]
        constraints = [
            models.UniqueConstraint(fields=["office", "shift_code"], name="unique_shift_code_per_office")
        ]

    def metrics(self):
        return compute_shift_metrics(self.start_time, self.end_time, self.break_start_time, self.break_end_time)

    def clean(self):
        super().clean()
        if self.start_time is not None and self.end_time is not None:
            try:
                self.metrics()
            except ShiftTimingError as exc:
                raise ValidationError({exc.field: exc.message})

        if self.recurring_schedule_enabled:
            rec_errors = {}
            if not self.recurrence_start_date:
                rec_errors["recurrence_start_date"] = "Recurrence start date is required when General Schedule is enabled."
            if not self.recurrence_end_date:
                rec_errors["recurrence_end_date"] = "Recurrence end date is required when General Schedule is enabled."
            if self.recurrence_start_date and self.recurrence_end_date:
                if self.recurrence_end_date < self.recurrence_start_date:
                    rec_errors["recurrence_end_date"] = "Recurrence end date cannot be earlier than start date."
                elif (self.recurrence_end_date - self.recurrence_start_date).days > 366:
                    rec_errors["recurrence_end_date"] = "Recurrence end date cannot be more than one year from start date."

            w_days = self.working_days
            if not w_days:
                rec_errors["working_days"] = "At least one working day must be selected."
            else:
                for d in w_days:
                    try:
                        val = int(d)
                        if not (0 <= val <= 6):
                            raise ValueError()
                    except (ValueError, TypeError):
                        rec_errors["working_days"] = "Working days must be integers between 0 (Monday) and 6 (Sunday)."
                        break
            if rec_errors:
                raise ValidationError(rec_errors)

    def save(self, *args, **kwargs):
        if self.start_time is not None and self.end_time is not None:
            self.crosses_midnight = self.end_time < self.start_time
            try:
                self.break_duration_minutes = self.metrics().break_minutes
            except ShiftTimingError:
                pass  # clean() reports it; legacy rows without break times keep their old value.
        super().save(*args, **kwargs)

    @property
    def working_days_list(self) -> list[int]:
        if not self.working_days:
            return default_working_days()
        try:
            return sorted(set(int(d) for d in self.working_days if 0 <= int(d) <= 6))
        except (ValueError, TypeError):
            return default_working_days()

    @property
    def working_days_display(self) -> str:
        return format_working_days(self.working_days_list)

    @property
    def recurring_schedule_display(self) -> str:
        return "Enabled" if self.recurring_schedule_enabled else "Disabled"

    @property
    def recurrence_period_display(self) -> str:
        if self.recurrence_start_date and self.recurrence_end_date:
            return f"{self.recurrence_start_date} to {self.recurrence_end_date}"
        return "Not configured"

    @property
    def target_assignments_display(self) -> str:
        if self.target_assignment_mode == self.TargetAssignmentMode.ALL_ELIGIBLE:
            return f"All active eligible employees in {self.office.office_name}"
        count = self.target_assignments.count() if self.pk else 0
        return f"{count} specific assignment{'s' if count != 1 else ''}"


    @property
    def has_break_times(self):
        return self.break_start_time is not None and self.break_end_time is not None

    @property
    def scheduled_work_minutes(self):
        try:
            return self.metrics().work_minutes
        except ShiftTimingError:
            return None

    @property
    def time_range(self):
        suffix = " (+1 day)" if self.crosses_midnight else ""
        return f"{self.start_time:%H:%M}\u2013{self.end_time:%H:%M}{suffix}"

    @property
    def break_range(self):
        if not self.has_break_times:
            return "Not set"
        return f"{self.break_start_time:%H:%M}\u2013{self.break_end_time:%H:%M} ({self.break_duration_minutes} min)"

    @property
    def work_duration_display(self):
        work = self.scheduled_work_minutes
        return format_minutes(work) if work is not None else "Not set"

    @property
    def summary(self):
        """Human summary: Morning Shift / 08:30-17:30 / Break ... / work duration / events."""
        work = self.scheduled_work_minutes
        break_part = (
            f"{self.break_start_time:%H:%M}\u2013{self.break_end_time:%H:%M} ({self.break_duration_minutes} minutes)"
            if self.has_break_times
            else "not set"
        )
        end_note = " (next day)" if self.crosses_midnight else ""
        return {
            "name": self.name,
            "time_range": f"{self.start_time:%H:%M}\u2013{self.end_time:%H:%M}{end_note}",
            "break": break_part,
            "work_duration": format_minutes(work) if work is not None else "unknown until break times are set",
            "required_events": REQUIRED_EVENT_COUNT,
        }

    @property
    def is_in_use(self):
        return self.schedules.exists()

    def __str__(self):
        return f"{self.office.office_code} \u2014 {self.name}"


class EmployeeSchedule(models.Model):
    """One employee, one shift, one work date, with the times frozen at creation.

    The ``scheduled_*`` datetimes (and grace/early-check-in minutes) are a snapshot of
    the Shift template. Editing the template later never changes an existing schedule.
    This status describes the schedule's lifecycle only; attendance outcomes such as
    present/late/absent live on AttendanceRecord.
    """

    class Status(models.TextChoices):
        PLANNED = "planned", "Planned (draft)"
        SCHEDULED = "scheduled", "Published"
        CANCELLED = "cancelled", "Cancelled"
        COMPLETED = "completed", "Completed"

    class ScheduleSource(models.TextChoices):
        MANUAL = "manual", "Manual"
        GENERATED = "generated", "Generated from Shift"
        CONTRACT = "contract", "Generated from contract"

    @property
    def is_contract_generated(self) -> bool:
        return self.schedule_source == self.ScheduleSource.CONTRACT

    employee_assignment = models.ForeignKey(
        "employees.EmployeeAssignment", on_delete=models.PROTECT, related_name="schedules"
    )
    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="schedules")
    work_date = models.DateField()
    scheduled_start_at = models.DateTimeField()
    scheduled_break_start_at = models.DateTimeField(null=True, blank=True)
    scheduled_break_end_at = models.DateTimeField(null=True, blank=True)
    scheduled_end_at = models.DateTimeField()
    grace_period_minutes = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Snapshot of the shift grace period when the schedule was created."
    )
    early_checkin_window_minutes = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Snapshot of the shift early check-in window when the schedule was created."
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    schedule_source = models.CharField(
        max_length=20,
        choices=ScheduleSource.choices,
        default=ScheduleSource.MANUAL,
        help_text="Source of the schedule: manual or generated from Shift General Schedule.",
    )
    cancelled_by_holiday = models.ForeignKey(
        "attendance.Holiday",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_schedules",
        help_text="Holiday that caused this generated schedule to be automatically cancelled.",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="schedules_assigned"
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["work_date", "scheduled_start_at"]
        indexes = [
            models.Index(fields=["work_date", "status"]),
            models.Index(fields=["shift", "work_date", "schedule_source"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(scheduled_end_at__gt=models.F("scheduled_start_at")),
                name="schedule_end_after_start",
            ),
            models.UniqueConstraint(
                fields=["employee_assignment", "scheduled_start_at"],
                condition=~Q(status="cancelled"),
                name="unique_assignment_schedule_start",
            ),
        ]

    LIVE_STATUSES = ("planned", "scheduled")

    def apply_snapshot(self, snapshot):
        self.scheduled_start_at = snapshot.start
        self.scheduled_break_start_at = snapshot.break_start
        self.scheduled_break_end_at = snapshot.break_end
        self.scheduled_end_at = snapshot.end
        self.grace_period_minutes = snapshot.grace_minutes
        self.early_checkin_window_minutes = snapshot.early_checkin_minutes

    def take_snapshot(self):
        """Re-freeze the times from the current shift template (explicit reschedule only)."""
        self.apply_snapshot(build_schedule_snapshot(self.shift, self.work_date))

    @property
    def is_generated(self) -> bool:
        return self.schedule_source == self.ScheduleSource.GENERATED


    @property
    def effective_grace_minutes(self):
        return self.shift.grace_period_minutes if self.grace_period_minutes is None else self.grace_period_minutes

    @property
    def effective_early_checkin_minutes(self):
        if self.early_checkin_window_minutes is None:
            return self.shift.early_checkin_window_minutes
        return self.early_checkin_window_minutes

    @property
    def scheduled_work_minutes(self):
        """Scheduled start-to-end minus the planned break (legacy rows: shift break length)."""
        total = int((self.scheduled_end_at - self.scheduled_start_at).total_seconds() // 60)
        if self.scheduled_break_start_at and self.scheduled_break_end_at:
            total -= int((self.scheduled_break_end_at - self.scheduled_break_start_at).total_seconds() // 60)
        else:
            total -= self.shift.break_duration_minutes
        return max(total, 0)

    def clean(self):
        super().clean()
        errors = {}
        start, end = self.scheduled_start_at, self.scheduled_end_at
        if start and end and end <= start:
            errors["scheduled_end_at"] = "Scheduled end must be later than scheduled start."
        break_start, break_end = self.scheduled_break_start_at, self.scheduled_break_end_at
        if (break_start is None) != (break_end is None):
            errors["scheduled_break_end_at"] = "Break start and break end must be set together."
        elif break_start and break_end and start and end:
            if not (start < break_start < break_end < end):
                errors["scheduled_break_start_at"] = "The break must lie inside the scheduled working time."
        if self.employee_assignment_id and self.shift_id:
            assignment = self.employee_assignment
            if assignment.position.office_id != self.shift.office_id:
                errors["shift"] = "Shift office must match the employee assignment's office."
            if self.status in self.LIVE_STATUSES and self.work_date:
                contract_end = assignment.employee.contract_end_date
                if assignment.start_date > self.work_date or (
                    assignment.end_date and assignment.end_date < self.work_date
                ):
                    errors["employee_assignment"] = "The assignment is not active on the selected work date."
                elif contract_end is not None and self.work_date > contract_end:
                    errors["employee_assignment"] = (
                        f"The work date is after the employee's contract end date ({contract_end})."
                    )
            if self.status in self.LIVE_STATUSES and start and end:
                clash = overlapping_schedules(assignment.employee_id, start, end, exclude_pk=self.pk).first()
                if clash is not None:
                    errors["work_date"] = (
                        f"This employee already has an overlapping schedule on {clash.work_date} "
                        f"({timezone.localtime(clash.scheduled_start_at):%H:%M}\u2013"
                        f"{timezone.localtime(clash.scheduled_end_at):%H:%M})."
                    )
        if errors:
            raise ValidationError(errors)

    @property
    def office(self):
        return self.shift.office

    @property
    def employee(self):
        return self.employee_assignment.employee

    # Read-only display helpers shared by the list, detail and preview (planned times, not attendance).
    @property
    def employee_label(self):
        return f"{self.employee.employee_id} \u2014 {self.employee.full_name}"

    @property
    def assignment_label(self):
        return assignment_position_label(self.employee_assignment)

    @property
    def scheduled_start_display(self):
        return format_local(self.scheduled_start_at, self.work_date)

    @property
    def scheduled_end_display(self):
        return format_local(self.scheduled_end_at, self.work_date)

    @property
    def scheduled_break_start_display(self):
        return format_local(self.scheduled_break_start_at, self.work_date) if self.scheduled_break_start_at else "Not set"

    @property
    def scheduled_break_end_display(self):
        return format_local(self.scheduled_break_end_at, self.work_date) if self.scheduled_break_end_at else "Not set"

    @property
    def break_minutes(self):
        if self.scheduled_break_start_at and self.scheduled_break_end_at:
            return int((self.scheduled_break_end_at - self.scheduled_break_start_at).total_seconds() // 60)
        return self.shift.break_duration_minutes

    @property
    def break_display(self):
        if not (self.scheduled_break_start_at and self.scheduled_break_end_at):
            return "Not set"
        start, end = timezone.localtime(self.scheduled_break_start_at), timezone.localtime(self.scheduled_break_end_at)
        return f"{start:%H:%M}\u2013{end:%H:%M} ({self.break_minutes} min)"

    @property
    def work_duration_display(self):
        return format_minutes(self.scheduled_work_minutes)

    def __str__(self):
        return f"{self.employee.employee_id} \u2014 {self.work_date}"


class AttendanceDevice(models.Model):
    class DeviceType(models.TextChoices):
        FACE_TERMINAL = "face_terminal", "Face terminal"
        KIOSK = "kiosk", "Kiosk"
        WEB_CAMERA = "web_camera", "Web camera station"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"
        MAINTENANCE = "maintenance", "Maintenance"
        OFFLINE = "offline", "Offline"

    office = models.ForeignKey("organization.Office", on_delete=models.PROTECT, related_name="attendance_devices")
    device_code = models.CharField(max_length=50, unique=True)
    device_name = models.CharField(max_length=120)
    device_type = models.CharField(max_length=25, choices=DeviceType.choices, default=DeviceType.FACE_TERMINAL)
    location_description = models.CharField(max_length=200, blank=True)
    credential_hash = models.CharField(max_length=255, blank=True, help_text="Store only a secure hash/reference, never a raw device secret.")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_authenticated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["office__office_name", "device_name"]

    def __str__(self):
        return f"{self.device_code} — {self.device_name}"


class FaceTemplate(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        REVOKED = "revoked", "Revoked"
        EXPIRED = "expired", "Expired"

    employee = models.ForeignKey("employees.Employee", on_delete=models.PROTECT, related_name="face_templates")
    provider = models.CharField(max_length=80, help_text="Face recognition engine/provider name.")
    provider_reference = models.CharField(max_length=255, blank=True)
    encrypted_template = models.BinaryField(null=True, blank=True, help_text="Encrypted biometric template only; do not store raw face images here by default.")
    model_version = models.CharField(max_length=80)
    consent_recorded_at = models.DateTimeField(null=True, blank=True)
    enrolled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="face_templates_enrolled"
    )
    enrolled_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    revoked_at = models.DateTimeField(null=True, blank=True)
    retention_until = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["employee__employee_id", "-enrolled_at"]
        indexes = [models.Index(fields=["employee", "status"])]

    def clean(self):
        super().clean()
        if not self.provider_reference and not self.encrypted_template:
            raise ValidationError("Provide an encrypted template or a provider reference.")

    def __str__(self):
        return f"Face template — {self.employee.employee_id} ({self.status})"


class FaceVerificationLog(models.Model):
    class EventType(models.TextChoices):
        ENROLLMENT = "enrollment", "Enrollment"
        CHECK_IN = "check_in", "Check In"
        BREAK_START = "break_start", "Break Out"
        BREAK_END = "break_end", "Break In"
        CHECK_OUT = "check_out", "Check Out"
        OTHER = "other", "Other verification"

    class MatchResult(models.TextChoices):
        MATCHED = "matched", "Matched"
        NOT_MATCHED = "not_matched", "Not matched"
        AMBIGUOUS = "ambiguous", "Ambiguous"
        ERROR = "error", "Processing error"

    class LivenessResult(models.TextChoices):
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"
        INCONCLUSIVE = "inconclusive", "Inconclusive"
        NOT_CHECKED = "not_checked", "Not checked"

    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.SET_NULL, null=True, blank=True, related_name="face_verification_logs"
    )
    device = models.ForeignKey(
        AttendanceDevice,
        on_delete=models.PROTECT,
        related_name="verification_logs",
        null=True,
        blank=True,
        help_text="Null for authenticated BYOD mobile attendance; physical kiosk device remains required for kiosk attendance.",
    )
    schedule = models.ForeignKey(
        EmployeeSchedule, on_delete=models.SET_NULL, null=True, blank=True, related_name="face_verification_logs"
    )
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    match_result = models.CharField(max_length=20, choices=MatchResult.choices)
    liveness_result = models.CharField(max_length=20, choices=LivenessResult.choices, default=LivenessResult.NOT_CHECKED)
    liveness_score = models.DecimalField(max_digits=8, decimal_places=6, null=True, blank=True, help_text="Bona-fide probability returned by the PAD model; not a certified security score.")
    match_score = models.DecimalField(max_digits=8, decimal_places=6, null=True, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    model_version = models.CharField(max_length=80, blank=True)
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["occurred_at", "match_result"])]

    def clean(self):
        super().clean()
        if self.schedule_id and self.employee_id:
            if self.schedule.employee_assignment.employee_id != self.employee_id:
                raise ValidationError({"employee": "Employee must match the selected schedule."})
        if self.schedule_id and self.device_id:
            if self.schedule.shift.office_id != self.device.office_id:
                raise ValidationError({"device": "Device office must match the scheduled office."})
        if self.match_score is not None and not (0 <= self.match_score <= 1):
            raise ValidationError({"match_score": "Match score must be between 0 and 1."})
        if self.liveness_score is not None and not (0 <= self.liveness_score <= 1):
            raise ValidationError({"liveness_score": "Liveness score must be between 0 and 1."})

    def __str__(self):
        return f"{self.event_type} — {self.match_result} — {self.occurred_at}"


class AttendancePolicy(models.Model):
    office = models.ForeignKey(
        "organization.Office", on_delete=models.PROTECT, null=True, blank=True,
        related_name="attendance_policies", help_text="Empty office means institution-wide default policy."
    )
    policy_name = models.CharField(max_length=100)
    early_checkin_window_minutes = models.PositiveSmallIntegerField(
        default=60,
        help_text="Deprecated and ignored: the early check-in window is defined on the Shift and frozen into each schedule.",
    )
    absent_after_minutes = models.PositiveSmallIntegerField(
        default=120,
        verbose_name="Absent threshold after scheduled start (minutes)",
        help_text=(
            "Check-in is no longer accepted this many minutes after the scheduled start; the employee "
            "is then treated as absent unless corrected. Arriving after the Shift grace period but "
            "before this threshold is only Late, not Absent."
        ),
    )
    require_liveness = models.BooleanField(
        default=True,
        help_text="Liveness/PAD is mandatory for face attendance and cannot be disabled: "
        "every face-based check-in must pass PAD before an event is recorded.",
    )
    max_failed_attempts = models.PositiveSmallIntegerField(default=5)
    failed_attempt_lockout_minutes = models.PositiveSmallIntegerField(default=15)
    effective_from = models.DateField()
    effective_until = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="attendance_policies_updated"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-effective_from"]
        constraints = [
            models.UniqueConstraint(
                fields=["office", "effective_from"],
                condition=Q(office__isnull=False),
                name="unique_policy_start_per_office",
            ),
            models.UniqueConstraint(
                fields=["effective_from"],
                condition=Q(office__isnull=True),
                name="unique_global_policy_start",
            ),
        ]

    def clean(self):
        super().clean()
        if not self.effective_from:
            return
        if self.effective_until and self.effective_until < self.effective_from:
            raise ValidationError({"effective_until": "End date cannot be before start date."})
        if self.is_active:
            # An office-specific policy overrides the institution-wide one, so only policies
            # with the same scope (same office, or both institution-wide) must not overlap.
            clashes = AttendancePolicy.objects.filter(is_active=True, office_id=self.office_id).exclude(pk=self.pk)
            clashes = clashes.filter(
                Q(effective_until__isnull=True) | Q(effective_until__gte=self.effective_from)
            )
            if self.effective_until:
                clashes = clashes.filter(effective_from__lte=self.effective_until)
            clash = clashes.first()
            if clash is not None:
                raise ValidationError(
                    f"An active policy for the same scope already covers part of this period: "
                    f"{clash.policy_name} ({clash.effective_from} to {clash.effective_until or 'open-ended'}). "
                    "Set an end date on it first, or change this period."
                )
        if not self.require_liveness:
            raise ValidationError(
                {
                    "require_liveness": (
                        "Liveness/PAD is mandatory for every face attendance event and "
                        "cannot be disabled on an attendance policy."
                    )
                }
            )

    def __str__(self):
        return f"{self.policy_name} — {self.office or 'Institution-wide'}"


class AttendanceRecord(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        PRESENT = "present", "Present"
        LATE = "late", "Late"
        INCOMPLETE = "incomplete", "Incomplete"
        ABSENT = "absent", "Absent"
        ON_LEAVE = "on_leave", "On approved leave"
        HOLIDAY = "holiday", "Holiday"
        REST_DAY = "rest_day", "Rest day"
        PENDING_REVIEW = "pending_review", "Pending review"

    class ReviewStatus(models.TextChoices):
        NOT_REQUIRED = "not_required", "Not required"
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    schedule = models.OneToOneField(EmployeeSchedule, on_delete=models.PROTECT, related_name="attendance_record")
    attendance_policy = models.ForeignKey(
        AttendancePolicy, on_delete=models.PROTECT, null=True, blank=True, related_name="attendance_records",
        help_text="Policy in force at the first event; later policy changes never recalculate this record.",
    )
    first_check_in_at = models.DateTimeField(null=True, blank=True)
    last_check_out_at = models.DateTimeField(null=True, blank=True)
    total_work_minutes = models.PositiveIntegerField(default=0)
    late_minutes = models.PositiveIntegerField(default=0)
    early_leave_minutes = models.PositiveIntegerField(default=0)
    approved_overtime_minutes = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    review_status = models.CharField(max_length=20, choices=ReviewStatus.choices, default=ReviewStatus.NOT_REQUIRED)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="attendance_records_reviewed"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-schedule__work_date"]
        indexes = [models.Index(fields=["status", "review_status"])]
        permissions = [
            ("use_mobile_face_attendance", "Can use mobile face attendance"),
            ("view_attendance_exceptions", "Can view attendance exceptions"),
        ]

    def clean(self):
        super().clean()
        if self.first_check_in_at and self.last_check_out_at and self.last_check_out_at < self.first_check_in_at:
            raise ValidationError({"last_check_out_at": "Check-out cannot be earlier than check-in."})

    @property
    def employee(self):
        return self.schedule.employee_assignment.employee

    @property
    def office(self):
        return self.schedule.shift.office

    @property
    def is_present(self):
        """Presence assessment: True when a Check In was recorded.

        Presence and completeness are separate assessments. An employee
        who checked in remains Present even when required events are missing.
        """
        return self.first_check_in_at is not None

    @property
    def is_incomplete(self):
        """Completeness assessment: True when required events are missing.

        A session is incomplete when it has a Check In but is missing
        one or more of the four required events (Break Out, Break In,
        Check Out). This is derived from stored events, not from a
        separate status flag, so it stays accurate even before the
        closing process runs.
        """
        from .services.workflow import CHECK_IN, SEQUENCE, recorded_events

        if self.first_check_in_at is None:
            return False
        events = recorded_events(self)
        if CHECK_IN not in events:
            return False
        return any(kind not in events for kind in SEQUENCE)

    @property
    def completeness_label(self):
        """Short label combining presence and completeness for display."""
        if not self.is_present:
            return self.get_status_display()
        if self.is_incomplete:
            return "Present (Incomplete)"
        return self.get_status_display()

    def __str__(self):
        return f"{self.employee.employee_id} — {self.schedule.work_date} — {self.status}"


class AttendancePersistenceFailure(models.Model):
    class Channel(models.TextChoices):
        KIOSK = "kiosk", "Kiosk"
        MOBILE = "mobile", "Mobile"

    incident_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attendance_persistence_failures",
    )
    schedule = models.ForeignKey(
        EmployeeSchedule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attendance_persistence_failures",
    )
    attendance_record = models.ForeignKey(
        AttendanceRecord,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attendance_persistence_failures",
    )
    expected_event_type = models.CharField(max_length=20, blank=True)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    persistence_stage = models.CharField(max_length=40)
    exception_type = models.CharField(max_length=100)
    diagnostic_summary = models.CharField(max_length=500, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        indexes = [
            models.Index(fields=["occurred_at", "channel"]),
            models.Index(fields=["employee", "occurred_at"]),
        ]

    def clean(self):
        super().clean()
        if not self.incident_id:
            raise ValidationError({"incident_id": "An incident identifier is required."})

    def __str__(self):
        return f"{self.incident_id} — {self.get_channel_display()} — {self.persistence_stage}"


class AttendanceEvent(models.Model):
    class EventType(models.TextChoices):
        CHECK_IN = "check_in", "Check In"
        BREAK_START = "break_start", "Break Out"
        BREAK_END = "break_end", "Break In"
        CHECK_OUT = "check_out", "Check Out"
        CORRECTION = "correction", "Approved correction"

    class Source(models.TextChoices):
        FACE_RECOGNITION = "face_recognition", "Face recognition"
        APPROVED_CORRECTION = "approved_correction", "Approved correction"
        AUTHORIZED_IMPORT = "authorized_import", "Authorized import"
        SYSTEM = "system", "System generated"

    attendance_record = models.ForeignKey(AttendanceRecord, on_delete=models.PROTECT, related_name="events")
    verification_log = models.ForeignKey(
        FaceVerificationLog, on_delete=models.PROTECT, null=True, blank=True, related_name="attendance_events"
    )
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    occurred_at = models.DateTimeField()
    source = models.CharField(max_length=25, choices=Source.choices)
    created_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="attendance_events_created"
    )
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class LocationStatus(models.TextChoices):
        INSIDE = "inside", "Inside geofence"
        OUTSIDE = "outside", "Outside geofence"
        INCONCLUSIVE = "inconclusive", "Inconclusive"
        NOT_PROVIDED = "not_provided", "Not provided"
        DYNAMIC = "dynamic", "Dynamic field location"

    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    location_accuracy_meters = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    location_source = models.CharField(max_length=20, blank=True)
    location_status = models.CharField(max_length=20, choices=LocationStatus.choices, blank=True)
    location_distance_meters = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    location_received_at = models.DateTimeField(null=True, blank=True)
    location_validation_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["occurred_at"]
        indexes = [models.Index(fields=["event_type", "occurred_at"])]
        constraints = [
            # Each of the four required events can exist only once per attendance record, so a
            # double click or concurrent request cannot store the same event twice.
            models.UniqueConstraint(
                fields=["attendance_record", "event_type"],
                condition=Q(event_type__in=["check_in", "break_start", "break_end", "check_out"]),
                name="unique_required_event_per_record",
            )
        ]

    def clean(self):
        super().clean()
        required = ("check_in", "break_start", "break_end", "check_out")
        if self.event_type in required and self.attendance_record_id and self._state.adding:
            # Enforce CHECK_IN -> BREAK_OUT -> BREAK_IN -> CHECK_OUT for every writer, not just the kiosk.
            from .services.workflow import LABELS, next_event_type, session_state

            expected = next_event_type(self.attendance_record)
            if self.event_type == "check_out":
                # Check Out is allowed even when break events are missing; missing
                # break events are flagged at finalization, not by blocking
                # Check Out. Only Check In is required.
                from .services.workflow import CHECK_IN as _CHECK_IN, recorded_events as _recorded_events

                if _CHECK_IN not in _recorded_events(self.attendance_record):
                    wanted = LABELS.get(expected, "nothing (the session is complete)")
                    raise ValidationError(
                        f"{LABELS[self.event_type]} is not allowed here. The next required event is {wanted}."
                    )
            elif expected != self.event_type:
                wanted = LABELS.get(expected, "nothing (the session is complete)")
                raise ValidationError(
                    f"{LABELS[self.event_type]} is not allowed here. The next required event is {wanted}."
                )
            last = session_state(self.attendance_record).last_event_at
            if last is not None and self.occurred_at and self.occurred_at < last:
                raise ValidationError("An event cannot occur before the previously recorded event.")
        if self.source == self.Source.FACE_RECOGNITION:
            if not self.verification_log_id:
                raise ValidationError({"verification_log": "Face attendance events require a verification log."})
            log = self.verification_log
            if log.match_result != FaceVerificationLog.MatchResult.MATCHED:
                raise ValidationError("Only matched face verification can create a face attendance event.")
            if log.liveness_result != FaceVerificationLog.LivenessResult.PASSED:
                raise ValidationError("Liveness must pass before a face attendance event is accepted.")
            if log.schedule_id and log.schedule_id != self.attendance_record.schedule_id:
                raise ValidationError({"verification_log": "Verification log must refer to this attendance record's schedule."})
            scheduled_employee_id = self.attendance_record.schedule.employee_assignment.employee_id
            if log.employee_id and log.employee_id != scheduled_employee_id:
                raise ValidationError({"verification_log": "Verified employee must match the attendance schedule."})
            if self.event_type != self.EventType.CORRECTION and log.event_type != self.event_type:
                raise ValidationError({"verification_log": "The verification log must be for the same event type."})

    def __str__(self):
        return f"{self.event_type} — record #{self.attendance_record_id} — {self.occurred_at}"


class Holiday(models.Model):
    class HolidayType(models.TextChoices):
        NATIONAL = "national", "National holiday"
        INSTITUTION = "institution", "Institution-wide holiday"
        OFFICE = "office", "Office-specific holiday"

    office = models.ForeignKey(
        "organization.Office", on_delete=models.PROTECT, null=True, blank=True,
        related_name="holidays", help_text="Leave empty for a holiday applying to the whole institution."
    )
    name = models.CharField(max_length=150)
    start_date = models.DateField()
    end_date = models.DateField()
    holiday_type = models.CharField(max_length=20, choices=HolidayType.choices, default=HolidayType.INSTITUTION)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="holidays_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_date", "name"]
        indexes = [models.Index(fields=["start_date", "end_date", "is_active"])]

    def clean(self):
        super().clean()
        if not self.start_date or not self.end_date or not self.name:
            return
        if self.end_date < self.start_date:
            raise ValidationError({"end_date": "Holiday end date cannot be before its start date."})
        if self.holiday_type == self.HolidayType.OFFICE and not self.office_id:
            raise ValidationError({"office": "An office-specific holiday must identify an office."})
        if self.holiday_type != self.HolidayType.OFFICE and self.office_id:
            raise ValidationError({"office": "Only office-specific holidays should specify an office."})
        if self.is_active:
            clash = Holiday.objects.filter(
                is_active=True, office_id=self.office_id, name__iexact=self.name.strip(),
                start_date__lte=self.end_date, end_date__gte=self.start_date,
            ).exclude(pk=self.pk).first()
            if clash is not None:
                raise ValidationError(
                    f"A holiday named '{clash.name}' already covers these dates for the same scope "
                    f"({clash.start_date} to {clash.end_date})."
                )

    @property
    def scope_label(self):
        return "All offices" if self.office_id is None else self.office.office_name

    @property
    def covers_all_offices(self):
        return self.office_id is None

    def __str__(self):
        return self.name
