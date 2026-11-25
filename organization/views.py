
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect
from django.shortcuts import render
from django.urls import reverse, reverse_lazy
from django.utils.http import urlencode
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from audittrail.models import AuditLog
from audittrail.services import log_action
from core.permissions import require_office_scoped_admin
from .forms import DepartmentForm, InstitutionProfileForm, OfficeForm, PositionForm
from .models import Department, InstitutionProfile, Office, Position


class OrganizationPermissionMixin(LoginRequiredMixin, PermissionRequiredMixin):
    raise_exception = True

    def dispatch(self, request, *args, **kwargs):
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office
        if is_staff_hr_user(request.user):
            if resolve_staff_hr_office(request.user) is None:
                raise PermissionDenied("Staff HR branch could not be resolved.")
            # Branch-scoped Staff HR may perform authorized operations on
            # branch-owned records (Department/Position) via their existing
            # permissions; views enforce object/office scope. Global/shared
            # org structures (Office/InstitutionProfile) remain read-only.
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                model = getattr(getattr(self, "model", None), "_meta", None)
                model_name = getattr(model, "model_name", "")
                if model_name in {"office", "institutionprofile"}:
                    raise PermissionDenied("Global organization records are read-only for Staff HR.")
        return super().dispatch(request, *args, **kwargs)



