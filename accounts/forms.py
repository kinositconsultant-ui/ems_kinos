from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group, Permission
from django.core import signing
from django.db import transaction
from django.utils.translation import override

from core.permissions import (
    DIRECTOR_GROUP, EMPLOYEE_GROUP_NAMES, HR_GROUP_NAME, IT_ADMIN_GROUP,
    MINISTRO_GROUP, STAFF_HR_GROUP, VICE_GROUP, XEFE_GROUP,
)
from employees.models import Employee, UserEmployeeLink, get_linked_employee

User = get_user_model()


class GroupForm(forms.ModelForm):
    role_names = EMPLOYEE_GROUP_NAMES | {
        name.casefold()
        for name in (
            DIRECTOR_GROUP, HR_GROUP_NAME, IT_ADMIN_GROUP, MINISTRO_GROUP,
            STAFF_HR_GROUP, VICE_GROUP, XEFE_GROUP, "direktur_utama",
        )
    }
    revision_salt = "accounts.group-edit"
    permissions = forms.ModelMultipleChoiceField(
        queryset=Permission.objects.select_related("content_type").order_by(
            "content_type__app_label", "content_type__model", "codename"
        ),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    empty_permissions = forms.BooleanField(required=False, widget=forms.CheckboxInput(
        attrs={"class": "form-check-input"},
    ))
    confirm_role_rename = forms.BooleanField(required=False, widget=forms.CheckboxInput(
        attrs={"class": "form-check-input"},
    ))
    revision = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Group
        fields = ("name", "permissions")
        widgets = {"name": forms.TextInput(attrs={"class": "form-control"})}

    def __init__(self, *args, current_permissions=(), **kwargs):
        self.current_permissions = {permission.pk: permission for permission in current_permissions}
        instance = kwargs.get("instance")
        self.original_name = instance.name if instance else ""
        initial = {}
        if instance:
            self.snapshot = [instance.pk, instance.name, sorted(self.current_permissions)]
            initial["revision"] = signing.dumps(self.snapshot, salt=self.revision_salt)
            initial["empty_permissions"] = not self.current_permissions
        super().__init__(*args, initial=initial, **kwargs)

    @property
    def is_business_role(self):
        return self.original_name.strip().casefold() in self.role_names

    @property
    def permission_sections(self):
        selected = {str(pk) for pk in self["permissions"].value() or []}
        sections = {}
        for permission in self.fields["permissions"].queryset:
            ct = permission.content_type
            sections.setdefault(f"{ct.app_label} / {ct.model}", []).append(
                (permission, str(permission.pk) in selected)
            )
        return sections

    def clean(self):
        cleaned = super().clean()
        permissions = cleaned.get("permissions")
        empty = self.data.get("empty_permissions")
        if permissions is not None and (
            empty not in (None, "on") or bool(permissions) == (empty == "on")
        ):
            self.add_error(
                "permissions",
                "Select permissions or confirm that this group should have no permissions.",
            )

        if self.instance.pk:
            try:
                submitted = signing.loads(cleaned.get("revision", ""), salt=self.revision_salt)
            except signing.BadSignature:
                submitted = None
            if submitted != self.snapshot:
                self.add_error(None, "This edit is out of date or incomplete. Reopen Edit to load the latest group.")

            name = cleaned.get("name")
            if (
                name and name != self.original_name
                and (self.is_business_role or name.strip().casefold() in self.role_names)
                and self.data.get("confirm_role_rename") != "on"
            ):
                self.add_error("confirm_role_rename", "Confirm the effect of renaming a business role.")
        return cleaned


class EmployeeProfileChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        status = "Active" if obj.employment_status == Employee.EmploymentStatus.ACTIVE else "Inactive"
        links = getattr(obj, "_linked_user_count", None)
        suffix = ""
        if links:
            suffix = f", {links} linked account{'s' if links != 1 else ''}"
        return f"{obj.employee_id}  -  {obj.full_name} ({status}{suffix})"


class EnglishGroupChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        if (obj.name or "").strip().lower() == "employess":
            return "Employees"
        return obj.name


class _UserEmployeeMixin:
    EMPLOYEE_GROUP_NAMES = {"employees", "employess"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        employees = Employee.objects.order_by("employee_id")
        try:
            from django.db.models import Count
            employees = employees.annotate(_linked_user_count=Count("user_account_links"))
        except Exception:
            pass
        self.fields["employee_profile"] = EmployeeProfileChoiceField(
            queryset=employees,
            required=False,
            empty_label="Select an employee profile",
            label="Employee Profile",
            help_text=(
                "Link this login account to an employee profile. "
                "One employee profile may be linked to multiple distinct user accounts; "
                "each user account may be linked to at most one employee profile."
            ),
            widget=forms.Select(attrs={"class": "form-select"}),
        )
        self.fields["groups"] = EnglishGroupChoiceField(
            queryset=Group.objects.order_by("name"),
            required=False,
            widget=forms.CheckboxSelectMultiple,
            label="Groups",
            help_text="User permissions are determined by the selected groups.",
        )
        if getattr(self, "instance", None) and getattr(self.instance, "pk", None):
            linked = get_linked_employee(self.instance)
            if linked is not None:
                self.initial.setdefault("employee_profile", linked.pk)
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
                widget.attrs["class"] = "form-check-input"
            else:
                existing = widget.attrs.get("class", "")
                widget.attrs["class"] = (existing or "form-control").strip()
        if "username" in self.fields:
            self.fields["username"].widget.attrs["autofocus"] = True
        for field_name in ("username", "full_name", "password1", "password2"):
            if field_name in self.fields:
                self.fields[field_name].error_messages["required"] = (
                    "This field is required."
                )

    def _is_employee_group_selected(self):
        groups = self.cleaned_data.get("groups") or []
        return any((group.name or "").strip().lower() in self.EMPLOYEE_GROUP_NAMES for group in groups)

    def clean(self):
        cleaned_data = super().clean()
        employee = cleaned_data.get("employee_profile")

        if self._is_employee_group_selected() and employee is None:
            self.add_error(
                "employee_profile",
                "An employee profile is required when the user is added to the Employees group.",
            )

        return cleaned_data

    def _save_employee_relation(self, user):
        """Persist the User<->Employee link via UserEmployeeLink only.

        UserEmployeeLink is the sole authoritative post-migration source.
        Legacy Employee.user is not written here to avoid stale/dual truth.
        """
        selected = self.cleaned_data.get("employee_profile")
        with transaction.atomic():
            UserEmployeeLink.objects.filter(user=user).delete()
            if selected is not None:
                UserEmployeeLink.objects.update_or_create(
                    user=user,
                    defaults={"employee": selected, "is_primary": False},
                )


class UserCreateForm(_UserEmployeeMixin, UserCreationForm):
    full_name = forms.CharField(
        max_length=150,
        required=False,
        label="Full Name",
        help_text="Enter the user's complete name.",
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "groups", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["full_name"].widget.attrs["required"] = True
        self.fields["username"].label = "Username"
        self.fields["username"].help_text = (
            "Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only."
        )
        self.fields["email"].label = "Email address"
        self.fields["email"].help_text = "Enter a valid email address."
        self.fields["is_active"].label = "Active"
        self.fields["is_active"].help_text = (
            "Designates whether this user should be treated as active. "
            "Unselect this instead of deleting the account."
        )
        self.fields["password1"].label = "Password"
        self.fields["password1"].help_text = (
            "<ul>"
            "<li>Your password cannot be too similar to your personal information.</li>"
            "<li>Your password must contain at least 8 characters.</li>"
            "<li>Your password cannot be a commonly used password.</li>"
            "<li>Your password cannot be entirely numeric.</li>"
            "</ul>"
        )
        self.fields["password2"].label = "Password confirmation"
        self.fields["password2"].help_text = (
            "Enter the same password as before, for verification."
        )
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
                widget.attrs["class"] = "form-check-input"
            else:
                existing = widget.attrs.get("class", "")
                widget.attrs["class"] = (existing or "form-control").strip()
        self.fields["username"].widget.attrs["autofocus"] = True
        if "password1" in self.fields:
            self.fields["password1"].widget.attrs["autocomplete"] = "new-password"
        if "password2" in self.fields:
            self.fields["password2"].widget.attrs["autocomplete"] = "new-password"
        if "full_name" in self.initial:
            self.initial["full_name"] = " ".join(
                str(self.initial["full_name"]).split()
            )

    def clean_full_name(self):
        full_name = (self.cleaned_data.get("full_name") or "").strip()
        if not full_name and hasattr(self, "data"):
            first_name = str(self.data.get("first_name", "")).strip()
            last_name = str(self.data.get("last_name", "")).strip()
            full_name = " ".join(part for part in (first_name, last_name) if part)
        if not full_name:
            raise forms.ValidationError("Full Name is required.")
        return full_name

    def _post_clean(self):
        with override("en"):
            super()._post_clean()

    def clean_password2(self):
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise forms.ValidationError(
                "The two password fields didn't match.",
                code="password_mismatch",
            )
        return password2

    def save(self, commit=True):
        user = super().save(commit=commit)
        user.first_name = self.cleaned_data["full_name"]
        user.last_name = ""
        if commit:
            user.save(update_fields=["first_name", "last_name"])
        if commit:
            self._save_employee_relation(user)
        return user


class UserUpdateForm(_UserEmployeeMixin, forms.ModelForm):
    full_name = forms.CharField(
        max_length=150,
        required=False,
        label="Full Name",
        help_text="Enter the user's complete name.",
    )

    class Meta:
        model = User
        fields = ("username", "email", "groups", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["full_name"].widget.attrs["required"] = True
        self.fields["username"].label = "Username"
        self.fields["username"].help_text = (
            "Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only."
        )
        self.fields["email"].label = "Email address"
        self.fields["is_active"].label = "Active"
        self.fields["is_active"].help_text = (
            "Designates whether this user should be treated as active. "
            "Unselect this instead of deleting the account."
        )
        if self.instance and self.instance.pk and not self.is_bound:
            self.initial["full_name"] = " ".join(
                part for part in (
                    str(self.instance.first_name or "").strip(),
                    str(self.instance.last_name or "").strip(),
                ) if part
            )

    def clean_full_name(self):
        full_name = (self.cleaned_data.get("full_name") or "").strip()
        if not full_name and hasattr(self, "data"):
            first_name = str(self.data.get("first_name", "")).strip()
            last_name = str(self.data.get("last_name", "")).strip()
            full_name = " ".join(part for part in (first_name, last_name) if part)
        if not full_name:
            raise forms.ValidationError("Full Name is required.")
        return full_name

    def save(self, commit=True):
        user = super().save(commit=commit)
        user.first_name = self.cleaned_data["full_name"]
        user.last_name = ""
        if commit:
            user.save(update_fields=["first_name", "last_name"])
        if commit:
            self._save_employee_relation(user)
        return user
