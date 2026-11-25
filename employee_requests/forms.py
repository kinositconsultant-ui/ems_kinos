from pathlib import Path

from django import forms
from django.db import models

from employees.models import EmployeeAssignment

from .models import EmployeeRequest, LeaveType


LEAVE_REQUEST_TYPES = {
    EmployeeRequest.RequestType.LEAVE,
    EmployeeRequest.RequestType.ABSENCE_PERMISSION,
}

ATTENDANCE_REQUEST_TYPES = {
    EmployeeRequest.RequestType.LATE_ARRIVAL,
    EmployeeRequest.RequestType.EARLY_DEPARTURE,
    EmployeeRequest.RequestType.OVERTIME,
    EmployeeRequest.RequestType.ATTENDANCE_CORRECTION,
}

OFFICIAL_DUTY_REQUEST_TYPES = {
    EmployeeRequest.RequestType.BUSINESS_TRIP,
}

REQUEST_CATEGORY_CHOICES = (
    ("leave", "Leave / Time Off"),
    ("official_duty", "Official Duty / Travel"),
    ("attendance", "Attendance Exception / Correction"),
)

REQUEST_TYPE_GROUPS = {
    "leave": LEAVE_REQUEST_TYPES,
    "official_duty": OFFICIAL_DUTY_REQUEST_TYPES,
    "attendance": ATTENDANCE_REQUEST_TYPES,
}

ATTENDANCE_FIELDS = {
    "attendance_record",
    "requested_check_in_at",
    "requested_check_out_at",
}

LEAVE_FIELDS = {"leave_type", "duration_type"}


class RequestBaseForm(forms.ModelForm):
    def clean(self):
        cleaned = super().clean()
        leave_type = cleaned.get("leave_type")
        request_type = cleaned.get("request_type")

        if request_type not in LEAVE_REQUEST_TYPES:
            cleaned["leave_type"] = None
            if request_type in ATTENDANCE_REQUEST_TYPES and not cleaned.get(
                "duration_type"
            ):
                if request_type in {
                    EmployeeRequest.RequestType.LATE_ARRIVAL,
                    EmployeeRequest.RequestType.EARLY_DEPARTURE,
                    EmployeeRequest.RequestType.OVERTIME,
                }:
                    cleaned["duration_type"] = EmployeeRequest.DurationType.HOURLY
                else:
                    cleaned["duration_type"] = EmployeeRequest.DurationType.FULL_DAY
        if request_type not in ATTENDANCE_REQUEST_TYPES:
            cleaned["attendance_record"] = None
            cleaned["requested_check_in_at"] = None
            cleaned["requested_check_out_at"] = None
        if request_type in OFFICIAL_DUTY_REQUEST_TYPES:
            cleaned["leave_type"] = None
            cleaned["attendance_record"] = None
            cleaned["requested_check_in_at"] = None
            cleaned["requested_check_out_at"] = None

        if (
            request_type == EmployeeRequest.RequestType.LEAVE
            and leave_type
            and leave_type.requires_attachment
            and not cleaned.get("attachment")
            and not (self.instance and self.instance.pk and self.instance.attachment)
        ):
            self.add_error(
                "attachment", "This leave type requires a supporting attachment."
            )
        return cleaned