class RegistryListView(OrganizationPermissionMixin, ListView):
    template_name = "organization/registry_list.html"
    context_object_name = "rows"
    paginate_by = 20
    search_fields = ()
    columns = ()
    detail_route = ""
    create_route = ""
    create_permission = ""
    delete_route = ""
    deactivate_route = ""
    filter_fields = ()
    permission_required = ""

    def get_queryset(self):
        queryset = super().get_queryset()
        query = self.request.GET.get("q", "").strip()
        if query and self.search_fields:
            from django.db.models import Q

            search = Q()
            for field in self.search_fields:
                search |= Q(**{f"{field}__icontains": query})
            queryset = queryset.filter(search)
        from core.permissions import (
            is_employee_group_user,
            is_branch_hr_user,
            resolve_employee_assignment,
            resolve_branch_hr_office,
        )

        hr_office = resolve_branch_hr_office(self.request.user) if is_branch_hr_user(self.request.user) else None
        for field, _label, _choices in self.filter_fields:
            if is_employee_group_user(self.request.user) and field in {
                "office_id", "department_id", "employee_id", "position_id",
            }:
                continue
            if is_branch_hr_user(self.request.user) and field == "office_id":
                queryset = queryset.filter(office=hr_office) if hr_office is not None else queryset.none()
                continue
            value = self.request.GET.get(field, "")
            if value:
                if is_branch_hr_user(self.request.user) and hr_office is not None and field == "department_id":
                    if not Department.objects.filter(pk=value, office=hr_office).exists():
                        continue
                if is_branch_hr_user(self.request.user) and hr_office is not None and field == "position_id":
                    if not Position.objects.filter(pk=value, office=hr_office).exists():
                        continue
                queryset = queryset.filter(**{field: value})
        # HR branch scoping by model.
        if is_branch_hr_user(self.request.user):
            if hr_office is None:
                return self.model.objects.none()
            model_name = self.model.__name__
            if model_name == "Office":
                queryset = queryset.filter(pk=hr_office.pk)
            elif model_name in ("Department", "Position"):
                queryset = queryset.filter(office=hr_office)
            elif model_name == "InstitutionProfile":
                pass  # shared/read-only; no office scoping
        if is_employee_group_user(self.request.user):
            employee_assignment = resolve_employee_assignment(self.request.user)
            if employee_assignment is None:
                return self.model.objects.none()
            office = employee_assignment.position.office
            model_name = self.model.__name__
            if model_name == "Office":
                queryset = queryset.filter(pk=office.pk)
            elif model_name in ("Department", "Position"):
                queryset = queryset.filter(office=office)
        # Director / Xefe scoping (non-superuser, non-IT Admin).
        from core.permissions import (
            is_director_user,
            is_xefe_user,
            resolve_director_office,
            resolve_xefe_assignment,
        )
        director_only = is_director_user(self.request.user)
        xefe_only = is_xefe_user(self.request.user)
        if director_only or xefe_only:
            model_name = self.model.__name__
            if model_name in ("Office", "Department", "Position"):
                if director_only:
                    office = resolve_director_office(self.request.user)
                    if office is None:
                        return self.model.objects.none()
                    if model_name == "Office":
                        queryset = queryset.filter(pk=office.pk)
                    else:
                        queryset = queryset.filter(office=office)
                else:
                    assignment = resolve_xefe_assignment(self.request.user)
                    if assignment is None:
                        return self.model.objects.none()
                    office = assignment.position.office
                    department = assignment.position.department
                    if model_name == "Office":
                        queryset = queryset.filter(pk=office.pk)
                    elif model_name == "Department":
                        queryset = queryset.filter(office=office)
                    elif model_name == "Position":
                        queryset = queryset.filter(office=office, department=department)
        return queryset

    @staticmethod
    def display_value(obj, field):
        value = obj
        for part in field.split("__"):
            if value is None:
                return "—"
            value = getattr(value, part, None)
        if value is None or value == "":
            return "—"
        if field.endswith("_id"):
            return value
        if "__" not in field and hasattr(obj, f"get_{field}_display"):
            value = getattr(obj, f"get_{field}_display")()
        return value

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from core.permissions import (
            is_employee_group_user,
            is_branch_hr_user,
            is_director_user,
            is_xefe_user,
        )

        context["columns"] = self.columns
        context["create_url"] = reverse(self.create_route) if self.create_route else None
        context["can_create"] = (
            bool(self.create_permission)
            and self.request.user.has_perm(self.create_permission)
        )
        context["search_query"] = self.request.GET.get("q", "")
        context["active_filters"] = {
            field: self.request.GET.get(field, "") for field, _label, _choices in self.filter_fields
        }
        director_only = is_director_user(self.request.user)
        xefe_only = is_xefe_user(self.request.user)
        removed_names = set()
        if director_only or xefe_only:
            removed_names.add("office_id")
        if xefe_only:
            removed_names.add("department_id")
        context["filters"] = [
            {
                "name": field,
                "label": label,
                "value": self.request.GET.get(field, ""),
                "choices": choices() if callable(choices) else choices,
            }
            for field, label, choices in self.filter_fields
            if field not in removed_names
            and not (
                is_employee_group_user(self.request.user)
                and field in {"office_id", "department_id", "employee_id", "position_id"}
            )
            and not (is_branch_hr_user(self.request.user) and field == "office_id")
        ]
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office
        if is_staff_hr_user(self.request.user):
            office = resolve_staff_hr_office(self.request.user)
            for item in context["filters"]:
                if item["name"] == "department_id":
                    item["choices"] = Department.objects.filter(office=office).values_list("pk", "department_name")
                elif item["name"] == "position_id":
                    item["choices"] = Position.objects.filter(office=office).values_list("pk", "position_title")
            context["can_create"] = False
        context["table_rows"] = [
            {
                "object": obj,
                "cells": [self.display_value(obj, field) for _label, field in self.columns],
                "url": reverse(self.detail_route, args=[obj.pk]),
                "delete_url": reverse(self.delete_route, args=[obj.pk])
                if self.delete_route else None,
                "deactivate_url": reverse(self.deactivate_route, args=[obj.pk])
                if self.deactivate_route else None,
                "has_references": RegistryDeleteView.has_references(obj),
                "can_deactivate": (
                    bool(self.deactivate_route)
                    and obj.is_active
                    and RegistryDeleteView.has_references(obj)
                    and self.request.user.is_superuser
                    and self.request.user.has_perm(
                        f"organization.change_{self.model._meta.model_name}"
                    )
                ),
            }
            for obj in context["object_list"]
        ]
        context["can_delete"] = (
            bool(self.delete_route)
            and self.request.user.is_superuser
            and self.request.user.has_perm(
                f"organization.delete_{self.model._meta.model_name}"
            )
        )
        context["can_deactivate"] = (
            bool(self.deactivate_route)
            and self.request.user.is_superuser
            and self.request.user.has_perm(
                f"organization.change_{self.model._meta.model_name}"
            )
        )
        context["page_title"] = self.page_title
        context["page_description"] = self.page_description
        query_params = self.request.GET.copy()
        query_params.pop("page", None)
        for removed in removed_names:
            query_params.pop(removed, None)
        context["querystring"] = urlencode(query_params)
        return context


