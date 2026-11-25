import mimetypes
import os
import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import urlencode
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from audittrail.models import AuditLog
from audittrail.services import log_action
from core.permissions import require_office_scoped_admin
from employee_requests.models import EmployeeRequest
from organization.models import Department, InstitutionProfile, Office, Position

from .forms import (
    CERTIFICATION_FORMSET_PREFIX, EmployeeAssignmentForm, EmployeeCertificationFormSet,
    EmployeeForm, EmployeeSelfServiceForm, EmployeeSelfServiceCertificationFormSet,
)
from .models import (
    Employee,
    EmployeeAssignment,
    EmployeeCertification,
    get_linked_employee,
)

logger = logging.getLogger(__name__)


def build_id_card(employee, assignment=None):
    """Data shown on the ID card. Only non-sensitive fields are exposed here: no national
    ID number, address, birth date, spouse, emergency contact, phone or contract data."""
    position = assignment.position if assignment else None
    return {
        "name": employee.full_name if employee else "",
        "employee_id": employee.employee_id if employee else "",
        "office": position.office.office_name if position else "",
        "department": position.department.department_name if position and position.department else "",
        "position": position.position_title if position else "",
        "photo_url": employee.photo.url if employee and employee.photo else "",
        "qr_url": employee.qr_code_data_url if employee and employee.pk else "",
    }


def get_certificate_permissions(user):
    return {
        "view": user.has_perm("employees.view_employeecertification"),
        "add": user.has_perm("employees.add_employeecertification"),
        "change": user.has_perm("employees.change_employeecertification"),
        "delete": user.has_perm("employees.delete_employeecertification"),
    }


class EmployeeAccessMixin(LoginRequiredMixin, PermissionRequiredMixin):
    raise_exception = True

    def dispatch(self, request, *args, **kwargs):
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office
        if is_staff_hr_user(request.user) and resolve_staff_hr_office(request.user) is None:
            raise PermissionDenied("Staff HR branch could not be resolved.")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = super().get_queryset()
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office, staff_hr_employee_ids
        if is_staff_hr_user(self.request.user):
            if queryset.model is Employee:
                return queryset.filter(pk__in=staff_hr_employee_ids(self.request.user))
            if queryset.model is EmployeeAssignment:
                return queryset.filter(position__office=resolve_staff_hr_office(self.request.user))
        return queryset



