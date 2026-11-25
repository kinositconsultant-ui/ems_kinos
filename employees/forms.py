from datetime import timedelta
import logging

from django import forms
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Q
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from PIL import Image, UnidentifiedImageError

from organization.models import Department, Office, Position

from attendance.models import Shift

from .models import Employee, EmployeeAssignment, EmployeeCertification
from .validators import ACCEPT_ATTRIBUTE, validate_certificate_file

logger = logging.getLogger(__name__)

SPOUSE_FIELDS = ("spouse_name", "spouse_phone", "spouse_email")

# (key, title, icon, [(field, grid class, conditional group)])
FULL = "col-12"
HALF = "col-12 col-md-6"
EMPLOYEE_FORM_SECTIONS = (
    ("personal", "Personal Information", "bi-person-vcard", (
        ("employee_id", HALF, ""), ("full_name", HALF, ""),
        ("preferred_name", HALF, ""),
        ("photo", HALF, ""), ("gender", HALF, ""),
        ("date_of_birth", HALF, ""), ("place_of_birth", HALF, ""),
        ("nationality", HALF, ""), ("identity_type", HALF, ""),
        ("identity_number", HALF, ""), ("identity_expiry_date", HALF, ""),
    )),
    ("contact", "Contact & Address", "bi-geo-alt", (
        ("personal_phone", HALF, ""), ("personal_email", HALF, ""),
        ("work_email", HALF, ""), ("municipality", HALF, ""),
        ("administrative_post", HALF, ""), ("suco", HALF, ""),
        ("aldeia", HALF, ""),
        ("residential_address", FULL, ""), ("permanent_address", FULL, ""),
    )),
    ("family", "Family & Emergency Contact", "bi-people", (
        ("marital_status", HALF, ""),
        ("spouse_name", HALF, "spouse"), ("spouse_phone", HALF, "spouse"),
        ("spouse_email", HALF, "spouse"),
        ("emergency_contact_name", HALF, ""), ("emergency_contact_relationship", HALF, ""),
        ("emergency_contact_phone", HALF, ""), ("emergency_contact_alternative_phone", HALF, ""),
        ("emergency_contact_address", FULL, ""), ("emergency_contact_notes", FULL, ""),
    )),
    ("employment", "Employment & Contract", "bi-briefcase", (
        ("hire_date", HALF, ""), ("employment_type", HALF, ""),
        ("employment_status", HALF, ""), ("termination_date", HALF, "termination"),
        ("contract_number", HALF, ""), ("contract_start_date", HALF, ""),
        ("contract_end_date", HALF, ""), ("@contract_status", HALF, ""),
        ("automatic_schedule_enabled", FULL, ""), ("automatic_schedule_shift", FULL, ""),
        ("notes", FULL, ""),
    )),
    ("assignment", "Office / Branch Assignment", "bi-building", (
        ("office", HALF, ""), ("department", HALF, ""),
        ("position", HALF, ""), ("assignment_start_date", HALF, ""),
    )),
    ("education", "Education & Qualifications", "bi-mortarboard", (
        ("education_level", HALF, ""), ("field_of_study", HALF, ""),
        ("certifications", FULL, ""), ("@certificates", FULL, ""),
    )),
)


