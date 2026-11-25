from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

import base64
import os
import re
import uuid
from io import BytesIO

try:
    import qrcode  # type: ignore
except ImportError:  # pragma: no cover - optional dependency is validated elsewhere.
    qrcode = None


class Employee(models.Model):
    class EmploymentType(models.TextChoices):
        PERMANENT = "permanent", "Permanent"
        CONTRACT = "contract", "Contract"
        PART_TIME = "part_time", "Part-time"
        TEMPORARY = "temporary", "Temporary"
        INTERN = "intern", "Intern"
        OTHER = "other", "Other"

    class EmploymentStatus(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"
        TERMINATED = "terminated", "Terminated"

    class IdentityType(models.TextChoices):
        NATIONAL_ID = "national_id", "National ID"
        PASSPORT = "passport", "Passport"
        DRIVERS_LICENSE = "drivers_license", "Driver's License"
        OTHER = "other", "Other"

    class Gender(models.TextChoices):
        MALE = "male", "Male"
        FEMALE = "female", "Female"
        OTHER = "other", "Other"
        PREFER_NOT_TO_SAY = "prefer_not_to_say", "Prefer not to say"

    class MaritalStatus(models.TextChoices):
        SINGLE = "single", "Single"
        MARRIED = "married", "Married"
        DIVORCED = "divorced", "Divorced"
        WIDOWED = "widowed", "Widowed"
        SEPARATED = "separated", "Separated"

    class DerivedContractStatus(models.TextChoices):
        """Read-only contract status computed from employment status and contract dates.

        It is never stored in the database, so it cannot drift away from
        ``employment_status`` / ``contract_start_date`` / ``contract_end_date``.
        """
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        UPCOMING = "upcoming", "Upcoming"
        TERMINATED = "terminated", "Terminated"
        NOT_SET = "not_set", "Not set"

    # Legacy single-link field preserved for migration compatibility; do not use
    # directly. One Employee may have multiple linked User accounts via
    # UserEmployeeLink (each User links to at most one Employee). Use
    # employee.linked_user_accounts / get_linked_user_ids() and
    # user.employee_profile (compat property) / get_linked_employee(user).
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="legacy_employee_profiles",
        editable=False,
        help_text="Legacy compatibility field (read-only). Authoritative source is UserEmployeeLink; do not write.",
    )
    employee_id = models.CharField(max_length=40, unique=True, blank=True, db_index=True)
    full_name = models.CharField(max_length=200)
    # Internal compatibility archive populated by the data migration. It is not
    # exposed in forms or the ordinary employee UI.
    legacy_external_identifier = models.CharField(
        max_length=60, blank=True, editable=False,
        help_text="Archived legacy external identifier retained for migration safety."
    )
    photo = models.ImageField(upload_to="employees/photos/%Y/%m/", blank=True, null=True)
    preferred_name = models.CharField(max_length=100, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    place_of_birth = models.CharField(max_length=120, blank=True)
    nationality = models.CharField(max_length=80, blank=True, default="Timor-Leste")
    identity_type = models.CharField(max_length=30, choices=IdentityType.choices, blank=True)
    identity_number = models.CharField(max_length=80, blank=True)
    identity_expiry_date = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, choices=Gender.choices, blank=True)

    personal_phone = models.CharField(max_length=35, blank=True)
    personal_email = models.EmailField(blank=True)
    work_email = models.EmailField(blank=True)
    residential_address = models.TextField(blank=True)
    permanent_address = models.TextField(blank=True)
    municipality = models.CharField(max_length=100, blank=True)
    administrative_post = models.CharField(max_length=100, blank=True)
    suco = models.CharField(max_length=100, blank=True)
    aldeia = models.CharField(max_length=100, blank=True)

    marital_status = models.CharField(
        max_length=20,
        choices=MaritalStatus.choices,
        default=MaritalStatus.SINGLE,
        blank=True,
    )
    spouse_name = models.CharField(max_length=150, blank=True)
    spouse_phone = models.CharField(max_length=35, blank=True)
    spouse_email = models.EmailField(blank=True)

    emergency_contact_name = models.CharField(max_length=150, blank=True)
    emergency_contact_relationship = models.CharField(max_length=80, blank=True)
    emergency_contact_phone = models.CharField(max_length=35, blank=True)
    emergency_contact_alternative_phone = models.CharField(max_length=35, blank=True)
    emergency_contact_address = models.TextField(blank=True)
    emergency_contact_notes = models.TextField(blank=True)

    hire_date = models.DateField(null=True, blank=True)
    employment_type = models.CharField(max_length=20, choices=EmploymentType.choices, default=EmploymentType.PERMANENT)
    employment_status = models.CharField(max_length=20, choices=EmploymentStatus.choices, default=EmploymentStatus.ACTIVE)
    termination_date = models.DateField(null=True, blank=True)

    contract_number = models.CharField(max_length=40, blank=True)
    contract_start_date = models.DateField(null=True, blank=True)
    contract_end_date = models.DateField(null=True, blank=True)

    # Contract-based automatic scheduling (opt-in). When enabled, the assignment's
    # contract period (bounded by the assignment dates) drives EmployeeSchedule
    # creation for the selected active shift of the assignment's office. Manual
    # scheduling and the Shift General Schedule stay available and unaffected.
    automatic_schedule_enabled = models.BooleanField(
        default=False,
        help_text="Generate employee schedules automatically from this employee's contract period.",
    )
    automatic_schedule_shift = models.ForeignKey(
        "attendance.Shift",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="automatic_schedule_employees",
        help_text="Shift used for automatic contract-based scheduling. Must be an active shift of the assignment's office.",
    )

    education_level = models.CharField(max_length=80, blank=True)
    field_of_study = models.CharField(max_length=120, blank=True)
    # Free-text legacy value kept so existing data is not lost. Structured, repeatable
    # certificates live in EmployeeCertification.
    certifications = models.TextField(blank=True)
    qr_token = models.CharField(max_length=64, unique=True, blank=True, null=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["employee_id"]
        indexes = [models.Index(fields=["employment_status", "employee_id"], name="employee_status_id_idx")]

    @property
    def qr_code_data_url(self):
        if not self.qr_token or qrcode is None:
            return ""
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
        qr.add_data(f"employee:{self.qr_token}")
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return f"data:image/png;base64,{encoded}"

    def get_derived_contract_status(self, today=None):
        """Return a DerivedContractStatus value; never mutates employment_status."""
        today = today or timezone.localdate()
        if self.employment_status == self.EmploymentStatus.TERMINATED:
            return self.DerivedContractStatus.TERMINATED
        start, end = self.contract_start_date, self.contract_end_date
        if not start and not end:
            return self.DerivedContractStatus.NOT_SET
        if end and end < today:
            return self.DerivedContractStatus.EXPIRED
        if start and start > today:
            return self.DerivedContractStatus.UPCOMING
        return self.DerivedContractStatus.ACTIVE

    @property
    def derived_contract_status(self):
        return self.get_derived_contract_status()

    @property
    def derived_contract_status_label(self):
        return self.DerivedContractStatus(self.get_derived_contract_status()).label

    @property
    def is_married(self):
        return self.marital_status == self.MaritalStatus.MARRIED

    @property
    def has_stored_spouse_details(self):
        return any((self.spouse_name, self.spouse_phone, self.spouse_email))

    @staticmethod
    def generate_employee_id():
        """Return the first unused EMP-xxxxxx identifier."""
        existing_ids = set(
            Employee.objects.filter(employee_id__startswith="EMP-")
            .values_list("employee_id", flat=True)
        )
        number = 1
        while True:
            candidate = f"EMP-{number:06d}"
            if candidate not in existing_ids:
                return candidate
            number += 1

    def clean(self):
        super().clean()
        if not self.full_name:
            raise ValidationError({"full_name": "Full name is required."})
        # A stale termination_date is kept (never wiped) when the status is not
        # Terminated, so it is only validated while it is actually relevant.
        if (
            self.employment_status == self.EmploymentStatus.TERMINATED
            and self.termination_date and self.hire_date
            and self.termination_date < self.hire_date
        ):
            raise ValidationError({"termination_date": "Termination date cannot be earlier than hire date."})
        if self.contract_start_date and self.contract_end_date and self.contract_end_date < self.contract_start_date:
            raise ValidationError({"contract_end_date": "Contract end date cannot be earlier than contract start date."})
        if self.emergency_contact_name and not self.emergency_contact_relationship:
            raise ValidationError({"emergency_contact_relationship": "Emergency contact relationship is required when a contact is provided."})

    def save(self, *args, **kwargs):
        if not self.employee_id:
            self.employee_id = self.generate_employee_id()
        if not self.qr_token:
            self.qr_token = uuid.uuid4().hex
        self.full_name = re.sub(r"\s+", " ", self.full_name or "").strip()
        if not self.full_name:
            # Resolve display name from linked account (authoritative), not legacy user FK.
            linked = None
            try:
                linked = self.primary_linked_user
            except Exception:
                linked = None
            if linked is not None:
                user_full_name = " ".join(
                    filter(None, [getattr(linked, "first_name", ""), getattr(linked, "last_name", "")])
                ).strip()
                if user_full_name:
                    self.full_name = user_full_name
        super().save(*args, **kwargs)

    def __str__(self):
        if self.employee_id and self.full_name:
            return f"{self.employee_id} — {self.full_name}"
        return self.full_name or self.employee_id or "Employee"



    @property
    def linked_user_accounts(self):
        return self.user_account_links.select_related("user")

    def get_linked_user_ids(self, *, active_only=True):
        qs = self.user_account_links.all()
        if active_only:
            qs = qs.filter(user__is_active=True)
        return list(qs.values_list("user_id", flat=True))

    @property
    def primary_linked_user(self):
        """Compatibility only: returns an arbitrary active linked user if exactly
        one exists, else None. Do not use for routing when ambiguity is possible."""
        ids = self.get_linked_user_ids(active_only=True)
        if len(ids) == 1:
            from django.contrib.auth import get_user_model
            return get_user_model().objects.filter(pk=ids[0]).first()
        return None

class EmployeeAssignment(models.Model):
    class AssignmentType(models.TextChoices):
        PRIMARY = "primary", "Primary assignment"
        ACTING = "acting", "Acting appointment"
        TEMPORARY = "temporary", "Temporary assignment"
        SECONDMENT = "secondment", "Secondment"

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="assignments")
    position = models.ForeignKey("organization.Position", on_delete=models.PROTECT, related_name="employee_assignments")
    assignment_type = models.CharField(max_length=20, choices=AssignmentType.choices, default=AssignmentType.PRIMARY)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    is_primary = models.BooleanField(default=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="employee_assignments_made"
    )
    assignment_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date", "employee__employee_id"]
        indexes = [models.Index(fields=["employee", "start_date", "end_date"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=models.F("start_date")),
                name="assignment_end_not_before_start",
            ),
            models.UniqueConstraint(
                fields=["employee"],
                condition=Q(is_primary=True, end_date__isnull=True),
                name="one_open_primary_assignment_per_employee",
            ),
        ]

    def clean(self):
        super().clean()
        # Populate an omitted end date with the contract end before validating, so a
        # finite contract never leaves an assignment effectively unlimited.
        self.apply_contract_end_default()
        errors = {}
        if self.end_date and self.end_date < self.start_date:
            errors["end_date"] = "End date cannot be earlier than start date."
        errors.update(self.contract_consistency_errors())
        errors.update(self.overlapping_primary_errors())
        if errors:
            raise ValidationError(errors)

    def apply_contract_end_default(self):
        """Bound an open-ended assignment by the employee's finite contract end.

        When the assignment end date is omitted and the contract has a finite end
        date, the contract end is stored as the assignment end date. Explicit end
        dates are preserved, and historical rows are never rewritten (this only
        runs when the assignment is being saved or edited).
        """
        if self.end_date is None and self.employee_id:
            employee = self.employee if self.employee_id else None
            contract_end = getattr(employee, "contract_end_date", None) if employee is not None else None
            if contract_end is not None:
                self.end_date = contract_end
        return self.end_date

    def overlapping_primary_errors(self):
        """Reject overlapping PRIMARY assignments (inclusive date ranges).

        The database only forbids two *open* primary assignments; two primary
        assignments with finite end dates can still overlap in time. A valid
        primary assignment must not overlap another primary assignment for the
        same employee. ``None`` end dates are treated as open-ended.
        """
        if not self.is_primary or not self.start_date or not self.employee_id:
            return {}
        others = EmployeeAssignment.objects.filter(
            employee_id=self.employee_id, is_primary=True,
        ).exclude(pk=self.pk)
        start = self.start_date
        end = self.end_date
        for other in others:
            if other.start_date is None:
                continue
            other_end = other.end_date
            # Inclusive overlap: the ranges intersect when neither starts after the other ends.
            if other_end is not None and start > other_end:
                continue
            if end is not None and other.start_date > end:
                continue
            return {
                "start_date": (
                    "This assignment overlaps another primary assignment for this employee "
                    f"({other.start_date} to {other_end or 'open'})."
                )
            }
        return {}

    def contract_consistency_errors(self):
        """Validate this assignment against the employee's finite contract dates.

        Only assignments that are saved/edited are validated; existing inconsistent
        rows are never rewritten. Employees without contract dates keep the existing
        unrestricted behavior.

        Historical continuity: an *existing* assignment that predates the current
        contract may belong to an earlier contract period (the system has no contract
        history). Only a brand-new assignment must start on or after the contract start
        date, so a legitimate renewal is never blocked by an older assignment.
        """
        errors = {}
        employee = self.employee if self.employee_id else None
        if employee is None or not self.start_date:
            return errors
        contract_start = employee.contract_start_date
        contract_end = employee.contract_end_date
        is_new = self.pk is None
        if contract_end and self.start_date > contract_end:
            errors["start_date"] = (
                f"Assignment start date cannot be later than the contract end date ({contract_end})."
            )
        if is_new and contract_start and self.start_date < contract_start:
            errors["start_date"] = (
                f"Assignment start date cannot be earlier than the contract start date ({contract_start})."
            )
        if contract_end and self.end_date and self.end_date > contract_end:
            errors["end_date"] = (
                f"Assignment end date cannot be later than the contract end date ({contract_end})."
            )
        if is_new and contract_start and self.end_date and self.end_date < contract_start:
            errors["end_date"] = "Assignment end date cannot be earlier than the contract start date."
        return errors

    @property
    def effective_end_date(self):
        """Assignment end, or the contract end when the assignment is open-ended.

        Lifecycle queries and reconciliation call this property before treating an
        assignment as current or eligible for future schedules.
        """
        if self.end_date is not None:
            return self.end_date
        employee = self.employee if self.employee_id else None
        return getattr(employee, "contract_end_date", None) if employee is not None else None

    def is_effective_active_on(self, value):
        """Whether this assignment is current and within its contract-bounded end."""
        if isinstance(value, tuple):
            start, end = value
        else:
            start = end = value
        effective_end = self.effective_end_date
        if self.start_date > end:
            return False
        return effective_end is None or effective_end >= start

    @property
    def office(self):
        return self.position.office

    def is_active_on(self, value):
        if isinstance(value, tuple):
            start, end = value
        else:
            start = end = value
        if self.start_date > end:
            return False
        if self.end_date is not None and self.end_date < start:
            return False
        return True

    @property
    def department(self):
        return self.position.department

    def __str__(self):
        return f"{self.employee.employee_id} — {self.position.position_title}"