class EmployeeListView(EmployeeAccessMixin, ListView):
    model = Employee
    template_name = "employees/employee_list.html"
    context_object_name = "employees"
    permission_required = "employees.view_employee"
    paginate_by = 20

    def get_queryset(self):
        today = timezone.localdate()
        from core.permissions import (
            DIRECTOR_GROUP,
            is_employee_group_user,
            is_branch_hr_user,
            is_it_admin_user,
            resolve_employee_assignment,
            resolve_branch_hr_office,
            resolve_director_office,
            resolve_xefe_assignment,
            XEFE_GROUP,
        )

        hr_office = resolve_branch_hr_office(self.request.user) if is_branch_hr_user(self.request.user) else None
        if is_branch_hr_user(self.request.user) and hr_office is None:
            return Employee.objects.none()
        self._hr_office = hr_office
        employee_assignment = (
            resolve_employee_assignment(self.request.user)
            if is_employee_group_user(self.request.user)
            else None
        )
        from core.permissions import is_branch_hr_user, resolve_branch_hr_office

        assignments = EmployeeAssignment.objects.filter(
            is_primary=True,
            start_date__lte=today,
        ).filter(Q(end_date__isnull=True) | Q(end_date__gte=today)).filter(
            Q(employee__contract_end_date__isnull=True)
            | Q(employee__contract_end_date__gte=today)
        ).select_related(
            "position__office", "position__department"
        )
        queryset = Employee.objects.prefetch_related(
            Prefetch("assignments", queryset=assignments, to_attr="current_assignments")
        )
        if self.request.user.groups.filter(name=XEFE_GROUP).exists():
            assignment = resolve_xefe_assignment(self.request.user)
            if assignment is None:
                return Employee.objects.none()
            queryset = queryset.filter(
                assignments__is_primary=True,
                assignments__start_date__lte=today,
                assignments__position__office_id=assignment.position.office_id,
                assignments__position__department_id=assignment.position.department_id,
            ).filter(
                Q(assignments__end_date__isnull=True)
                | Q(assignments__end_date__gte=today)
            ).distinct()
            q = self.request.GET.get("q", "").strip()
            if q:
                queryset = queryset.filter(
                    Q(employee_id__icontains=q)
                    | Q(full_name__icontains=q)
                    | Q(work_email__icontains=q)
                    | Q(personal_phone__icontains=q)
                    | Q(personal_email__icontains=q)
                )
            for field in ("employment_status", "employment_type"):
                value = self.request.GET.get(field)
                if value:
                    queryset = queryset.filter(**{field: value})
            return queryset.order_by("employee_id")
        if self.request.user.groups.filter(name=DIRECTOR_GROUP).exists() \
                and not self.request.user.is_superuser \
                and not is_it_admin_user(self.request.user):
            director_office = resolve_director_office(self.request.user)
            if director_office is None:
                return Employee.objects.none()
            self._director_office = director_office
            queryset = queryset.filter(
                assignments__is_primary=True,
                assignments__start_date__lte=today,
                assignments__position__office=director_office,
            ).filter(
                Q(assignments__end_date__isnull=True)
                | Q(assignments__end_date__gte=today)
            ).distinct()
            q = self.request.GET.get("q", "").strip()
            if q:
                queryset = queryset.filter(
                    Q(employee_id__icontains=q)
                    | Q(full_name__icontains=q)
                    | Q(work_email__icontains=q)
                    | Q(personal_phone__icontains=q)
                    | Q(personal_email__icontains=q)
                )
            for field in ("employment_status", "employment_type"):
                value = self.request.GET.get(field)
                if value:
                    queryset = queryset.filter(**{field: value})
            department_id = self.request.GET.get("department_id")
            if department_id:
                from organization.models import Department
                # Departemen filter tidak boleh mengajak cross-join ke assignment riwayat di
                # kantor lain. Hanya departemen yang masih berada dalam kantor direktur yang
                # diperbolehkan; selain itu cakupan tetap kosong (fail closed).
                if not Department.objects.filter(pk=department_id, office=director_office).exists():
                    return Employee.objects.none()
                queryset = queryset.filter(
                    assignments__is_primary=True,
                    assignments__start_date__lte=today,
                    assignments__position__office=director_office,
                ).filter(
                    Q(assignments__end_date__isnull=True) | Q(assignments__end_date__gte=today)
                ).filter(assignments__position__department_id=department_id)
            return queryset.order_by("employee_id")
        if is_employee_group_user(self.request.user):
            if employee_assignment is None:
                return Employee.objects.none()
            queryset = queryset.filter(pk=employee_assignment.employee_id)
        query = self.request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(
                Q(employee_id__icontains=query)
                | Q(full_name__icontains=query)
                | Q(work_email__icontains=query)
                | Q(personal_phone__icontains=query)
                | Q(personal_email__icontains=query)
            )
        for field in ("employment_status", "employment_type"):
            value = self.request.GET.get(field)
            if value:
                queryset = queryset.filter(**{field: value})
        assignment_filters = {
            "office_id": "assignments__position__office_id",
            "department_id": "assignments__position__department_id",
            "position_id": "assignments__position_id",
        }
        # HR branch scoping: never trust request-supplied office/department/position.
        if hr_office is not None:
            queryset = queryset.filter(
                assignments__is_primary=True,
                assignments__start_date__lte=today,
                assignments__position__office=hr_office,
            ).filter(
                Q(assignments__end_date__isnull=True)
                | Q(assignments__end_date__gte=today)
            ).filter(
                Q(assignments__employee__contract_end_date__isnull=True)
                | Q(assignments__employee__contract_end_date__gte=today)
            )
        for field, lookup in assignment_filters.items():
            if field == "office_id":
                # HR office is forced above; ignore any supplied value.
                if hr_office is not None:
                    continue
            value = self.request.GET.get(field)
            if value and hr_office is not None and field in ("department_id", "position_id"):
                # Validate the supplied filter belongs to the HR office.
                from organization.models import Department, Position

                if field == "department_id":
                    if not Department.objects.filter(pk=value, office=hr_office).exists():
                        continue
                else:
                    if not Position.objects.filter(pk=value, office=hr_office).exists():
                        continue
            if value:
                queryset = queryset.filter(
                    assignments__is_primary=True,
                    assignments__start_date__lte=today,
                ).filter(
                    Q(assignments__end_date__isnull=True)
                    | Q(assignments__end_date__gte=today)
                ).filter(**{lookup: value})
        from core.permissions import is_staff_hr_user, staff_hr_employee_ids
        if is_staff_hr_user(self.request.user):
            queryset = queryset.filter(pk__in=staff_hr_employee_ids(self.request.user))
        return queryset.distinct().order_by("employee_id")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from core.permissions import (
            is_employee_group_user,
            is_branch_hr_user,
            is_director_user,
            is_it_admin_user,
            is_xefe_user,
            resolve_director_office,
            resolve_xefe_assignment,
        )

        employee_only = is_employee_group_user(self.request.user)
        hr_only = is_branch_hr_user(self.request.user)
        director_only = is_director_user(self.request.user)
        xefe_only = is_xefe_user(self.request.user)

        director_office = resolve_director_office(self.request.user) if director_only else None
        xefe_assignment = resolve_xefe_assignment(self.request.user) if xefe_only else None

        def department_choices():
            if director_only and director_office is not None:
                return Department.objects.filter(office=director_office).order_by("department_name").values_list("id", "department_name")
            if hr_only and getattr(self, "_hr_office", None) is not None:
                return Department.objects.filter(office=self._hr_office).order_by("department_name").values_list("id", "department_name")
            return Department.objects.order_by("department_name").values_list("id", "department_name")

        def position_choices():
            from core.permissions import is_it_admin_user

            if xefe_only and xefe_assignment is not None:
                position = xefe_assignment.position
                department = getattr(position, "department", None)
                office = getattr(position, "office", None)
                if (
                    department is not None
                    and office is not None
                    and getattr(department, "office_id", None) == office.pk
                ):
                    return Position.objects.filter(
                        office=office, department=department
                    ).order_by("position_title").values_list("id", "position_title")
                return Position.objects.none()
            if director_only and director_office is not None:
                return Position.objects.filter(office=director_office).order_by("position_title").values_list("id", "position_title")
            if hr_only and getattr(self, "_hr_office", None) is not None:
                return Position.objects.filter(office=self._hr_office).order_by("position_title").values_list("id", "position_title")
            if self.request.user.is_superuser or is_it_admin_user(self.request.user):
                return Position.objects.order_by("position_title").values_list("id", "position_title")
            return Position.objects.none()

        filter_definitions = (
            ("employment_status", "Employment status", Employee.EmploymentStatus.choices),
            ("employment_type", "Employment type", Employee.EmploymentType.choices),
            ("office_id", "Office", ([(getattr(self, "_hr_office", None).pk, getattr(self, "_hr_office", None).office_name)] if getattr(self, "_hr_office", None) is not None else Office.objects.order_by("office_name").values_list("id", "office_name"))),
            ("department_id", "Department", department_choices()),
            ("position_id", "Position", position_choices()),
        )
        removed_names = set()
        if director_only or xefe_only:
            removed_names.add("office_id")
        if xefe_only:
            removed_names.add("department_id")
        context.update(
            page_title="Employee List",
            search_query=self.request.GET.get("q", ""),
            filters=[
                {
                    "name": name,
                    "label": label,
                    "choices": choices,
                    "value": self.request.GET.get(name, ""),
                }
                for name, label, choices in filter_definitions
                if name not in removed_names
                and not (
                    employee_only and name in {"office_id", "department_id", "position_id"}
                )
                and not (hr_only and name == "office_id")
            ],
            querystring=urlencode({
                key: value for key, value in self.request.GET.items()
                if key != "page" and key not in removed_names
            }),
            can_add=self.request.user.has_perm("employees.add_employee"),
            can_deactivate=(
                self.request.user.is_superuser
                and self.request.user.has_perm("employees.change_employee")
            ),
        )
        context["table_rows"] = [
            {
                "employee": employee,
                "assignment": employee.current_assignments[0]
                if employee.current_assignments else None,
                "deactivate_url": reverse(
                    "employees:employee-deactivate", args=[employee.pk]
                ),
            }
            for employee in context["employees"]
        ]
        return context


