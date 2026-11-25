from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class InstitutionProfile(models.Model):
    """The single institution served by this deployment; the primary key is fixed to 1."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    name = models.CharField(max_length=180)
    legal_name = models.CharField(max_length=220, blank=True)
    institution_code = models.CharField(max_length=40, unique=True)
    institution_type = models.CharField(
        max_length=40,
        choices=[
            ("government", "Government institution"),
            ("state_owned", "State-owned enterprise"),
            ("private", "Private company"),
            ("ngo", "NGO"),
            ("other", "Other"),
        ],
        default="other",
    )
    registration_number = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=35, blank=True)
    address = models.TextField(blank=True)
    mission = models.TextField(
        blank=True,
        default="",
        help_text="Enter the institution's official mission statement.",
    )
    vision = models.TextField(
        blank=True,
        default="",
        help_text="Enter the institution's official vision statement.",
    )
    timezone = models.CharField(max_length=50, default="Asia/Dili")
    logo = models.ImageField(upload_to="institution/", blank=True, null=True)
    institution_photo = models.ImageField(
        upload_to="institution/profile/",
        blank=True,
        null=True,
        help_text="Large image displayed on the Institution Profile page.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "institution profile"
        verbose_name_plural = "institution profile"

    def save(self, *args, **kwargs):
        self.pk = 1
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("The institution profile is a singleton and cannot be deleted here.")

    def __str__(self):
        return self.name


class Office(models.Model):
    class OfficeType(models.TextChoices):
        HEADQUARTERS = "headquarters", "Headquarters"
        REGIONAL = "regional", "Regional office"
        BRANCH = "branch", "Branch office"
        SERVICE_CENTER = "service_center", "Service center"
        OTHER = "other", "Other"

    parent_office = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="child_offices",
    )
    office_code = models.CharField(max_length=30, unique=True)
    office_name = models.CharField(max_length=160)
    office_type = models.CharField(max_length=30, choices=OfficeType.choices, default=OfficeType.BRANCH)
    municipality = models.CharField(max_length=100, blank=True)
    administrative_post = models.CharField(max_length=100, blank=True)
    suco = models.CharField(max_length=100, blank=True)
    aldeia = models.CharField(max_length=100, blank=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=35, blank=True)
    email = models.EmailField(blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    timezone = models.CharField(max_length=50, default="Asia/Dili")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["office_name"]
        indexes = [models.Index(fields=["municipality", "is_active"])]

    def clean(self):
        super().clean()
        if self.parent_office_id and self.pk and self.parent_office_id == self.pk:
            raise ValidationError({"parent_office": "An office cannot be its own parent."})

    def __str__(self):
        return f"{self.office_code} — {self.office_name}"


class Department(models.Model):
    office = models.ForeignKey(Office, on_delete=models.PROTECT, related_name="departments")
    parent_department = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="subdepartments"
    )
    department_code = models.CharField(max_length=30)
    department_name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["office__office_name", "department_name"]
        constraints = [
            models.UniqueConstraint(fields=["office", "department_code"], name="unique_department_code_per_office")
        ]

    def clean(self):
        super().clean()
        if self.parent_department_id and self.office_id:
            if self.parent_department.office_id != self.office_id:
                raise ValidationError({"parent_department": "Parent department must belong to the same office."})
            if self.pk and self.parent_department_id == self.pk:
                raise ValidationError({"parent_department": "A department cannot be its own parent."})

    def __str__(self):
        return f"{self.department_code} — {self.department_name}"


class Position(models.Model):
    office = models.ForeignKey(Office, on_delete=models.PROTECT, related_name="positions")
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True, related_name="positions"
    )
    reports_to_position = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="subordinate_positions"
    )
    position_code = models.CharField(max_length=30)
    position_title = models.CharField(max_length=140)
    is_management = models.BooleanField(default=False)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["office", "position_code"], name="unique_position_code_per_office")
        ]

    def clean(self):
        super().clean()
        if self.department_id and self.office_id and self.department.office_id != self.office_id:
            raise ValidationError({"department": "Department must belong to the same office as this position."})
        if self.pk and self.reports_to_position_id == self.pk:
            raise ValidationError({"reports_to_position": "A position cannot report to itself."})

    def __str__(self):
        return f"{self.position_title} ({self.office.office_code})"
