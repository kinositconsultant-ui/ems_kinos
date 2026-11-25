from django import forms
from django.db.models import Q
from django.utils import timezone

from core.permissions import offices_for_user
from employees.models import Employee, EmployeeAssignment
from organization.models import Department, Office

from .models import (
    AttendanceDevice,
    AttendancePolicy,
    AttendanceRecord,
    EmployeeSchedule,
    Holiday,
    Shift,
)
from .services.scheduling import (
    WEEKDAY_NAMES,
    ShiftTimingError,
    build_schedule_snapshot,
    format_minutes,
    format_working_days,
    holidays_on,
)

WEEKDAY_CHOICES = tuple((i, name) for i, name in enumerate(WEEKDAY_NAMES))


def format_assignment_label(assignment):
    position = assignment.position
    department = position.department.department_name if position.department_id else "No department"
    kind = "primary" if assignment.is_primary else assignment.get_assignment_type_display().lower()
    return (
        f"{assignment.employee.employee_id} \u2014 {assignment.employee.full_name} \u00b7 "
        f"{position.position_title} ({department}, {position.office.office_name}) \u00b7 {kind}"
    )


def eligible_assignments(offices, today=None):
    """Assignments HR may schedule: active employee, assignment not yet ended, office in scope.

    Shared by the form and the preview endpoint so both enforce the same rule.
    """
    today = today or timezone.localdate()
    return EmployeeAssignment.objects.filter(
        employee__employment_status=Employee.EmploymentStatus.ACTIVE,
        position__office__in=offices,
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=today)).filter(
        Q(employee__contract_end_date__isnull=True) | Q(employee__contract_end_date__gte=today)
    )


def eligible_shifts(offices):
    """Active shifts of the offices in scope (inactive shifts cannot be used for new schedules)."""
    return Shift.objects.filter(office__in=offices, is_active=True)


