from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

try:
    from attendance.models import Holiday
except Exception:
    Holiday = None


class LeaveType(models.Model):
    class Basis(models.TextChoices):
        STATUTORY = "statutory", "Statutory"
        COMPANY_POLICY = "company_policy", "Company policy"

    class PaidStatus(models.TextChoices):
        PAID = "paid", "Paid"
        PARTIALLY_PAID = "partially_paid", "Partially paid"
        UNPAID = "unpaid", "Unpaid"

    class Unit(models.TextChoices):
        DAYS = "days", "Days"
        HOURS = "hours", "Hours"

    class Category(models.TextChoices):
        ANNUAL = "annual", "Annual leave"
        SICK = "sick", "Sick leave"
        MARRIAGE_BEREAVEMENT = "marriage_bereavement", "Marriage / bereavement / community or religious event"
        MATERNITY = "maternity", "Maternity leave"
        PATERNITY = "paternity", "Paternity leave"
        PREGNANCY_MEDICAL = "pregnancy_medical", "Pregnancy medical appointment / nursing time off"
        UNPAID = "unpaid", "Unpaid leave"
        PERSONAL = "personal", "Personal leave"
        STUDY_TRAINING = "study_training", "Study / training leave"
        COMPASSIONATE = "compassionate", "Additional compassionate leave"
        OTHER = "other", "Other"

    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    category = models.CharField(
        max_length=40, choices=Category.choices, default=Category.OTHER, blank=True
    )
    basis = models.CharField(
        max_length=20, choices=Basis.choices, default=Basis.COMPANY_POLICY
    )
    paid_status = models.CharField(
        max_length=20, choices=PaidStatus.choices, default=PaidStatus.PAID
    )
    unit = models.CharField(max_length=10, choices=Unit.choices, default=Unit.DAYS)
    is_paid = models.BooleanField(default=True)
    requires_approval = models.BooleanField(default=True)
    requires_attachment = models.BooleanField(default=False)
    attachment_instructions = models.TextField(blank=True)
    tracks_balance = models.BooleanField(default=False)
    max_days_per_year = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.code} — {self.name}"

    def save(self, *args, **kwargs):
        self.is_paid = self.paid_status in (
            self.PaidStatus.PAID,
            self.PaidStatus.PARTIALLY_PAID,
        )
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.max_days_per_year is not None and self.max_days_per_year < 0:
            raise ValidationError({"max_days_per_year": "Maximum days cannot be negative."})