class EmployeeDetailView(EmployeeAccessMixin, DetailView):
    model = Employee
    template_name = "employees/employee_detail.html"
    permission_required = "employees.view_employee"
    queryset = Employee.objects.prefetch_related(
        "assignments__position__office",
        "assignments__position__department",
        "face_templates",
        "employee_certifications",
    )

    def get_queryset(self):
        queryset = super().get_queryset()
        from core.permissions import (
            DIRECTOR_GROUP,
            is_it_admin_user,
            is_employee_group_user,
            is_branch_hr_user,
            resolve_director_office,
            resolve_employee_assignment,
            resolve_branch_hr_office,
            resolve_xefe_assignment,
            XEFE_GROUP,
        )

        if is_employee_group_user(self.request.user):
            assignment = resolve_employee_assignment(self.request.user)
            if assignment is None:
                return Employee.objects.none()
            return queryset.filter(pk=assignment.employee_id)

        if self.request.user.groups.filter(name=XEFE_GROUP).exists():
            assignment = resolve_xefe_assignment(self.request.user)
            if assignment is None:
                return Employee.objects.none()
            today = timezone.localdate()
            return queryset.filter(
                assignments__is_primary=True,
                assignments__start_date__lte=today,
                assignments__position__office_id=assignment.position.office_id,
                assignments__position__department_id=assignment.position.department_id,
            ).filter(
                Q(assignments__end_date__isnull=True)
                | Q(assignments__end_date__gte=today)
            ).distinct()

        if self.request.user.groups.filter(name=DIRECTOR_GROUP).exists() \
                and not self.request.user.is_superuser \
                and not is_it_admin_user(self.request.user):
            director_office = resolve_director_office(self.request.user)
            if director_office is None:
                return Employee.objects.none()
            today = timezone.localdate()
            return queryset.filter(
                assignments__is_primary=True,
                assignments__start_date__lte=today,
                assignments__position__office=director_office,
            ).filter(
                Q(assignments__end_date__isnull=True)
                | Q(assignments__end_date__gte=today),
                Q(assignments__employee__contract_end_date__isnull=True)
                | Q(assignments__employee__contract_end_date__gte=today),
            ).distinct()

        if is_branch_hr_user(self.request.user):
            hr_office = resolve_branch_hr_office(self.request.user)
            if hr_office is None:
                return Employee.objects.none()
            today = timezone.localdate()
            return queryset.filter(
                assignments__is_primary=True,
                assignments__start_date__lte=today,
                assignments__position__office=hr_office,
            ).filter(
                Q(assignments__end_date__isnull=True)
                | Q(assignments__end_date__gte=today)
            ).distinct()
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        employee = self.object
        today = timezone.localdate()
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office
        assignment_queryset = employee.assignments.all()
        if is_staff_hr_user(self.request.user):
            assignment_queryset = assignment_queryset.filter(position__office=resolve_staff_hr_office(self.request.user))
        assignments = list(assignment_queryset)
        current = [
            item for item in assignments
            if item.is_primary and item.start_date <= today
            and (item.end_date is None or item.end_date >= today)
            and item.is_effective_active_on(today)
        ]
        certificate_perms = get_certificate_permissions(self.request.user)
        institution = InstitutionProfile.objects.filter(pk=1).first()
        context.update(
            page_title="Employee Details",
            institution=institution,
            id_card=build_id_card(employee, current[0] if current else None),
            contract_status_label=employee.derived_contract_status_label,
            contract_status_code=employee.derived_contract_status,
            show_spouse=employee.is_married,
            can_view_certificates=certificate_perms["view"],
            certifications=list(employee.employee_certifications.all()) if certificate_perms["view"] else [],
            assignments=assignments,
            current_assignments=current,
            can_edit=self.request.user.has_perm("employees.change_employee"),
            can_add_assignment=self.request.user.has_perm("employees.add_employeeassignment"),
            can_deactivate=(
                employee.employment_status == Employee.EmploymentStatus.ACTIVE
                and self.request.user.is_superuser
                and self.request.user.has_perm("employees.change_employee")
            ),
            deactivate_url=reverse("employees:employee-deactivate", args=[employee.pk]),
            face_templates=employee.face_templates.all(),
            recent_requests=EmployeeRequest.objects.filter(
                employee_assignment__employee=employee,
                **({"branch": resolve_staff_hr_office(self.request.user)} if is_staff_hr_user(self.request.user) else {})
            ).order_by("-submitted_at")[:5],
        )
        return context