def certification_upload_to(instance, filename):
    """Store documents under an opaque name; the original name is kept in the DB."""
    extension = os.path.splitext(filename or "")[1].lower()
    return f"employees/certifications/{instance.employee_id}/{uuid.uuid4().hex}{extension}"


class EmployeeCertification(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="employee_certifications")
    certificate_name = models.CharField(max_length=200)
    issuing_organization = models.CharField(max_length=200, blank=True)
    certificate_number = models.CharField(max_length=80, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    document_file = models.FileField(upload_to=certification_upload_to, max_length=255, blank=True)
    original_filename = models.CharField(max_length=255, blank=True, editable=False)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["certificate_name", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(issue_date__isnull=True)
                | Q(expiry_date__isnull=True)
                | Q(expiry_date__gte=models.F("issue_date")),
                name="certification_expiry_not_before_issue",
            ),
        ]

    def clean(self):
        super().clean()
        if self.issue_date and self.expiry_date and self.expiry_date < self.issue_date:
            raise ValidationError({"expiry_date": "Expiry date cannot be earlier than issue date."})

    @property
    def is_expired(self):
        return bool(self.expiry_date and self.expiry_date < timezone.localdate())

    @property
    def display_filename(self):
        if not self.document_file:
            return ""
        return self.original_filename or os.path.basename(self.document_file.name)

    def delete(self, *args, **kwargs):
        # Remove the stored file only after the row deletion has committed.
        name = self.document_file.name if self.document_file else ""
        storage = self.document_file.storage
        result = super().delete(*args, **kwargs)
        if name:
            transaction.on_commit(lambda: storage.delete(name))
        return result

    def __str__(self):
        return f"{self.certificate_name} ({self.employee.employee_id})"


