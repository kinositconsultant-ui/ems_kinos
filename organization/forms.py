from django import forms

from .models import Department, InstitutionProfile, Office, Position


class InstitutionPhotoWidget(forms.ClearableFileInput):
    clear_checkbox_label = "Remove institution photo"



class BootstrapModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"


class InstitutionProfileForm(BootstrapModelForm):
    class Meta:
        model = InstitutionProfile
        fields = (
            "name", "legal_name", "institution_code", "institution_type",
            "registration_number", "email", "phone", "address", "mission", "vision",
            "timezone", "logo", "institution_photo",
        )
        labels = {
            "name": "Institution name",
            "legal_name": "Legal name",
            "institution_code": "Institution code",
            "institution_type": "Institution type",
            "registration_number": "Registration number",
            "email": "Email",
            "phone": "Phone",
            "address": "Address",
            "mission": "Mission",
            "vision": "Vision",
            "timezone": "Time zone",
            "logo": "Institution logo",
            "institution_photo": "Institution photo",
        }
        widgets = {
            "address": forms.Textarea(attrs={"rows": 3}),
            "mission": forms.Textarea(attrs={"rows": 5}),
            "vision": forms.Textarea(attrs={"rows": 5}),
            "institution_photo": InstitutionPhotoWidget(
                attrs={"accept": "image/jpeg,image/png,image/webp"},
            ),
        }

    def clean_logo(self):
        logo = self.cleaned_data.get("logo")
        if not logo or not hasattr(logo, "read"):
            return logo
        if logo.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Logo file must be 5 MB or smaller.")
        try:
            from PIL import Image

            image = Image.open(logo)
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise forms.ValidationError("Upload a JPG, PNG or WEBP image.")
            image.verify()
            logo.seek(0)
        except forms.ValidationError:
            raise
        except Exception as exc:
            raise forms.ValidationError("The selected logo is not a valid image.") from exc
        return logo

    def clean_institution_photo(self):
        photo = self.cleaned_data.get("institution_photo")
        if photo is False or not photo or not hasattr(photo, "read"):
            return photo
        if photo.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Institution photo must be 5 MB or smaller.")
        try:
            from PIL import Image

            image = Image.open(photo)
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise forms.ValidationError("Upload a JPG, PNG or WEBP image.")
            image.verify()
            photo.seek(0)
        except forms.ValidationError:
            raise
        except Exception as exc:
            raise forms.ValidationError("The selected image is not a valid photo.") from exc
        return photo