class EmployeeFormContextMixin:
    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["assigned_by"] = self.request.user
        return kwargs

    def get_certification_formset(self, bound=False):
        """Return the certificate formset, or None when it must not be processed.

        - Users without any add/change/delete certificate permission get no editable formset.
        - A POST that carries no certificate management form (e.g. an older client) simply
          leaves certificates untouched instead of failing the whole employee form.
        """
        perms = get_certificate_permissions(self.request.user)
        if not (perms["add"] or perms["change"] or perms["delete"]):
            return None
        data = files = None
        if bound:
            if f"{CERTIFICATION_FORMSET_PREFIX}-TOTAL_FORMS" not in self.request.POST:
                return None
            data, files = self.request.POST, self.request.FILES
        formset_class = type(
            "EmployeeCertificationFormSetForUser",
            (EmployeeCertificationFormSet,),
            {
                "can_add_certificates": perms["add"],
                "can_change_certificates": perms["change"],
                "can_delete_certificates": perms["delete"],
            },
        )
        return formset_class(
            data, files,
            instance=self.object if self.object is not None else Employee(),
            prefix=CERTIFICATION_FORMSET_PREFIX,
        )

    def post(self, request, *args, **kwargs):
        self.object = self.get_object() if self.pk_url_kwarg in self.kwargs else None
        form = self.get_form()
        formset = self.get_certification_formset(bound=True)
        form_is_valid = form.is_valid()
        formset_is_valid = formset.is_valid() if formset is not None else True
        if form_is_valid and formset_is_valid:
            return self.forms_valid(form, formset)
        return self.render_to_response(self.get_context_data(form=form, cert_formset=formset))

    def forms_valid(self, form, formset):
        is_create = self.object is None
        old_data = {}
        if not is_create:
            old_employee = Employee.objects.get(pk=self.object.pk)
            old_data = {
                "employee_id": old_employee.employee_id,
                "full_name": old_employee.full_name,
                "employment_status": old_employee.employment_status,
                "contract_start_date": str(old_employee.contract_start_date) if old_employee.contract_start_date else None,
                "contract_end_date": str(old_employee.contract_end_date) if old_employee.contract_end_date else None,
                "automatic_schedule_enabled": old_employee.automatic_schedule_enabled,
                "automatic_schedule_shift_id": old_employee.automatic_schedule_shift_id,
            }
        with transaction.atomic():
            self.object = form.save()
            if formset is not None:
                formset.instance = self.object
                formset.save()
        messages.success(self.request, "Employee record saved successfully.")
        assignment = self.object.assignments.select_related("position__office").order_by("-start_date").first()
        office = assignment.position.office if assignment else None
        new_data = {
            "employee_id": self.object.employee_id,
            "full_name": self.object.full_name,
            "employment_status": self.object.employment_status,
            "contract_start_date": str(self.object.contract_start_date) if self.object.contract_start_date else None,
            "contract_end_date": str(self.object.contract_end_date) if self.object.contract_end_date else None,
            "automatic_schedule_enabled": self.object.automatic_schedule_enabled,
            "automatic_schedule_shift_id": self.object.automatic_schedule_shift_id,
        }
        log_action(
            actor_user=self.request.user,
            office=office,
            action=AuditLog.Action.CREATE if is_create else AuditLog.Action.UPDATE,
            entity_type="Employee",
            object_id=self.object.pk,
            before_data=old_data,
            after_data=new_data,
            ip_address=self.request.META.get("REMOTE_ADDR"),
        )
        self._report_scheduling(form)
        return HttpResponseRedirect(self.get_success_url())

    def _report_scheduling(self, form):
        result = getattr(form, "scheduling_result", None)
        if result is None:
            return
        if result.pending_reasons:
            messages.warning(
                self.request,
                "Automatic scheduling is pending: " + result.pending_reason,
            )
        if result.created_count:
            messages.success(
                self.request,
                f"Automatic scheduling created {result.created_count} schedule(s) "
                f"({result.skipped_existing_count} already present, {result.conflict_count} conflicts).",
            )
        elif result.eligible:
            messages.info(
                self.request,
                "Automatic scheduling found nothing new to create "
                f"({result.skipped_existing_count} already present, {result.conflict_count} conflicts).",
            )
        if result.cancelled_count:
            messages.info(
                self.request,
                f"Automatic scheduling cancelled {result.cancelled_count} future schedule(s).",
            )
        for blocker in result.blocking_conflicts:
            messages.error(self.request, blocker)

    def get_context_data(self, **kwargs):
        formset = kwargs.pop("cert_formset", None)
        context = super().get_context_data(**kwargs)
        if formset is None and self.request.method != "POST":
            formset = self.get_certification_formset()
        perms = get_certificate_permissions(self.request.user)
        employee = self.object
        assignment = None
        if employee is not None and employee.pk:
            assignment = (
                employee.assignments.filter(is_primary=True, start_date__lte=timezone.localdate())
                .select_related("position__office", "position__department")
                .order_by("-start_date").first()
            )
        error_sections = ("education",) if formset is not None and formset.has_any_errors else ()
        context.update(
            page_title=self.page_title,
            back_url=reverse("employees:employee-list"),
            institution=InstitutionProfile.objects.filter(pk=1).first(),
            sections=context["form"].get_sections(error_sections=error_sections),
            cert_formset=formset,
            cert_perms=perms,
            existing_certifications=(
                list(employee.employee_certifications.all())
                if employee is not None and employee.pk and perms["view"] and formset is None else []
            ),
            id_card=build_id_card(employee if employee is not None and employee.pk else None, assignment),
            today=timezone.localdate(),
            stored_spouse=bool(employee is not None and employee.pk and employee.has_stored_spouse_details),
        )
        return context

    def get_success_url(self):
        return reverse("employees:employee-detail", args=[self.object.pk])