class RequestForm(RequestBaseForm):
    request_category = forms.ChoiceField(
        choices=REQUEST_CATEGORY_CHOICES,
        required=False,
        label="Request category",
    )

    class Meta:
        model = EmployeeRequest
        fields = (
            "employee_assignment",
            "request_type",
            "duration_type",
            "leave_type",
            "start_date",
            "end_date",
            "attendance_record",
            "requested_check_in_at",
            "requested_check_out_at",
            "reason",
            "attachment",
        )
        labels = {
            "employee_assignment": "Employee assignment",
            "attendance_record": "Attendance record (for correction)",
            "request_type": "Request type",
            "leave_type": "Leave type",
            "start_date": "Start date",
            "end_date": "End date",
            "requested_check_in_at": "Requested check-in",
            "requested_check_out_at": "Requested check-out",
            "reason": "Reason",
            "attachment": "Supporting document",
        }
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "requested_check_in_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "requested_check_out_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "reason": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, employee=None, user=None, **kwargs):
        self._employee = employee
        self._user = user
        super().__init__(*args, **kwargs)

        from django.utils import timezone

        today = timezone.localdate()
        assignment_qs = EmployeeAssignment.objects.select_related(
            "employee", "position__office"
        ).filter(start_date__lte=today).filter(
            models.Q(end_date__isnull=True) | models.Q(end_date__gte=today)
        ).filter(
            models.Q(employee__contract_end_date__isnull=True)
            | models.Q(employee__contract_end_date__gte=today)
        )
        from core.permissions import is_hr_user, resolve_hr_office

        hr_office = resolve_hr_office(user) if is_hr_user(user) else None
        if hr_office is not None:
            assignment_qs = assignment_qs.filter(position__office=hr_office)
        if employee is not None:
            assignment_qs = assignment_qs.filter(employee=employee)
        self.fields["employee_assignment"].queryset = assignment_qs
        self.fields["attendance_record"].queryset = self.fields[
            "attendance_record"
        ].queryset.select_related("schedule__employee_assignment__employee")
        if hr_office is not None:
            self.fields["attendance_record"].queryset = self.fields[
                "attendance_record"
            ].queryset.filter(schedule__employee_assignment__position__office=hr_office)
        self.fields["leave_type"].queryset = LeaveType.objects.filter(is_active=True)

        if employee is not None:
            self.fields["attendance_record"].queryset = self.fields[
                "attendance_record"
            ].queryset.filter(schedule__employee_assignment__employee=employee)

        if employee is not None and not self.instance.pk:
            active_assignments = list(assignment_qs)
            if len(active_assignments) == 1:
                self.fields["employee_assignment"].initial = active_assignments[0].pk

        request_type = None
        request_category = None
        if self.data:
            request_type = self.data.get("request_type")
            request_category = self.data.get("request_category")
        elif self.initial:
            request_type = self.initial.get("request_type")
            request_category = self.initial.get("request_category")
        elif self.instance and self.instance.pk:
            request_type = self.instance.request_type
            request_category = self._category_for_request_type(request_type)

        if not request_category and request_type:
            request_category = self._category_for_request_type(request_type)

        if request_category in REQUEST_TYPE_GROUPS:
            allowed_types = REQUEST_TYPE_GROUPS[request_category]
            self.fields["request_type"].choices = [
                (value, label)
                for value, label in EmployeeRequest.RequestType.choices
                if value in allowed_types
            ]

        if request_category:
            self.fields["request_category"].initial = request_category

        if request_type in LEAVE_REQUEST_TYPES:
            for field_name in ATTENDANCE_FIELDS:
                self.fields.pop(field_name, None)
        elif request_type in ATTENDANCE_REQUEST_TYPES:
            self.fields.pop("leave_type", None)
        elif request_type in OFFICIAL_DUTY_REQUEST_TYPES:
            for field_name in LEAVE_FIELDS | ATTENDANCE_FIELDS:
                self.fields.pop(field_name, None)

        if request_type is None and request_category:
            if request_category == "leave":
                for field_name in ATTENDANCE_FIELDS:
                    self.fields.pop(field_name, None)
            elif request_category == "attendance":
                self.fields.pop("leave_type", None)
            elif request_category == "official_duty":
                for field_name in LEAVE_FIELDS | ATTENDANCE_FIELDS:
                    self.fields.pop(field_name, None)

        if "leave_type" in self.fields:
            self.fields["leave_type"].required = (
                request_type == EmployeeRequest.RequestType.LEAVE
            )
        if "attendance_record" in self.fields:
            self.fields["attendance_record"].required = (
                request_type == EmployeeRequest.RequestType.ATTENDANCE_CORRECTION
            )
        self.fields["duration_type"].required = request_type in LEAVE_REQUEST_TYPES or (
            request_type in ATTENDANCE_REQUEST_TYPES
        )
        self.fields["reason"].required = True

        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean_employee_assignment(self):
        assignment = self.cleaned_data.get("employee_assignment")
        if assignment is None:
            return assignment
        if (
            self._user is not None
            and getattr(self._user, "employee_profile", None) is not None
            and not self._user.is_staff
        ):
            from employees.models import is_user_linked_to_employee
            if not is_user_linked_to_employee(self._user, assignment.employee_id):
                raise forms.ValidationError(
                    "You can only submit requests for your own assignment."
                )
        elif self._employee is not None:
            if assignment.employee_id != self._employee.pk:
                raise forms.ValidationError(
                    "You can only submit requests for your own assignment."
                )
        return assignment

    def clean(self):
        cleaned = super().clean()
        request_type = cleaned.get("request_type")
        cleaned["request_category"] = cleaned.get("request_category") or self._category_for_request_type(
            request_type
        )
        return cleaned

    @staticmethod
    def _category_for_request_type(request_type):
        if request_type in LEAVE_REQUEST_TYPES:
            return "leave"
        if request_type in OFFICIAL_DUTY_REQUEST_TYPES:
            return "official_duty"
        if request_type in ATTENDANCE_REQUEST_TYPES:
            return "attendance"
        return ""

    def clean_attachment(self):
        file = self.cleaned_data.get("attachment")
        if not file:
            return file
        allowed = {".pdf", ".jpg", ".jpeg", ".png"}
        if Path(file.name).suffix.lower() not in allowed:
            raise forms.ValidationError("Upload a PDF, JPG or PNG document.")
        if file.size > 10 * 1024 * 1024:
            raise forms.ValidationError("Attachment must be 10 MB or smaller.")
        return file


class LeaveTypeForm(forms.ModelForm):
    class Meta:
        model = LeaveType
        fields = (
            "code",
            "name",
            "description",
            "category",
            "basis",
            "paid_status",
            "unit",
            "requires_approval",
            "requires_attachment",
            "attachment_instructions",
            "tracks_balance",
            "max_days_per_year",
            "is_active",
        )
        labels = {
            "code": "Leave type code",
            "name": "Leave type name",
            "description": "Description",
            "category": "Leave category",
            "basis": "Legal / policy basis",
            "paid_status": "Paid status",
            "unit": "Unit",
            "requires_approval": "Requires approval",
            "requires_attachment": "Requires attachment",
            "attachment_instructions": "Attachment instructions",
            "tracks_balance": "Tracks balance",
            "max_days_per_year": "Maximum days per year",
            "is_active": "Active",
        }
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"


class DecisionForm(forms.Form):
    decision_note = forms.CharField(
        required=False,
        label="Decision note",
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
    )