class EmployeeForm(forms.ModelForm):
    office = forms.ModelChoiceField(
        queryset=Office.objects.none(), required=True, label="Office / Branch"
    )
    department = forms.ModelChoiceField(
        queryset=Department.objects.none(), required=False, label="Department"
    )
    position = forms.ModelChoiceField(
        queryset=Position.objects.none(), required=True, label="Position"
    )
    assignment_start_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        label="Assignment start date",
    )
    automatic_schedule_shift = forms.ModelChoiceField(
        queryset=Shift.objects.none(),
        required=False,
        label="Automatic work shift",
        help_text=(
            "Required when automatic scheduling is enabled. Only active shifts of the "
            "selected office can be used."
        ),
    )

    class Meta:
        model = Employee
        fields = (
            "employee_id", "full_name", "photo", "preferred_name",
            "date_of_birth", "place_of_birth", "nationality", "identity_type",
            "identity_number", "identity_expiry_date", "gender", "personal_phone",
            "personal_email", "work_email", "residential_address", "permanent_address",
            "municipality", "administrative_post", "suco", "aldeia", "marital_status",
            "spouse_name", "spouse_phone", "spouse_email", "emergency_contact_name",
            "emergency_contact_relationship", "emergency_contact_phone",
            "emergency_contact_alternative_phone", "emergency_contact_address",
            "emergency_contact_notes", "hire_date", "employment_type",
            "employment_status", "termination_date", "contract_number",
            "contract_start_date", "contract_end_date",
            "automatic_schedule_enabled", "automatic_schedule_shift",
            "education_level", "field_of_study", "certifications", "notes",
        )
        labels = {
            "employee_id": "Employee ID",
            "full_name": "Full name",
            "photo": "Employee photo",
            "identity_number": "Identity document number",
            "personal_phone": "Personal phone",
            "personal_email": "Personal email",
            "work_email": "Work email",
            "contract_number": "Contract number",
            "contract_start_date": "Contract start date",
            "contract_end_date": "Contract end date",
            "termination_date": "Termination date",
            "certifications": "Other qualifications (free text)",
            "notes": "Notes",
        }
        help_texts = {
            "contract_end_date": "Leave empty if the employment arrangement has no end date.",
            "notes": "Employment and contract notes.",
        }
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "identity_expiry_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "hire_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "termination_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "contract_start_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "contract_end_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "residential_address": forms.Textarea(attrs={"rows": 2}),
            "permanent_address": forms.Textarea(attrs={"rows": 2}),
            "emergency_contact_address": forms.Textarea(attrs={"rows": 2}),
            "emergency_contact_notes": forms.Textarea(attrs={"rows": 2}),
            "certifications": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 3}),
            "photo": forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
        }

    def __init__(self, *args, assigned_by=None, **kwargs):
        self.assigned_by = assigned_by
        super().__init__(*args, **kwargs)
        self.fields["employee_id"].required = False  # generated by the model if blank
        self.fields["office"].queryset = Office.objects.filter(is_active=True).order_by("office_name")
        self.fields["department"].queryset = Department.objects.filter(is_active=True).select_related("office").order_by("department_name")
        self.fields["position"].queryset = Position.objects.filter(is_active=True).select_related("office", "department").order_by("position_title")

        from core.permissions import is_branch_hr_user, resolve_branch_hr_office

        hr_office = resolve_branch_hr_office(assigned_by) if is_branch_hr_user(assigned_by) else None
        self._hr_office = hr_office
        from core.permissions import is_staff_hr_user
        if is_staff_hr_user(assigned_by) and hr_office is None:
            for name in ("office", "department", "position"):
                self.fields[name].queryset = self.fields[name].queryset.none()
        if hr_office is not None:
            self.fields["office"].queryset = Office.objects.filter(pk=hr_office.pk)
            self.fields["office"].initial = hr_office
            self.fields["department"].queryset = Department.objects.filter(is_active=True, office=hr_office).select_related("office").order_by("department_name")
            self.fields["position"].queryset = Position.objects.filter(is_active=True, office=hr_office).select_related("office", "department").order_by("position_title")

        if self.instance and self.instance.pk:
            current_assignment = (
                self.instance.assignments.filter(is_primary=True, start_date__lte=timezone.localdate())
                .select_related("position__office", "position__department")
                .order_by("-start_date")
                .first()
            )
            if current_assignment:
                self.fields["office"].initial = current_assignment.position.office
                self.fields["department"].initial = current_assignment.position.department
                self.fields["position"].initial = current_assignment.position
                self.fields["assignment_start_date"].initial = current_assignment.start_date

        # Automatic work shift: restricted server-side to active shifts of the assignment's
        # office. The instance's own shift stays selectable when editing so nothing is lost.
        shift_queryset = Shift.objects.filter(is_active=True).select_related("office")
        office_id = None
        if self.is_bound:
            raw_office = self.data.get(self.add_prefix("office"))
            if raw_office and str(raw_office).isdigit():
                office_id = int(raw_office)
        if office_id is None and self.instance and self.instance.pk:
            current = (
                self.instance.assignments.filter(is_primary=True)
                .select_related("position__office")
                .order_by("-start_date")
                .first()
            )
            if current is not None:
                office_id = current.position.office_id
        if office_id is None and self.initial.get("office"):
            office_id = self.initial["office"].pk if hasattr(self.initial["office"], "pk") else self.initial["office"]
        if office_id is not None:
            shift_queryset = shift_queryset.filter(office_id=office_id)
        if self.instance and self.instance.pk and self.instance.automatic_schedule_shift_id:
            shift_queryset = Shift.objects.filter(
                Q(pk__in=shift_queryset.values("pk")) | Q(pk=self.instance.automatic_schedule_shift_id)
            ).select_related("office")
        self.fields["automatic_schedule_shift"].queryset = shift_queryset.order_by("office__office_name", "start_time")

        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select form-select-sm"
            elif isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control form-control-sm"

    def get_sections(self, error_sections=()):
        """Describe the accordion sections. A section is open when it is the first
        one or contains an error (``error_sections`` lets the view flag sections
        whose errors live outside this form, e.g. the certificate formset)."""
        sections = []
        for index, (key, title, icon, definitions) in enumerate(EMPLOYEE_FORM_SECTIONS):
            fields = []
            for name, col, group in definitions:
                if name.startswith("@"):  # non-model block rendered by the template
                    fields.append({"bf": None, "special": name[1:], "col": col, "group": group})
                elif name in self.fields:
                    fields.append({"bf": self[name], "special": "", "col": col, "group": group})
            has_errors = key in error_sections or any(item["bf"] is not None and item["bf"].errors for item in fields)
            sections.append({
                "key": key, "title": title, "icon": icon, "fields": fields,
                "has_errors": has_errors, "open": index == 0 or has_errors,
            })
        return sections

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if not photo or not isinstance(photo, UploadedFile):
            return photo

        max_size = 5 * 1024 * 1024
        if photo.size > max_size:
            raise ValidationError("Employee photo must not be larger than 5 MB.")

        try:
            photo.seek(0)
            image = Image.open(photo)
            image_format = (image.format or "").upper()
            if image_format not in {"JPEG", "PNG", "WEBP"}:
                raise ValidationError("Upload a JPEG, PNG, or WEBP image.")
            image.verify()
            photo.seek(0)
        except (UnidentifiedImageError, OSError, ValueError):
            raise ValidationError("The uploaded file is not a valid image.")
        return photo

    def clean(self):
        cleaned_data = super().clean()
        office = cleaned_data.get("office")
        department = cleaned_data.get("department")
        position = cleaned_data.get("position")
        start_date = cleaned_data.get("assignment_start_date") or cleaned_data.get("hire_date") or timezone.localdate()

        if position and office and position.office_id != office.pk:
            self.add_error("position", "The selected position does not belong to the selected office/branch.")
        if department and office and department.office_id != office.pk:
            self.add_error("department", "The selected department does not belong to the selected office/branch.")
        if position and department and position.department_id != department.pk:
            self.add_error("position", "The selected position does not belong to the selected department.")

        hr_office = getattr(self, "_hr_office", None)
        if hr_office is not None:
            if office is not None and office.pk != hr_office.pk:
                self.add_error("office", "You may only assign employees to your branch office.")
            if department is not None and department.office_id != hr_office.pk:
                self.add_error("department", "The selected department does not belong to your branch office.")
            if position is not None and position.office_id != hr_office.pk:
                self.add_error("position", "The selected position does not belong to your branch office.")

        if position and not office:
            cleaned_data["office"] = position.office
            office = position.office
        if position and not department and position.department_id:
            cleaned_data["department"] = position.department
            department = position.department

        if not position:
            self.add_error("position", "Select the employee's current office position.")
        if not office:
            self.add_error("office", "Select the employee's office/branch.")

        # termination_date is only meaningful (and visible) for Terminated employees.
        # An older value is kept untouched when the status is something else.
        if (
            cleaned_data.get("employment_status") == Employee.EmploymentStatus.TERMINATED
            and cleaned_data.get("termination_date")
            and cleaned_data.get("hire_date")
            and cleaned_data["termination_date"] < cleaned_data["hire_date"]
        ):
            self.add_error("termination_date", "Termination date cannot be earlier than hire date.")

        contract_start = cleaned_data.get("contract_start_date")
        contract_end = cleaned_data.get("contract_end_date")
        if contract_start and contract_end and contract_end < contract_start:
            self.add_error("contract_end_date", "Contract end date cannot be earlier than contract start date.")

        if cleaned_data.get("emergency_contact_name") and not cleaned_data.get("emergency_contact_relationship"):
            self.add_error("emergency_contact_relationship", "Enter the relationship for the emergency contact.")
        if cleaned_data.get("marital_status") != Employee.MaritalStatus.MARRIED:
            # Spouse inputs are hidden for every status other than Married, so whatever was
            # posted for them is ignored on the server (JavaScript is not trusted) and the
            # stored values are kept exactly as they were: nothing is silently erased and
            # nothing can be changed through hidden inputs.
            for name in SPOUSE_FIELDS:
                self._errors.pop(name, None)
                cleaned_data[name] = getattr(self.instance, name, "") or ""

        current_assignment = None
        if self.instance and self.instance.pk:
            current_assignment = (
                self.instance.assignments.filter(is_primary=True, start_date__lte=timezone.localdate())
                .order_by("-start_date")
                .first()
            )
        if current_assignment and position and current_assignment.position_id != position.pk:
            if start_date <= current_assignment.start_date:
                self.add_error(
                    "assignment_start_date",
                    "A transfer must start after the current primary assignment's start date.",
                )
        # Contract boundaries for the assignment this form creates or updates. A brand
        # new assignment must fall inside the contract period; an existing assignment
        # that predates the current contract is historical continuity (the system has
        # no contract history) and is not rejected here.
        is_new_assignment = current_assignment is None
        if start_date and contract_end and start_date > contract_end:
            self.add_error(
                "assignment_start_date",
                f"Assignment start date cannot be later than the contract end date ({contract_end}).",
            )
        elif is_new_assignment and start_date and contract_start and start_date < contract_start:
            self.add_error(
                "assignment_start_date",
                f"Assignment start date cannot be earlier than the contract start date ({contract_start}).",
            )
        cleaned_data["assignment_start_date"] = start_date

        automatic_enabled = cleaned_data.get("automatic_schedule_enabled")
        automatic_shift = cleaned_data.get("automatic_schedule_shift")
        if automatic_enabled and automatic_shift is None:
            self.add_error(
                "automatic_schedule_shift",
                "Select an automatic work shift, or turn automatic scheduling off.",
            )
        if automatic_shift is not None:
            if not automatic_shift.is_active:
                self.add_error("automatic_schedule_shift", "The selected shift is inactive.")
            elif office is not None and automatic_shift.office_id != office.pk:
                self.add_error(
                    "automatic_schedule_shift",
                    "The selected shift does not belong to the selected office/branch.",
                )
        return cleaned_data

    def save(self, commit=True, assigned_by=None):
        previous_state = None
        if self.instance and self.instance.pk:
            from .services.contract_scheduling import snapshot_employee_state

            previous_state = snapshot_employee_state(self.instance)

        employee = super().save(commit=False)
        if not employee.employee_id:
            employee.employee_id = Employee.generate_employee_id()
        if not employee.qr_token:
            # A QR token is opaque and does not contain personal information.
            import uuid
            employee.qr_token = uuid.uuid4().hex

        if not commit:
            return employee

        office = self.cleaned_data.get("office")
        position = self.cleaned_data.get("position")
        start_date = self.cleaned_data.get("assignment_start_date") or employee.hire_date or timezone.localdate()
        assigned_by = assigned_by or self.assigned_by

        with transaction.atomic():
            employee.save()
            current = (
                employee.assignments.filter(is_primary=True, start_date__lte=timezone.localdate())
                .order_by("-start_date")
                .first()
            )
            if position:
                if current and current.position_id == position.pk:
                    # Keep the existing assignment; do not create a duplicate primary assignment.
                    pass
                else:
                    if current:
                        current.end_date = start_date - timedelta(days=1)
                        current.save(update_fields=["end_date", "updated_at"])
                    # A brand new assignment is bounded by the employee's contract end date
                    # when the contract is finite (an open-ended contract stays open-ended).
                    EmployeeAssignment.objects.create(
                        employee=employee,
                        position=position,
                        assignment_type=EmployeeAssignment.AssignmentType.PRIMARY,
                        start_date=start_date,
                        end_date=employee.contract_end_date,
                        is_primary=True,
                        assigned_by=assigned_by,
                    )
        # Contract-based automatic scheduling runs only for this employee.
        self.scheduling_result = self._synchronize_scheduling(employee, assigned_by, previous_state)
        return employee

    @staticmethod
    def _synchronize_scheduling(employee, actor_user, previous_state):
        from .services.contract_scheduling import synchronize_employee

        try:
            return synchronize_employee(employee, actor_user=actor_user, previous=previous_state)
        except Exception:
            logger.exception("Contract scheduling failed for employee %s", employee.employee_id)
            return None