class EmployeeCreateView(EmployeeAccessMixin, EmployeeFormContextMixin, CreateView):
    model = Employee
    form_class = EmployeeForm
    template_name = "employees/employee_form.html"
    permission_required = "employees.add_employee"
    page_title = "Add Employee"


class EmployeeUpdateView(EmployeeAccessMixin, EmployeeFormContextMixin, UpdateView):
    model = Employee
    form_class = EmployeeForm
    template_name = "employees/employee_form.html"
    permission_required = "employees.change_employee"
    page_title = "Edit Employee"


class EmployeeSelfServiceProfileView(LoginRequiredMixin, View):
    template_name = "employees/employee_self_service_form.html"

    def dispatch(self, request, *args, **kwargs):
        from core.permissions import is_employee_group_user

        if not is_employee_group_user(request.user):
            raise PermissionDenied("Only users in the Employee group can use profile self-service.")
        return super().dispatch(request, *args, **kwargs)

    def get_employee(self):
        employee = getattr(self.request.user, "employee_profile", None)
        if employee is None:
            raise PermissionDenied("Your account is not linked to an employee profile.")
        return employee

    def get(self, request):
        employee = self.get_employee()
        form = EmployeeSelfServiceForm(instance=employee)
        formset = EmployeeSelfServiceCertificationFormSet(instance=employee, prefix=CERTIFICATION_FORMSET_PREFIX)
        return self.render(form, formset)

    def post(self, request):
        employee = self.get_employee()
        form = EmployeeSelfServiceForm(request.POST, request.FILES, instance=employee)
        formset = EmployeeSelfServiceCertificationFormSet(
            request.POST, request.FILES, instance=employee, prefix=CERTIFICATION_FORMSET_PREFIX,
        )
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                form.save()
                formset.save()
            messages.success(request, "Your profile was updated successfully.")
            return redirect("dashboard")
        return self.render(form, formset)

    def render(self, form, formset):
        return render(self.request, self.template_name, {
            "form": form,
            "cert_formset": formset,
            "cert_perms": {"view": True, "add": True, "change": True, "delete": False},
            "page_title": "Edit My Profile",
            "stored_spouse": bool(form.instance.has_stored_spouse_details),
            "institution": InstitutionProfile.objects.filter(pk=1).first(),
        })