class EmployeeRequest(models.Model):
    class RequestType(models.TextChoices):
        LEAVE = "leave", "Leave"
        ABSENCE_PERMISSION = "absence_permission", "Absence permission"
        LATE_ARRIVAL = "late_arrival", "Late arrival"
        EARLY_DEPARTURE = "early_departure", "Early departure"
        ATTENDANCE_CORRECTION = "attendance_correction", "Attendance correction"
        OVERTIME = "overtime", "Overtime"
        BUSINESS_TRIP = "business_trip", "Business trip"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        UNDER_REVIEW = "under_review", "Under review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    class DurationType(models.TextChoices):
        FULL_DAY = "full_day", "Full day"
        HALF_DAY = "half_day", "Half day"
        HOURLY = "hourly", "Hourly"

    request_number = models.CharField(max_length=40, unique=True)
    employee_assignment = models.ForeignKey(
        "employees.EmployeeAssignment", on_delete=models.PROTECT, related_name="requests"
    )
    branch = models.ForeignKey(
        "organization.Office",
        on_delete=models.PROTECT,
        related_name="employee_requests",
        null=True,
        blank=True,
    )
    workflow_stage = models.CharField(max_length=30, default="xefe_review")
    xefe_review_note = models.TextField(blank=True)
    forwarded_at = models.DateTimeField(null=True, blank=True)
    schedule_processed_at = models.DateTimeField(null=True, blank=True)
    schedule_processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employee_requests_schedule_processed",
    )
    attendance_record = models.ForeignKey(
        "attendance.AttendanceRecord", on_delete=models.PROTECT, null=True, blank=True, related_name="requests"
    )
    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.PROTECT, null=True, blank=True, related_name="requests"
    )
    request_type = models.CharField(max_length=30, choices=RequestType.choices)
    duration_type = models.CharField(
        max_length=20, choices=DurationType.choices, default=DurationType.FULL_DAY, blank=True
    )
    start_date = models.DateField()
    end_date = models.DateField()
    requested_check_in_at = models.DateTimeField(null=True, blank=True)
    requested_check_out_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField()
    attachment = models.FileField(upload_to="employee_requests/%Y/%m/", null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="employee_requests_submitted"
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    final_decision_at = models.DateTimeField(null=True, blank=True)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="employee_requests_processed"
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    processing_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-submitted_at"]
        indexes = [models.Index(fields=["status", "request_type", "submitted_at"])]
        permissions = [
            ("view_leave_calendar", "Can view department leave calendar"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            errors["end_date"] = "Request end date cannot be before its start date."

        assignment = None
        if self.employee_assignment_id:
            try:
                assignment = self.employee_assignment
            except Exception:
                assignment = None
        employee = getattr(assignment, "employee", None) if assignment else None
        if assignment is not None:
            assignment_invalid = False
            if self.start_date is not None and assignment.start_date > self.start_date:
                assignment_invalid = True
            if (
                self.end_date is not None
                and assignment.end_date is not None
                and assignment.end_date < self.end_date
            ):
                assignment_invalid = True
            if assignment_invalid:
                errors["employee_assignment"] = "The selected assignment is not valid for the requested period."
            if employee is not None and getattr(employee, "employment_status", None) not in (None, "active"):
                errors["employee_assignment"] = "The employee is not active."

        if employee is not None:
            contract_end = getattr(employee, "contract_end_date", None)
            if contract_end is not None and self.end_date is not None and self.end_date > contract_end:
                errors["end_date"] = "The requested period exceeds the employee contract end date."

        if self.request_type == self.RequestType.LEAVE:
            if not self.leave_type_id:
                errors["leave_type"] = "Leave requests must specify a leave type."
        elif self.request_type == self.RequestType.ATTENDANCE_CORRECTION:
            if not self.attendance_record_id:
                errors["attendance_record"] = "Correction requests must identify the attendance record."
            elif self.attendance_record is not None and self.employee_assignment_id:
                record_assignment_id = getattr(self.attendance_record, "schedule", None)
                record_assignment_id = getattr(record_assignment_id, "employee_assignment_id", None)
                if record_assignment_id is not None and record_assignment_id != self.employee_assignment_id:
                    errors["attendance_record"] = "Attendance record must belong to this employee assignment."
            duplicate = EmployeeRequest.objects.filter(
                attendance_record=self.attendance_record,
                request_type=self.RequestType.ATTENDANCE_CORRECTION,
                status__in=[self.Status.PENDING, self.Status.UNDER_REVIEW, self.Status.APPROVED],
            ).exclude(pk=self.pk)
            if self.attendance_record_id and duplicate.exists():
                errors["attendance_record"] = "A correction request already exists for this attendance record."
        else:
            if self.leave_type_id and self.request_type != self.RequestType.LEAVE:
                # Leave type is only meaningful for leave requests.
                pass

        if self.request_type in (self.RequestType.LEAVE, self.RequestType.ABSENCE_PERMISSION):
            if self.attendance_record_id:
                errors["attendance_record"] = "Attendance record is not relevant for leave requests."
            if self.requested_check_in_at or self.requested_check_out_at:
                errors["requested_check_in_at"] = "Requested check-in/out is not relevant for leave requests."
        else:
            if self.leave_type_id and self.request_type not in (
                self.RequestType.LEAVE,
                self.RequestType.ABSENCE_PERMISSION,
            ):
                errors["leave_type"] = "Leave type is not relevant for this request type."

        if self.request_type in (self.RequestType.LEAVE, self.RequestType.ABSENCE_PERMISSION) and employee is not None:
            overlapping = EmployeeRequest.objects.filter(
                employee_assignment__employee=employee,
                request_type__in=[self.RequestType.LEAVE, self.RequestType.ABSENCE_PERMISSION],
                status__in=[self.Status.PENDING, self.Status.UNDER_REVIEW, self.Status.APPROVED],
            ).filter(start_date__lte=self.end_date, end_date__gte=self.start_date).exclude(pk=self.pk)
            if overlapping.exists():
                errors["start_date"] = "The requested leave period overlaps an existing request."

        if self.requested_check_in_at and self.requested_check_out_at:
            if self.requested_check_out_at <= self.requested_check_in_at:
                errors["requested_check_out_at"] = "Requested check-out must be after requested check-in."

        if errors:
            raise ValidationError(errors)

    @property
    def employee(self):
        return self.employee_assignment.employee

    @property
    def office(self):
        return self.employee_assignment.position.office

    @property
    def is_leave_request(self):
        return self.request_type in (self.RequestType.LEAVE, self.RequestType.ABSENCE_PERMISSION)

    @property
    def is_official_duty(self):
        return self.request_type in (self.RequestType.BUSINESS_TRIP,)

    @property
    def is_attendance_exception(self):
        return self.request_type in (
            self.RequestType.LATE_ARRIVAL,
            self.RequestType.EARLY_DEPARTURE,
            self.RequestType.OVERTIME,
            self.RequestType.ATTENDANCE_CORRECTION,
        )

    @property
    def internal_note(self):
        return self.processing_note

    @property
    def requested_days(self):
        if self.duration_type == self.DurationType.HALF_DAY:
            return 0.5
        if self.duration_type == self.DurationType.HOURLY:
            if self.requested_check_in_at and self.requested_check_out_at:
                delta = self.requested_check_out_at - self.requested_check_in_at
                return round(delta.total_seconds() / 3600.0, 2)
            return None
        holidays = set()
        if Holiday is not None:
            office = getattr(
                getattr(self.employee_assignment, "position", None),
                "office",
                None,
            )
            covering_holidays = Holiday.objects.filter(
                Q(office__isnull=True) | Q(office=office),
                is_active=True,
                start_date__lte=self.end_date,
                end_date__gte=self.start_date,
            )
            for holiday in covering_holidays:
                current = max(self.start_date, holiday.start_date)
                holiday_end = min(self.end_date, holiday.end_date)
                while current <= holiday_end:
                    holidays.add(current)
                    current += timedelta(days=1)
        count = 0
        current = self.start_date
        while current <= self.end_date:
            if current.weekday() < 5 and current not in holidays:
                count += 1
            current += timedelta(days=1)
        return count if count > 0 else 1

    @property
    def approved_days(self):
        if self.status != self.Status.APPROVED:
            return None
        return self.requested_days

    def __str__(self):
        return f"{self.request_number} — {self.request_type} — {self.status}"


class RequestApproval(models.Model):
    class Decision(models.TextChoices):
        PENDING = "pending", "Pending"
        RECOMMENDED = "recommended", "Recommended"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SKIPPED = "skipped", "Skipped"

    request = models.ForeignKey(EmployeeRequest, on_delete=models.PROTECT, related_name="approval_steps")
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="request_approval_tasks"
    )
    step_number = models.PositiveSmallIntegerField(default=1)
    decision = models.CharField(max_length=20, choices=Decision.choices, default=Decision.PENDING)
    assigned_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    class Meta:
        ordering = ["request_id", "step_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["request", "step_number", "approver"],
                name="unique_request_approval_step",
            )
        ]

    def clean(self):
        super().clean()
        if self.step_number < 1:
            raise ValidationError({"step_number": "Approval step must be at least 1."})
        if self.decision == self.Decision.PENDING and self.decided_at:
            raise ValidationError({"decided_at": "A pending approval must not have a decision timestamp."})

    def __str__(self):
        return f"{self.request.request_number} — step {self.step_number} — {self.decision}"


class Notification(models.Model):
    class NotificationType(models.TextChoices):
        REQUEST_SUBMITTED = "request_submitted", "Request submitted"
        REQUEST_APPROVED = "request_approved", "Request approved"
        REQUEST_REJECTED = "request_rejected", "Request rejected"
        REQUEST_UPDATED = "request_updated", "Request updated"
        ATTENDANCE_ALERT = "attendance_alert", "Attendance alert"
        DEVICE_ALERT = "device_alert", "Device alert"
        GENERAL = "general", "General notification"

    class Channel(models.TextChoices):
        IN_APP = "in_app", "In-app"
        EMAIL = "email", "Email"
        PUSH = "push", "Push"

    class DeliveryStatus(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    recipient_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    request = models.ForeignKey(
        EmployeeRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications"
    )
    notification_type = models.CharField(max_length=30, choices=NotificationType.choices, default=NotificationType.GENERAL)
    title = models.CharField(max_length=180)
    message = models.TextField()
    channel = models.CharField(max_length=15, choices=Channel.choices, default=Channel.IN_APP)
    delivery_status = models.CharField(max_length=15, choices=DeliveryStatus.choices, default=DeliveryStatus.QUEUED)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient_user", "read_at", "created_at"])]

    def __str__(self):
        return f"{self.recipient_user} — {self.title}"


class ContractExpiryAlert(models.Model):
    """Persistent deduplication record for contract-expiry in-app notifications.

    One row per (employee, contract end date, recipient, alert type). The database
    unique constraint is the authoritative guard: concurrent daily runs cannot both
    deliver the same alert. The row is created in the same transaction as its
    Notification, so a rollback never records a successful delivery.
    """

    class AlertType(models.TextChoices):
        UPCOMING = "upcoming", "Contract expiring soon"

    employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.PROTECT,
        related_name="contract_expiry_alerts",
    )
    contract_end_date = models.DateField()
    recipient_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="contract_expiry_alerts",
    )
    alert_type = models.CharField(
        max_length=20, choices=AlertType.choices, default=AlertType.UPCOMING
    )
    notification = models.OneToOneField(
        Notification,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contract_expiry_alert",
    )
    sent_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sent_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "contract_end_date", "recipient_user", "alert_type"],
                name="unique_contract_expiry_alert_recipient",
            ),
        ]

    def __str__(self):
        return (
            f"{self.get_alert_type_display()} — {self.employee.employee_id} — "
            f"{self.contract_end_date} — {self.recipient_user}"
        )