class RegistryDetailView(OrganizationPermissionMixin, DetailView):
    template_name = "organization/registry_detail.html"
    permission_required = ""
    detail_fields = ()
    edit_route = ""
    delete_route = ""
    deactivate_route = ""

    def get_queryset(self):
        queryset = super().get_queryset()
        from core.permissions import (
            is_employee_group_user,
            is_branch_hr_user,
            resolve_employee_assignment,
            resolve_branch_hr_office,
        )

        if is_employee_group_user(self.request.user):
            assignment = resolve_employee_assignment(self.request.user)
            if assignment is None:
                return self.model.objects.none()
            office = assignment.position.office
            model_name = self.model.__name__
            if model_name == "Office":
                return queryset.filter(pk=office.pk)
            if model_name in ("Department", "Position"):
                return queryset.filter(office=office)
            return queryset

        if not is_branch_hr_user(self.request.user):
            return queryset
        hr_office = resolve_branch_hr_office(self.request.user)
        if hr_office is None:
            return self.model.objects.none()
        model_name = self.model.__name__
        if model_name == "Office":
            return queryset.filter(pk=hr_office.pk)
        if model_name in ("Department", "Position"):
            return queryset.filter(office=hr_office)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        obj = self.object
        context["details"] = [
            (label, RegistryListView.display_value(obj, field))
            for label, field in self.detail_fields
        ]
        context["edit_url"] = (
            reverse(self.edit_route, args=[obj.pk]) if self.edit_route else None
        )
        context["can_edit"] = (
            bool(self.edit_route)
            and self.request.user.has_perm(self.permission_required.replace("view_", "change_"))
        )
        from core.permissions import is_staff_hr_user
        if is_staff_hr_user(self.request.user):
            context["can_edit"] = False
        context["delete_url"] = (
            reverse(self.delete_route, args=[obj.pk]) if self.delete_route else None
        )
        context["can_delete"] = (
            bool(self.delete_route)
            and self.request.user.is_superuser
            and self.request.user.has_perm(
                f"organization.delete_{self.model._meta.model_name}"
            )
        )
        context["has_references"] = RegistryDeleteView.has_references(obj)
        context["deactivate_url"] = (
            reverse(self.deactivate_route, args=[obj.pk])
            if self.deactivate_route else None
        )
        context["can_deactivate"] = (
            bool(self.deactivate_route)
            and obj.is_active
            and context["has_references"]
            and self.request.user.is_superuser
            and self.request.user.has_perm(
                f"organization.change_{self.model._meta.model_name}"
            )
        )
        context["page_title"] = self.page_title
        context["back_url"] = reverse(self.back_route)
        return context