class EmployeeCertificationDownloadView(EmployeeAccessMixin, View):
    """Serve a certificate document only to users who may view certificates."""

    permission_required = "employees.view_employeecertification"

    def has_permission(self):
        if getattr(self.request.user, "is_authenticated", False):
            from core.permissions import is_employee_group_user

            if is_employee_group_user(self.request.user):
                employee = get_linked_employee(self.request.user)
                certification = EmployeeCertification.objects.filter(pk=self.kwargs.get("pk")).first()
                return bool(employee and certification and certification.employee_id == employee.pk)
        return super().has_permission()

    def get(self, request, pk):
        from core.permissions import is_staff_hr_user, staff_hr_employee_ids
        certificates = EmployeeCertification.objects.all()
        if is_staff_hr_user(request.user):
            certificates = certificates.filter(employee_id__in=staff_hr_employee_ids(request.user))
        certification = get_object_or_404(certificates, pk=pk)
        from core.permissions import is_branch_hr_user, resolve_branch_hr_office

        from core.permissions import is_employee_group_user

        if is_employee_group_user(request.user):
            employee = get_linked_employee(request.user)
            if employee is None or certification.employee_id != employee.pk:
                raise Http404()

        if is_branch_hr_user(request.user):
            hr_office = resolve_branch_hr_office(request.user)
            if hr_office is None:
                raise Http404()
            # employee must be in HR office
            today = timezone.localdate()
            emp_ok = Employee.objects.filter(pk=certification.employee_id).filter(
                assignments__is_primary=True,
                assignments__start_date__lte=today,
                assignments__position__office=hr_office,
            ).filter(
                Q(assignments__end_date__isnull=True) | Q(assignments__end_date__gte=today)
            ).exists()
            if not emp_ok:
                raise Http404()
        if not certification.document_file:
            raise Http404("This certificate has no document.")
        try:
            handle = certification.document_file.open("rb")
        except (FileNotFoundError, ValueError):
            raise Http404("The stored document could not be found.")
        filename = certification.display_filename or os.path.basename(certification.document_file.name)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        response = FileResponse(handle, content_type=content_type, as_attachment=False, filename=filename)
        response["X-Content-Type-Options"] = "nosniff"
        return response