class EmployeeSelfServiceForm(forms.ModelForm):
    PERSONAL_FIELDS = (
        "full_name", "photo", "preferred_name", "gender", "date_of_birth",
        "place_of_birth", "nationality", "identity_type", "identity_number",
        "identity_expiry_date",
    )
    CONTACT_FIELDS = (
        "personal_phone", "personal_email", "residential_address",
        "permanent_address", "municipality", "administrative_post", "suco", "aldeia",
    )
    FAMILY_FIELDS = (
        "marital_status", "spouse_name", "spouse_phone", "spouse_email",
        "emergency_contact_name", "emergency_contact_relationship",
        "emergency_contact_phone", "emergency_contact_alternative_phone",
        "emergency_contact_address", "emergency_contact_notes",
    )
    EDUCATION_FIELDS = ("education_level", "field_of_study", "certifications")

    class Meta:
        model = Employee
        fields = (
            "full_name", "photo", "preferred_name", "gender", "date_of_birth",
            "place_of_birth", "nationality", "identity_type", "identity_number",
            "identity_expiry_date", "personal_phone", "personal_email",
            "residential_address", "permanent_address", "municipality",
            "administrative_post", "suco", "aldeia", "marital_status",
            "spouse_name", "spouse_phone", "spouse_email", "emergency_contact_name",
            "emergency_contact_relationship", "emergency_contact_phone",
            "emergency_contact_alternative_phone", "emergency_contact_address",
            "emergency_contact_notes", "education_level", "field_of_study",
            "certifications",
        )
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "identity_expiry_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "residential_address": forms.Textarea(attrs={"rows": 2}),
            "permanent_address": forms.Textarea(attrs={"rows": 2}),
            "emergency_contact_address": forms.Textarea(attrs={"rows": 2}),
            "emergency_contact_notes": forms.Textarea(attrs={"rows": 2}),
            "certifications": forms.Textarea(attrs={"rows": 2}),
            "photo": forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
        }

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if not photo or not isinstance(photo, UploadedFile):
            return photo
        if photo.size > 5 * 1024 * 1024:
            raise ValidationError("Employee photo must not be larger than 5 MB.")
        try:
            photo.seek(0)
            image_format = (Image.open(photo).format or "").upper()
            Image.open(photo).verify()
            photo.seek(0)
        except (UnidentifiedImageError, OSError, ValueError):
            raise ValidationError("The uploaded file is not a valid image.")
        if image_format not in {"JPEG", "PNG", "WEBP"}:
            raise ValidationError("Upload a JPEG, PNG, or WEBP image.")
        return photo

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("marital_status") != Employee.MaritalStatus.MARRIED:
            for name in SPOUSE_FIELDS:
                self._errors.pop(name, None)
                cleaned_data[name] = getattr(self.instance, name, "") or ""
        if cleaned_data.get("emergency_contact_name") and not cleaned_data.get("emergency_contact_relationship"):
            self.add_error("emergency_contact_relationship", "Enter the relationship for the emergency contact.")
        return cleaned_data