class UserEmployeeLink(models.Model):
    """Links a login User account to an Employee profile.

    Each User may be linked to at most one Employee (OneToOne on user).
    One Employee may be linked to multiple distinct Users. Permissions, groups,
    sessions, notifications, and audit actors remain account-specific.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="employee_account_link",
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="user_account_links",
    )
    is_primary = models.BooleanField(
        default=False,
        help_text="Optional display marker only. Do not use for workflow reviewers, routing, permissions, or authorization.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["employee_id", "user__username"]
        verbose_name = "User-Employee link"
        verbose_name_plural = "User-Employee links"

    def __str__(self):
        return f"{self.user} → {self.employee}"


def get_linked_employee(user):
    """Resolve the Employee linked to this User via UserEmployeeLink.

    The legacy ``Employee.user`` field is compatibility-only data and is never
    consulted here, so a stale value cannot authorize access after its link is
    removed.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    try:
        link = UserEmployeeLink.objects.select_related("employee").filter(user=user).first()
        return link.employee if link is not None else None
    except Exception:
        return None


def is_user_linked_to_employee(user, employee_or_id):
    employee = get_linked_employee(user)
    if employee is None:
        return False
    emp_id = getattr(employee_or_id, "pk", employee_or_id)
    return employee.pk == emp_id