class EmployeeDeactivateView(EmployeeAccessMixin, View):
    permission_required = "employees.change_employee"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_office_scoped_admin(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        return get_object_or_404(Employee, pk=self.kwargs["pk"])

    def get(self, request, *args, **kwargs):
        employee = self.get_object()
        if employee.employment_status != Employee.EmploymentStatus.ACTIVE:
            messages.error(request, "Only an active employee can be deactivated.")
            return redirect("employees:employee-detail", pk=employee.pk)
        return render(
            request,
            "core/action_confirm.html",
            {
                "page_title": "Deactivate employee",
                "prompt": "Deactivate this employee account record while preserving its employment history:",
                "object_label": str(employee),
                "warning": "This action prevents the employee from being treated as active; historical assignments, attendance, requests and biometrics are retained.",
                "confirm_label": "Deactivate employee",
                "back_url": reverse("employees:employee-detail", args=[employee.pk]),
            },
        )

    def post(self, request, *args, **kwargs):
        employee = self.get_object()
        if employee.employment_status != Employee.EmploymentStatus.ACTIVE:
            messages.error(request, "Only an active employee can be deactivated.")
            return redirect("employees:employee-detail", pk=employee.pk)
        assignment = employee.assignments.select_related("position__office").order_by(
            "-start_date"
        ).first()
        office = assignment.position.office if assignment else None
        with transaction.atomic():
            log_action(
                actor_user=request.user,
                office=office,
                action=AuditLog.Action.UPDATE,
                entity_type="Employee",
                object_id=employee.pk,
                before_data={"employment_status": employee.employment_status},
                after_data={"employment_status": Employee.EmploymentStatus.INACTIVE},
                reason="Employee deactivated; historical record retained.",
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            employee.employment_status = Employee.EmploymentStatus.INACTIVE
            employee.save(update_fields=["employment_status", "updated_at"])
            from .services.contract_scheduling import snapshot_employee_state, synchronize_employee

            previous_state = snapshot_employee_state(employee)
            previous_state["employment_status"] = Employee.EmploymentStatus.ACTIVE
            synchronize_employee(
                employee,
                actor_user=request.user,
                previous=previous_state,
            )
        messages.success(request, f'"{employee}" was deactivated. Its history was retained.')
        return redirect("employees:employee-detail", pk=employee.pk)

    def get_success_url(self):
        return reverse("employees:employee-detail", args=[self.object.pk])


class AssignmentListView(EmployeeAccessMixin, ListView):
    model = EmployeeAssignment
    template_name = "employees/assignment_list.html"
    context_object_name = "assignments"
    permission_required = "employees.view_employeeassignment"
    paginate_by = 20

    def get_queryset(self):
        queryset = EmployeeAssignment.objects.select_related(
            "employee", "position__office", "position__department", "assigned_by"
        )
        from core.permissions import is_branch_hr_user, resolve_branch_hr_office

        hr_office = resolve_branch_hr_office(self.request.user) if is_branch_hr_user(self.request.user) else None
        if is_branch_hr_user(self.request.user):
            if hr_office is None:
                return EmployeeAssignment.objects.none()
            queryset = queryset.filter(position__office=hr_office)
        from core.permissions import is_employee_group_user, resolve_employee_assignment

        if is_employee_group_user(self.request.user):
            assignment = resolve_employee_assignment(self.request.user)
            if assignment is None:
                return EmployeeAssignment.objects.none()
            queryset = queryset.filter(employee=assignment.employee)
        self._hr_office = hr_office
        # ignore forged office_id param for HR
        query = self.request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(
                Q(employee__employee_id__icontains=query)
                | Q(employee__full_name__icontains=query)
                | Q(position__position_title__icontains=query)
                | Q(position__office__office_name__icontains=query)
            )
        for field in ("position__office_id", "position__department_id", "position_id", "assignment_type", "is_primary"):
            if field == "position__office_id" and hr_office is not None:
                continue
            value = self.request.GET.get(field)
            if value and hr_office is not None and field in ("position__department_id", "position_id"):
                from organization.models import Department, Position

                if field == "position__department_id":
                    if not Department.objects.filter(pk=value, office=hr_office).exists():
                        continue
                else:
                    if not Position.objects.filter(pk=value, office=hr_office).exists():
                        continue
            if value:
                queryset = queryset.filter(**{field: value})
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from core.permissions import is_employee_group_user, is_branch_hr_user

        context.update(
            page_title="Employee Assignments & Transfers",
            search_query=self.request.GET.get("q", ""),
            can_add=self.request.user.has_perm("employees.add_employeeassignment"),
            can_edit=self.request.user.has_perm("employees.change_employeeassignment"),
            offices=(Office.objects.filter(pk=getattr(self, "_hr_office", None).pk) if getattr(self, "_hr_office", None) is not None else Office.objects.order_by("office_name")),
            departments=(Department.objects.filter(office=getattr(self, "_hr_office", None)).order_by("department_name") if getattr(self, "_hr_office", None) is not None else Department.objects.order_by("department_name")),
            positions=(Position.objects.filter(office=getattr(self, "_hr_office", None)).order_by("position_title") if getattr(self, "_hr_office", None) is not None else Position.objects.order_by("position_title")),
            assignment_types=EmployeeAssignment.AssignmentType.choices,
            is_employee=is_employee_group_user(self.request.user),
            is_hr=is_branch_hr_user(self.request.user),
        )
        return context


class AssignmentDetailView(EmployeeAccessMixin, DetailView):
    model = EmployeeAssignment
    template_name = "employees/assignment_detail.html"
    permission_required = "employees.view_employeeassignment"
    
    def get_queryset(self):
        queryset = EmployeeAssignment.objects.select_related(
            "employee", "position__office", "position__department", "assigned_by"
        )
        from core.permissions import is_branch_hr_user, resolve_branch_hr_office

        if is_branch_hr_user(self.request.user):
            hr_office = resolve_branch_hr_office(self.request.user)
            if hr_office is None:
                return EmployeeAssignment.objects.none()
            queryset = queryset.filter(position__office=hr_office)
        from core.permissions import is_employee_group_user, resolve_employee_assignment

        if is_employee_group_user(self.request.user):
            assignment = resolve_employee_assignment(self.request.user)
            if assignment is None:
                return EmployeeAssignment.objects.none()
            queryset = queryset.filter(employee=assignment.employee)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            page_title="Assignment Details",
            can_edit=self.request.user.has_perm("employees.change_employeeassignment"),
        )
        return context


class AssignmentFormContextMixin:
    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        # Cache for form_valid's previous-state snapshot (UpdateView only).
        self.object = obj
        return obj

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            page_title=self.page_title,
            back_url=reverse("employees:assignment-list"),
        )
        return context
        context = super().get_context_data(**kwargs)
        context.update(
            page_title=self.page_title,
            back_url=reverse("employees:assignment-list"),
        )
        return context

    def form_valid(self, form):
        is_create = self.object is None
        old_data = {}
        if not is_create and self.object.pk:
            old_assignment = EmployeeAssignment.objects.get(pk=self.object.pk)
            old_data = {
                "employee_id": old_assignment.employee_id,
                "position_id": old_assignment.position_id,
                "assignment_type": old_assignment.assignment_type,
                "start_date": str(old_assignment.start_date),
                "end_date": str(old_assignment.end_date) if old_assignment.end_date else None,
                "is_primary": old_assignment.is_primary,
            }
        if not form.instance.assigned_by_id:
            form.instance.assigned_by = self.request.user
        previous_state = None
        if self.object is not None and self.object.pk:
            from .services.contract_scheduling import snapshot_employee_state
            previous_state = snapshot_employee_state(self.object.employee)
        messages.success(self.request, "Employee assignment saved.")
        response = super().form_valid(form)
        new_assignment = self.object
        new_data = {
            "employee_id": new_assignment.employee_id,
            "position_id": new_assignment.position_id,
            "assignment_type": new_assignment.assignment_type,
            "start_date": str(new_assignment.start_date),
            "end_date": str(new_assignment.end_date) if new_assignment.end_date else None,
            "is_primary": new_assignment.is_primary,
        }
        office = new_assignment.position.office
        log_action(
            actor_user=self.request.user,
            office=office,
            action=AuditLog.Action.CREATE if is_create else AuditLog.Action.UPDATE,
            entity_type="EmployeeAssignment",
            object_id=new_assignment.pk,
            before_data=old_data,
            after_data=new_data,
            ip_address=self.request.META.get("REMOTE_ADDR"),
        )
        self._synchronize_contract_schedules(previous_state)
        return response

    def _synchronize_contract_schedules(self, previous_state):
        assignment = self.object
        if assignment is None or not getattr(assignment, "employee_id", None):
            return
        from .services.contract_scheduling import synchronize_employee

        try:
            result = synchronize_employee(assignment.employee, actor_user=self.request.user, previous=previous_state)
        except Exception:
            logger.exception("Contract scheduling failed for assignment %s", getattr(assignment, "pk", None))
            return
        if result is None:
            return
        if result.pending_reasons and not result.created_count:
            messages.warning(self.request, "Automatic scheduling is pending: " + result.pending_reason)
        elif result.created_count:
            messages.success(
                self.request,
                f"Automatic scheduling created {result.created_count} schedule(s) "
                f"({result.skipped_existing_count} already present, {result.conflict_count} conflicts).",
            )
        if result.cancelled_count:
            messages.info(self.request, f"Automatic scheduling cancelled {result.cancelled_count} future schedule(s).")
        for blocker in result.blocking_conflicts:
            messages.error(self.request, blocker)


class AssignmentCreateView(EmployeeAccessMixin, AssignmentFormContextMixin, CreateView):
    model = EmployeeAssignment
    form_class = EmployeeAssignmentForm
    template_name = "employees/assignment_form.html"
    permission_required = "employees.add_employeeassignment"
    page_title = "Add Assignment or Transfer"
    success_url = reverse_lazy("employees:assignment-list")


class AssignmentUpdateView(EmployeeAccessMixin, AssignmentFormContextMixin, UpdateView):
    model = EmployeeAssignment
    form_class = EmployeeAssignmentForm
    template_name = "employees/assignment_form.html"
    permission_required = "employees.change_employeeassignment"
    page_title = "Edit Assignment"

    def get_success_url(self):
        return reverse("employees:assignment-detail", args=[self.object.pk])