class EmployeeAssignmentForm(forms.ModelForm):
    class Meta:
        model = EmployeeAssignment
        fields = (
            "employee", "position", "assignment_type", "start_date",
            "end_date", "is_primary", "assignment_reason",
        )
        labels = {
            "employee": "Employee",
            "position": "Position",
            "assignment_type": "Assignment type",
            "start_date": "Start date",
            "end_date": "End date",
            "is_primary": "Primary assignment",
            "assignment_reason": "Assignment reason",
        }
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "end_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "assignment_reason": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        from core.permissions import is_branch_hr_user, resolve_branch_hr_office

        hr_office = resolve_branch_hr_office(self.user) if is_branch_hr_user(self.user) else None
        self._hr_office = hr_office
        from core.permissions import is_staff_hr_user
        if is_staff_hr_user(self.user) and hr_office is None:
            self.fields["employee"].queryset = Employee.objects.none()
            self.fields["position"].queryset = Position.objects.none()
        if hr_office is not None:
            self.fields["employee"].queryset = Employee.objects.filter(
                assignments__is_primary=True,
                assignments__start_date__lte=timezone.localdate(),
                assignments__position__office=hr_office,
            ).filter(
                Q(assignments__end_date__isnull=True) | Q(assignments__end_date__gte=timezone.localdate())
            ).distinct()
            # Also allow employees with no assignment (onboarding) ? For assignment create, employee already exists.
            self.fields["position"].queryset = Position.objects.filter(is_active=True, office=hr_office).select_related("office", "department")
        self.fields["position"].queryset = self.fields["position"].queryset.select_related("office", "department")
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned_data = super().clean()
        hr_office = getattr(self, "_hr_office", None)
        if hr_office is not None:
            employee = cleaned_data.get("employee")
            position = cleaned_data.get("position")
            if employee is not None:
                from employees.models import EmployeeAssignment
                today = timezone.localdate()
                emp_assignments = EmployeeAssignment.objects.filter(
                    employee=employee, is_primary=True, start_date__lte=today,
                ).filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
                emp_office = None
                if emp_assignments.count() == 1:
                    emp_office = emp_assignments.first().position.office
                if emp_office is None or emp_office.pk != hr_office.pk:
                    self.add_error("employee", "This employee does not belong to your branch office.")
            if position is not None and position.office_id != hr_office.pk:
                self.add_error("position", "The selected position does not belong to your branch office.")

        # Contract consistency: a brand-new assignment must fall inside the employee's
        # finite contract. An omitted end with a finite contract is populated with the
        # contract end by the model clean() (apply_contract_end_default), so the
        # assignment is never left effectively unlimited. Existing assignments that
        # predate the current contract are historical continuity (the system has no
        # contract history) and are not rejected for that alone.
        employee = cleaned_data.get("employee")
        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")
        if employee is not None and start_date is not None:
            contract_start = employee.contract_start_date
            contract_end = employee.contract_end_date
            is_new = self.instance.pk is None
            if contract_end and start_date > contract_end:
                self.add_error(
                    "start_date",
                    f"Assignment start date cannot be later than the contract end date ({contract_end}).",
                )
            if is_new and contract_start and start_date < contract_start:
                self.add_error(
                    "start_date",
                    f"Assignment start date cannot be earlier than the contract start date ({contract_start}).",
                )
            if contract_end and end_date is not None and end_date > contract_end:
                self.add_error(
                    "end_date",
                    f"The assignment cannot end after the contract end date ({contract_end}).",
                )
        return cleaned_data