class RegistryDeleteView(OrganizationPermissionMixin, View):
    model = None
    list_route = ""

    def get_permission_required(self):
        return (f"organization.delete_{self.model._meta.model_name}",)

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_office_scoped_admin(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        return get_object_or_404(self.model, pk=self.kwargs["pk"])

    @staticmethod
    def has_references(obj):
        return any(
            relation.related_model.objects.filter(
                **{relation.field.attname: obj.pk}
            ).exists()
            for relation in obj._meta.related_objects
        )

    def get(self, request, *args, **kwargs):
        obj = self.get_object()
        if self.has_references(obj):
            messages.error(
                request,
                "This record cannot be deleted because related or historical records still refer to it.",
            )
            return redirect(self.list_route)
        return render(
            request,
            "core/action_confirm.html",
            {
                "page_title": "Delete record",
                "prompt": "You are about to permanently delete this unused master record:",
                "object_label": str(obj),
                "warning": "This action cannot be undone. Records with dependent data are protected.",
                "confirm_label": "Delete permanently",
                "back_url": reverse(self.list_route),
            },
        )

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        if self.model is Office and self.has_references(obj):
            messages.error(
                request,
                "This office cannot be deleted because related or historical records still refer to it.",
            )
            return redirect(self.list_route)

        office = obj.office if isinstance(obj, (Department, Position)) else None
        try:
            with transaction.atomic():
                log_action(
                    actor_user=request.user,
                    office=office,
                    action=AuditLog.Action.DELETE,
                    entity_type=self.model.__name__,
                    object_id=obj.pk,
                    before_data={"label": str(obj)},
                    reason="Unused master record deleted from the management interface.",
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                obj.delete()
        except (ProtectedError, IntegrityError):
            messages.error(
                request,
                "This record is in use and cannot be deleted. Deactivate it or update its dependents instead.",
            )
            return redirect(self.list_route)

        messages.success(request, f'"{obj}" was deleted.')
        return redirect(self.list_route)


class RegistryDeactivateView(OrganizationPermissionMixin, View):
    model = None
    list_route = ""

    def get_permission_required(self):
        return (f"organization.change_{self.model._meta.model_name}",)

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_office_scoped_admin(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        return get_object_or_404(self.model, pk=self.kwargs["pk"])

    def get(self, request, *args, **kwargs):
        obj = self.get_object()
        if not obj.is_active:
            messages.error(request, "This record is already inactive.")
            return redirect(self.list_route)
        return render(
            request,
            "core/action_confirm.html",
            {
                "page_title": "Deactivate record",
                "prompt": "Deactivate this record while preserving all references and history:",
                "object_label": str(obj),
                "warning": "The record will remain available to historical records but cannot be used as an active master record.",
                "confirm_label": "Deactivate",
                "back_url": reverse(self.list_route),
            },
        )

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        with transaction.atomic():
            obj = self.model.objects.select_for_update().get(pk=obj.pk)
            if not obj.is_active:
                messages.error(request, "This record is already inactive.")
                return redirect(self.list_route)
            office = obj if isinstance(obj, Office) else getattr(obj, "office", None)
            log_action(
                actor_user=request.user,
                office=office,
                action=AuditLog.Action.UPDATE,
                entity_type=self.model.__name__,
                object_id=obj.pk,
                before_data={"is_active": True},
                after_data={"is_active": False},
                reason="Master record deactivated; all dependent and historical records retained.",
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            obj.is_active = False
            obj.save(update_fields=["is_active", "updated_at"])
        messages.success(request, f'"{obj}" was deactivated.')
        return redirect(self.list_route)


class RegistryCreateView(OrganizationPermissionMixin, CreateView):
    template_name = "organization/registry_form.html"
    permission_required = ""
    list_route = ""
    page_title = ""
    page_description = ""

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        try:
            # DepartmentForm/PositionForm accept user for Staff HR scoping.
            from inspect import signature
            if "user" in signature(self.form_class.__init__).parameters:
                kwargs["user"] = self.request.user
        except Exception:
            pass
        return kwargs

    def get_success_url(self):
        return reverse(self.list_route)

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"{self.page_title} saved successfully.")
        office = self.object if isinstance(self.object, Office) else getattr(self.object, "office", None)
        log_action(
            actor_user=self.request.user,
            office=office,
            action=AuditLog.Action.CREATE,
            entity_type=self.model.__name__,
            object_id=self.object.pk,
            after_data={"label": str(self.object)},
            ip_address=self.request.META.get("REMOTE_ADDR"),
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            page_title=self.page_title,
            page_description=self.page_description,
            back_url=reverse(self.list_route),
        )
        return context


class RegistryUpdateView(OrganizationPermissionMixin, UpdateView):
    template_name = "organization/registry_form.html"
    permission_required = ""
    list_route = ""
    page_title = ""
    page_description = ""

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        try:
            from inspect import signature
            if "user" in signature(self.form_class.__init__).parameters:
                kwargs["user"] = self.request.user
        except Exception:
            pass
        return kwargs

    def get_success_url(self):
        return reverse(self.detail_route, args=[self.object.pk])

    def form_valid(self, form):
        old_data = {"label": str(self.object)}
        response = super().form_valid(form)
        messages.success(self.request, f"{self.page_title} saved successfully.")
        office = self.object if isinstance(self.object, Office) else getattr(self.object, "office", None)
        log_action(
            actor_user=self.request.user,
            office=office,
            action=AuditLog.Action.UPDATE,
            entity_type=self.model.__name__,
            object_id=self.object.pk,
            before_data=old_data,
            after_data={"label": str(self.object)},
            ip_address=self.request.META.get("REMOTE_ADDR"),
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            page_title=self.page_title,
            page_description=self.page_description,
            back_url=reverse(self.list_route),
        )
        return context



def institution_profile(request):
    from core.permissions import is_staff_hr_user
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path())
    if not request.user.has_perm("organization.view_institutionprofile"):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied
    profile = InstitutionProfile.objects.first()
    if profile is None:
        if not request.user.has_perm("organization.add_institutionprofile"):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied
        return redirect("organization:institution-update")
    details = [
        ("Institution name", profile.name),
        ("Legal name", profile.legal_name or "—"),
        ("Institution code", profile.institution_code),
        ("Institution type", profile.get_institution_type_display()),
        ("Registration number", profile.registration_number or "—"),
        ("Email", profile.email or "—"),
        ("Phone", profile.phone or "—"),
        ("Address", profile.address or "—"),
        ("Time zone", profile.timezone),
        ("Updated", profile.updated_at),
    ]
    return render(
        request,
        "organization/institution_detail.html",
        {"profile": profile, "details": details,
         "can_edit": not is_staff_hr_user(request.user) and request.user.has_perm("organization.change_institutionprofile")},
    )


def institution_profile_edit(request):
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path())
    instance = InstitutionProfile.objects.first()
    permission = (
        "organization.change_institutionprofile"
        if instance
        else "organization.add_institutionprofile"
    )
    if not request.user.has_perm(permission):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied
    view_class = InstitutionProfileUpdateView if instance else InstitutionProfileCreateView
    return view_class.as_view()(request, pk=instance.pk if instance else None)


class InstitutionProfileCreateView(OrganizationPermissionMixin, CreateView):
    model = InstitutionProfile
    form_class = InstitutionProfileForm
    template_name = "organization/registry_form.html"
    permission_required = "organization.add_institutionprofile"
    success_url = reverse_lazy("organization:institution-detail")

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Institution profile created.")
        log_action(
            actor_user=self.request.user,
            office=None,
            action=AuditLog.Action.CREATE,
            entity_type="InstitutionProfile",
            object_id=self.object.pk,
            after_data={"label": str(self.object)},
            ip_address=self.request.META.get("REMOTE_ADDR"),
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            page_title="Create Institution Profile",
            page_description="Set up the single institution profile.",
            back_url=reverse("organization:institution-detail"),
        )
        return context


class InstitutionProfileUpdateView(OrganizationPermissionMixin, UpdateView):
    model = InstitutionProfile
    form_class = InstitutionProfileForm
    template_name = "organization/registry_form.html"
    permission_required = "organization.change_institutionprofile"
    success_url = reverse_lazy("organization:institution-detail")

    def get_object(self, queryset=None):
        return get_object_or_404(InstitutionProfile, pk=self.kwargs.get("pk", 1))

    def form_valid(self, form):
        old_data = {"label": str(self.object)}
        response = super().form_valid(form)
        messages.success(self.request, "Institution profile updated.")
        log_action(
            actor_user=self.request.user,
            office=None,
            action=AuditLog.Action.UPDATE,
            entity_type="InstitutionProfile",
            object_id=self.object.pk,
            before_data=old_data,
            after_data={"label": str(self.object)},
            ip_address=self.request.META.get("REMOTE_ADDR"),
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            page_title="Edit Institution Profile",
            page_description="Update institution details and contact information.",
            back_url=reverse("organization:institution-detail"),
        )
        return context


class OfficeListView(RegistryListView):
    model = Office
    permission_required = "organization.view_office"
    page_title = "Offices & Branches"
    page_description = "Manage institution headquarters, offices and branches."
    columns = (
        ("Code", "office_code"), ("Office", "office_name"), ("Type", "office_type"),
        ("Parent office", "parent_office"), ("Municipality", "municipality"),
        ("Administrative post", "administrative_post"), ("Status", "is_active"),
    )
    search_fields = ("office_code", "office_name", "municipality", "address")
    detail_route = "organization:office-detail"
    create_route = "organization:office-create"
    create_permission = "organization.add_office"
    delete_route = "organization:office-delete"
    deactivate_route = "organization:office-deactivate"
    filter_fields = (
        ("office_type", "Office type", Office.OfficeType.choices),
        ("municipality", "Municipality", lambda: [
            (name, name) for name in Office.objects.order_by()
            .values_list("municipality", flat=True).exclude(municipality="").distinct()
        ]),
        ("is_active", "Status", (("True", "Active"), ("False", "Inactive"))),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("parent_office")


class OfficeDetailView(RegistryDetailView):
    model = Office
    permission_required = "organization.view_office"
    page_title = "Office Details"
    back_route = "organization:office-list"
    edit_route = "organization:office-update"
    delete_route = "organization:office-delete"
    deactivate_route = "organization:office-deactivate"
    detail_fields = (
        ("Office code", "office_code"), ("Office name", "office_name"),
        ("Office type", "office_type"), ("Parent office", "parent_office"),
        ("Municipality", "municipality"), ("Administrative post", "administrative_post"),
        ("Suco", "suco"), ("Aldeia", "aldeia"), ("Address", "address"),
        ("Phone", "phone"), ("Email", "email"), ("Time zone", "timezone"),
        ("Latitude", "latitude"), ("Longitude", "longitude"), ("Active", "is_active"),
        ("Created", "created_at"), ("Updated", "updated_at"),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("parent_office")


class OfficeCreateView(RegistryCreateView):
    model = Office
    form_class = OfficeForm
    permission_required = "organization.add_office"
    list_route = "organization:office-list"
    page_title = "Add Office"
    page_description = "Create an office or branch."


class OfficeUpdateView(RegistryUpdateView):
    model = Office
    form_class = OfficeForm
    permission_required = "organization.change_office"
    list_route = "organization:office-list"
    detail_route = "organization:office-detail"
    page_title = "Edit Office"
    page_description = "Update office information."


class DepartmentListView(RegistryListView):
    model = Department
    permission_required = "organization.view_department"
    page_title = "Departments"
    page_description = "Organize departments within each office."
    columns = (
        ("Code", "department_code"), ("Department", "department_name"), ("Office", "office"),
        ("Parent department", "parent_department"), ("Status", "is_active"),
    )
    search_fields = ("department_code", "department_name", "office__office_name")
    detail_route = "organization:department-detail"
    create_route = "organization:department-create"
    create_permission = "organization.add_department"
    delete_route = "organization:department-delete"
    deactivate_route = "organization:department-deactivate"
    filter_fields = (
        ("office_id", "Office", lambda: Office.objects.order_by("office_name").values_list("id", "office_name")),
        ("is_active", "Status", (("True", "Active"), ("False", "Inactive"))),
        ("parent_department_id", "Parent department", lambda: Department.objects.order_by(
            "department_name").values_list("id", "department_name")),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("office", "parent_department")


class DepartmentDetailView(RegistryDetailView):
    model = Department
    permission_required = "organization.view_department"
    page_title = "Department Details"
    back_route = "organization:department-list"
    edit_route = "organization:department-update"
    delete_route = "organization:department-delete"
    deactivate_route = "organization:department-deactivate"
    detail_fields = (
        ("Department code", "department_code"), ("Department name", "department_name"),
        ("Office", "office"), ("Parent department", "parent_department"),
        ("Description", "description"), ("Active", "is_active"),
        ("Created", "created_at"), ("Updated", "updated_at"),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("office", "parent_department")


class DepartmentCreateView(RegistryCreateView):
    model = Department
    form_class = DepartmentForm
    permission_required = "organization.add_department"
    list_route = "organization:department-list"
    page_title = "Add Department"
    page_description = "Create a department in an office."


class DepartmentUpdateView(RegistryUpdateView):
    model = Department
    form_class = DepartmentForm
    permission_required = "organization.change_department"
    list_route = "organization:department-list"
    detail_route = "organization:department-detail"
    page_title = "Edit Department"
    page_description = "Update department details."


class PositionListView(RegistryListView):
    model = Position
    permission_required = "organization.view_position"
    page_title = "Positions & Reporting Structure"
    page_description = "Manage positions and the reporting hierarchy."
    columns = (
        ("Code", "position_code"), ("Position", "position_title"), ("Office", "office"),
        ("Department", "department"), ("Reports to", "reports_to_position"),
        ("Management", "is_management"), ("Status", "is_active"),
    )
    search_fields = ("position_code", "position_title", "description")
    detail_route = "organization:position-detail"
    create_route = "organization:position-create"
    create_permission = "organization.add_position"
    delete_route = "organization:position-delete"
    deactivate_route = "organization:position-deactivate"
    filter_fields = (
        ("office_id", "Office", lambda: Office.objects.order_by("office_name").values_list("id", "office_name")),
        ("department_id", "Department", lambda: Department.objects.order_by(
            "department_name").values_list("id", "department_name")),
        # ("position_category", "Category", Position._meta.get_field("position_category").choices),
        # ("position_level", "Level", lambda: Position.objects.order_by(
        #     "position_level").values_list("position_level", "position_level").distinct()),
        ("is_management", "Management", (("True", "Management"), ("False", "Other positions"))),
        ("is_active", "Status", (("True", "Active"), ("False", "Inactive"))),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("office", "department", "reports_to_position")


class PositionDetailView(RegistryDetailView):
    model = Position
    permission_required = "organization.view_position"
    page_title = "Position Details"
    back_route = "organization:position-list"
    edit_route = "organization:position-update"
    delete_route = "organization:position-delete"
    deactivate_route = "organization:position-deactivate"
    detail_fields = (
        ("Position code", "position_code"), ("Position title", "position_title"),
        ("Office", "office"), ("Department", "department"),
        ("Reports to", "reports_to_position"),("Management position", "is_management"),
        ("Description", "description"), ("Active", "is_active"),
        ("Created", "created_at"), ("Updated", "updated_at"),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("office", "department", "reports_to_position")


class PositionCreateView(RegistryCreateView):
    model = Position
    form_class = PositionForm
    permission_required = "organization.add_position"
    list_route = "organization:position-list"
    page_title = "Add Position"
    page_description = "Define a position and its reporting line."


class PositionUpdateView(RegistryUpdateView):
    model = Position
    form_class = PositionForm
    permission_required = "organization.change_position"
    list_route = "organization:position-list"
    detail_route = "organization:position-detail"
    page_title = "Edit Position"
    page_description = "Update position information."


class OfficeDeleteView(RegistryDeleteView):
    model = Office
    list_route = "organization:office-list"


class DepartmentDeleteView(RegistryDeleteView):
    model = Department
    list_route = "organization:department-list"


class PositionDeleteView(RegistryDeleteView):
    model = Position
    list_route = "organization:position-list"


class OfficeDeactivateView(RegistryDeactivateView):
    model = Office
    list_route = "organization:office-list"


class DepartmentDeactivateView(RegistryDeactivateView):
    model = Department
    list_route = "organization:department-list"


class PositionDeactivateView(RegistryDeactivateView):
    model = Position
    list_route = "organization:position-list"
