from django import forms

from .models import AuditLog


class AuditLogFilterForm(forms.Form):
    action = forms.ChoiceField(
        choices=(("", "All actions"),) + tuple(AuditLog.Action.choices),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
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

    def clean(self):
        cleaned = super().clean()
        start_date = cleaned.get("start_date")
        end_date = cleaned.get("end_date")
        if start_date and end_date and end_date < start_date:
            self.add_error("end_date", "End date cannot be earlier than start date.")
        return cleaned