class OfficeForm(BootstrapModelForm):
    class Meta:
        model = Office
        fields = (
            "parent_office", "office_code", "office_name", "office_type",
            "municipality", "administrative_post", "suco", "aldeia", "address",
            "phone", "email", "latitude", "longitude", "timezone", "is_active",
        )
        labels = {
            "parent_office": "Parent office",
            "office_code": "Office code",
            "office_name": "Office name",
            "office_type": "Office type",
            "municipality": "Municipality",
            "administrative_post": "Administrative post",
            "suco": "Suco",
            "aldeia": "Aldeia",
            "address": "Address",
            "phone": "Phone",
            "email": "Email",
            "latitude": "Latitude",
            "longitude": "Longitude",
            "timezone": "Time zone",
            "is_active": "Active",
        }
        widgets = {"address": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["parent_office"].queryset = Office.objects.exclude(pk=self.instance.pk)

    def clean_parent_office(self):
        parent = self.cleaned_data.get("parent_office")
        current = parent
        while current is not None:
            if current.pk == self.instance.pk:
                raise forms.ValidationError("An office cannot be placed under itself or one of its descendants.")
            current = current.parent_office
        return parent


class DepartmentForm(BootstrapModelForm):
    class Meta:
        model = Department
        fields = (
            "office", "parent_department", "department_code", "department_name",
            "description", "is_active",
        )
        labels = {
            "office": "Office",
            "parent_department": "Parent department",
            "department_code": "Department code",
            "department_name": "Department name",
            "description": "Description",
            "is_active": "Active",
        }
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._staff_hr_office = None
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office
        if is_staff_hr_user(user):
            self._staff_hr_office = resolve_staff_hr_office(user)
        if self._staff_hr_office is not None:
            self.fields["office"].queryset = Office.objects.filter(pk=self._staff_hr_office.pk)
            self.fields["office"].initial = self._staff_hr_office
            self.fields["parent_department"].queryset = Department.objects.filter(office=self._staff_hr_office).select_related("office")
        else:
            self.fields["parent_department"].queryset = Department.objects.select_related("office")
        if self.instance.pk:
            self.fields["parent_department"].queryset = self.fields["parent_department"].queryset.exclude(
                pk=self.instance.pk
            )

    def clean(self):
        cleaned = super().clean()
        office = cleaned.get("office")
        parent = cleaned.get("parent_department")
        if self._staff_hr_office is not None and office is not None and office.pk != self._staff_hr_office.pk:
            self.add_error("office", "You may only manage departments in your authorized branch.")
        if self._staff_hr_office is not None and parent is not None and parent.office_id != self._staff_hr_office.pk:
            self.add_error("parent_department", "Parent department must belong to your authorized branch.")
        if office and parent and parent.office_id != office.pk:
            self.add_error("parent_department", "Parent department must belong to the selected office.")
        current = parent
        while current is not None:
            if current.pk == self.instance.pk:
                self.add_error("parent_department", "A department cannot be placed under itself or a descendant.")
                break
            current = current.parent_department
        return cleaned


class PositionForm(BootstrapModelForm):
    class Meta:
        model = Position
        fields = (
            "office", "department", "reports_to_position", "position_code",
            "position_title","is_management", "description", "is_active",
        )
        labels = {
            "office": "Office",
            "department": "Department",
            "reports_to_position": "Reports to position",
            "position_code": "Position code",
            "position_title": "Position title",
            "is_management": "Management position",
            "description": "Description",
            "is_active": "Active",
        }
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._staff_hr_office = None
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office
        if is_staff_hr_user(user):
            self._staff_hr_office = resolve_staff_hr_office(user)
        if self._staff_hr_office is not None:
            self.fields["office"].queryset = Office.objects.filter(pk=self._staff_hr_office.pk)
            self.fields["office"].initial = self._staff_hr_office
            self.fields["department"].queryset = Department.objects.filter(office=self._staff_hr_office, is_active=True).select_related("office")
            self.fields["reports_to_position"].queryset = Position.objects.filter(office=self._staff_hr_office).select_related("office")
        else:
            self.fields["department"].queryset = Department.objects.select_related("office")
            self.fields["reports_to_position"].queryset = Position.objects.select_related("office")
        if self.instance.pk:
            self.fields["reports_to_position"].queryset = self.fields["reports_to_position"].queryset.exclude(
                pk=self.instance.pk
            )

    def clean(self):
        cleaned = super().clean()
        office = cleaned.get("office")
        department = cleaned.get("department")
        manager = cleaned.get("reports_to_position")
        if self._staff_hr_office is not None:
            if office is not None and office.pk != self._staff_hr_office.pk:
                self.add_error("office", "You may only manage positions in your authorized branch.")
            if department is not None and department.office_id != self._staff_hr_office.pk:
                self.add_error("department", "Department must belong to your authorized branch.")
            if manager is not None and manager.office_id != self._staff_hr_office.pk:
                self.add_error("reports_to_position", "Reporting position must belong to your authorized branch.")
        if office and department and department.office_id != office.pk:
            self.add_error("department", "Department must belong to the selected office.")
        current = manager
        while current is not None:
            if current.pk == self.instance.pk:
                self.add_error("reports_to_position", "A reporting hierarchy cannot contain a cycle.")
                break
            current = current.reports_to_position
        return cleaned