def _delete_stored_file_on_commit(storage, name):
    if name:
        transaction.on_commit(lambda: storage.delete(name))


class EmployeeCertificationForm(forms.ModelForm):
    """One certificate = one metadata set + at most one document, so uploads from
    different rows can never be mixed up."""

    class Meta:
        model = EmployeeCertification
        fields = (
            "certificate_name", "issuing_organization", "certificate_number",
            "issue_date", "expiry_date", "document_file", "notes",
        )
        labels = {
            "certificate_name": "Certificate name",
            "issuing_organization": "Issuing organization",
            "certificate_number": "Certificate number",
            "issue_date": "Issue date",
            "expiry_date": "Expiry date",
            "document_file": "Document (PDF, JPG, PNG, WEBP · max 10 MB)",
        }
        widgets = {
            "issue_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "expiry_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"rows": 2}),
            # Plain FileInput (not ClearableFileInput): submitting without a file always keeps
            # the stored document, and no unauthenticated MEDIA_URL link is rendered.
            "document_file": forms.FileInput(attrs={"accept": ACCEPT_ATTRIBUTE}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._previous_file_name = self.instance.document_file.name if self.instance.pk and self.instance.document_file else ""
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control form-control-sm"

    def clean_document_file(self):
        upload = self.cleaned_data.get("document_file")
        return validate_certificate_file(upload)

    def clean(self):
        cleaned_data = super().clean()
        issue, expiry = cleaned_data.get("issue_date"), cleaned_data.get("expiry_date")
        if issue and expiry and expiry < issue:
            self.add_error("expiry_date", "Expiry date cannot be earlier than issue date.")
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        upload = self.cleaned_data.get("document_file")
        if isinstance(upload, UploadedFile):
            instance.original_filename = (upload.name or "").replace("\\", "/").split("/")[-1][:255]
        if commit:
            instance.save()
            new_name = instance.document_file.name if instance.document_file else ""
            if self._previous_file_name and self._previous_file_name != new_name:
                # Old file is removed only after the new one is stored and the DB commit succeeded.
                _delete_stored_file_on_commit(instance.document_file.storage, self._previous_file_name)
                self._previous_file_name = new_name
        return instance


class BaseEmployeeCertificationFormSet(BaseInlineFormSet):
    """Server-side permission and size checks for the certificate rows."""

    can_add_certificates = False
    can_change_certificates = False
    can_delete_certificates = False

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue
            if self.can_delete and self._should_delete_form(form):
                if form.instance.pk and not self.can_delete_certificates:
                    raise ValidationError("You do not have permission to delete certificates.")
                continue
            if not form.has_changed():
                continue
            if form.instance.pk and not self.can_change_certificates:
                raise ValidationError("You do not have permission to change certificates.")
            if not form.instance.pk and not self.can_add_certificates:
                raise ValidationError("You do not have permission to add certificates.")

    @property
    def has_any_errors(self):
        return self.is_bound and (any(self.errors) or bool(self.non_form_errors()))


EmployeeCertificationFormSet = inlineformset_factory(
    Employee,
    EmployeeCertification,
    form=EmployeeCertificationForm,
    formset=BaseEmployeeCertificationFormSet,
    extra=0,
    can_delete=True,
    max_num=25,
    validate_max=True,
)

EmployeeSelfServiceCertificationFormSet = inlineformset_factory(
    Employee,
    EmployeeCertification,
    form=EmployeeCertificationForm,
    formset=type(
        "EmployeeSelfServiceCertificationFormSetBase",
        (BaseEmployeeCertificationFormSet,),
        {
            "can_add_certificates": True,
            "can_change_certificates": True,
            "can_delete_certificates": False,
        },
    ),
    extra=0,
    can_delete=False,
    max_num=25,
    validate_max=True,
)
CERTIFICATION_FORMSET_PREFIX = "certs"