class OfficeAwareSelect(forms.Select):
    """Select whose options carry ``data-office`` so the page can filter them client-side.

    ``unavailable`` maps option values to a reason; those options are rendered disabled so they
    stay visible (with an explanation in the label) but cannot be chosen.
    """

    def __init__(self, *args, office_map=None, unavailable=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.office_map = office_map or {}
        self.unavailable = unavailable or {}

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        key = getattr(value, "value", value)
        if key in self.office_map:
            option["attrs"]["data-office"] = self.office_map[key]
        if key in self.unavailable and not selected:
            option["attrs"]["disabled"] = True
            option["attrs"]["data-unavailable"] = "1"
        return option


class OfficeAwareSelectMultiple(forms.SelectMultiple):
    """SelectMultiple whose options carry ``data-office`` so the page can filter them client-side."""

    def __init__(self, *args, office_map=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.office_map = office_map or {}

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        key = getattr(value, "value", value)
        if key in self.office_map:
            option["attrs"]["data-office"] = self.office_map[key]
        return option


class AttendanceModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"


class ShiftForm(AttendanceModelForm):
    """Shift template: break duration and 'crosses midnight' are derived, not entered.

    Includes General Schedule (recurring schedule) configuration directly on the Shift.
    """

    working_days = forms.MultipleChoiceField(
        choices=WEEKDAY_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Working days",
        help_text="Days of the week on which routine schedules should be generated.",
    )
    target_assignments = forms.ModelMultipleChoiceField(
        queryset=EmployeeAssignment.objects.none(),
        required=False,
        label="Specific employee assignments",
        help_text="Select specific active assignments in the shift's office.",
    )

    class Meta:
        model = Shift
        fields = (
            "office", "shift_code", "name", "start_time", "break_start_time",
            "break_end_time", "end_time", "grace_period_minutes",
            "early_checkin_window_minutes", "is_active",
            "recurring_schedule_enabled", "recurrence_start_date", "recurrence_end_date",
            "working_days", "target_assignment_mode", "target_assignments",
        )
        labels = {
            "office": "Office",
            "shift_code": "Shift code",
            "name": "Shift name",
            "start_time": "Start time (Check In)",
            "break_start_time": "Break starts (Break Out)",
            "break_end_time": "Break ends (Break In)",
            "end_time": "End time (Check Out)",
            "grace_period_minutes": "Grace period (minutes)",
            "early_checkin_window_minutes": "Early check-in (minutes before start)",
            "is_active": "Active",
            "recurring_schedule_enabled": "Enable recurring general schedule",
            "recurrence_start_date": "Recurrence start date",
            "recurrence_end_date": "Recurrence end date",
            "working_days": "Working days",
            "target_assignment_mode": "Target employee assignments",
            "target_assignments": "Specific employee assignments",
        }
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "end_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "break_start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "break_end_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "recurrence_start_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "recurrence_end_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "target_assignment_mode": forms.RadioSelect(attrs={"class": "form-check-input"}),
        }

    def summary_rows(self):
        if not self.is_valid():
            return None
        info = self.instance.summary
        rows = [
            ("Shift", info["name"]),
            ("Time", info["time_range"]),
            ("Break", info["break"]),
            ("Scheduled work duration", info["work_duration"]),
            ("Required attendance events", f"{info['required_events']} (Check In, Break Out, Break In, Check Out)"),
            ("General schedule", "Enabled" if self.cleaned_data.get("recurring_schedule_enabled") else "Disabled"),
        ]
        if self.cleaned_data.get("recurring_schedule_enabled"):
            w_days = self.cleaned_data.get("working_days") or []
            mode = self.cleaned_data.get("target_assignment_mode")
            targets = self.cleaned_data.get("target_assignments") or []
            office = self.cleaned_data.get("office")
            office_name = office.office_name if office else (self.instance.office.office_name if self.instance.office_id else "")
            target_text = f"All active eligible employees in {office_name}" if mode == "all" else f"{len(targets)} specific employee assignment(s)"
            rows.extend([
                ("Recurrence period", f"{self.cleaned_data.get('recurrence_start_date')} to {self.cleaned_data.get('recurrence_end_date')}"),
                ("Working days", format_working_days(w_days)),
                ("Target employees", target_text),
            ])
        return rows

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["break_start_time"].required = True
        self.fields["break_end_time"].required = True
        offices = offices_for_user(user) if user is not None else Office.objects.filter(is_active=True)
        self.fields["office"].queryset = offices
        if self.instance.pk and self.instance.is_in_use:
            # Existing schedules reference this office; moving the shift would orphan them.
            self.fields["office"].disabled = True
            self.fields["office"].help_text = "Locked: this shift is already used by schedules."

        assignments = eligible_assignments(offices)
        self.fields["target_assignment_mode"].required = False
        self.fields["target_assignments"].widget = OfficeAwareSelectMultiple(
            office_map={a.pk: a.position.office_id for a in assignments},
            attrs={"class": "form-select", "size": "8"},
        )
        self.fields["target_assignments"].queryset = assignments
        self.fields["target_assignments"].label_from_instance = format_assignment_label

        if self.instance.pk:
            self.fields["working_days"].initial = [str(d) for d in self.instance.working_days_list]
            self.fields["target_assignments"].initial = self.instance.target_assignments.all()
        else:
            self.fields["working_days"].initial = ["0", "1", "2", "3", "4"]
            self.fields["target_assignment_mode"].initial = Shift.TargetAssignmentMode.ALL_ELIGIBLE

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("target_assignment_mode"):
            cleaned["target_assignment_mode"] = Shift.TargetAssignmentMode.ALL_ELIGIBLE
        w_days = cleaned.get("working_days")
        if w_days:
            cleaned["working_days"] = [int(d) for d in w_days]
        else:
            cleaned["working_days"] = [0, 1, 2, 3, 4]

        recurring = cleaned.get("recurring_schedule_enabled")
        if recurring:
            start_date = cleaned.get("recurrence_start_date")
            end_date = cleaned.get("recurrence_end_date")
            if not start_date:
                self.add_error("recurrence_start_date", "Recurrence start date is required when General Schedule is enabled.")
            if not end_date:
                self.add_error("recurrence_end_date", "Recurrence end date is required when General Schedule is enabled.")
            if start_date and end_date:
                if end_date < start_date:
                    self.add_error("recurrence_end_date", "Recurrence end date cannot be earlier than start date.")
                elif (end_date - start_date).days > 366:
                    self.add_error("recurrence_end_date", "Recurrence end date cannot be more than one year from start date.")

            if not w_days:
                self.add_error("working_days", "At least one working day must be selected.")

            mode = cleaned.get("target_assignment_mode")
            office = cleaned.get("office") or getattr(self.instance, "office", None)
            if mode == Shift.TargetAssignmentMode.SPECIFIC:
                targets = cleaned.get("target_assignments")
                if not targets:
                    self.add_error("target_assignments", "Please select at least one employee assignment for specific targeting.")
                elif office:
                    for assignment in targets:
                        if assignment.position.office_id != office.pk:
                            self.add_error(
                                "target_assignments",
                                f"Assignment '{assignment}' does not belong to office '{office.office_name}'.",
                            )
                        if assignment.employee.employment_status != Employee.EmploymentStatus.ACTIVE:
                            self.add_error(
                                "target_assignments",
                                f"Employee '{assignment.employee.full_name}' is inactive.",
                            )
                        if start_date and end_date:
                            if assignment.start_date > end_date or (assignment.end_date and assignment.end_date < start_date):
                                self.add_error(
                                    "target_assignments",
                                    f"Assignment '{assignment}' is not active during the recurrence period.",
                                )
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        w_days = self.cleaned_data.get("working_days")
        if w_days:
            instance.working_days = [int(d) for d in w_days]
        if commit:
            instance.save()
            self.save_m2m()
            if instance.target_assignment_mode == Shift.TargetAssignmentMode.ALL_ELIGIBLE:
                instance.target_assignments.clear()
        return instance



class EmployeeScheduleForm(AttendanceModelForm):
    """HR picks office, employee, shift and date; the times are frozen from the shift.

    Employee and Shift choices are rendered by the server (so Edit always shows the saved values)
    and narrowed to the chosen office by the page script. Every choice is validated again here.
    """

    office = forms.ModelChoiceField(
        queryset=Office.objects.none(),
        label="Office / Branch",
        help_text="Choose the office first: only its employees and shifts can then be selected.",
    )

    class Meta:
        model = EmployeeSchedule
        fields = ("office", "employee_assignment", "shift", "work_date", "status", "notes")
        labels = {
            "employee_assignment": "Employee",
            "shift": "Shift",
            "work_date": "Work date",
            "status": "Schedule status",
            "notes": "Notes",
        }
        widgets = {
            "work_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        today = timezone.localdate()
        editing = bool(self.instance.pk)
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office
        from django.core.exceptions import PermissionDenied
        if is_staff_hr_user(user):
            staff_office = resolve_staff_hr_office(user)
            if staff_office is None or (editing and (
                self.instance.employee_assignment.position.office_id != staff_office.pk
                or self.instance.shift.office_id != staff_office.pk
            )):
                raise PermissionDenied("Staff HR may only schedule employees in their own branch.")
        offices = offices_for_user(user) if user is not None else Office.objects.filter(is_active=True)

        current_office_id = self.instance.shift.office_id if editing else None
        if editing:  # keep the saved office visible even if it is no longer in scope / active
            offices_field_qs = Office.objects.filter(Q(pk__in=offices.values("pk")) | Q(pk=current_office_id))
            self.fields["office"].initial = current_office_id
        else:
            offices_field_qs = offices
        self.fields["office"].queryset = offices_field_qs.order_by("office_name")

        raw = self.data.get(self.add_prefix("office")) if self.is_bound else None
        selected = raw or (str(current_office_id) if editing else "") or str(self.initial.get("office") or "")
        self.selected_office_id = selected if str(selected).isdigit() else ""
        no_office = not self.selected_office_id

        # Employee choices: eligible assignments, plus the saved one when editing (history is kept).
        valid_assignments = eligible_assignments(offices, today)
        assignments = valid_assignments
        valid_assignment_ids = set()
        if editing:
            valid_assignment_ids = set(valid_assignments.values_list("pk", flat=True))
            assignments = EmployeeAssignment.objects.filter(
                Q(pk__in=valid_assignments.values("pk")) | Q(pk=self.instance.employee_assignment_id)
            )
        assignments = assignments.select_related(
            "employee", "position__office", "position__department"
        ).order_by("position__office__office_name", "employee__employee_id")

        # Shift choices: active shifts, plus the saved one when editing.
        valid_shifts = eligible_shifts(offices)
        shifts = valid_shifts
        if editing:
            shifts = Shift.objects.filter(Q(pk__in=valid_shifts.values("pk")) | Q(pk=self.instance.shift_id))
        shifts = shifts.select_related("office").order_by("office__office_name", "start_time")

        def assignment_label(assignment):
            label = self._assignment_label(assignment)
            if editing and assignment.pk == self.instance.employee_assignment_id and assignment.pk not in valid_assignment_ids:
                label += " \u2014 no longer available for new schedules (kept for history)"
            return label

        def shift_label(shift):
            label = self._shift_label(shift)
            if not shift.is_active:
                label += " \u2014 inactive (kept for history)"
            elif not shift.has_break_times and not (editing and shift.pk == self.instance.shift_id):
                label += " \u2014 cannot be used: add break times to this shift first"
            return label

        unavailable_shifts = {
            s.pk: "no break times"
            for s in shifts
            if s.is_active and not s.has_break_times and not (editing and s.pk == self.instance.shift_id)
        }

        # IMPORTANT: assign the widget *before* the queryset. ModelChoiceField hands its choices to the
        # widget when the queryset is set; replacing the widget afterwards left Employee and Shift with
        # zero <option> elements (the empty dropdowns reported on the Add and Edit pages).
        employee_field, shift_field = self.fields["employee_assignment"], self.fields["shift"]
        employee_field.widget = OfficeAwareSelect(
            office_map={a.pk: a.position.office_id for a in assignments},
            attrs={"class": "form-select"},
        )
        shift_field.widget = OfficeAwareSelect(
            office_map={s.pk: s.office_id for s in shifts}, unavailable=unavailable_shifts,
            attrs={"class": "form-select"},
        )
        employee_field.label_from_instance = assignment_label
        shift_field.label_from_instance = shift_label
        employee_field.queryset = assignments
        shift_field.queryset = shifts
        employee_field.empty_label = "Select an office first" if no_office else "Select an employee"
        shift_field.empty_label = "Select an office first" if no_office else "Select a shift"
        employee_field.help_text = "Employees with an active assignment at the selected office."
        shift_field.help_text = (
            "Active shifts of the selected office. Start, break and end times come from the shift; "
            "you do not type them."
        )
        for field, empty_text, none_text in (
            (employee_field, "Select an employee", "No employees with an active assignment were found for this office."),
            (shift_field, "Select a shift", "No active shifts were found for this office. Add one under Shifts first."),
        ):
            field.widget.attrs.update({
                "data-empty-text": empty_text,
                "data-none-text": none_text,
                "data-locked-text": "Select an office first",
            })
            if no_office:
                field.widget.attrs["disabled"] = True  # a plain HTML attribute: the page script enables it

        status_choices = [
            (EmployeeSchedule.Status.PLANNED, "Planned (draft, not usable at the kiosk)"),
            (EmployeeSchedule.Status.SCHEDULED, "Published (ready for attendance)"),
            (EmployeeSchedule.Status.CANCELLED, "Cancelled"),
        ]
        if editing and self.instance.status not in {value for value, _ in status_choices}:
            status_choices.append((self.instance.status, self.instance.get_status_display()))
        self.fields["status"].choices = status_choices
        self.fields["status"].help_text = (
            "This is the lifecycle of the schedule only. Present, Late or Absent are attendance results."
        )
        self.fields["work_date"].help_text = "The scheduled times below are built from the shift for this date."
        self.holiday_notice = []
        self.historical_notes = []
        if editing:
            if self.instance.employee_assignment_id not in valid_assignment_ids:
                self.historical_notes.append(
                    "The saved employee assignment is no longer available for new schedules (inactive employee "
                    "or ended assignment). It stays selected so no information is lost; you cannot switch to "
                    "another unavailable employee."
                )
            if not self.instance.shift.is_active:
                self.historical_notes.append(
                    "The saved shift is inactive. It stays selected so no information is lost; you can only "
                    "switch to an active shift of the same office."
                )

    @staticmethod
    def _assignment_label(assignment):
        position = assignment.position
        department = position.department.department_name if position.department_id else "No department"
        kind = "primary" if assignment.is_primary else assignment.get_assignment_type_display().lower()
        return (
            f"{assignment.employee.employee_id} \u2014 {assignment.employee.full_name} \u00b7 "
            f"{position.position_title} ({department}, {position.office.office_name}) \u00b7 {kind}"
        )

    @staticmethod
    def _shift_label(shift):
        if shift.has_break_times:
            break_part = f"break {shift.break_start_time:%H:%M}\u2013{shift.break_end_time:%H:%M}"
        else:
            break_part = "break times not set"
        return f"{shift.name} ({shift.shift_code}) \u00b7 {shift.time_range} \u00b7 {break_part}"

    def clean(self):
        cleaned = super().clean()
        office = cleaned.get("office")
        assignment = cleaned.get("employee_assignment")
        shift = cleaned.get("shift")
        if office and assignment and assignment.position.office_id != office.pk:
            self.add_error("employee_assignment", "This employee is not assigned to the selected office.")
        if office and shift and shift.office_id != office.pk:
            self.add_error("shift", "This shift belongs to a different office.")
        shift_changed = shift is not None and (self.instance._state.adding or self.instance.shift_id != shift.pk)
        if shift and shift_changed and not shift.has_break_times and "shift" not in self.errors:
            # A saved schedule on a legacy shift can still be edited (notes, status); only choosing it is blocked.
            self.add_error("shift", "This shift has no break times yet. Edit the shift first (four events are required).")
        if self.instance.pk and hasattr(self.instance, "attendance_record") and self.changed_data:
            frozen = {"employee_assignment", "shift", "work_date"} & set(self.changed_data)
            if frozen:
                raise forms.ValidationError(
                    "This schedule already has attendance. Employee, shift and work date cannot be changed; "
                    "cancel and re-create it through a correction instead."
                )
        return cleaned

    def _post_clean(self):
        cleaned = getattr(self, "cleaned_data", {})
        shift, work_date = cleaned.get("shift"), cleaned.get("work_date")
        if shift and work_date and "shift" not in self.errors and "work_date" not in self.errors:
            changed = (
                self.instance._state.adding
                or self.instance.shift_id != shift.pk
                or self.instance.work_date != work_date
            )
            if changed:  # explicit reschedule re-freezes the times; plain edits keep the old snapshot
                try:
                    self.instance.apply_snapshot(build_schedule_snapshot(shift, work_date))
                except ShiftTimingError as exc:
                    self.add_error("shift", exc.message)
        super()._post_clean()
        if shift and work_date and not self.errors:
            self.holiday_notice = list(holidays_on(shift.office, work_date))

    def summary_rows(self):
        """Pre-save summary shown to HR; only available once the form is valid."""
        info = self._summary()
        if info is None:
            return None
        return [
            ("Employee", info["employee"]),
            ("Position", info["position"]),
            ("Office / Branch", info["office"]),
            ("Shift", info["shift"]),
            ("Work date", info["work_date"]),
            ("Scheduled working hours", info["working_hours"]),
            ("Break start\u2013end", info["break"]),
            ("Four required attendance events", info["events"]),
        ]

    def _summary(self):
        if not self.is_valid():
            return None
        schedule, shift = self.instance, self.cleaned_data["shift"]
        assignment = self.cleaned_data["employee_assignment"]
        local = timezone.localtime
        return {
            "employee": f"{assignment.employee.employee_id} \u2014 {assignment.employee.full_name}",
            "office": shift.office.office_name,
            "position": assignment.position.position_title,
            "shift": f"{shift.name} ({shift.shift_code})",
            "work_date": schedule.work_date,
            "working_hours": (
                f"{local(schedule.scheduled_start_at):%H:%M} \u2013 {local(schedule.scheduled_end_at):%H:%M}"
                f" ({format_minutes(schedule.scheduled_work_minutes)} of work)"
            ),
            "break": (
                f"{local(schedule.scheduled_break_start_at):%H:%M} \u2013 {local(schedule.scheduled_break_end_at):%H:%M}"
                if schedule.scheduled_break_start_at else "not set"
            ),
            "events": "Check In \u2192 Break Out \u2192 Break In \u2192 Check Out",
            "holidays": self.holiday_notice,
        }

    def save(self, commit=True):
        if not self.instance.pk:
            self.instance.schedule_source = EmployeeSchedule.ScheduleSource.MANUAL
        return super().save(commit=commit)



class HolidayForm(AttendanceModelForm):
    class Meta:
        model = Holiday
        fields = (
            "holiday_type", "office", "name", "start_date", "end_date",
            "description", "is_active",
        )
        labels = {
            "holiday_type": "Holiday coverage",
            "office": "Office (only for an office-specific holiday)",
            "name": "Holiday name",
            "start_date": "Start date",
            "end_date": "End date",
            "description": "Description",
            "is_active": "Active",
        }
        help_texts = {
            "holiday_type": (
                "National and Institution holidays apply to ALL offices and must not name an office. "
                "Office-specific holidays apply only to the office you choose."
            ),
            "office": "Leave empty for National / Institution holidays; required for an office-specific holiday.",
        }
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "end_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._staff_hr_office = None
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office
        if is_staff_hr_user(user):
            self._staff_hr_office = resolve_staff_hr_office(user)
            if self._staff_hr_office is not None:
                self.fields["office"].queryset = Office.objects.filter(pk=self._staff_hr_office.pk)
                self.fields["office"].initial = self._staff_hr_office
                self.fields["office"].required = True
                # Branch-scoped Staff HR cannot create/modify holidays that affect all offices.
                choices = [(v, l) for v, l in self.fields["holiday_type"].choices if v == Holiday.HolidayType.OFFICE]
                if choices:
                    self.fields["holiday_type"].choices = choices
                    self.fields["holiday_type"].initial = Holiday.HolidayType.OFFICE
                self.fields["holiday_type"].help_text = (
                    "Branch-scoped Staff HR may manage office-specific holidays for their authorized branch only. "
                    "National and Institution holidays affect other offices and are not available here."
                )
            else:
                self.fields["office"].queryset = Office.objects.none()
        elif user is not None:
            self.fields["office"].queryset = offices_for_user(user)

    def clean(self):
        cleaned = super().clean()
        if self._staff_hr_office is not None:
            office = cleaned.get("office")
            htype = cleaned.get("holiday_type")
            if htype != Holiday.HolidayType.OFFICE:
                self.add_error("holiday_type", "Branch-scoped Staff HR may only manage office-specific holidays.")
            if office is None or office.pk != self._staff_hr_office.pk:
                self.add_error("office", "Select your authorized branch office.")
        return cleaned


class AttendancePolicyForm(AttendanceModelForm):
    class Meta:
        model = AttendancePolicy
        fields = (
            "office", "policy_name", "effective_from", "effective_until",
            "require_liveness", "absent_after_minutes", "max_failed_attempts",
            "failed_attempt_lockout_minutes", "is_active",
        )
        labels = {
            "office": "Office (empty means institution-wide)",
            "policy_name": "Policy name",
            "effective_from": "Effective from",
            "effective_until": "Effective until (empty = open-ended)",
            "require_liveness": "Liveness required (mandatory for face attendance)",
            "absent_after_minutes": "Absent threshold after scheduled start (minutes)",
            "max_failed_attempts": "Maximum failed attempts",
            "failed_attempt_lockout_minutes": "Failed-attempt lockout (minutes)",
            "is_active": "Active",
        }
        help_texts = {
            "office": (
                "An office-specific policy overrides the institution-wide one for that office. "
                "Two active policies of the same scope cannot overlap in time."
            ),
            "effective_from": "The policy is applied by the date an attendance event happens; past records keep the policy they started under.",
            "require_liveness": (
                "PAD is mandatory for face attendance and cannot be disabled; the stored flag "
                "is retained for audit history only."
            ),
            "max_failed_attempts": "Failed face attempts allowed before a temporary lockout (reserved for the lockout feature).",
            "failed_attempt_lockout_minutes": "How long the lockout lasts (reserved for the lockout feature).",
        }
        widgets = {
            "effective_from": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "effective_until": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._staff_hr_office = None
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office
        if is_staff_hr_user(user):
            self._staff_hr_office = resolve_staff_hr_office(user)
            if self._staff_hr_office is not None:
                self.fields["office"].queryset = Office.objects.filter(pk=self._staff_hr_office.pk)
                self.fields["office"].initial = self._staff_hr_office
                self.fields["office"].required = True
                self.fields["office"].label = "Office"
                self.fields["office"].help_text = (
                    "Branch-scoped Staff HR may manage policies for their authorized branch only. "
                    "Institution-wide policies affect all offices and are not available here."
                )
            else:
                self.fields["office"].queryset = Office.objects.none()
        elif user is not None:
            self.fields["office"].queryset = offices_for_user(user)

    def clean(self):
        cleaned = super().clean()
        if self._staff_hr_office is not None:
            office = cleaned.get("office")
            if office is None or office.pk != self._staff_hr_office.pk:
                self.add_error("office", "Select your authorized branch office.")
        return cleaned


class AttendanceDeviceForm(AttendanceModelForm):
    class Meta:
        model = AttendanceDevice
        fields = (
            "office", "device_code", "device_name", "device_type",
            "location_description", "status",
        )
        labels = {
            "office": "Office",
            "device_code": "Device code",
            "device_name": "Device name",
            "device_type": "Device type",
            "location_description": "Location description",
            "status": "Device status",
        }
        widgets = {"location_description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._staff_hr_office = None
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office
        if is_staff_hr_user(user):
            self._staff_hr_office = resolve_staff_hr_office(user)
            if self._staff_hr_office is not None:
                self.fields["office"].queryset = Office.objects.filter(pk=self._staff_hr_office.pk)
                self.fields["office"].initial = self._staff_hr_office
            else:
                self.fields["office"].queryset = Office.objects.none()
        elif user is not None:
            self.fields["office"].queryset = offices_for_user(user)

    def clean(self):
        cleaned = super().clean()
        if self._staff_hr_office is not None:
            office = cleaned.get("office")
            if office is None or office.pk != self._staff_hr_office.pk:
                self.add_error("office", "Select your authorized branch office.")
        return cleaned


class HolidayFilterForm(forms.Form):
    office = forms.ChoiceField(required=False, label="Office")
    start_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))


class AttendanceReportFilterForm(forms.Form):
    start_date = forms.DateField(
        required=False,
        label="From",
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    end_date = forms.DateField(
        required=False,
        label="To",
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    office = forms.ModelChoiceField(
        queryset=Office.objects.none(),
        required=False,
        empty_label="All offices",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    department = forms.ModelChoiceField(
        queryset=Department.objects.none(),
        required=False,
        empty_label="All departments",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    employee = forms.ModelChoiceField(
        queryset=Employee.objects.none(),
        required=False,
        empty_label="All employees",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    status = forms.ChoiceField(
        choices=(("", "All statuses"),) + tuple(AttendanceRecord.Status.choices),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        from core.permissions import (
            DIRECTOR_GROUP, XEFE_GROUP,
            is_branch_hr_user, is_employee_group_user, is_executive_institution_user, is_hr_user,
            resolve_branch_hr_office, resolve_director_office, resolve_executive_institution_scope,
            resolve_hr_office, resolve_xefe_assignment, user_has_exact_group,
        )
        user = self.user
        today = timezone.localdate()
        if not getattr(user, "is_authenticated", False):
            for f in ("office", "department", "employee"):
                self.fields[f].queryset = self.fields[f].queryset.none()
            return
        # Executive institution reporting: all authorized-institution offices/departments
        # as filter choices (narrowing only); report/export queryset is separately scoped.
        if is_executive_institution_user(user):
            scope, _err = resolve_executive_institution_scope(user)
            if scope is None:
                for f in ("office", "department", "employee"):
                    self.fields[f].queryset = self.fields[f].queryset.none()
                return
            self.fields["office"].queryset = scope["offices_qs"]
            self.fields["department"].queryset = Department.objects.filter(
                office__in=scope["offices_qs"], is_active=True
            ).select_related("office").order_by("office__office_name", "department_name")
            emp_qs = Employee.objects.filter(employment_status=Employee.EmploymentStatus.ACTIVE)
            active_asn = Q(assignments__is_primary=True, assignments__start_date__lte=today) & (Q(assignments__end_date__isnull=True) | Q(assignments__end_date__gte=today))
            active_asn &= (Q(contract_end_date__isnull=True) | Q(contract_end_date__gte=today))
            emp_qs = emp_qs.filter(active_asn, assignments__position__office__in=scope["offices_qs"])
            self.fields["employee"].queryset = emp_qs.distinct().order_by("employee_id")
            return
        if is_employee_group_user(user):
            self.fields.pop("office", None); self.fields.pop("department", None); self.fields.pop("employee", None)
            return
        office = dept = None
        if user_has_exact_group(user, XEFE_GROUP):
            a = resolve_xefe_assignment(user)
            if a is None:
                for f in ("office", "department", "employee"):
                    self.fields[f].queryset = self.fields[f].queryset.none()
                return
            office, dept = a.position.office, a.position.department
        elif user_has_exact_group(user, DIRECTOR_GROUP):
            office = resolve_director_office(user)
            if office is None:
                for f in ("office", "department", "employee"):
                    self.fields[f].queryset = self.fields[f].queryset.none()
                return
        elif is_branch_hr_user(user):
            office = resolve_branch_hr_office(user)
            if office is None:
                for f in ("office", "department", "employee"):
                    self.fields[f].queryset = self.fields[f].queryset.none()
                return
        elif is_hr_user(user):
            office = resolve_hr_office(user)
            if office is None:
                for f in ("office", "department", "employee"):
                    self.fields[f].queryset = self.fields[f].queryset.none()
                return

        emp_qs = Employee.objects.filter(employment_status=Employee.EmploymentStatus.ACTIVE)
        active_asn = Q(assignments__is_primary=True, assignments__start_date__lte=today) & (Q(assignments__end_date__isnull=True) | Q(assignments__end_date__gte=today))
        active_asn &= (Q(contract_end_date__isnull=True) | Q(contract_end_date__gte=today))
        if office is not None and not getattr(user, "is_superuser", False):
            self.fields["office"].queryset = Office.objects.filter(pk=office.pk)
            self.fields["office"].initial = office.pk
            dept_qs = Department.objects.filter(office=office, is_active=True)
            if dept is not None:
                dept_qs = dept_qs.filter(pk=dept.pk)
                self.fields["department"].initial = dept.pk
            self.fields["department"].queryset = dept_qs.order_by("department_name")
            emp_qs = emp_qs.filter(active_asn, assignments__position__office=office)
            if dept is not None:
                emp_qs = emp_qs.filter(assignments__position__department=dept)
            self.fields["employee"].queryset = emp_qs.distinct().order_by("employee_id")
            # hide office selector for branch-scoped users (scope is fixed server-side anyway)
            self.fields.pop("office", None)
            if dept is not None:
                self.fields.pop("department", None)
            return

        self.fields["office"].queryset = Office.objects.order_by("office_name")
        self.fields["department"].queryset = Department.objects.filter(is_active=True).select_related("office").order_by("office__office_name", "department_name")
        self.fields["employee"].queryset = emp_qs.order_by("employee_id")

    def clean(self):
        cleaned = super().clean()
        start_date = cleaned.get("start_date")
        end_date = cleaned.get("end_date")
        if start_date and end_date and end_date < start_date:
            self.add_error("end_date", "End date cannot be earlier than start date.")
        return cleaned
