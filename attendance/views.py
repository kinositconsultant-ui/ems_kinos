import base64
import math
import binascii
import csv
import json
import logging
import uuid
import re
from types import SimpleNamespace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

try:
    import cv2
except ImportError:  # pragma: no cover - optional dependency is validated elsewhere.
    cv2 = None

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional dependency is validated elsewhere.
    np = None

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Count, F, Prefetch, Q, Sum
from django.db.models.deletion import ProtectedError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import urlencode
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from audittrail.models import AuditLog
from audittrail.services import log_action
from core.permissions import (
    can_manage_groups,
    effective_primary_assignments,
    offices_for_user,
    require_office_scoped_admin,
)
from employee_requests.models import EmployeeRequest
from employees.models import Employee, EmployeeAssignment
from organization.models import Department, Office

from .forms import (
    AttendanceDeviceForm,
    AttendancePolicyForm,
    AttendanceReportFilterForm,
    EmployeeScheduleForm,
    HolidayForm,
    ShiftForm,
    eligible_assignments,
    eligible_shifts,
)
from .models import AttendancePolicy, EmployeeSchedule, Holiday, Shift
from .models import (
    AttendanceDevice,
    AttendanceEvent,
    AttendancePersistenceFailure,
    AttendanceRecord,
    FaceTemplate,
    FaceVerificationLog,
)
from .services.face_encryption import encrypt_embedding
from .services.face_enrollment import build_enrollment_embedding
from .services.face_encryption import get_encryption_key
from .services.face_engine import FaceEngine, FaceEngineError, get_face_model_status
from .services.face_attendance import verify_attendance_frame
from .services.face_liveness import get_face_liveness_status
from .services.scheduling import (
    ShiftTimingError,
    assignment_position_label,
    build_schedule_snapshot,
    format_local,
    format_minutes,
    holidays_on,
    overlapping_schedules,
    policy_for,
    sync_holiday_schedules,
    sync_shift_general_schedule_changes,
)
from .services.workflow import (
    BREAK_OUT,
    CHECK_IN,
    CHECKOUT_GRACE_MINUTES,
    LABELS,
    EventRejected,
    apply_event_to_record,
    decide_next_event,
    next_event_type,
    recalculate_record,
    schedule_candidates,
    session_state,
)


logger = logging.getLogger(__name__)


def _sanitized_persistence_summary(exc, limit=500):
    summary = " ".join(str(exc).split())
    summary = re.sub(r"[A-Za-z]:[\\/][^\s]+", "[path]", summary)
    summary = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "[address]", summary)
    return summary[:limit]


def _persist_attendance_failure(*, incident_id, employee, schedule, record, event_type,
                                channel, stage, exc):
    diagnostic = AttendancePersistenceFailure(
        incident_id=incident_id,
        employee=employee,
        schedule=schedule,
        attendance_record=record,
        expected_event_type=event_type or "",
        channel=channel,
        persistence_stage=stage,
        exception_type=type(exc).__name__,
        diagnostic_summary=_sanitized_persistence_summary(exc),
    )
    diagnostic.full_clean()
    diagnostic.save(force_insert=True)


def _face_yaw_measurement(face_row):
    row = np.asarray(face_row, dtype=np.float32).reshape(-1)
    if row.size < 15 or not np.all(np.isfinite(row[4:14])):
        raise ValueError("The detected face landmarks are incomplete for the head-turn challenge.")
    right_eye = row[4:6]
    left_eye = row[6:8]
    nose = row[8:10]
    eye_midpoint = (right_eye + left_eye) / 2
    eye_distance = float(np.linalg.norm(right_eye - left_eye))
    if eye_distance <= 0:
        raise ValueError("The detected eye landmarks are too close to validate the head-turn challenge.")
    return float((nose[0] - eye_midpoint[0]) / eye_distance)


class AttendanceAccessMixin(LoginRequiredMixin, PermissionRequiredMixin):
    raise_exception = True


class KioskAccessMixin(AttendanceAccessMixin):
    def dispatch(self, request, *args, **kwargs):
        from core.permissions import is_branch_hr_user, is_employee_group_user

        if getattr(self, "mobile_mode", False):
            return super().dispatch(request, *args, **kwargs)
        if is_employee_group_user(request.user) or is_branch_hr_user(request.user):
            raise PermissionDenied("Kiosk management and attendance access is not available to this account.")
        return super().dispatch(request, *args, **kwargs)


class ModelListView(AttendanceAccessMixin, ListView):
    template_name = "attendance/model_list.html"
    context_object_name = "items"
    paginate_by = 20
    permission_required = ""
    page_title = ""
    page_description = ""
    columns = ()
    detail_route = ""
    create_route = ""
    create_permission = ""
    edit_route = ""
    edit_permission = ""
    filters = ()
    date_filter_fields = {}

    def get_queryset(self):
        from core.permissions import (
            is_branch_hr_user,
            is_employee_group_user,
            resolve_branch_hr_office,
            resolve_employee_assignment,
        )

        hr_office = resolve_branch_hr_office(self.request.user) if is_branch_hr_user(self.request.user) else None
        if is_branch_hr_user(self.request.user) and hr_office is None:
            return self.model.objects.none()
        employee_assignment = (
            resolve_employee_assignment(self.request.user)
            if is_employee_group_user(self.request.user)
            else None
        )
        if is_employee_group_user(self.request.user) and employee_assignment is None:
            return self.model.objects.none()

        queryset = super().get_queryset()
        query = self.request.GET.get("q", "").strip()
        if query and self.search_fields:
            search = Q()
            for field in self.search_fields:
                search |= Q(**{f"{field}__icontains": query})
            queryset = queryset.filter(search)
        for name, lookup, _label, _choices in self.filters:
            if is_employee_group_user(self.request.user) and name in {
                "office_id", "department_id", "employee_id",
            }:
                continue
            if self.model.__name__ == "Holiday" and name == "office_id":
                continue
            if is_branch_hr_user(self.request.user) and name == "office_id":
                # Never trust supplied office id; force branch HR office.
                queryset = queryset.filter(**{lookup: hr_office.pk})
                continue
            value = self.request.GET.get(name, "")
            if value:
                if is_branch_hr_user(self.request.user):
                    from employees.models import Employee
                    from organization.models import Department, Position
                    from attendance.models import Shift

                    allowed = True
                    if name == "department_id":
                        allowed = Department.objects.filter(pk=value, office=hr_office).exists()
                    elif name == "employee_id":
                        allowed = Employee.objects.filter(
                            pk=value,
                            assignments__is_primary=True,
                            assignments__position__office=hr_office,
                        ).exists()
                    elif name == "position_id":
                        allowed = Position.objects.filter(pk=value, office=hr_office).exists()
                    elif name == "shift_id":
                        allowed = Shift.objects.filter(pk=value, office=hr_office).exists()
                    if not allowed:
                        continue
                queryset = queryset.filter(**{lookup: value})
        # Direct office scoping for models that own an office.
        if is_branch_hr_user(self.request.user) and hr_office is not None:
            model_fields = {f.name for f in self.model._meta.fields}
            if "office" in model_fields and not any(f[0] == "office_id" for f in self.filters):
                queryset = queryset.filter(office=hr_office)
        start = self.request.GET.get("start_date", "")
        end = self.request.GET.get("end_date", "")
        if start and "start_date" in self.date_filter_fields:
            queryset = queryset.filter(**{f"{self.date_filter_fields['start_date']}__gte": start})
        if end and "end_date" in self.date_filter_fields:
            queryset = queryset.filter(**{f"{self.date_filter_fields['end_date']}__lte": end})
        # Model-specific branch scoping for branch HR (Staff HR / legacy HR Office).
        from core.permissions import is_branch_hr_user, resolve_branch_hr_office

        if is_branch_hr_user(self.request.user):
            hr_office = resolve_branch_hr_office(self.request.user)
            if hr_office is not None:
                model_name = self.model.__name__
                if model_name == "EmployeeSchedule":
                    queryset = queryset.filter(employee_assignment__position__office=hr_office)
                elif model_name == "AttendanceRecord":
                    queryset = queryset.filter(schedule__employee_assignment__position__office=hr_office)
                elif model_name == "AttendanceEvent":
                    queryset = queryset.filter(attendance_record__schedule__employee_assignment__position__office=hr_office)
                elif model_name == "Holiday":
                    queryset = queryset.filter(Q(office=hr_office) | Q(office__isnull=True))
                elif model_name in {"FaceTemplate", "FaceVerificationLog"}:
                    employee_lookup = "employee" if model_name == "FaceTemplate" else "employee"
                    queryset = queryset.filter(
                        **{f"{employee_lookup}__assignments__position__office": hr_office,
                           f"{employee_lookup}__assignments__is_primary": True}
                    ).distinct()
        if is_employee_group_user(self.request.user) and employee_assignment is not None:
            employee = employee_assignment.employee
            office = employee_assignment.position.office
            model_name = self.model.__name__
            if model_name == "EmployeeSchedule":
                queryset = queryset.filter(employee_assignment__employee=employee)
            elif model_name in {"AttendanceRecord", "AttendanceRecordDetailView"}:
                queryset = queryset.filter(schedule__employee_assignment__employee=employee)
            elif model_name == "AttendanceEvent":
                queryset = queryset.filter(attendance_record__schedule__employee_assignment__employee=employee)
            elif model_name == "Holiday":
                queryset = queryset.filter(
                    Q(office=employee_assignment.position.office) | Q(office__isnull=True)
                )
            elif model_name in {"FaceTemplate", "FaceVerificationLog"}:
                queryset = queryset.filter(employee=employee)
            else:
                model_fields = {field.name for field in self.model._meta.fields}
                if "office" in model_fields:
                    queryset = queryset.filter(office=office)
                elif model_name in {"Shift", "AttendanceDevice", "Holiday", "AttendancePolicy"}:
                    queryset = queryset.filter(office=office)
        return queryset


    @staticmethod
    def value_for(obj, field):
        if field == "office" and hasattr(obj, "office_id") and obj.office_id is None:
            return "All Offices"
        value = obj
        for part in field.split("__"):
            value = getattr(value, part, None) if value is not None else None
        if value is None or value == "":
            return "—"
        if "__" not in field and hasattr(obj, f"get_{field}_display"):
            value = getattr(obj, f"get_{field}_display")()
        return value

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from core.permissions import is_branch_hr_user, is_employee_group_user, resolve_branch_hr_office
        from core.permissions import is_director_user, is_xefe_user, resolve_director_office, resolve_xefe_assignment

        hr_office = resolve_branch_hr_office(self.request.user) if is_branch_hr_user(self.request.user) else None
        self._hr_office = hr_office
        director_only = is_director_user(self.request.user)
        xefe_only = is_xefe_user(self.request.user)
        director_office = resolve_director_office(self.request.user) if director_only else None
        xefe_assignment = resolve_xefe_assignment(self.request.user) if xefe_only else None
        from attendance.models import AttendanceDevice, Shift
        from employees.models import Employee
        from organization.models import Department, Position

        removed_names = set()
        if director_only or xefe_only:
            removed_names.add("office_id")
        if xefe_only:
            removed_names.add("department_id")

        filter_controls = [
            {
                "name": name,
                "label": label,
                "value": self.request.GET.get(name, ""),
                "choices": (
                    Department.objects.filter(office=director_office).values_list("pk", "department_name")
                    if director_only and director_office is not None and name == "department_id"
                    else Department.objects.filter(office=hr_office).values_list("pk", "department_name")
                    if hr_office is not None and name == "department_id"
                    else Employee.objects.filter(
                        assignments__is_primary=True,
                        assignments__position__office=hr_office,
                    ).distinct().values_list("pk", "full_name")
                    if hr_office is not None and name == "employee_id"
                    else Position.objects.filter(office=hr_office).values_list("pk", "position_title")
                    if hr_office is not None and name == "position_id"
                    else Shift.objects.filter(office=hr_office).values_list("pk", "name")
                    if hr_office is not None and name == "shift_id"
                    else Shift.objects.filter(office=xefe_assignment.position.office).values_list("pk", "name")
                    if xefe_only and xefe_assignment is not None and name == "shift_id"
                    else AttendanceDevice.objects.filter(office=hr_office).values_list("pk", "device_name")
                    if hr_office is not None and name in {"device_id", "attendance_device_id"}
                    else choices() if callable(choices) else choices
                ),
            }
            for name, _lookup, label, choices in self.filters
            if name not in removed_names
            and not (
                is_employee_group_user(self.request.user)
                and name in {"office_id", "department_id", "employee_id"}
            )
            and not (
                is_branch_hr_user(self.request.user) and name == "office_id"
            )
        ]
        context.update(
            page_title=self.page_title,
            page_description=self.page_description,
            search_query=self.request.GET.get("q", ""),
            create_url=reverse(self.create_route) if self.create_route else None,
            can_create=bool(self.create_permission) and self.request.user.has_perm(self.create_permission),
            filter_controls=filter_controls,
            start_date=self.request.GET.get("start_date", ""),
            end_date=self.request.GET.get("end_date", ""),
            show_date_filters=bool(self.date_filter_fields),
            columns=self.columns,
            querystring=self.request.GET.copy(),
        )
        query_params = self.request.GET.copy()
        query_params.pop("page", None)
        for removed in removed_names:
            query_params.pop(removed, None)
        context["querystring"] = urlencode(query_params)
        context["table_rows"] = [
            {
                "cells": [self.value_for(item, field) for _label, field in self.columns],
                "url": reverse(self.detail_route, args=[item.pk]),
                "edit_url": (
                    reverse(self.edit_route, args=[item.pk])
                    if self.edit_route and self.request.user.has_perm(self.edit_permission)
                    else None
                ),
                "actions": self.get_row_actions(item),
            }
            for item in context["items"]
        ]
        return context

    def get_row_actions(self, item):
        return []


class ModelDetailView(AttendanceAccessMixin, DetailView):
    template_name = "attendance/model_detail.html"
    permission_required = ""
    page_title = ""
    detail_fields = ()
    back_route = ""
    def get_queryset(self):
        queryset = super().get_queryset()
        from core.permissions import (
            is_employee_group_user,
            is_branch_hr_user,
            resolve_employee_assignment,
            resolve_branch_hr_office,
        )


        employee_assignment = (
            resolve_employee_assignment(self.request.user)
            if is_employee_group_user(self.request.user)
            else None
        )
        if is_employee_group_user(self.request.user):
            if employee_assignment is None:
                return self.model.objects.none()
            employee = employee_assignment.employee
            office = employee_assignment.position.office
            model_name = self.model.__name__
            if model_name == "EmployeeSchedule":
                return queryset.filter(employee_assignment__employee=employee)
            if model_name == "AttendanceRecord":
                return queryset.filter(schedule__employee_assignment__employee=employee)
            if model_name == "AttendanceEvent":
                return queryset.filter(attendance_record__schedule__employee_assignment__employee=employee)
            if model_name == "Holiday":
                return queryset.filter(
                    Q(office=employee_assignment.position.office) | Q(office__isnull=True)
                )
            if model_name in {"FaceTemplate", "FaceVerificationLog"}:
                return queryset.filter(employee=employee)
            model_fields = {field.name for field in self.model._meta.fields}
            if "office" in model_fields:
                return queryset.filter(office=office)
            return queryset.none()

        if not is_branch_hr_user(self.request.user):
            return queryset
        hr_office = resolve_branch_hr_office(self.request.user)
        if hr_office is None:
            return self.model.objects.none()
        model_name = self.model.__name__
        if model_name == "EmployeeSchedule":
            return queryset.filter(employee_assignment__position__office=hr_office)
        if model_name == "AttendanceRecord":
            return queryset.filter(schedule__employee_assignment__position__office=hr_office)
        if model_name == "AttendanceEvent":
            return queryset.filter(attendance_record__schedule__employee_assignment__position__office=hr_office)
        if model_name == "Holiday":
            return queryset.filter(Q(office=hr_office) | Q(office__isnull=True))
        if model_name in {"FaceTemplate", "FaceVerificationLog"}:
            return queryset.filter(
                employee__assignments__is_primary=True,
                employee__assignments__position__office=hr_office,
            ).distinct()
        model_fields = {f.name for f in self.model._meta.fields}
        if "office" in model_fields:
            return queryset.filter(office=hr_office)
        return queryset.none()


    edit_route = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            page_title=self.page_title,
            details=[
                (label, ModelListView.value_for(self.object, field))
                for label, field in self.detail_fields
            ],
            back_url=reverse(self.back_route),
            edit_url=reverse(self.edit_route, args=[self.object.pk]) if self.edit_route else None,
            can_edit=bool(self.edit_route)
            and self.request.user.has_perm(self.permission_required.replace("view_", "change_")),
        )
        return context


class AttendanceDeactivateView(AttendanceAccessMixin, View):
    model = None
    state_field = "is_active"
    inactive_value = False
    permission_required = ""
    back_route = ""
    action_label = "Deactivate"
    action_reason = "Record deactivated; historical data retained."

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            from core.permissions import is_branch_hr_user
            if not is_branch_hr_user(request.user):
                require_office_scoped_admin(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        obj = get_object_or_404(self.model, pk=self.kwargs["pk"])
        from core.permissions import is_branch_hr_user, resolve_branch_hr_office
        if is_branch_hr_user(self.request.user):
            office = resolve_branch_hr_office(self.request.user)
            if office is None:
                raise PermissionDenied("Your branch scope could not be resolved.")
            # Block global/shared records that affect all offices for branch HR.
            obj_office = getattr(obj, "office", None)
            model_name = self.model.__name__
            if model_name in {"Holiday", "AttendancePolicy"}:
                if obj_office is None or obj_office.pk != office.pk:
                    raise PermissionDenied("You may only manage records in your authorized branch.")
            elif obj_office is not None and obj_office.pk != office.pk:
                raise PermissionDenied("You may only manage records in your authorized branch.")
        return obj

    def _is_active(self, item):
        value = getattr(item, self.state_field)
        return value is not False and value != self.inactive_value

    def get(self, request, *args, **kwargs):
        item = self.get_object()
        if not self._is_active(item):
            messages.error(request, f"This {self.model._meta.verbose_name} is already inactive.")
            return redirect(self.back_route)
        return render(
            request,
            "core/action_confirm.html",
            {
                "page_title": f"{self.action_label} {self.model._meta.verbose_name}",
                "prompt": f"{self.action_label} this record while retaining historical references:",
                "object_label": str(item),
                "warning": "This status change is recorded in the audit trail; history will not be deleted.",
                "confirm_label": self.action_label,
                "back_url": reverse(self.back_route),
            },
        )

    def post(self, request, *args, **kwargs):
        item = self.get_object()
        with transaction.atomic():
            item = self.model.objects.select_for_update().get(pk=item.pk)
            if not self._is_active(item):
                messages.error(request, f"This {self.model._meta.verbose_name} is already inactive.")
                return redirect(self.back_route)
            previous_value = getattr(item, self.state_field)
            office = getattr(item, "office", None)
            log_action(
                actor_user=request.user,
                office=office,
                action=AuditLog.Action.UPDATE,
                entity_type=self.model.__name__,
                object_id=item.pk,
                before_data={self.state_field: previous_value},
                after_data={self.state_field: self.inactive_value},
                reason=self.action_reason,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            setattr(item, self.state_field, self.inactive_value)
            update_fields = [self.state_field]
            if any(field.name == "updated_at" for field in item._meta.fields):
                update_fields.append("updated_at")
            item.save(update_fields=update_fields)
        messages.success(request, f"{item} was {self.action_label.lower()}d.")
        return redirect(self.back_route)


class AttendanceDeleteView(AttendanceAccessMixin, View):
    model = None
    permission_required = ""
    back_route = ""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            from core.permissions import is_branch_hr_user
            if not is_branch_hr_user(request.user):
                require_office_scoped_admin(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        obj = get_object_or_404(self.model, pk=self.kwargs["pk"])
        from core.permissions import is_branch_hr_user, resolve_branch_hr_office
        if is_branch_hr_user(self.request.user):
            office = resolve_branch_hr_office(self.request.user)
            if office is None:
                raise PermissionDenied("Your branch scope could not be resolved.")
            obj_office = getattr(obj, "office", None)
            model_name = self.model.__name__
            if model_name in {"Holiday", "AttendancePolicy"}:
                if obj_office is None or obj_office.pk != office.pk:
                    raise PermissionDenied("You may only manage records in your authorized branch.")
            elif obj_office is not None and obj_office.pk != office.pk:
                raise PermissionDenied("You may only manage records in your authorized branch.")
        return obj

    @staticmethod
    def has_references(item):
        return any(
            relation.related_model.objects.filter(
                **{relation.field.attname: item.pk}
            ).exists()
            for relation in item._meta.related_objects
        )

    def get(self, request, *args, **kwargs):
        item = self.get_object()
        if self.has_references(item):
            messages.error(
                request,
                f"This {self.model._meta.verbose_name} is referenced by historical data and cannot be deleted.",
            )
            return redirect(self.back_route)
        return render(
            request,
            "core/action_confirm.html",
            {
                "page_title": f"Delete {self.model._meta.verbose_name}",
                "prompt": "Permanently delete this unused master record:",
                "object_label": str(item),
                "warning": "This action cannot be undone. Any record with dependent history is protected.",
                "confirm_label": "Delete permanently",
                "back_url": reverse(self.back_route),
            },
        )

    def post(self, request, *args, **kwargs):
        try:
            with transaction.atomic():
                item = self.model.objects.select_for_update().get(
                    pk=self.kwargs["pk"]
                )
                if self.has_references(item):
                    messages.error(
                        request,
                        f"This {self.model._meta.verbose_name} is referenced by historical data and cannot be deleted.",
                    )
                    return redirect(self.back_route)
                log_action(
                    actor_user=request.user,
                    office=getattr(item, "office", None),
                    action=AuditLog.Action.DELETE,
                    entity_type=self.model.__name__,
                    object_id=item.pk,
                    before_data={"label": str(item)},
                    reason="Unused attendance master record deleted from the management interface.",
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                item.delete()
        except self.model.DoesNotExist:
            raise Http404
        except (ProtectedError, IntegrityError):
            messages.error(
                request,
                "This record is in use and cannot be deleted. Deactivate it instead.",
            )
            return redirect(self.back_route)
        messages.success(request, f'"{item}" was deleted.')
        return redirect(self.back_route)


class ShiftDeactivateView(AttendanceDeactivateView):
    model = Shift
    permission_required = "attendance.change_shift"
    back_route = "attendance:shift-list"

    def post(self, request, *args, **kwargs):
        item = self.get_object()
        response = super().post(request, *args, **kwargs)
        item.refresh_from_db()
        sync_shift_general_schedule_changes(item, actor_user=request.user)
        return response


class HolidayDeactivateView(AttendanceDeactivateView):
    model = Holiday
    permission_required = "attendance.change_holiday"
    back_route = "attendance:holiday-list"

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        res = sync_holiday_schedules(actor_user=request.user)
        if res.restored_count > 0:
            messages.info(request, f"{res.restored_count} future schedule(s) restored after holiday deactivation.")
        return response



class AttendancePolicyDeactivateView(AttendanceDeactivateView):
    model = AttendancePolicy
    permission_required = "attendance.change_attendancepolicy"
    back_route = "attendance:policy-list"


class AttendanceDeviceDeactivateView(KioskAccessMixin, AttendanceDeactivateView):
    model = AttendanceDevice
    state_field = "status"
    inactive_value = AttendanceDevice.Status.INACTIVE
    permission_required = "attendance.change_attendancedevice"
    back_route = "attendance:device-list"
    action_reason = "Attendance device deactivated; verification history retained."


class ShiftDeleteView(AttendanceDeleteView):
    model = Shift
    permission_required = "attendance.delete_shift"
    back_route = "attendance:shift-list"


class AttendanceDeviceDeleteView(KioskAccessMixin, AttendanceDeleteView):
    model = AttendanceDevice
    permission_required = "attendance.delete_attendancedevice"
    back_route = "attendance:device-list"


class ModelFormContextMixin:
    template_name = "attendance/model_form.html"
    page_title = ""
    back_route = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context.get("form")
        context.update(
            page_title=self.page_title,
            back_url=reverse(self.back_route),
            show_preview=hasattr(form, "summary_rows"),
            holiday_notice=getattr(form, "holiday_notice", []),
        )
        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        if getattr(self, "form_receives_user", False):
            kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        is_create = self.object is None
        old_data = {}
        if not is_create and self.object.pk:
            old_obj = self.model.objects.get(pk=self.object.pk)
            old_data = {"label": str(old_obj)}
        if self.request.POST.get("action") == "preview" and hasattr(form, "summary_rows"):
            # Show the summary first; nothing is saved until HR confirms with Save.
            return self.render_to_response(self.get_context_data(form=form, summary_rows=form.summary_rows()))
        if hasattr(form.instance, "assigned_by_id") and not form.instance.assigned_by_id:
            form.instance.assigned_by = self.request.user
        if hasattr(form.instance, "created_by_id") and not form.instance.created_by_id:
            form.instance.created_by = self.request.user
        if hasattr(form.instance, "updated_by_id"):
            form.instance.updated_by = self.request.user
        for holiday in getattr(form, "holiday_notice", []):
            messages.warning(
                self.request,
                f"Note: {form.instance.work_date} is a holiday ({holiday.name}, {holiday.scope_label}). "
                "The schedule was saved because working on holidays is allowed.",
            )
        response = super().form_valid(form)
        messages.success(self.request, f"{self.page_title} saved successfully.")
        office = getattr(self.object, "office", None) or getattr(getattr(self.object, "shift", None), "office", None)
        log_action(
            actor_user=self.request.user,
            office=office,
            action=AuditLog.Action.CREATE if is_create else AuditLog.Action.UPDATE,
            entity_type=self.model.__name__,
            object_id=self.object.pk,
            before_data=old_data,
            after_data={"label": str(self.object)},
            ip_address=self.request.META.get("REMOTE_ADDR"),
        )
        return response


class ModelCreateView(AttendanceAccessMixin, ModelFormContextMixin, CreateView):
    permission_required = ""
    list_route = ""
    success_url = ""

    def get_success_url(self):
        return reverse(self.success_url or self.list_route)


class ModelUpdateView(AttendanceAccessMixin, ModelFormContextMixin, UpdateView):
    permission_required = ""
    list_route = ""
    detail_route = ""

    def get_success_url(self):
        return reverse(self.detail_route, args=[self.object.pk])


class ShiftListView(ModelListView):
    model = Shift
    permission_required = "attendance.view_shift"
    page_title = "Shifts"
    page_description = "Configure work shifts and check-in windows."
    search_fields = ("shift_code", "name", "office__office_name")
    columns = (
        ("Code", "shift_code"), ("Shift", "name"), ("Office", "office__office_name"),
        ("Working time", "time_range"), ("Break", "break_range"),
        ("Scheduled work", "work_duration_display"), ("Grace (min)", "grace_period_minutes"),
        ("Early check-in (min)", "early_checkin_window_minutes"), ("Active", "is_active"),
    )
    detail_route = "attendance:shift-detail"
    create_route = "attendance:shift-create"
    create_permission = "attendance.add_shift"
    filters = (
        ("office_id", "office_id", "Office", lambda: Office.objects.order_by("office_name").values_list("id", "office_name")),
        ("is_active", "is_active", "Status", (("True", "Active"), ("False", "Inactive"))),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("office")

    def get_row_actions(self, item):
        if (
            not AttendanceDeleteView.has_references(item)
            and self.request.user.is_superuser
            and self.request.user.has_perm("attendance.delete_shift")
        ):
            return [{
                "url": reverse("attendance:shift-delete", args=[item.pk]),
                "label": "Delete",
                "icon": "bi-trash",
            }]
        if (
            item.is_active
            and AttendanceDeleteView.has_references(item)
            and self.request.user.is_superuser
            and self.request.user.has_perm("attendance.change_shift")
        ):
            return [{
                "url": reverse("attendance:shift-deactivate", args=[item.pk]),
                "label": "Deactivate",
                "icon": "bi-toggle-off",
            }]
        return []


class ShiftDetailView(ModelDetailView):
    model = Shift
    permission_required = "attendance.view_shift"
    page_title = "Shift Details"
    detail_route = ""
    back_route = "attendance:shift-list"
    edit_route = "attendance:shift-update"
    detail_fields = (
        ("Code", "shift_code"), ("Name", "name"), ("Office", "office"),
        ("Working time", "time_range"), ("Break", "break_range"),
        ("Scheduled work duration", "work_duration_display"),
        ("Grace period (minutes)", "grace_period_minutes"),
        ("Early check-in (minutes before start)", "early_checkin_window_minutes"),
        ("Recurring schedule", "recurring_schedule_display"),
        ("Recurrence period", "recurrence_period_display"),
        ("Working days", "working_days_display"),
        ("Target employees", "target_assignments_display"),
        ("Active", "is_active"), ("Created", "created_at"), ("Updated", "updated_at"),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("office")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            can_delete=(
                not AttendanceDeleteView.has_references(self.object)
                and self.request.user.is_superuser
                and self.request.user.has_perm("attendance.delete_shift")
            ),
            delete_url=reverse(
                "attendance:shift-delete", args=[self.object.pk]
            ),
            can_deactivate=(
                self.object.is_active
                and AttendanceDeleteView.has_references(self.object)
                and self.request.user.is_superuser
                and self.request.user.has_perm("attendance.change_shift")
            ),
            action_url=reverse(
                "attendance:shift-deactivate", args=[self.object.pk]
            ),
            action_label="Deactivate shift",
        )
        return context


class ShiftCreateView(ModelCreateView):
    template_name = "attendance/shift_form.html"
    form_receives_user = True
    model = Shift
    form_class = ShiftForm
    permission_required = "attendance.add_shift"
    page_title = "Add Shift"
    back_route = "attendance:shift-list"
    list_route = "attendance:shift-list"

    def form_valid(self, form):
        response = super().form_valid(form)
        shift = self.object
        if shift.recurring_schedule_enabled:
            result = sync_shift_general_schedule_changes(shift, actor_user=self.request.user)
            messages.info(
                self.request,
                f"General Schedule generated {result.created_count} routine schedule(s) "
                f"({result.skipped_holiday_count} holidays skipped, {result.conflict_count} conflicts reported).",
            )
        return response


class ShiftUpdateView(ModelUpdateView):
    template_name = "attendance/shift_form.html"
    form_receives_user = True
    model = Shift
    form_class = ShiftForm
    permission_required = "attendance.change_shift"
    page_title = "Edit Shift"
    back_route = "attendance:shift-list"
    detail_route = "attendance:shift-detail"
    queryset = Shift.objects.select_related("office")

    def form_valid(self, form):
        response = super().form_valid(form)
        shift = self.object
        result = sync_shift_general_schedule_changes(shift, actor_user=self.request.user)
        if shift.recurring_schedule_enabled:
            messages.info(
                self.request,
                f"General Schedule synced: {result.created_count} schedule(s) created, "
                f"{result.cancelled_count} cancelled, {result.restored_count} restored.",
            )
        elif result.cancelled_count > 0:
            messages.info(
                self.request,
                f"General Schedule disabled: {result.cancelled_count} future routine schedule(s) cancelled.",
            )
        return response


class ScheduleListView(ModelListView):
    model = EmployeeSchedule
    permission_required = "attendance.view_employeeschedule"
    page_title = "Employee Schedules"
    page_description = "Planned shifts per employee and date. These are planned times, not attendance records."
    search_fields = (
        "employee_assignment__employee__employee_id",
        "employee_assignment__employee__full_name",
        "shift__name",
    )
    columns = (
        ("Employee ID / Name", "employee_label"),
        ("Office", "shift__office__office_name"), ("Shift", "shift__name"),
        ("Work date", "work_date"), ("Scheduled start", "scheduled_start_display"),
        ("Break", "break_display"), ("Scheduled end", "scheduled_end_display"),
        ("Source", "get_schedule_source_display"),
        ("Schedule status", "status"),
    )
    detail_route = "attendance:schedule-detail"
    edit_route = "attendance:schedule-update"
    edit_permission = "attendance.change_employeeschedule"
    create_route = "attendance:schedule-create"
    create_permission = "attendance.add_employeeschedule"
    filters = (
        ("office_id", "shift__office_id", "Office", lambda: Office.objects.order_by("office_name").values_list("id", "office_name")),
        ("department_id", "employee_assignment__position__department_id", "Department", lambda: Department.objects.order_by("department_name").values_list("id", "department_name")),
        ("employee_id", "employee_assignment__employee_id", "Employee", lambda: [
            (pk, f"{code} — {name}")
            for pk, code, name in Employee.objects.order_by("employee_id").values_list("id", "employee_id", "full_name")
        ]),
        ("shift_id", "shift_id", "Shift", lambda: Shift.objects.order_by("name").values_list("id", "name")),
        ("schedule_source", "schedule_source", "Source", EmployeeSchedule.ScheduleSource.choices),
        ("status", "status", "Status", EmployeeSchedule.Status.choices),
    )
    date_filter_fields = {"start_date": "work_date", "end_date": "work_date"}

    def get_queryset(self):
        from core.permissions import (
            DIRECTOR_GROUP, STAFF_HR_GROUP, resolve_director_office,
            resolve_staff_hr_office,
        )

        director_office = (
            resolve_director_office(self.request.user)
            if self.request.user.groups.filter(name=DIRECTOR_GROUP).exists()
            else None
        )
        queryset = super().get_queryset().select_related(
            "employee_assignment__employee", "employee_assignment__position__office",
            "employee_assignment__position__department", "shift__office",
        )
        if self.request.user.groups.filter(name=DIRECTOR_GROUP).exists():
            if director_office is None:
                return queryset.none()
            queryset = queryset.filter(employee_assignment__position__office=director_office)
        is_staff_hr = (
            not self.request.user.is_superuser
            and self.request.user.groups.filter(name=STAFF_HR_GROUP).exists()
        )
        if is_staff_hr:
            staff_office = resolve_staff_hr_office(self.request.user)
            if staff_office is None:
                return queryset.none()
            queryset = queryset.filter(
                employee_assignment__position__office=staff_office
            )
        employee = getattr(self.request.user, "employee_profile", None)
        if (
            not self.request.user.is_staff
            and not self.request.user.is_superuser
            and not is_staff_hr
            and employee is not None
            and not self.request.user.groups.filter(name=DIRECTOR_GROUP).exists()
        ):
            queryset = queryset.filter(employee_assignment__employee=employee)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from core.permissions import (
            DIRECTOR_GROUP, STAFF_HR_GROUP, resolve_director_office,
            resolve_staff_hr_office,
        )

        is_staff_hr = (
            not self.request.user.is_superuser
            and self.request.user.groups.filter(name=STAFF_HR_GROUP).exists()
        )
        if is_staff_hr:
            office = resolve_staff_hr_office(self.request.user)
        elif self.request.user.groups.filter(name=DIRECTOR_GROUP).exists():
            office = resolve_director_office(self.request.user)
        else:
            return context
        if office is None:
            if is_staff_hr:
                for control in context["filter_controls"]:
                    if control["name"] in {"office_id", "department_id", "employee_id", "shift_id"}:
                        control["choices"] = []
            return context
        from attendance.models import Shift
        from employees.models import Employee
        from organization.models import Department, Office

        for control in context["filter_controls"]:
            if control["name"] == "office_id":
                control["choices"] = Office.objects.filter(pk=office.pk).values_list("pk", "office_name")
            elif control["name"] == "department_id":
                control["choices"] = Department.objects.filter(
                    office=office,
                ).values_list("pk", "department_name")
            elif control["name"] == "employee_id":
                control["choices"] = [
                    (pk, f"{code} — {name}")
                    for pk, code, name in Employee.objects.filter(
                        assignments__is_primary=True,
                        assignments__position__office=office,
                    ).distinct().order_by("employee_id").values_list("pk", "employee_id", "full_name")
                ]
            elif control["name"] == "shift_id":
                control["choices"] = Shift.objects.filter(
                    office=office,
                ).values_list("pk", "name")
        return context

    def get_row_actions(self, item):
        if (
            item.status in EmployeeSchedule.LIVE_STATUSES
            and not AttendanceRecord.objects.filter(schedule_id=item.pk).exists()
            and self.request.user.is_superuser
            and self.request.user.has_perm("attendance.change_employeeschedule")
        ):
            return [{
                "url": reverse("attendance:schedule-cancel", args=[item.pk]),
                "label": "Cancel",
                "icon": "bi-x-circle",
            }]
        return []


class ScheduleDetailView(ModelDetailView):
    """Read-only schedule detail: planned times, the expected event order, and (separately) real events."""

    model = EmployeeSchedule
    template_name = "attendance/schedule_detail.html"
    permission_required = "attendance.view_employeeschedule"
    page_title = "Schedule Details"
    back_route = "attendance:schedule-list"
    edit_route = "attendance:schedule-update"

    def get_queryset(self):
        from core.permissions import (
            DIRECTOR_GROUP, STAFF_HR_GROUP, resolve_director_office,
            resolve_staff_hr_office,
        )

        queryset = super().get_queryset().select_related(
            "employee_assignment__employee", "employee_assignment__position__department",
            "employee_assignment__position__office", "shift__office", "assigned_by",
        )
        is_director = self.request.user.groups.filter(name=DIRECTOR_GROUP).exists()
        if is_director:
            director_office = resolve_director_office(self.request.user)
            if director_office is None:
                return queryset.none()
            queryset = queryset.filter(employee_assignment__position__office=director_office)
        is_staff_hr = (
            not self.request.user.is_superuser
            and self.request.user.groups.filter(name=STAFF_HR_GROUP).exists()
        )
        if is_staff_hr:
            staff_office = resolve_staff_hr_office(self.request.user)
            if staff_office is None:
                return queryset.none()
            queryset = queryset.filter(
                employee_assignment__position__office=staff_office
            )
        employee = getattr(self.request.user, "employee_profile", None)
        if (
            not self.request.user.is_staff
            and not self.request.user.is_superuser
            and not is_staff_hr
            and employee is not None
            and not is_director
        ):
            queryset = queryset.filter(employee_assignment__employee=employee)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        schedule = self.object
        employee = schedule.employee
        record = AttendanceRecord.objects.filter(schedule_id=schedule.pk).first()
        context["schedule"] = schedule
        plan_rows = [
            ("Employee ID", employee.employee_id),
            ("Employee full name", employee.full_name),
            ("Office / Branch", schedule.office.office_name),
            ("Assignment / Position", schedule.assignment_label),
            ("Shift", f"{schedule.shift.name} ({schedule.shift.shift_code})"),
            ("Work date", schedule.work_date),
            ("Scheduled start", schedule.scheduled_start_display),
            ("Scheduled break start", schedule.scheduled_break_start_display),
            ("Scheduled break end", schedule.scheduled_break_end_display),
            ("Scheduled end", schedule.scheduled_end_display),
            ("Break duration", f"{schedule.break_minutes} minutes"),
            ("Scheduled work duration", f"{schedule.work_duration_display} (break excluded)"),
            ("Grace period (frozen with the schedule)", f"{schedule.effective_grace_minutes} minutes"),
            ("Early check-in window (frozen)", f"{schedule.effective_early_checkin_minutes} minutes"),
            ("Schedule status", schedule.get_status_display()),
            ("Schedule source", schedule.get_schedule_source_display()),
        ]
        if schedule.cancelled_by_holiday:
            plan_rows.append(("Cancelled by holiday", schedule.cancelled_by_holiday.name))
        plan_rows.extend([
            ("Assigned by", schedule.assigned_by or "\u2014"),
            ("Notes", schedule.notes or "\u2014"),
        ])
        context["plan_rows"] = plan_rows

        context["expected_sequence"] = [
            {"label": "Check In", "planned": schedule.scheduled_start_display},
            {"label": "Break Out", "planned": schedule.scheduled_break_start_display},
            {"label": "Break In", "planned": schedule.scheduled_break_end_display},
            {"label": "Check Out", "planned": schedule.scheduled_end_display},
        ]
        context["attendance_record"] = record
        context["actual_events"] = (
            list(record.events.select_related("verification_log").order_by("occurred_at")) if record else []
        )
        context["can_cancel"] = (
            schedule.status in EmployeeSchedule.LIVE_STATUSES
            and record is None
            and self.request.user.is_superuser
            and self.request.user.has_perm("attendance.change_employeeschedule")
        )
        context["action_url"] = reverse("attendance:schedule-cancel", args=[schedule.pk])
        context["action_label"] = "Cancel schedule"
        return context


class ScheduleFormMixin:
    template_name = "attendance/schedule_form.html"
    form_receives_user = True
    model = EmployeeSchedule
    form_class = EmployeeScheduleForm
    back_route = "attendance:schedule-list"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context["form"]
        context.update(
            preview_url=reverse("attendance:schedule-preview"),
            schedule_pk=self.object.pk if getattr(self, "object", None) and self.object.pk else "",
            historical_notes=getattr(form, "historical_notes", []),
        )
        return context


class ScheduleCreateView(ScheduleFormMixin, ModelCreateView):
    permission_required = "attendance.add_employeeschedule"
    page_title = "Add Employee Schedule"
    list_route = "attendance:schedule-list"


class ScheduleUpdateView(ScheduleFormMixin, ModelUpdateView):
    def get_queryset(self):
        queryset = super().get_queryset()
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office
        if is_staff_hr_user(self.request.user):
            office = resolve_staff_hr_office(self.request.user)
            return queryset.filter(employee_assignment__position__office=office, shift__office=office) if office else queryset.none()
        return queryset

    permission_required = "attendance.change_employeeschedule"
    page_title = "Edit Employee Schedule"
    detail_route = "attendance:schedule-detail"
    queryset = EmployeeSchedule.objects.select_related(
        "employee_assignment__employee", "employee_assignment__position", "shift__office"
    )


class SchedulePreviewView(AttendanceAccessMixin, View):
    """JSON preview of the planned times for the chosen office/employee/shift/date. Saves nothing.

    It applies the same eligibility rules as the form, so it never reveals employees or shifts the
    user could not schedule.
    """

    def has_permission(self):
        user = self.request.user
        return user.is_authenticated and (
            user.has_perm("attendance.add_employeeschedule") or user.has_perm("attendance.change_employeeschedule")
        )

    @staticmethod
    def _int(value):
        return int(value) if value and str(value).isdigit() else None

    def get(self, request):
        params = request.GET
        office_id, assignment_id = self._int(params.get("office")), self._int(params.get("employee_assignment"))
        shift_id, schedule_id = self._int(params.get("shift")), self._int(params.get("schedule"))
        work_date = parse_date(params.get("work_date", "") or "")
        if not (office_id and assignment_id and shift_id and work_date):
            return JsonResponse({
                "ok": False, "incomplete": True,
                "message": "Choose office, employee, shift and work date to see the planned times.",
            })

        existing = None
        if schedule_id:
            existing = EmployeeSchedule.objects.select_related("shift__office").filter(pk=schedule_id).first()
        offices = offices_for_user(request.user)
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office
        if is_staff_hr_user(request.user) and existing is not None:
            staff_office = resolve_staff_hr_office(request.user)
            if staff_office is None or existing.shift.office_id != staff_office.pk or existing.employee_assignment.position.office_id != staff_office.pk:
                raise PermissionDenied("This schedule belongs to another branch.")
        office = offices.filter(pk=office_id).first()
        if office is None and existing is not None and existing.shift.office_id == office_id:
            office = existing.shift.office  # saved office stays visible on Edit
        if office is None:
            return JsonResponse({"ok": False, "errors": ["You cannot schedule employees for this office."]})

        assignments = eligible_assignments(offices)
        shifts = eligible_shifts(offices)
        if existing is not None:
            assignments = EmployeeAssignment.objects.filter(
                Q(pk__in=assignments.values("pk")) | Q(pk=existing.employee_assignment_id)
            )
            shifts = Shift.objects.filter(Q(pk__in=shifts.values("pk")) | Q(pk=existing.shift_id))
        assignment = assignments.select_related(
            "employee", "position__office", "position__department"
        ).filter(pk=assignment_id).first()
        shift = shifts.select_related("office").filter(pk=shift_id).first()
        errors = []
        if assignment is None:
            errors.append("This employee cannot be scheduled (not active, or not available to you).")
        elif assignment.position.office_id != office.pk:
            errors.append("This employee is not assigned to the selected office.")
        if shift is None:
            errors.append("This shift is not available (inactive or not available to you).")
        elif shift.office_id != office.pk:
            errors.append("This shift belongs to a different office.")
        if errors:
            return JsonResponse({"ok": False, "errors": errors})

        same_as_saved = (
            existing is not None
            and existing.employee_assignment_id == assignment.pk
            and existing.shift_id == shift.pk
            and existing.work_date == work_date
        )
        if same_as_saved:  # show the frozen snapshot, not the (possibly edited) shift template
            start, break_start, break_end, end = (
                existing.scheduled_start_at, existing.scheduled_break_start_at,
                existing.scheduled_break_end_at, existing.scheduled_end_at,
            )
            source = "saved"
            break_minutes = existing.break_minutes
            work_minutes = existing.scheduled_work_minutes
        else:
            try:
                snapshot = build_schedule_snapshot(shift, work_date)
            except ShiftTimingError as exc:
                return JsonResponse({"ok": False, "errors": [exc.message + " Edit the shift first."]})
            start, break_start, break_end, end = snapshot.start, snapshot.break_start, snapshot.break_end, snapshot.end
            source = "shift"
            break_minutes = int((break_end - break_start).total_seconds() // 60)
            work_minutes = int((end - start).total_seconds() // 60) - break_minutes

        warnings = []
        holidays = [
            {"name": h.name, "scope": h.scope_label} for h in holidays_on(shift.office, work_date)
        ]
        if not same_as_saved:
            clash = overlapping_schedules(assignment.employee_id, start, end, exclude_pk=schedule_id).first()
            if clash is not None:
                warnings.append(
                    f"This employee already has an overlapping schedule on {clash.work_date} "
                    f"({clash.scheduled_start_display} to {clash.scheduled_end_display}). Saving will be refused."
                )
        local = lambda value: format_local(value, work_date)  # noqa: E731
        return JsonResponse({
            "ok": True,
            "source": source,
            "warnings": warnings,
            "holidays": holidays,
            "preview": {
                "employee_id": assignment.employee.employee_id,
                "employee_name": assignment.employee.full_name,
                "office": office.office_name,
                "position": assignment_position_label(assignment),
                "shift": f"{shift.name} ({shift.shift_code})",
                "work_date": work_date.isoformat(),
                "scheduled_start": local(start),
                "scheduled_break_start": local(break_start),
                "scheduled_break_end": local(break_end),
                "scheduled_end": local(end),
                "break_duration": f"{break_minutes} minutes",
                "work_duration": f"{format_minutes(work_minutes)} (break excluded)",
                "sequence": ["CHECK_IN", "BREAK_OUT", "BREAK_IN", "CHECK_OUT"],
            },
        })


class ScheduleCancelView(AttendanceAccessMixin, View):
    permission_required = "attendance.change_employeeschedule"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            from core.permissions import is_branch_hr_user
            if not is_branch_hr_user(request.user):
                require_office_scoped_admin(request.user)
        return super().dispatch(request, *args, **kwargs)

    def _get_schedule(self, pk, *, lock=False):
        queryset = EmployeeSchedule.objects.select_related(
            "attendance_record",
            "employee_assignment__position__office",
        )
        if lock:
            queryset = queryset.select_for_update()
        return get_object_or_404(queryset, pk=pk)

    def get(self, request, pk):
        schedule = self._get_schedule(pk)
        if schedule.status not in EmployeeSchedule.LIVE_STATUSES:
            messages.error(request, "Only a planned or published schedule can be cancelled.")
            return redirect("attendance:schedule-detail", pk=schedule.pk)
        if hasattr(schedule, "attendance_record"):
            messages.error(request, "A schedule linked to an attendance record cannot be cancelled.")
            return redirect("attendance:schedule-detail", pk=schedule.pk)
        return render(
            request,
            "core/action_confirm.html",
            {
                "page_title": "Cancel employee schedule",
                "prompt": "Cancel this scheduled shift while retaining its schedule record:",
                "object_label": str(schedule),
                "warning": "A schedule linked to attendance history cannot be cancelled. This cancellation is recorded in the audit trail.",
                "confirm_label": "Cancel schedule",
                "back_url": reverse("attendance:schedule-detail", args=[schedule.pk]),
            },
        )

    def post(self, request, pk):
        with transaction.atomic():
            schedule = self._get_schedule(pk, lock=True)
            if schedule.status not in EmployeeSchedule.LIVE_STATUSES:
                messages.error(request, "Only a planned or published schedule can be cancelled.")
                return redirect("attendance:schedule-detail", pk=schedule.pk)
            if hasattr(schedule, "attendance_record"):
                messages.error(request, "A schedule linked to an attendance record cannot be cancelled.")
                return redirect("attendance:schedule-detail", pk=schedule.pk)
            previous_status = schedule.status
            schedule.status = EmployeeSchedule.Status.CANCELLED
            schedule.full_clean()
            log_action(
                actor_user=request.user,
                office=schedule.shift.office,
                action=AuditLog.Action.UPDATE,
                entity_type="EmployeeSchedule",
                object_id=schedule.pk,
                before_data={"status": previous_status},
                after_data={"status": schedule.status},
                reason="Scheduled shift cancelled; schedule record retained.",
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            schedule.save(update_fields=["status", "updated_at"])
        messages.success(request, "Schedule cancelled.")
        return redirect("attendance:schedule-detail", pk=schedule.pk)


class HolidayListView(ModelListView):
    model = Holiday
    permission_required = "attendance.view_holiday"
    page_title = "Holidays"
    page_description = "Institution-wide and office-specific holidays."
    search_fields = ("name", "description", "office__office_name")
    columns = (
        ("Holiday", "name"), ("Coverage", "scope_label"), ("Type", "holiday_type"),
        ("Start", "start_date"), ("End", "end_date"), ("Active", "is_active"),
    )
    detail_route = "attendance:holiday-detail"
    create_route = "attendance:holiday-create"
    create_permission = "attendance.add_holiday"
    filters = (
        ("office_id", "office_id", "Office", lambda: Office.objects.order_by(
            "office_name"
        ).values_list("id", "office_name")),
        ("is_active", "is_active", "Status", (("True", "Active"), ("False", "Inactive"))),
    )
    date_filter_fields = {"start_date": "start_date", "end_date": "end_date"}

    def get_queryset(self):
        return super().get_queryset().select_related("office", "created_by")

    def get_row_actions(self, item):
        if (
            item.is_active
            and self.request.user.is_superuser
            and self.request.user.has_perm("attendance.change_holiday")
        ):
            return [{
                "url": reverse("attendance:holiday-deactivate", args=[item.pk]),
                "label": "Deactivate",
                "icon": "bi-toggle-off",
            }]
        return []


class HolidayDetailView(ModelDetailView):
    model = Holiday
    permission_required = "attendance.view_holiday"
    page_title = "Holiday Details"
    back_route = "attendance:holiday-list"
    edit_route = "attendance:holiday-update"
    detail_fields = (
        ("Name", "name"), ("Coverage", "scope_label"), ("Type", "holiday_type"),
        ("Start date", "start_date"), ("End date", "end_date"),
        ("Description", "description"), ("Active", "is_active"),
        ("Created by", "created_by"), ("Created", "created_at"),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("office", "created_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            can_deactivate=(
                self.object.is_active
                and self.request.user.is_superuser
                and self.request.user.has_perm("attendance.change_holiday")
            ),
            action_url=reverse(
                "attendance:holiday-deactivate", args=[self.object.pk]
            ),
            action_label="Deactivate holiday",
        )
        return context


class HolidayCreateView(ModelCreateView):
    form_receives_user = True
    model = Holiday
    form_class = HolidayForm
    permission_required = "attendance.add_holiday"
    page_title = "Add Holiday"
    back_route = "attendance:holiday-list"
    list_route = "attendance:holiday-list"

    def form_valid(self, form):
        response = super().form_valid(form)
        res = sync_holiday_schedules(self.object, actor_user=self.request.user)
        if res.cancelled_count > 0 or res.restored_count > 0:
            messages.info(
                self.request,
                f"Schedules synchronized for holiday '{self.object.name}': "
                f"{res.cancelled_count} future schedule(s) cancelled, {res.restored_count} restored.",
            )
        return response


class HolidayUpdateView(ModelUpdateView):
    form_receives_user = True
    model = Holiday
    form_class = HolidayForm
    permission_required = "attendance.change_holiday"
    page_title = "Edit Holiday"
    back_route = "attendance:holiday-list"
    detail_route = "attendance:holiday-detail"
    queryset = Holiday.objects.select_related("office", "created_by")

    def get_queryset(self):
        qs = super().get_queryset()
        from core.permissions import is_branch_hr_user, resolve_branch_hr_office
        if is_branch_hr_user(self.request.user):
            office = resolve_branch_hr_office(self.request.user)
            if office is None:
                return qs.none()
            # Branch HR may only edit office-specific holidays in their branch.
            return qs.filter(office=office)
        return qs

    def form_valid(self, form):
        response = super().form_valid(form)
        res = sync_holiday_schedules(self.object, actor_user=self.request.user)
        if res.cancelled_count > 0 or res.restored_count > 0:
            messages.info(
                self.request,
                f"Schedules synchronized for holiday '{self.object.name}': "
                f"{res.cancelled_count} future schedule(s) cancelled, {res.restored_count} restored.",
            )
        return response



class AttendancePolicyListView(ModelListView):
    model = AttendancePolicy
    permission_required = "attendance.view_attendancepolicy"
    page_title = "Attendance Policies"
    page_description = "Review policy effective periods and verification rules."
    search_fields = ("policy_name", "office__office_name")
    columns = (
        ("Policy", "policy_name"), ("Office", "office"), ("Effective from", "effective_from"),
        ("Effective until", "effective_until"), ("Liveness required", "require_liveness"),
        ("Absent threshold after scheduled start (min)", "absent_after_minutes"), ("Active", "is_active"),
    )
    detail_route = "attendance:policy-detail"
    create_route = "attendance:policy-create"
    create_permission = "attendance.add_attendancepolicy"
    filters = (
        ("office_id", "office_id", "Office", lambda: Office.objects.order_by("office_name").values_list("id", "office_name")),
        ("is_active", "is_active", "Status", (("True", "Active"), ("False", "Inactive"))),
    )
    date_filter_fields = {"start_date": "effective_from", "end_date": "effective_until"}

    def get_queryset(self):
        return super().get_queryset().select_related("office", "updated_by")

    def get_row_actions(self, item):
        if (
            item.is_active
            and self.request.user.is_superuser
            and self.request.user.has_perm("attendance.change_attendancepolicy")
        ):
            return [{
                "url": reverse("attendance:policy-deactivate", args=[item.pk]),
                "label": "Deactivate",
                "icon": "bi-toggle-off",
            }]
        return []


class AttendancePolicyDetailView(ModelDetailView):
    model = AttendancePolicy
    permission_required = "attendance.view_attendancepolicy"
    page_title = "Attendance Policy Details"
    back_route = "attendance:policy-list"
    edit_route = "attendance:policy-update"
    detail_fields = (
        ("Policy name", "policy_name"), ("Office", "office"),
        ("Absent threshold after scheduled start (minutes)", "absent_after_minutes"),
        ("Liveness required", "require_liveness"),
        ("Maximum failed attempts", "max_failed_attempts"),
        ("Lockout (minutes)", "failed_attempt_lockout_minutes"),
        ("Effective from", "effective_from"), ("Effective until", "effective_until"),
        ("Active", "is_active"), ("Updated by", "updated_by"), ("Updated", "updated_at"),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("office", "updated_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            can_deactivate=(
                self.object.is_active
                and self.request.user.is_superuser
                and self.request.user.has_perm("attendance.change_attendancepolicy")
            ),
            action_url=reverse(
                "attendance:policy-deactivate", args=[self.object.pk]
            ),
            action_label="Deactivate policy",
        )
        return context


class AttendancePolicyCreateView(ModelCreateView):
    form_receives_user = True
    model = AttendancePolicy
    form_class = AttendancePolicyForm
    permission_required = "attendance.add_attendancepolicy"
    page_title = "Add Attendance Policy"
    back_route = "attendance:policy-list"
    list_route = "attendance:policy-list"


class AttendancePolicyUpdateView(ModelUpdateView):
    form_receives_user = True
    model = AttendancePolicy
    form_class = AttendancePolicyForm
    permission_required = "attendance.change_attendancepolicy"
    page_title = "Edit Attendance Policy"
    back_route = "attendance:policy-list"
    detail_route = "attendance:policy-detail"
    queryset = AttendancePolicy.objects.select_related("office", "updated_by")

    def get_queryset(self):
        qs = super().get_queryset()
        from core.permissions import is_branch_hr_user, resolve_branch_hr_office
        if is_branch_hr_user(self.request.user):
            office = resolve_branch_hr_office(self.request.user)
            if office is None:
                return qs.none()
            return qs.filter(office=office)
        return qs


class AttendanceDeviceListView(KioskAccessMixin, ModelListView):
    model = AttendanceDevice
    permission_required = "attendance.view_attendancedevice"
    page_title = "Registered Attendance Devices"
    page_description = "Device status is shown from stored records; no live connection is implied."
    search_fields = ("device_code", "device_name", "office__office_name", "location_description")
    columns = (
        ("Code", "device_code"), ("Device", "device_name"), ("Office", "office__office_name"),
        ("Type", "device_type"), ("Location", "location_description"), ("Status", "status"),
        ("Last seen", "last_seen_at"), ("Last authenticated", "last_authenticated_at"),
    )
    detail_route = "attendance:device-detail"
    create_route = "attendance:device-create"
    create_permission = "attendance.add_attendancedevice"
    filters = (
        ("office_id", "office_id", "Office", lambda: Office.objects.order_by("office_name").values_list("id", "office_name")),
        ("device_type", "device_type", "Type", AttendanceDevice.DeviceType.choices),
        ("status", "status", "Status", AttendanceDevice.Status.choices),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("office")

    def get_row_actions(self, item):
        if (
            not AttendanceDeleteView.has_references(item)
            and self.request.user.is_superuser
            and self.request.user.has_perm("attendance.delete_attendancedevice")
        ):
            return [{
                "url": reverse("attendance:device-delete", args=[item.pk]),
                "label": "Delete",
                "icon": "bi-trash",
            }]
        if (
            item.status != AttendanceDevice.Status.INACTIVE
            and AttendanceDeleteView.has_references(item)
            and self.request.user.is_superuser
            and self.request.user.has_perm("attendance.change_attendancedevice")
        ):
            return [{
                "url": reverse("attendance:device-deactivate", args=[item.pk]),
                "label": "Deactivate",
                "icon": "bi-toggle-off",
            }]
        return []


class AttendanceDeviceDetailView(KioskAccessMixin, ModelDetailView):
    model = AttendanceDevice
    permission_required = "attendance.view_attendancedevice"
    page_title = "Attendance Device Details"
    back_route = "attendance:device-list"
    edit_route = "attendance:device-update"
    detail_fields = (
        ("Device code", "device_code"), ("Device name", "device_name"),
        ("Office", "office"), ("Device type", "device_type"),
        ("Location", "location_description"), ("Status", "status"),
        ("Last seen (recorded)", "last_seen_at"),
        ("Last authenticated (recorded)", "last_authenticated_at"),
        ("Created", "created_at"), ("Updated", "updated_at"),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("office")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            can_delete=(
                not AttendanceDeleteView.has_references(self.object)
                and self.request.user.is_superuser
                and self.request.user.has_perm("attendance.delete_attendancedevice")
            ),
            delete_url=reverse(
                "attendance:device-delete", args=[self.object.pk]
            ),
            can_deactivate=(
                self.object.status != AttendanceDevice.Status.INACTIVE
                and AttendanceDeleteView.has_references(self.object)
                and self.request.user.is_superuser
                and self.request.user.has_perm("attendance.change_attendancedevice")
            ),
            action_url=reverse(
                "attendance:device-deactivate", args=[self.object.pk]
            ),
            action_label="Deactivate device",
        )
        return context


class AttendanceDeviceCreateView(KioskAccessMixin, ModelCreateView):
    model = AttendanceDevice
    form_class = AttendanceDeviceForm
    permission_required = "attendance.add_attendancedevice"
    page_title = "Register Attendance Device"
    back_route = "attendance:device-list"
    list_route = "attendance:device-list"


class AttendanceDeviceUpdateView(KioskAccessMixin, ModelUpdateView):
    model = AttendanceDevice
    form_class = AttendanceDeviceForm
    permission_required = "attendance.change_attendancedevice"
    page_title = "Edit Attendance Device"
    back_route = "attendance:device-list"
    detail_route = "attendance:device-detail"
    queryset = AttendanceDevice.objects.select_related("office")


class FaceTemplateListView(ModelListView):
    model = FaceTemplate
    permission_required = "attendance.view_facetemplate"
    page_title = "Face Enrollment Records"
    page_description = "Enrollment is not enabled until a trusted provider is integrated."
    search_fields = ("employee__employee_id", "employee__full_name", "provider")
    columns = (
        ("Employee", "employee"), ("Provider", "provider"),
        ("Provider reference", "provider_reference"), ("Model version", "model_version"),
        ("Enrolled", "enrolled_at"), ("Status", "status"),
        ("Revoked", "revoked_at"), ("Retention until", "retention_until"),
    )
    detail_route = "attendance:face-template-detail"
    filters = (
        ("status", "status", "Status", FaceTemplate.Status.choices),
        ("provider", "provider", "Provider", lambda: FaceTemplate.objects.order_by(
            "provider").values_list("provider", "provider").distinct()),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("employee")

    def get_row_actions(self, item):
        if (
            item.status == FaceTemplate.Status.ACTIVE
            and self.request.user.is_superuser
            and self.request.user.has_perm("attendance.change_facetemplate")
        ):
            return [{
                "url": reverse("attendance:face-revoke", args=[item.pk]),
                "label": "Revoke",
                "icon": "bi-shield-x",
            }]
        return []


class FaceTemplateDetailView(ModelDetailView):
    model = FaceTemplate
    permission_required = "attendance.view_facetemplate"
    page_title = "Face Enrollment Details"
    back_route = "attendance:face-template-list"
    detail_fields = (
        ("Employee", "employee"), ("Provider", "provider"),
        ("Provider reference", "provider_reference"), ("Model version", "model_version"),
        ("Consent recorded", "consent_recorded_at"), ("Enrolled by", "enrolled_by"),
        ("Enrolled at", "enrolled_at"), ("Status", "status"),
        ("Revoked at", "revoked_at"), ("Retention until", "retention_until"),
    )

    def get_queryset(self):
        return super().get_queryset().select_related("employee", "enrolled_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_revoke"] = (
            self.object.status == FaceTemplate.Status.ACTIVE
            and self.request.user.is_superuser
            and self.request.user.has_perm("attendance.change_facetemplate")
        )
        context["revoke_url"] = reverse(
            "attendance:face-revoke", args=[self.object.pk]
        )
        return context


class FaceTemplateRevokeView(AttendanceAccessMixin, View):
    permission_required = "attendance.change_facetemplate"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            from core.permissions import is_branch_hr_user
            if not is_branch_hr_user(request.user):
                require_office_scoped_admin(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, pk):
        template = get_object_or_404(FaceTemplate.objects.select_related("employee"), pk=pk)
        if template.status != FaceTemplate.Status.ACTIVE:
            messages.error(request, "Only an active enrollment can be revoked.")
            return redirect("attendance:face-template-detail", pk=template.pk)
        return render(
            request,
            "core/action_confirm.html",
            {
                "page_title": "Revoke face enrollment",
                "prompt": "Revoke the active encrypted face template for this employee:",
                "object_label": str(template),
                "warning": "The encrypted biometric data will be retained for its audit/retention lifecycle but cannot be used for matching after revocation.",
                "confirm_label": "Revoke enrollment",
                "back_url": reverse(
                    "attendance:face-template-detail", args=[template.pk]
                ),
            },
        )

    def post(self, request, pk):
        with transaction.atomic():
            template = get_object_or_404(
                FaceTemplate.objects.select_for_update().select_related("employee"),
                pk=pk,
            )
            if template.status != FaceTemplate.Status.ACTIVE:
                messages.error(request, "Only an active enrollment can be revoked.")
                return redirect("attendance:face-template-detail", pk=template.pk)
            assignment = template.employee.assignments.select_related(
                "position__office"
            ).order_by("-start_date").first()
            office = assignment.position.office if assignment else None
            previous_status = template.status
            template.status = FaceTemplate.Status.REVOKED
            template.revoked_at = timezone.now()
            log_action(
                actor_user=request.user,
                office=office,
                action=AuditLog.Action.REVOKE_FACE,
                entity_type="FaceTemplate",
                object_id=template.pk,
                before_data={"status": previous_status},
                after_data={"status": template.status, "revoked_at": template.revoked_at.isoformat()},
                reason="Face template revoked; encrypted biometric data retained.",
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            template.save(update_fields=["status", "revoked_at"])
        messages.success(request, "Face enrollment record revoked.")
        return redirect("attendance:face-template-detail", pk=template.pk)


class FaceEnrollmentAccessMixin(AttendanceAccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if not can_manage_groups(request.user):
            return self.handle_no_permission()
        return super().dispatch(request, *args, **kwargs)


def _enrollment_id(value, label):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise ValueError(f"Select a valid {label}.")
    return value


def _enrollment_device(value, *, lock=False):
    device_id = _enrollment_id(value, "attendance device")
    devices = AttendanceDevice.objects.select_related("office")
    if lock:
        devices = devices.select_for_update()
    device = devices.filter(pk=device_id, status=AttendanceDevice.Status.ACTIVE).first()
    if device is None:
        raise ValueError("The selected attendance device does not exist or is no longer active.")
    if not device.office.is_active:
        raise ValueError("The selected attendance device is not associated with an active branch.")
    return device


def _enrollment_employees(device):
    assignments = (
        effective_primary_assignments().order_by().values("employee_id")
        .annotate(total=Count("pk"), in_branch=Count("pk", filter=Q(position__office_id=device.office_id)))
        .filter(total=1, in_branch=1)
    )
    return Employee.objects.filter(pk__in=assignments.values("employee_id"))


def _validate_enrollment_pair(device_id, employee_id, *, office_id=None, lock=False):
    device = _enrollment_device(device_id, lock=lock)
    if office_id is not None and device.office_id != office_id:
        raise ValueError("The attendance device changed branches. Start a new challenge.")
    employees = _enrollment_employees(device)
    if lock:
        employees = employees.select_for_update()
    if employees.only("pk").filter(pk=employee_id).first() is None:
        raise ValueError("The employee must have one current primary assignment in this device's branch.")
    return device


class FaceEnrollmentChallengeView(FaceEnrollmentAccessMixin, View):
    permission_required = "attendance.add_facetemplate"

    def post(self, request):
        try:
            payload = json.loads(request.body.decode("utf-8")) if request.body else {}
            if not isinstance(payload, dict):
                raise ValueError
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Malformed enrollment challenge request."}, status=400)
        try:
            employee_id = _enrollment_id(payload.get("employee_id"), "employee")
            device = _validate_enrollment_pair(payload.get("device_id"), employee_id)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)

        challenge = {
            "id": uuid.uuid4().hex,
            "employee_id": employee_id,
            "device_id": device.pk,
            "office_id": device.office_id,
            "user_id": request.user.pk,
            "sequence": ["center", "left", "right"],
            "expires_at": (timezone.now() + timedelta(minutes=2)).isoformat(),
            "used": False,
            "accepted_steps": [],
        }
        request.session["face_enrollment_challenge"] = challenge
        request.session.modified = True
        return JsonResponse(
            {
                "ok": True,
                "challenge_id": challenge["id"],
                "employee_id": employee_id,
                "device_id": device.pk,
                "sequence": challenge["sequence"],
                "next_step": challenge["sequence"][0],
                "expires_in_seconds": 120,
                "message": "Capture and validate each step in order. A single face must remain visible.",
            }
        )


class FaceEnrollmentSubmitView(FaceEnrollmentAccessMixin, View):
    permission_required = "attendance.add_facetemplate"
    max_request_size = 12 * 1024 * 1024
    max_capture_size = 4 * 1024 * 1024
    max_image_width = 1920
    max_image_height = 1080

    def post(self, request):
        content_length = request.META.get("CONTENT_LENGTH")
        try:
            content_length = int(content_length) if content_length else 0
        except ValueError:
            return JsonResponse({"ok": False, "error": "The enrollment request has an invalid content length."}, status=400)
        if content_length > self.max_request_size:
            return JsonResponse({"ok": False, "error": "The enrollment request is too large. Capture lower-resolution frames and try again."}, status=413)
        if len(request.body) > self.max_request_size:
            return JsonResponse({"ok": False, "error": "The enrollment request is too large. Capture lower-resolution frames and try again."}, status=413)

        try:
            payload = json.loads(request.body.decode("utf-8")) if request.body else {}
            if not isinstance(payload, dict):
                raise ValueError
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Malformed enrollment payload."}, status=400)

        employee_id = payload.get("employee_id")
        challenge_id = payload.get("challenge_id")
        capture = payload.get("capture")

        if not employee_id or not challenge_id or not isinstance(capture, dict):
            return JsonResponse({"ok": False, "error": "The current challenge frame was missing from the request."}, status=400)

        challenge = request.session.get("face_enrollment_challenge")
        if not challenge or challenge.get("id") != challenge_id:
            return JsonResponse({"ok": False, "error": "The liveness challenge is missing or expired."}, status=400)
        challenge = dict(challenge)
        try:
            employee_id = _enrollment_id(employee_id, "employee")
            device_id = _enrollment_id(payload.get("device_id"), "attendance device")
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        if challenge.get("employee_id") != employee_id:
            return JsonResponse({"ok": False, "error": "The challenge is for a different employee."}, status=400)
        if challenge.get("device_id") != device_id:
            return JsonResponse({"ok": False, "error": "The challenge is for a different attendance device."}, status=400)
        if challenge.get("user_id") != request.user.pk:
            return JsonResponse({"ok": False, "error": "The challenge belongs to a different signed-in user."}, status=400)
        if challenge.get("used"):
            return JsonResponse({"ok": False, "error": "This liveness challenge has already been used."}, status=400)

        expires_at = challenge.get("expires_at")
        if not expires_at:
            return JsonResponse({"ok": False, "error": "The challenge does not include an expiration time."}, status=400)
        try:
            expires_dt = datetime.fromisoformat(expires_at)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "The challenge expiration value is invalid."}, status=400)
        if timezone.now() > expires_dt:
            return JsonResponse({"ok": False, "error": "The liveness challenge has expired."}, status=400)

        expected_sequence = challenge.get("sequence", ["center", "left", "right"])
        accepted_steps = list(challenge.get("accepted_steps", []))
        if len(accepted_steps) >= len(expected_sequence):
            return JsonResponse({"ok": False, "error": "The challenge has already accepted all required frames."}, status=400)
        expected_step = expected_sequence[len(accepted_steps)]
        if capture.get("step") != expected_step:
            return JsonResponse({"ok": False, "error": f"The next required challenge frame is {expected_step}."}, status=400)

        if not request.user.is_authenticated:
            return JsonResponse({"ok": False, "error": "You must be signed in to enroll a face."}, status=403)
        if cv2 is None or np is None:
            return JsonResponse({"ok": False, "error": "OpenCV and NumPy are required for face enrollment."}, status=400)

        try:
            _validate_enrollment_pair(device_id, employee_id, office_id=challenge["office_id"])
            expected = expected_step
            data_url = capture.get("image")
            if not isinstance(data_url, str) or "," not in data_url:
                raise ValueError(f"The {expected} capture is not a valid image data URL.")
            header, encoded = data_url.split(",", 1)
            if header not in ("data:image/jpeg;base64", "data:image/png;base64") or not encoded:
                raise ValueError(f"The {expected} capture must be a base64 JPEG or PNG image.")
            try:
                image_bytes = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"The {expected} capture contains malformed base64 image data.") from exc
            if not image_bytes or len(image_bytes) > self.max_capture_size:
                raise ValueError(f"The {expected} capture is empty or exceeds the image size limit.")
            image_array = np.frombuffer(image_bytes, dtype=np.uint8)
            frame = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
            if frame is None or frame.size == 0:
                raise ValueError(f"The {expected} capture could not be decoded as an image.")
            height, width = frame.shape[:2]
            if width <= 0 or height <= 0 or width > self.max_image_width or height > self.max_image_height:
                raise ValueError(f"The {expected} image dimensions exceed the {self.max_image_width}x{self.max_image_height} limit.")

            detections = FaceEngine().detect_faces(frame)
            if not detections:
                raise ValueError(f"No face was detected in the {expected} challenge frame.")
            if len(detections) > 1:
                raise ValueError(f"Multiple faces were detected in the {expected} challenge frame; exactly one is required.")
            face = detections[0]
            x, y, w, h = face["bbox"]
            face_center_x = (x + (w / 2)) / max(frame.shape[1], 1)
            if expected == "center" and not (0.3 <= face_center_x <= 0.7):
                raise ValueError("Keep your face centered in the frame during the challenge.")

            yaw = _face_yaw_measurement(face.get("face_row"))
            center_yaw = challenge.get("center_yaw")
            if expected == "left" and (center_yaw is None or yaw - center_yaw > -0.12):
                raise ValueError("Turn your face until the nose shifts toward the left edge of the preview.")
            if expected == "right" and (center_yaw is None or yaw - center_yaw < 0.12):
                raise ValueError("Turn your face until the nose shifts toward the right edge of the preview.")

            if expected == "center":
                challenge["center_yaw"] = yaw
            accepted_steps.append(expected)
            challenge["accepted_steps"] = accepted_steps

            if expected == "right":
                embedding = build_enrollment_embedding(frame)
                with transaction.atomic():
                    device = _validate_enrollment_pair(
                        device_id, employee_id, office_id=challenge["office_id"], lock=True
                    )
                    FaceTemplate.objects.filter(employee_id=employee_id, status=FaceTemplate.Status.ACTIVE).update(
                        status=FaceTemplate.Status.REVOKED,
                        revoked_at=timezone.now(),
                    )
                    template = FaceTemplate.objects.create(
                        employee_id=employee_id,
                        provider="YuNet+SFace",
                        provider_reference="",
                        encrypted_template=encrypt_embedding(embedding),
                        model_version="YuNet/SFace-2021dec",
                        consent_recorded_at=timezone.now(),
                        enrolled_by=request.user,
                        status=FaceTemplate.Status.ACTIVE,
                    )
                    log_action(
                        actor_user=request.user,
                        office=device.office,
                        action=AuditLog.Action.ENROLL_FACE,
                        entity_type="FaceTemplate",
                        object_id=template.pk,
                        after_data={
                            "employee_id": employee_id,
                            "device_id": device.pk,
                            "device_code": device.device_code,
                            "office_id": device.office_id,
                            "provider": template.provider,
                            "model_version": template.model_version,
                        },
                        reason="Face template enrolled via liveness challenge.",
                        ip_address=request.META.get("REMOTE_ADDR"),
                    )
                challenge["used"] = True

            request.session["face_enrollment_challenge"] = challenge
            request.session.modified = True
            complete = expected == "right"
            return JsonResponse(
                {
                    "ok": True,
                    "accepted": True,
                    "complete": complete,
                    "step": expected,
                    "progress": len(accepted_steps),
                    "next_step": None if complete else expected_sequence[len(accepted_steps)],
                    "message": "Enrollment completed successfully." if complete else f"{expected.title()} challenge frame accepted.",
                    "employee_id": employee_id,
                }
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return JsonResponse({"ok": False, "accepted": False, "step": expected_step, "error": str(exc)}, status=400)


class FaceEnrollmentView(FaceEnrollmentAccessMixin, TemplateView):
    template_name = "attendance/face_enrollment.html"
    permission_required = "attendance.view_facetemplate"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        engine_status = get_face_model_status()
        context["page_title"] = "Face Enrollment"
        context["engine_status"] = engine_status
        context["provider_configured"] = bool(engine_status.get("available", False))
        context["liveness_available"] = bool(engine_status.get("available", False))
        context["enrollment_disabled_reason"] = "" if context["liveness_available"] else (
            engine_status.get("error") or "No face-recognition provider or liveness engine is configured."
        )
        context["devices"] = AttendanceDevice.objects.filter(
            status=AttendanceDevice.Status.ACTIVE
        ).select_related("office").defer("credential_hash")
        device_id = self.request.GET.get("device_id")
        if device_id:
            try:
                device = _enrollment_device(device_id)
            except ValueError as exc:
                context["device_error"] = str(exc)
            else:
                context["selected_device"] = device
                context["employees"] = _enrollment_employees(device).order_by("employee_id").prefetch_related(
                    Prefetch("face_templates", queryset=FaceTemplate.objects.defer("encrypted_template"))
                )
        return context


class FaceAttendanceKioskView(KioskAccessMixin, TemplateView):
    template_name = "attendance/face_kiosk.html"
    permission_required = "attendance.view_attendancerecord"

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.is_superuser:
            raise PermissionDenied(
                "The Face Attendance Kiosk interface is restricted to superusers."
            )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        engine_status = get_face_model_status()
        try:
            get_encryption_key()
            encryption_configured = True
            encryption_error = ""
        except RuntimeError as exc:
            encryption_configured = False
            encryption_error = str(exc)

        liveness_status = get_face_liveness_status()
        context.update(
            page_title="Face Attendance Kiosk",
            engine_status=engine_status,
            model_version="YuNet/SFace-2021dec",
            encryption_configured=encryption_configured,
            encryption_error=encryption_error,
            liveness_available=bool(liveness_status.get("available")),
            liveness_status=liveness_status,
            devices=AttendanceDevice.objects.filter(
                status=AttendanceDevice.Status.ACTIVE
            ).select_related("office"),
            server_time=timezone.localtime(timezone.now()),
        )
        return context


class FaceAttendanceVerifyView(KioskAccessMixin, View):
    permission_required = (
        "attendance.add_attendanceevent",
        "attendance.add_attendancerecord",
        "attendance.change_attendancerecord",
        "attendance.add_faceverificationlog",
    )
    max_request_size = 5 * 1024 * 1024
    max_capture_size = 4 * 1024 * 1024
    max_image_width = 1920
    max_image_height = 1080
    match_threshold = 0.65
    ambiguous_margin = 0.05
    model_version = "YuNet/SFace-2021dec"

    def dispatch(self, request, *args, **kwargs):
        # The interactive kiosk path (/attendance/kiosk/verify/) is restricted to
        # superusers. The mobile path (MobileFaceAttendanceVerifyView, mobile_mode=True)
        # is intentionally excluded here so independently authenticated device/mobile
        # workflows are not affected.
        if not getattr(self, "mobile_mode", False):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not request.user.is_superuser:
                return self._json_error(
                    "The Face Attendance Kiosk interface is restricted to superusers.",
                    403,
                    stage="authorization",
                )
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def _attendance_policy(office, on_date):
        """
        Get the active attendance policy for a specific office and date.

        An office-specific policy takes precedence over the
        institution-wide default policy.
        Returns None when no applicable policy exists.
        """
        from django.db.models import Q
        from .models import AttendancePolicy

        applicable_policies = (
            AttendancePolicy.objects.filter(
                is_active=True,
                effective_from__lte=on_date,
            )
            .filter(
                Q(effective_until__isnull=True)
                | Q(effective_until__gte=on_date)
            )
        )

        # Prefer the policy configured specifically for this office.
        office_policy = (
            applicable_policies
            .filter(office_id=office.pk)
            .order_by("-effective_from", "-pk")
            .first()
        )

        if office_policy is not None:
            return office_policy

        # If no office-specific policy applies, use the
        # institution-wide default, where office is NULL.
        return (
            applicable_policies
            .filter(office__isnull=True)
            .order_by("-effective_from", "-pk")
            .first()
        )

    @staticmethod
    def _json_error(message, status, *, stage="attendance", **extra):
        return JsonResponse(
            {
                "ok": False,
                "stage": stage,
                "message": message,
                "server_time": timezone.localtime(timezone.now()).isoformat(),
                **extra,
            },
            status=status,
        )

    def _save_verification(
        self,
        *,
        device,
        occurred_at,
        event_type,
        match_result,
        liveness_result=FaceVerificationLog.LivenessResult.NOT_CHECKED,
        liveness_score=None,
        match_score=None,
        employee=None,
        schedule=None,
        failure_reason="",
    ):
        valid_score = (
            Decimal(str(round(match_score, 6)))
            if match_score is not None and 0 <= match_score <= 1
            else None
        )
        valid_liveness_score = (
            Decimal(str(round(liveness_score, 6)))
            if liveness_score is not None and 0 <= liveness_score <= 1
            else None
        )
        log = FaceVerificationLog(
            employee=employee,
            device=device,
            schedule=schedule,
            event_type=event_type,
            match_result=match_result,
            liveness_result=liveness_result,
            liveness_score=valid_liveness_score,
            match_score=valid_score,
            failure_reason=failure_reason[:255],
            model_version=self.model_version,
            occurred_at=occurred_at,
        )
        log.full_clean()
        log.save(force_insert=True)
        return log

    def _diagnose_no_schedule(self, employee, device, now, today):
        """Provide detailed context when no open schedule candidate matches.

        Distinguishes rejection reasons so the kiosk can show an accurate,
        user-friendly message without leaking internal details.
        """
        # Schedules published for this employee today, regardless of device office.
        schedules_today = (
            EmployeeSchedule.objects.filter(
                employee_assignment__employee=employee,
                work_date=today,
            )
            .select_related("shift__office", "cancelled_by_holiday", "employee_assignment__position__office")
        )

        if schedules_today.exists():
            for sched in schedules_today:
                if not sched.shift.is_active:
                    return f"Shift '{sched.shift.name}' is inactive and cannot be used for attendance."
                if sched.status == EmployeeSchedule.Status.CANCELLED:
                    if sched.cancelled_by_holiday:
                        return f"Schedule for today was cancelled due to holiday '{sched.cancelled_by_holiday.name}'."
                    return "Schedule for today was cancelled."
                if sched.status != EmployeeSchedule.Status.SCHEDULED:
                    return f"Schedule for today is in '{sched.get_status_display()}' status."
                if sched.shift.office_id != device.office_id:
                    return (
                        f"Schedule for today is assigned to {sched.shift.office.office_name}, "
                        f"not this device's office ({device.office.office_name})."
                    )
                opens = sched.scheduled_start_at - timedelta(minutes=sched.effective_early_checkin_minutes)
                closes = sched.scheduled_end_at + timedelta(minutes=CHECKOUT_GRACE_MINUTES)
                if now < opens:
                    local_start = timezone.localtime(sched.scheduled_start_at).strftime("%H:%M")
                    local_open = timezone.localtime(opens).strftime("%H:%M")
                    return f"Shift starts at {local_start}. Check-in window opens at {local_open}."
                if now > closes:
                    local_end = timezone.localtime(sched.scheduled_end_at).strftime("%H:%M")
                    return f"Shift attendance window closed at {local_end}."

        # No usable published schedule for today. Check whether the employee has
        # an active assignment at this device's office at all.
        active_assignment = EmployeeAssignment.objects.filter(
            employee=employee,
            position__office=device.office,
            start_date__lte=today,
        ).filter(Q(end_date__isnull=True) | Q(end_date__gte=today)).filter(
            Q(employee__contract_end_date__isnull=True) | Q(employee__contract_end_date__gte=today)
        ).first()
        if not active_assignment:
            return "No active assignment found for this employee at this office."
        if schedules_today.exists():
            return "No work schedule is published for today."
        return "No active assignment found for this employee at this office."

    def post(self, request):
        now = timezone.now()
        today = timezone.localdate(now)

        if getattr(self, "mobile_mode", False):
            location, location_error = self._parse_location(request)
            if location_error:
                return self._json_error(location_error, 400, stage="location")
            self._mobile_location = location

        if getattr(self, "mobile_mode", False):
            from employees.models import get_linked_employee
            logged_employee = get_linked_employee(request.user)
            if logged_employee is None:
                return self._json_error("An employee profile is required for mobile attendance.", 403, stage="employee")
            assignment = (
                EmployeeAssignment.objects.filter(
                    employee=logged_employee,
                    is_primary=True,
                    start_date__lte=today,
                )
                .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
                .filter(Q(employee__contract_end_date__isnull=True) | Q(employee__contract_end_date__gte=today))
                .select_related("position__office")
                .first()
            )
            if assignment is None or assignment.position is None or assignment.position.office is None:
                return self._json_error("No active organizational office assignment was found.", 409, stage="employee")
            device = None
            attendance_scope = SimpleNamespace(
                office=assignment.position.office,
                office_id=assignment.position.office_id,
            )

        try:
            content_length = int(request.META.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            return self._json_error("The request has an invalid content length.", 400, stage="image")
        if content_length > self.max_request_size:
            return self._json_error("The captured image is too large.", 413, stage="image")

        if getattr(self, "mobile_mode", False):
            from employees.models import get_linked_employee
            logged_employee = get_linked_employee(request.user)
            if logged_employee is None:
                return self._json_error("An employee profile is required for mobile attendance.", 403, stage="employee")
            assignment = (
                EmployeeAssignment.objects.filter(
                    employee=logged_employee,
                    is_primary=True,
                    start_date__lte=today,
                )
                .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
                .filter(Q(employee__contract_end_date__isnull=True) | Q(employee__contract_end_date__gte=today))
                .select_related("position__office")
                .first()
            )
            if assignment is None or assignment.position is None or assignment.position.office is None:
                return self._json_error("No active organizational office assignment was found.", 409, stage="employee")
            device = None
            attendance_scope = SimpleNamespace(
                office=assignment.position.office,
                office_id=assignment.position.office_id,
            )
        else:
            try:
                device_id = int(request.POST.get("device_id", ""))
            except (TypeError, ValueError):
                return self._json_error("Select an active attendance device.", 400, stage="device")
            device = AttendanceDevice.objects.filter(pk=device_id).select_related("office").first()
            if device is None:
                return self._json_error("The selected attendance device does not exist.", 404, stage="device")
            attendance_scope = device

        uploaded_image = request.FILES.get("image")
        if uploaded_image is None:
            return self._json_error("Capture an image before submitting attendance.", 400, stage="image")
        if uploaded_image.content_type not in ("image/jpeg", "image/png"):
            return self._json_error("Capture a JPEG or PNG image.", 400, stage="image")
        if uploaded_image.size <= 0 or uploaded_image.size > self.max_capture_size:
            return self._json_error("The captured image is empty or exceeds the size limit.", 413, stage="image")

        image_bytes = uploaded_image.read(self.max_capture_size + 1)
        if not image_bytes or len(image_bytes) > self.max_capture_size:
            return self._json_error("The captured image is empty or exceeds the size limit.", 413, stage="image")
        try:
            frame = cv2.imdecode(
                np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
            )
        except cv2.error:
            return self._json_error(
                "The uploaded file could not be decoded as a supported image.",
                400,
                stage="image",
            )
        if frame is None or frame.size == 0:
            return self._json_error("The uploaded file could not be decoded as an image.", 400, stage="image")
        height, width = frame.shape[:2]
        if width <= 0 or height <= 0 or width > self.max_image_width or height > self.max_image_height:
            return self._json_error(
                f"Image dimensions must not exceed {self.max_image_width}x{self.max_image_height}.",
                400,
                stage="image",
            )

        if device is not None and device.status != AttendanceDevice.Status.ACTIVE:
            self._save_verification(
                device=device,
                occurred_at=now,
                event_type=FaceVerificationLog.EventType.OTHER,
                match_result=FaceVerificationLog.MatchResult.ERROR,
                failure_reason="The selected attendance device is inactive.",
            )
            return self._json_error("This attendance device is inactive.", 409, stage="device")

        # Policy is known from the selected device and server date; use it to decide whether PAD is mandatory.
        policy = self._attendance_policy(attendance_scope.office, today)
        templates = FaceTemplate.objects.filter(
            status=FaceTemplate.Status.ACTIVE,
            encrypted_template__isnull=False,
        ).select_related("employee")
        try:
            result = verify_attendance_frame(
                frame,
                templates=templates,
                threshold=self.match_threshold,
                top_two_margin=self.ambiguous_margin,
                # PAD is mandatory for every face-based attendance attempt. The effective
                # policy governs scheduling rules; it must never allow a face match to
                # substitute for a passed liveness check.
                require_liveness=True,
            )
        except (FaceEngineError, ValueError, RuntimeError, cv2.error) as exc:
            self._save_verification(
                device=device,
                occurred_at=now,
                event_type=FaceVerificationLog.EventType.OTHER,
                match_result=FaceVerificationLog.MatchResult.ERROR,
                failure_reason=str(exc),
            )
            return self._json_error(
                "Face recognition could not safely complete. No attendance was recorded.",
                503,
                stage="engine",
                detail=str(exc),
            )

        match_result = result.get("match_result", FaceVerificationLog.MatchResult.ERROR)
        score = result.get("match_score")
        if match_result != FaceVerificationLog.MatchResult.MATCHED:
            known_results = {
                FaceVerificationLog.MatchResult.NOT_MATCHED,
                FaceVerificationLog.MatchResult.AMBIGUOUS,
                FaceVerificationLog.MatchResult.ERROR,
            }
            if match_result not in known_results:
                match_result = FaceVerificationLog.MatchResult.ERROR
            self._save_verification(
                device=device,
                occurred_at=now,
                event_type=FaceVerificationLog.EventType.OTHER,
                match_result=match_result,
                liveness_result=result.get("liveness_result", FaceVerificationLog.LivenessResult.NOT_CHECKED),
                liveness_score=result.get("liveness_score"),
                match_score=score,
                failure_reason=result.get("failure_reason", "Face was not recognized."),
            )
            message = (
                "The face match is ambiguous. Move closer and try again."
                if match_result == FaceVerificationLog.MatchResult.AMBIGUOUS
                else "No enrolled employee matched this face."
                if match_result == FaceVerificationLog.MatchResult.NOT_MATCHED
                else "Face verification failed. No attendance was recorded."
            )
            failure_reason = result.get("failure_reason", "")
            if "No face was detected" in failure_reason:
                message = "No face was detected. Position one face in the camera and try again."
            elif "Multiple faces were detected" in failure_reason:
                message = "More than one face was detected. Make sure only one person is in the frame."
            return self._json_error(message, 422, stage="verification", match_result=match_result)

        employee = Employee.objects.filter(pk=result.get("employee_id")).first()
        if employee is None:
            self._save_verification(
                device=device,
                occurred_at=now,
                event_type=FaceVerificationLog.EventType.OTHER,
                match_result=FaceVerificationLog.MatchResult.ERROR,
                match_score=score,
                failure_reason="The matched employee record is unavailable.",
            )
            return self._json_error(
                "The matched employee record is unavailable. No attendance was recorded.",
                409,
                stage="employee",
            )
        if getattr(self, "mobile_mode", False):
            from employees.models import get_linked_employee
            logged_employee = get_linked_employee(request.user)
            if logged_employee is None or employee.pk != logged_employee.pk:
                self._save_verification(
                    device=device,
                    occurred_at=now,
                    event_type=FaceVerificationLog.EventType.OTHER,
                    match_result=FaceVerificationLog.MatchResult.MATCHED,
                    liveness_result=result.get("liveness_result", FaceVerificationLog.LivenessResult.NOT_CHECKED),
                    liveness_score=result.get("liveness_score"),
                    match_score=score,
                    employee=employee,
                    failure_reason="Matched employee does not match the logged-in employee.",
                )
                return self._json_error(
                    "The matched face does not belong to the logged-in employee.",
                    409,
                    stage="employee",
                )

        if employee.employment_status != Employee.EmploymentStatus.ACTIVE:
            self._save_verification(
                device=device,
                occurred_at=now,
                event_type=FaceVerificationLog.EventType.OTHER,
                match_result=FaceVerificationLog.MatchResult.MATCHED,
                liveness_result=result.get("liveness_result", FaceVerificationLog.LivenessResult.NOT_CHECKED),
                liveness_score=result.get("liveness_score"),
                match_score=score,
                employee=employee,
                failure_reason="The matched employee is inactive.",
            )
            return self._json_error(
                "This employee is inactive and cannot record attendance.",
                409,
                stage="employee",
            )

        policy = policy_for(attendance_scope.office, today)

        if getattr(self, "mobile_mode", False):
            liveness_result = result.get("liveness_result", FaceVerificationLog.LivenessResult.NOT_CHECKED)
            if liveness_result != FaceVerificationLog.LivenessResult.PASSED:
                self._save_verification(
                    device=device,
                    occurred_at=now,
                    event_type=FaceVerificationLog.EventType.OTHER,
                    match_result=FaceVerificationLog.MatchResult.MATCHED,
                    liveness_result=liveness_result,
                    liveness_score=result.get("liveness_score"),
                    match_score=score,
                    employee=employee,
                    failure_reason=result.get("liveness_failure_reason", "Liveness check did not pass."),
                )
                return self._json_error(
                    "Liveness check failed. No attendance was recorded.",
                    422,
                    stage="verification",
                )
            location = getattr(self, "_mobile_location", None)
            accuracy = location.get("accuracy")
            self._mobile_location_status = AttendanceEvent.LocationStatus.DYNAMIC
            self._mobile_location_distance = None
            self._mobile_location_reason = (
                "Dynamic field location recorded; GPS accuracy exceeds 100 metres."
                if accuracy is not None and accuracy > 100
                else "Dynamic field location recorded."
            )

        open_sessions, completed_schedules = schedule_candidates(employee, attendance_scope, now)
        if not open_sessions:
            if completed_schedules:
                duplicate_log = self._save_verification(
                    device=device,
                    occurred_at=now,
                    event_type=FaceVerificationLog.EventType.CHECK_OUT,
                    match_result=FaceVerificationLog.MatchResult.MATCHED,
                    liveness_result=result.get("liveness_result", FaceVerificationLog.LivenessResult.NOT_CHECKED),
                    liveness_score=result.get("liveness_score"),
                    match_score=score,
                    employee=employee,
                    schedule=completed_schedules[0][0],
                    failure_reason="All four attendance events are already recorded.",
                )
                return self._json_error(
                    "All four attendance events are already recorded for this schedule. The work session is finished.",
                    409,
                    stage="duplicate",
                    verification_id=duplicate_log.pk,
                    next_action=None,
                    next_action_label="Session complete",
                )
            # Check if today is a holiday for this device's office
            active_holidays = holidays_on(attendance_scope.office, today)
            if active_holidays.exists():
                holiday = active_holidays.first()
                failure_reason = f"Today is a holiday ({holiday.name}). Routine attendance is not required."
                self._save_verification(
                    device=device,
                    occurred_at=now,
                    event_type=FaceVerificationLog.EventType.OTHER,
                    match_result=FaceVerificationLog.MatchResult.MATCHED,
                    liveness_result=result.get("liveness_result", FaceVerificationLog.LivenessResult.NOT_CHECKED),
                    liveness_score=result.get("liveness_score"),
                    match_score=score,
                    employee=employee,
                    failure_reason=failure_reason,
                )
                return self._json_error(
                    "Today is a holiday. Routine attendance is not required.",
                    409,
                    stage="holiday",
                    holiday=holiday.name,
                )

            diagnosis = self._diagnose_no_schedule(employee, attendance_scope, now, today)
            failure_reason = (
                f"No valid published schedule is available for this employee, device, and server time. {diagnosis}"
                if diagnosis
                else "No valid published schedule is available for this employee, device, and server time."
            )
            self._save_verification(
                device=device,
                occurred_at=now,
                event_type=FaceVerificationLog.EventType.OTHER,
                match_result=FaceVerificationLog.MatchResult.MATCHED,
                liveness_result=result.get("liveness_result", FaceVerificationLog.LivenessResult.NOT_CHECKED),
                liveness_score=result.get("liveness_score"),
                match_score=score,
                employee=employee,
                failure_reason=failure_reason,
            )
            message = (
                f"No valid schedule is available for this employee at this device and server time. {diagnosis} "
                "Attendance is only accepted against a published schedule."
                if diagnosis
                else "No valid schedule is available for this employee at this device and server time. "
                "Attendance is only accepted against a published schedule."
            )
            return self._json_error(
                message,
                409,
                stage="schedule",
                diagnosis=diagnosis,
            )
        if len(open_sessions) > 1:
            self._save_verification(
                device=device,
                occurred_at=now,
                event_type=FaceVerificationLog.EventType.OTHER,
                match_result=FaceVerificationLog.MatchResult.MATCHED,
                liveness_result=result.get("liveness_result", FaceVerificationLog.LivenessResult.NOT_CHECKED),
                liveness_score=result.get("liveness_score"),
                match_score=score,
                employee=employee,
                failure_reason="More than one valid schedule matched; manual schedule review is required.",
            )
            return self._json_error(
                "More than one valid schedule matches this employee and device. Ask an administrator to review the schedule.",
                409,
                stage="schedule",
            )

        schedule, record = open_sessions[0]
        if record is not None and record.attendance_policy_id:
            policy = record.attendance_policy  # history keeps the policy it started under
        log_type = next_event_type(record) if record is not None else CHECK_IN
        log_type = log_type if log_type in FaceVerificationLog.EventType.values else FaceVerificationLog.EventType.OTHER

        def reject(rejection, *, locked_record=None):
            reject_log = self._save_verification(
                device=device,
                occurred_at=now,
                event_type=log_type,
                match_result=FaceVerificationLog.MatchResult.MATCHED,
                liveness_result=result.get("liveness_result", FaceVerificationLog.LivenessResult.NOT_CHECKED),
                liveness_score=result.get("liveness_score"),
                match_score=score,
                employee=employee,
                schedule=schedule,
                failure_reason=rejection.message,
            )
            state = session_state(locked_record or record) if (locked_record or record) is not None else None
            return self._json_error(
                rejection.message,
                rejection.status,
                stage=rejection.stage,
                verification_id=reject_log.pk,
                next_action=state.next_type if state else CHECK_IN,
                next_action_label=state.next_label if state else LABELS[CHECK_IN],
            )

        try:
            # Pre-flight check so policy/window rejections never wait on a lock.
            decide_next_event(schedule, record, now, policy)
        except EventRejected as rejection:
            return reject(rejection)

        # PAD is mandatory for every face-based attendance attempt. A face match
        # alone never authorizes an attendance event; the liveness result decides.
        live_result = result.get(
            "liveness_result", FaceVerificationLog.LivenessResult.NOT_CHECKED
        )
        if live_result not in (
            FaceVerificationLog.LivenessResult.PASSED,
            FaceVerificationLog.LivenessResult.FAILED,
            FaceVerificationLog.LivenessResult.INCONCLUSIVE,
            FaceVerificationLog.LivenessResult.NOT_CHECKED,
        ):
            live_result = FaceVerificationLog.LivenessResult.INCONCLUSIVE

        if live_result != FaceVerificationLog.LivenessResult.PASSED:
            live_score = result.get("liveness_score")
            live_reason = result.get("liveness_failure_reason") or {
                FaceVerificationLog.LivenessResult.FAILED: (
                    "PAD did not pass; no attendance event was recorded."
                ),
                FaceVerificationLog.LivenessResult.INCONCLUSIVE: (
                    "Liveness is unavailable or inconclusive. No attendance was recorded."
                ),
                FaceVerificationLog.LivenessResult.NOT_CHECKED: (
                    "Liveness was not checked; face attendance requires a passed liveness check. "
                    "No attendance event was recorded."
                ),
            }.get(
                live_result,
                "Liveness verification did not pass; no attendance event was recorded.",
            )
            self._save_verification(
                device=device,
                occurred_at=now,
                event_type=log_type,
                match_result=FaceVerificationLog.MatchResult.MATCHED,
                liveness_result=live_result,
                liveness_score=live_score,
                match_score=score,
                employee=employee,
                schedule=schedule,
                failure_reason=live_reason,
            )
            if live_result == FaceVerificationLog.LivenessResult.FAILED:
                message = "The face anti-spoofing check did not pass. No attendance was recorded."
            elif live_result == FaceVerificationLog.LivenessResult.NOT_CHECKED:
                message = (
                    "Liveness was not checked. Face attendance requires a passed liveness check. "
                    "No attendance was recorded."
                )
            else:
                message = "Face liveness is unavailable or inconclusive. No attendance was recorded."
            return self._json_error(
                message,
                409,
                stage="liveness",
                liveness_result=live_result,
                liveness_score=live_score,
            )

        rejection = None
        persistence_stage = "persistence_start"
        verification_log_id = None
        schedule_id = None
        incident_id = uuid.uuid4()
        existing_record_id = record.pk if record is not None else None
        expected_event_type = ""
        try:
            with transaction.atomic():
                locked_schedule = EmployeeSchedule.objects.select_for_update().select_related(
                    "employee_assignment", "employee_assignment__employee", "shift"
                ).get(pk=schedule.pk)
                schedule_id = locked_schedule.pk
                record = AttendanceRecord.objects.select_for_update().filter(schedule=locked_schedule).first()
                try:
                    # Authoritative decision under the lock: a concurrent request may have
                    # stored an event after the pre-flight check above.
                    event_type = decide_next_event(locked_schedule, record, now, policy)
                    expected_event_type = event_type
                except EventRejected as exc:
                    rejection = exc
                else:
                    persistence_stage = "verification_log_creation"
                    log = self._save_verification(
                        device=device,
                        occurred_at=now,
                        event_type=event_type,
                        match_result=FaceVerificationLog.MatchResult.MATCHED,
                        liveness_result=FaceVerificationLog.LivenessResult(
                            result.get("liveness_result", "not_checked")
                        ),
                    liveness_score=result.get("liveness_score"),
                        match_score=score,
                        employee=employee,
                        schedule=locked_schedule,
                    )
                    verification_log_id = log.pk
                    if record is None:
                        persistence_stage = "attendance_record_creation"
                        record = AttendanceRecord(schedule=locked_schedule)
                        record.full_clean(exclude=["schedule"])
                        record.save()
                    persistence_stage = "attendance_event_construction"
                    event = AttendanceEvent(
                        attendance_record=record,
                        verification_log=log,
                        event_type=event_type,
                        occurred_at=now,
                        source=AttendanceEvent.Source.FACE_RECOGNITION,
                        created_by_user=request.user,
                        latitude=(
                            self._mobile_location["latitude"]
                            if getattr(self, "mobile_mode", False) and getattr(self, "_mobile_location", None)
                            else None
                        ),
                        longitude=(
                            self._mobile_location["longitude"]
                            if getattr(self, "mobile_mode", False) and getattr(self, "_mobile_location", None)
                            else None
                        ),
                        location_accuracy_meters=(
                            Decimal(str(self._mobile_location["accuracy"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                            if getattr(self, "mobile_mode", False) and getattr(self, "_mobile_location", None) and self._mobile_location.get("accuracy") is not None
                            else None
                        ),
                        location_source=(
                            self._mobile_location.get("source", "mobile")
                            if getattr(self, "mobile_mode", False) and getattr(self, "_mobile_location", None)
                            else ""
                        ),
                        location_status=(
                            getattr(self, "_mobile_location_status", "")
                            if getattr(self, "mobile_mode", False)
                            else ""
                        ),
                        location_distance_meters=(
                            Decimal(str(self._mobile_location_distance))
                            if getattr(self, "mobile_mode", False) and getattr(self, "_mobile_location_distance", None) is not None
                            else None
                        ),
                        location_received_at=(
                            timezone.now() if getattr(self, "mobile_mode", False) else None
                        ),
                        location_validation_reason=(
                            getattr(self, "_mobile_location_reason", "") or ""
                            if getattr(self, "mobile_mode", False)
                            else ""
                        ),
                    )  # closes event = AttendanceEvent
                    liveness_result_for_event = result.get(
                        "liveness_result", FaceVerificationLog.LivenessResult.NOT_CHECKED
                    )
                    # Always treat missing/invalid liveness as unverified.
                    # The view always requires PAD for face attendance (require_liveness=True
                    # is passed to verify_attendance_frame), so if we reach this point with
                    # a non-PASSED result, it means PAD either failed, was inconclusive,
                    # or was somehow not properly executed.
                    live_reason_map = {
                        FaceVerificationLog.LivenessResult.PASSED: (
                            "Face matched by the kiosk and liveness (PAD) verification passed; "
                            "attendance event recorded."
                        ),
                        FaceVerificationLog.LivenessResult.FAILED: (
                            "The face anti-spoofing check did not pass. No attendance was recorded."
                        ),
                        FaceVerificationLog.LivenessResult.INCONCLUSIVE: (
                            "Liveness is unavailable or inconclusive. No attendance was recorded."
                        ),
                        FaceVerificationLog.LivenessResult.NOT_CHECKED: (
                            "Liveness was not checked. Face attendance requires a passed liveness check. "
                            "No attendance event was recorded."
                        ),
                    }
                    live_result_key = live_reason_map.get(
                        liveness_result_for_event,
                        "Liveness verification did not pass; no attendance event was recorded.",
                    )
                    reason = live_result_key
                    persistence_stage = "attendance_event_validation"
                    event.full_clean()
                    persistence_stage = "attendance_event_save"
                    event.save(force_insert=True)
                    persistence_stage = "workflow_application"
                    apply_event_to_record(record, locked_schedule, event_type, now, policy)
                    persistence_stage = "record_recalculation"
                    recalculate_record(record, locked_schedule)
                    persistence_stage = "record_validation"
                    record.full_clean()
                    persistence_stage = "record_save"
                    record.save()
                    persistence_stage = "transaction_completion"
                    actor = request.user
                    log_action(
                        actor_user=actor,
                        office=locked_schedule.shift.office,
                        action=AuditLog.Action.CREATE,
                        entity_type="AttendanceEvent",
                        object_id=event.pk,
                        after_data={
                            "event_type": event_type,
                            "occurred_at": event.occurred_at.isoformat(),
                            "employee_id": employee.employee_id,
                            "schedule_id": locked_schedule.pk,
                            "record_id": record.pk,
                            "source": event.source,
                        },
                        reason=f"{LABELS[event_type]} recorded via {'mobile' if getattr(self, 'mobile_mode', False) else 'kiosk'}.",
                        ip_address=request.META.get("REMOTE_ADDR"),
                    )
        except IntegrityError:
            return self._json_error(
                "This event was just recorded by another request. No duplicate was stored.",
                409,
                stage="duplicate",
            )
        except (DatabaseError, ValidationError, InvalidOperation) as exc:
            logger.exception(
                "Attendance persistence failure incident %s at stage %s for employee %s schedule %s verification_log %s channel %s",
                incident_id,
                persistence_stage,
                employee.pk,
                schedule_id,
                verification_log_id,
                "mobile" if getattr(self, "mobile_mode", False) else "kiosk",
            )
            try:
                existing_record = (
                    AttendanceRecord.objects.filter(pk=existing_record_id).first()
                    if existing_record_id is not None
                    else None
                )
                _persist_attendance_failure(
                    incident_id=incident_id,
                    employee=employee,
                    schedule=EmployeeSchedule.objects.filter(pk=schedule_id).first(),
                    record=existing_record,
                    event_type=expected_event_type,
                    channel="mobile" if getattr(self, "mobile_mode", False) else "kiosk",
                    stage=persistence_stage,
                    exc=exc,
                )
            except (DatabaseError, ValidationError, InvalidOperation) as diagnostic_error:
                logger.exception(
                    "Could not persist attendance failure incident %s: %s",
                    incident_id,
                    _sanitized_persistence_summary(diagnostic_error),
                )
            return self._json_error(
                "Attendance could not be saved consistently. No attendance event was recorded.",
                500,
                stage="attendance",
            )
        if rejection is not None:
            return reject(rejection, locked_record=record)

        # Warn if Break Out is recorded after the scheduled break end time.
        break_warning = ""
        if event_type == BREAK_OUT and locked_schedule.scheduled_break_end_at is not None and now >= locked_schedule.scheduled_break_end_at:
            break_warning = " (Break time is over)"

        state = session_state(record)
        return JsonResponse(
            {
                "ok": True,
                "stage": "attendance",
                "message": f"{LABELS[event_type]} recorded successfully.{break_warning}"
                + ("" if state.next_type is None else f" Next action: {state.next_label}."),
                "action": event_type,
                "action_label": LABELS[event_type],
                "next_action": state.next_type,
                "next_action_label": state.next_label,
                "session_complete": state.complete,
                "last_event_at": timezone.localtime(now).isoformat(),
                "employee_id": employee.employee_id,
                "employee_name": employee.full_name,
                "attendance_status": record.completeness_label,
                "check_in": timezone.localtime(record.first_check_in_at).isoformat(),
                "check_out": (
                    timezone.localtime(record.last_check_out_at).isoformat() if record.last_check_out_at else None
                ),
                "total_work_minutes": record.total_work_minutes if state.complete else None,
                "server_time": timezone.localtime(now).isoformat(),
                "verification_id": log.pk,
            },
        )


class FaceVerificationListView(ModelListView):
    model = FaceVerificationLog
    permission_required = "attendance.view_faceverificationlog"
    page_title = "Face Verification History"
    page_description = "Read-only verification outcomes; failures do not create attendance."
    search_fields = ("employee__employee_id", "device__device_code", "failure_reason")
    columns = (
        ("Employee", "employee"), ("Device", "device"),
        ("Event", "event_type"), ("Match", "match_result"),
        ("Liveness", "liveness_result"), ("Liveness score", "liveness_score"), ("Match score", "match_score"),
        ("Failure reason", "failure_reason"), ("Occurred", "occurred_at"),
    )
    detail_route = "attendance:verification-detail"
    filters = (
        ("event_type", "event_type", "Event", FaceVerificationLog.EventType.choices),
        ("match_result", "match_result", "Match result", FaceVerificationLog.MatchResult.choices),
        ("liveness_result", "liveness_result", "Liveness", FaceVerificationLog.LivenessResult.choices),
        ("device_id", "device_id", "Device", lambda: AttendanceDevice.objects.order_by(
            "device_name").values_list("id", "device_name")),
    )
    date_filter_fields = {"start_date": "occurred_at", "end_date": "occurred_at"}

    def get_queryset(self):
        queryset = super().get_queryset().select_related("employee", "device")
        from core.permissions import is_employee_group_user, resolve_employee_assignment

        if is_employee_group_user(self.request.user):
            assignment = resolve_employee_assignment(self.request.user)
            if assignment is None:
                return queryset.none()
            office = assignment.position.office
            return queryset.filter(
                employee=assignment.employee,
                schedule__employee_assignment__employee=assignment.employee,
                schedule__employee_assignment__position__office=office,
                schedule__shift__office=office,
            )
        return queryset


class FaceVerificationDetailView(ModelDetailView):
    model = FaceVerificationLog
    permission_required = "attendance.view_faceverificationlog"
    page_title = "Face Verification Details"
    back_route = "attendance:verification-list"
    detail_fields = (
        ("Employee", "employee"), ("Device", "device"), ("Schedule", "schedule"),
        ("Event type", "event_type"), ("Match result", "match_result"),
        ("Liveness result", "liveness_result"), ("Liveness score", "liveness_score"), ("Match score", "match_score"),
        ("Failure reason", "failure_reason"), ("Model version", "model_version"),
        ("Occurred", "occurred_at"),
    )

    def get_queryset(self):
        queryset = super().get_queryset().select_related("employee", "device", "schedule")
        from core.permissions import is_employee_group_user, resolve_employee_assignment

        if is_employee_group_user(self.request.user):
            assignment = resolve_employee_assignment(self.request.user)
            if assignment is None:
                return queryset.none()
            office = assignment.position.office
            return queryset.filter(
                employee=assignment.employee,
                schedule__employee_assignment__employee=assignment.employee,
                schedule__employee_assignment__position__office=office,
                schedule__shift__office=office,
            )
        return queryset


class AttendanceRecordListView(ModelListView):
    model = AttendanceRecord
    permission_required = "attendance.view_attendancerecord"
    page_title = "Attendance History"
    page_description = "Attendance summaries are read-only and based on recorded schedules and events."
    search_fields = (
        "schedule__employee_assignment__employee__employee_id",
        "schedule__employee_assignment__employee__full_name",
        "schedule__employee_assignment__employee__full_name",
        "schedule__shift__office__office_name",
    )
    columns = (
        ("Employee", "schedule__employee_assignment__employee"),
        ("Office", "schedule__shift__office"), ("Department", "schedule__employee_assignment__position__department"),
        ("Position", "schedule__employee_assignment__position"), ("Work date", "schedule__work_date"),
        ("Scheduled start", "schedule__scheduled_start_at"), ("Scheduled end", "schedule__scheduled_end_at"),
        ("Check-in", "first_check_in_at"), ("Check-out", "last_check_out_at"),
        ("Late (min)", "late_minutes"), ("Status", "completeness_label"),
    )
    detail_route = "attendance:record-detail"
    filters = (
        ("office_id", "schedule__shift__office_id", "Office", lambda: Office.objects.order_by("office_name").values_list("id", "office_name")),
        ("department_id", "schedule__employee_assignment__position__department_id", "Department", lambda: Department.objects.order_by("department_name").values_list("id", "department_name")),
        ("employee_id", "schedule__employee_assignment__employee_id", "Employee", lambda: Employee.objects.order_by("employee_id").values_list("id", "employee_id")),
        ("incomplete", "is_incomplete", "Completeness", (("True", "Incomplete only"), ("False", "Complete only"))),
    )
    date_filter_fields = {"start_date": "schedule__work_date", "end_date": "schedule__work_date"}

    def get_queryset(self):
        from core.permissions import (
            is_branch_hr_user,
            is_employee_group_user,
            resolve_branch_hr_office,
            resolve_employee_assignment,
        )

        hr_office = resolve_branch_hr_office(self.request.user) if is_branch_hr_user(self.request.user) else None
        if is_branch_hr_user(self.request.user) and hr_office is None:
            return self.model.objects.none()
        employee_assignment = (
            resolve_employee_assignment(self.request.user)
            if is_employee_group_user(self.request.user)
            else None
        )
        if is_employee_group_user(self.request.user) and employee_assignment is None:
            return self.model.objects.none()

        queryset = super().get_queryset().select_related(
            "schedule__employee_assignment__employee",
            "schedule__employee_assignment__position__office",
            "schedule__employee_assignment__position__department",
            "schedule__shift__office",
        )
        query = self.request.GET.get("q", "").strip()
        if query and self.search_fields:
            search = Q()
            for field in self.search_fields:
                search |= Q(**{f"{field}__icontains": query})
            queryset = queryset.filter(search)
        for name, lookup, _label, _choices in self.filters:
            if is_employee_group_user(self.request.user) and name in {
                "office_id", "department_id", "employee_id",
            }:
                continue
            if self.model.__name__ == "Holiday" and name == "office_id":
                continue
            if is_branch_hr_user(self.request.user) and name == "office_id":
                queryset = queryset.filter(**{lookup: hr_office.pk})
                continue
            value = self.request.GET.get(name, "")
            if value:
                if is_branch_hr_user(self.request.user):
                    from employees.models import Employee
                    from organization.models import Department, Position
                    from attendance.models import Shift

                    allowed = True
                    if name == "department_id":
                        allowed = Department.objects.filter(pk=value, office=hr_office).exists()
                    elif name == "employee_id":
                        allowed = Employee.objects.filter(
                            pk=value,
                            assignments__is_primary=True,
                            assignments__position__office=hr_office,
                        ).exists()
                    elif name == "position_id":
                        allowed = Position.objects.filter(pk=value, office=hr_office).exists()
                    elif name == "shift_id":
                        allowed = Shift.objects.filter(pk=value, office=hr_office).exists()
                    if not allowed:
                        continue
                queryset = queryset.filter(**{lookup: value})
        # Direct office scoping for models that own an office.
        if is_branch_hr_user(self.request.user) and hr_office is not None:
            model_fields = {f.name for f in self.model._meta.fields}
            if "office" in model_fields and not any(f[0] == "office_id" for f in self.filters):
                queryset = queryset.filter(office=hr_office)
        start = self.request.GET.get("start_date", "")
        end = self.request.GET.get("end_date", "")
        if start and "start_date" in self.date_filter_fields:
            queryset = queryset.filter(**{f"{self.date_filter_fields['start_date']}__gte": start})
        if end and "end_date" in self.date_filter_fields:
            queryset = queryset.filter(**{f"{self.date_filter_fields['end_date']}__lte": end})
        # Model-specific branch scoping for branch HR (Staff HR / legacy HR Office).
        from core.permissions import is_branch_hr_user, resolve_branch_hr_office

        if is_branch_hr_user(self.request.user):
            hr_office = resolve_branch_hr_office(self.request.user)
            if hr_office is not None:
                model_name = self.model.__name__
                if model_name == "EmployeeSchedule":
                    queryset = queryset.filter(employee_assignment__position__office=hr_office)
                elif model_name == "AttendanceRecord":
                    queryset = queryset.filter(schedule__employee_assignment__position__office=hr_office)
                elif model_name == "AttendanceEvent":
                    queryset = queryset.filter(attendance_record__schedule__employee_assignment__position__office=hr_office)
                elif model_name == "Holiday":
                    queryset = queryset.filter(Q(office=hr_office) | Q(office__isnull=True))
                elif model_name in {"FaceTemplate", "FaceVerificationLog"}:
                    employee_lookup = "employee" if model_name == "FaceTemplate" else "employee"
                    queryset = queryset.filter(
                        **{f"{employee_lookup}__assignments__position__office": hr_office,
                           f"{employee_lookup}__assignments__is_primary": True}
                    ).distinct()
        if is_employee_group_user(self.request.user) and employee_assignment is not None:
            employee = employee_assignment.employee
            office = employee_assignment.position.office
            model_name = self.model.__name__
            if model_name == "EmployeeSchedule":
                queryset = queryset.filter(employee_assignment__employee=employee)
            elif model_name in {"AttendanceRecord", "AttendanceRecordDetailView"}:
                queryset = queryset.filter(schedule__employee_assignment__employee=employee)
            elif model_name == "AttendanceEvent":
                queryset = queryset.filter(attendance_record__schedule__employee_assignment__employee=employee)
            elif model_name == "Holiday":
                queryset = queryset.filter(
                    Q(office=employee_assignment.position.office) | Q(office__isnull=True)
                )
            elif model_name in {"FaceTemplate", "FaceVerificationLog"}:
                queryset = queryset.filter(employee=employee)
            else:
                model_fields = {field.name for field in self.model._meta.fields}
                if "office" in model_fields:
                    queryset = queryset.filter(office=office)
                elif model_name in {"Shift", "AttendanceDevice", "Holiday", "AttendancePolicy"}:
                    queryset = queryset.filter(office=office)
        # Custom status filter: "Present" means first_check_in_at IS NOT NULL
        status_value = self.request.GET.get("status", "")
        if status_value == AttendanceRecord.Status.PRESENT:
            queryset = queryset.filter(first_check_in_at__isnull=False)
        elif status_value:
            queryset = queryset.filter(status=status_value)
        return queryset


    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Add status filter control for UI (not in self.filters because it's handled manually in get_queryset)
        from core.permissions import is_branch_hr_user, is_employee_group_user, resolve_branch_hr_office
        from core.permissions import is_director_user, is_xefe_user, resolve_director_office, resolve_xefe_assignment
        
        hr_office = resolve_branch_hr_office(self.request.user) if is_branch_hr_user(self.request.user) else None
        director_only = is_director_user(self.request.user)
        xefe_only = is_xefe_user(self.request.user)
        director_office = resolve_director_office(self.request.user) if director_only else None
        xefe_assignment = resolve_xefe_assignment(self.request.user) if xefe_only else None
        
        # Determine if status filter should be shown (hidden for employee group users)
        show_status_filter = not is_employee_group_user(self.request.user)
        if show_status_filter:
            if director_only or xefe_only:
                # For director/xefe, status filter is shown
                pass
            elif is_branch_hr_user(self.request.user) and hr_office is None:
                show_status_filter = False
        
        if show_status_filter:
            status_choices = AttendanceRecord.Status.choices
            status_control = {
                "name": "status",
                "label": "Status",
                "value": self.request.GET.get("status", ""),
                "choices": status_choices,
            }
            # Insert status filter at the beginning of filter_controls
            context["filter_controls"].insert(0, status_control)
        
        return context


class TodayAttendanceListView(AttendanceRecordListView):
    page_title = "Today's Attendance"
    columns = (
        ("Employee Code", "employee_id"),
        ("Employee Name", "employee_name"),
        ("Office", "office"),
        ("Shift", "shift"),
        ("Scheduled Time", "scheduled_time"),
        ("Check-in Time", "check_in"),
        ("Check-out Time", "check_out"),
        ("Late Minutes", "late_minutes"),
        ("Attendance Status", "completeness_label"),
    )

    def get_queryset(self):
        queryset = super().get_queryset()
        if "start_date" not in self.request.GET and "end_date" not in self.request.GET:
            queryset = queryset.filter(schedule__work_date=timezone.localdate())
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        def display_time(value):
            return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else "—"

        context["table_rows"] = []
        for record in context["items"]:
            schedule = record.schedule
            employee = schedule.employee_assignment.employee
            context["table_rows"].append(
                {
                    "cells": [
                        employee.employee_id,
                        employee.full_name,
                        schedule.shift.office.office_name,
                        schedule.shift.name,
                        f"{display_time(schedule.scheduled_start_at)} – {display_time(schedule.scheduled_end_at)}",
                        display_time(record.first_check_in_at),
                        display_time(record.last_check_out_at),
                        record.late_minutes,
                        record.completeness_label,
                    ],
                    "url": reverse("attendance:record-detail", args=[record.pk]),
                }
            )
        return context


class AttendanceReportView(AttendanceAccessMixin, TemplateView):
    template_name = "attendance/report.html"
    permission_required = "attendance.view_attendancerecord"
    report_type = "daily"
    report_titles = {
        "daily": "Daily Attendance Report",
        "monthly": "Monthly Attendance Report",
        "offices": "Office Attendance Report",
        "employees": "Employee Attendance Report",
    }

    def get_form(self):
        data = self.request.GET.copy()
        today = timezone.localdate()
        if self.report_type == "daily":
            default_start = default_end = today
        elif self.report_type == "monthly":
            default_start = today.replace(day=1)
            default_end = today
        else:
            default_start = default_end = None
        if "start_date" not in data and default_start:
            data["start_date"] = default_start.isoformat()
        if "end_date" not in data and default_end:
            data["end_date"] = default_end.isoformat()
        return AttendanceReportFilterForm(data, user=self.request.user)

    def get_records(self, form):
        records = _scope_attendance_record_queryset(
            self.request.user,
            AttendanceRecord.objects.select_related(
                "schedule__employee_assignment__employee",
                "schedule__employee_assignment__position__office",
                "schedule__shift__office",
            ),
        ).order_by(
            "schedule__work_date",
            "schedule__shift__office__office_name",
            "schedule__employee_assignment__employee__employee_id",
        )
        filters = form.cleaned_data
        if filters.get("start_date"):
            records = records.filter(schedule__work_date__gte=filters["start_date"])
        if filters.get("end_date"):
            records = records.filter(schedule__work_date__lte=filters["end_date"])
        if filters.get("office"):
            records = records.filter(schedule__shift__office=filters["office"])
        if filters.get("department"):
            records = records.filter(
                schedule__employee_assignment__position__department=filters["department"]
            )
        if filters.get("employee"):
            records = records.filter(
                schedule__employee_assignment__employee=filters["employee"]
            )
        if filters.get("status"):
            records = records.filter(status=filters["status"])
        return records

    @staticmethod
    def detail_row(record):
        employee = record.schedule.employee_assignment.employee
        office = record.schedule.shift.office
        event_times = {
            event_type: record.events.filter(event_type=event_type)
            .order_by("occurred_at")
            .values_list("occurred_at", flat=True)
            .first()
            for event_type in (
                AttendanceEvent.EventType.CHECK_IN,
                AttendanceEvent.EventType.BREAK_START,
                AttendanceEvent.EventType.BREAK_END,
                AttendanceEvent.EventType.CHECK_OUT,
            )
        }
        return (
            record.schedule.work_date,
            employee.employee_id,
            employee.full_name,
            office.office_code,
            office.office_name,
            event_times[AttendanceEvent.EventType.CHECK_IN],
            event_times[AttendanceEvent.EventType.BREAK_START],
            event_times[AttendanceEvent.EventType.BREAK_END],
            event_times[AttendanceEvent.EventType.CHECK_OUT],
            record.total_work_minutes,
            record.late_minutes,
            record.completeness_label,
        )

    def get_aggregate_rows(self, records):
        if self.report_type == "offices":
            aggregates = records.values(
                "schedule__shift__office__office_code",
                "schedule__shift__office__office_name",
            ).annotate(
                record_count=Count("pk"),
                present_count=Count("pk", filter=Q(first_check_in_at__isnull=False)),
                late_count=Count("pk", filter=Q(status=AttendanceRecord.Status.LATE)),
                absent_count=Count("pk", filter=Q(status=AttendanceRecord.Status.ABSENT)),
                incomplete_count=Count("pk", filter=Q(status=AttendanceRecord.Status.INCOMPLETE)),
                total_work_minutes=Sum("total_work_minutes"),
            ).order_by("schedule__shift__office__office_name")
            return (
                ("Office code", "Office", "Records", "Present", "Late", "Absent", "Incomplete", "Work minutes"),
                [
                    (
                        item["schedule__shift__office__office_code"],
                        item["schedule__shift__office__office_name"],
                        item["record_count"],
                        item["present_count"],
                        item["late_count"],
                        item["absent_count"],
                        item["incomplete_count"],
                        item["total_work_minutes"] or 0,
                    )
                    for item in aggregates
                ],
            )
        aggregates = records.values(
            "schedule__employee_assignment__employee__employee_id",
            "schedule__employee_assignment__employee__full_name",
            "schedule__shift__office__office_name",
        ).annotate(
            record_count=Count("pk"),
            present_count=Count("pk", filter=Q(first_check_in_at__isnull=False)),
            late_count=Count("pk", filter=Q(status=AttendanceRecord.Status.LATE)),
            absent_count=Count("pk", filter=Q(status=AttendanceRecord.Status.ABSENT)),
            incomplete_count=Count("pk", filter=Q(status=AttendanceRecord.Status.INCOMPLETE)),
            total_work_minutes=Sum("total_work_minutes"),
        ).order_by(
            "schedule__employee_assignment__employee__employee_id"
        )
        return (
            ("Employee code", "Employee", "Office", "Records", "Present", "Late", "Absent", "Incomplete", "Work minutes"),
            [
                (
                    item["schedule__employee_assignment__employee__employee_id"],
                    item["schedule__employee_assignment__employee__full_name"],
                    item["schedule__shift__office__office_name"],
                    item["record_count"],
                    item["present_count"],
                    item["late_count"],
                    item["absent_count"],
                    item["incomplete_count"],
                    item["total_work_minutes"] or 0,
                )
                for item in aggregates
            ],
        )

    @staticmethod
    def csv_value(value):
        value = "" if value is None else str(value)
        if value.startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + value
        return value

    @staticmethod
    def xlsx_cell_value(value):
        if value is None or value == "":
            return ""
        if hasattr(value, "tzinfo") and hasattr(value, "strftime"):
            try:
                if timezone.is_aware(value):
                    value = timezone.localtime(value)
                return value.replace(tzinfo=None)
            except Exception:
                pass
        if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day") and not hasattr(value, "hour"):
            return value
        if isinstance(value, (int, float, Decimal)):
            return value
        text = str(value)
        if text.startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + text
        return text

    def export_metadata_lines(self, form):
        cleaned = getattr(form, "cleaned_data", {}) or {}
        parts = []
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        if start or end:
            parts.append(f"Period: {start or '—'} to {end or '—'}")
        for key, label in (("office", "Office"), ("department", "Department"), ("employee", "Employee"), ("status", "Status")):
            value = cleaned.get(key)
            if value:
                parts.append(f"{label}: {getattr(value, 'get_status_display', lambda: value)() if key == 'status' else value}")
        parts.append(f"Authorized scope applied: yes")
        return parts

    def csv_response(self, headers, rows, form):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="{self.report_type}-attendance-report.csv"'
        )
        response.write("\ufeff")
        writer = csv.writer(response)
        writer.writerow([self.report_titles[self.report_type]])
        for line in self.export_metadata_lines(form):
            writer.writerow([self.csv_value(line)])
        writer.writerow([])
        writer.writerow(headers)
        writer.writerows(
            [[self.csv_value(value) for value in row] for row in rows]
        )
        return response

    def xlsx_response(self, headers, rows, form):
        from io import BytesIO
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = self.report_type[:31] or "Report"
        row_num = 1
        ws.cell(row=row_num, column=1, value=self.report_titles[self.report_type]).font = Font(bold=True, size=13)
        row_num += 1
        for line in self.export_metadata_lines(form):
            ws.cell(row=row_num, column=1, value=line)
            row_num += 1
        row_num += 1
        header_row = row_num
        header_fill = PatternFill("solid", fgColor="E9ECEF")
        for col, header in enumerate(headers, start=1):
            cell = ws.cell(row=row_num, column=col, value=header)
            cell.font = Font(bold=True)
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        row_num += 1
        for row in rows:
            for col, value in enumerate(row, start=1):
                cell = ws.cell(row=row_num, column=col, value=self.xlsx_cell_value(value))
                if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day") and not hasattr(value, "hour"):
                    cell.number_format = "yyyy-mm-dd"
                elif hasattr(value, "hour") and hasattr(value, "year"):
                    cell.number_format = "yyyy-mm-dd hh:mm"
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            row_num += 1
        ws.freeze_panes = f"A{header_row + 1}"
        if headers and rows:
            ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(headers))}{header_row + len(rows)}"
        for col, header in enumerate(headers, start=1):
            max_len = len(str(header))
            sample_rows = rows[:50]
            for row in sample_rows:
                if col - 1 < len(row):
                    val = row[col - 1]
                    if hasattr(val, "strftime"):
                        max_len = max(max_len, 16)
                    else:
                        max_len = max(max_len, min(len(str(val or "")), 40))
            ws.column_dimensions[get_column_letter(col)].width = min(max(max_len + 2, 10), 42)
        output = BytesIO()
        wb.save(output)
        output.seek(0)
        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="{self.report_type}-attendance-report.xlsx"'
        )
        return response

    def get(self, request, *args, **kwargs):
        self.report_type = kwargs.get("report_type", self.report_type)
        if self.report_type not in self.report_titles:
            return HttpResponse(status=404)
        form = self.get_form()
        if not form.is_valid():
            context = self.get_context_data(
                report_form=form,
                rows=[],
                headers=[],
                report_title=self.report_titles[self.report_type],
                report_type=self.report_type,
                result_count=0,
                csv_export_url="?",
                xlsx_export_url="?",
            )
            return self.render_to_response(context, status=400)
        records = self.get_records(form)
        if self.report_type in ("offices", "employees"):
            headers, rows = self.get_aggregate_rows(records)
        else:
            headers = (
                "Work date", "Employee code", "Employee", "Office code", "Office",
                "Check-in", "Break Out", "Break In", "Check-out",
                "Work minutes", "Late minutes", "Status",
            )
            rows = [self.detail_row(record) for record in records]
        export = request.GET.get("export")
        if export == "csv":
            return self.csv_response(headers, rows, form)
        if export == "xlsx":
            return self.xlsx_response(headers, rows, form)
        csv_params = {key: val[0] for key, val in request.GET.lists()}
        csv_params["export"] = "csv"
        xlsx_params = {key: val[0] for key, val in request.GET.lists()}
        xlsx_params["export"] = "xlsx"
        context = self.get_context_data(
            report_form=form,
            headers=headers,
            rows=rows,
            report_title=self.report_titles[self.report_type],
            report_type=self.report_type,
            result_count=len(rows),
            csv_export_url=f"?{urlencode(csv_params)}",
            xlsx_export_url=f"?{urlencode(xlsx_params)}",
        )
        return self.render_to_response(context)


class AttendanceRecordDetailView(ModelDetailView):
    model = AttendanceRecord
    permission_required = "attendance.view_attendancerecord"
    page_title = "Attendance Details"
    back_route = "attendance:history"
    detail_fields = (
        ("Employee", "schedule__employee_assignment__employee"),
        ("Office", "schedule__shift__office"),
        ("Department", "schedule__employee_assignment__position__department"),
        ("Position", "schedule__employee_assignment__position"),
        ("Work date", "schedule__work_date"), ("Shift", "schedule__shift"),
        ("Scheduled start", "schedule__scheduled_start_at"),
        ("Scheduled end", "schedule__scheduled_end_at"),
        ("First check-in", "first_check_in_at"), ("Last check-out", "last_check_out_at"),
        ("Total work (minutes)", "total_work_minutes"), ("Late (minutes)", "late_minutes"),
        ("Early leave (minutes)", "early_leave_minutes"),
        ("Approved overtime (minutes)", "approved_overtime_minutes"),
        ("Status", "completeness_label"), ("Remarks", "remarks"),
    )

    def get_queryset(self):
        queryset = super().get_queryset().select_related(
            "schedule__employee_assignment__employee",
            "schedule__employee_assignment__position__office",
            "schedule__employee_assignment__position__department",
            "schedule__shift",
        ).prefetch_related("events", "schedule__face_verification_logs")
        return _scope_attendance_record_queryset(self.request.user, queryset)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["events"] = self.object.events.select_related("created_by_user", "verification_log")
        context["verification_logs"] = (
            self.object.schedule.face_verification_logs.select_related("device")
            if self.request.user.has_perm("attendance.view_faceverificationlog")
            else []
        )
        context["requests"] = EmployeeRequest.objects.filter(
            attendance_record=self.object
        ).select_related("employee_assignment__employee")
        return context


class AttendanceEventListView(ModelListView):
    model = AttendanceEvent
    permission_required = "attendance.view_attendanceevent"
    page_title = "Attendance Events"
    page_description = "Immutable attendance event history."
    search_fields = (
        "attendance_record__schedule__employee_assignment__employee__employee_id",
        "attendance_record__schedule__employee_assignment__employee__full_name",
        "reason",
    )
    columns = (
        ("Record", "attendance_record_id"), ("Employee", "attendance_record__employee"),
        ("Office", "attendance_record__office"), ("Event", "event_type"),
        ("Occurred", "occurred_at"), ("Source", "source"), ("Created by", "created_by_user"),
        ("Latitude", "latitude"), ("Longitude", "longitude"), ("GPS accuracy (m)", "location_accuracy_meters"),
    )
    detail_route = "attendance:event-detail"
    filters = (
        ("event_type", "event_type", "Event type", AttendanceEvent.EventType.choices),
        ("source", "source", "Source", AttendanceEvent.Source.choices),
        ("office_id", "attendance_record__schedule__shift__office_id", "Office", lambda: Office.objects.order_by("office_name").values_list("id", "office_name")),
    )
    date_filter_fields = {"start_date": "occurred_at", "end_date": "occurred_at"}

    def get_queryset(self):
        queryset = super().get_queryset().select_related(
            "attendance_record__schedule__employee_assignment__employee",
            "attendance_record__schedule__employee_assignment__position__office",
            "attendance_record__schedule__employee_assignment__position__department",
            "attendance_record__schedule__shift__office", "created_by_user",
        )
        records = _scope_attendance_record_queryset(self.request.user, AttendanceRecord.objects.all())
        return queryset.filter(attendance_record__in=records)


class AttendanceEventDetailView(ModelDetailView):
    model = AttendanceEvent
    permission_required = "attendance.view_attendanceevent"
    page_title = "Attendance Event Details"
    back_route = "attendance:event-list"
    detail_fields = (
        ("Attendance record", "attendance_record_id"),
        ("Employee", "attendance_record__employee"), ("Office", "attendance_record__office"),
        ("Verification log", "verification_log"), ("Event type", "event_type"),
        ("Occurred at", "occurred_at"), ("Source", "source"),
        ("Created by", "created_by_user"), ("Reason", "reason"), ("Created", "created_at"),
        ("Latitude", "latitude"), ("Longitude", "longitude"),
        ("GPS accuracy (metres)", "location_accuracy_meters"), ("Location source", "location_source"),
        ("Location received at", "location_received_at"), ("Location status", "location_status"),
        ("Location validation reason", "location_validation_reason"),
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.object.latitude is not None and self.object.longitude is not None:
            context["map_url"] = (
                "https://www.google.com/maps?q="
                f"{format(Decimal(str(self.object.latitude)), 'f')},"
                f"{format(Decimal(str(self.object.longitude)), 'f')}"
            )
        return context

    def get_queryset(self):
        queryset = super().get_queryset().select_related(
            "attendance_record__schedule__employee_assignment__employee",
            "attendance_record__schedule__shift__office", "verification_log", "created_by_user",
        )
        # Scope direct event URLs by the authorized attendance records so a
        # user cannot reach another department's event by guessing its ID.
        records = _scope_attendance_record_queryset(self.request.user, AttendanceRecord.objects.all())
        return queryset.filter(attendance_record__in=records)


def _scope_attendance_record_queryset(user, queryset):
    """Scope attendance records for reporting/list/detail/review access.

    Uses established role resolvers/assignments. Does not infer attendance
    authority from EmployeeRequest approval-task assignment. Executive
    (Ministro/Vice) scope is institution-wide read reporting only when explicitly
    configured; it never grants unrestricted cross-institution access or mutations.
    """
    from core.permissions import (
        DIRECTOR_GROUP, STAFF_HR_GROUP, XEFE_GROUP,
        is_branch_hr_user, is_employee_group_user, is_executive_institution_user, is_hr_user,
        is_it_admin_user,
        resolve_branch_hr_office, resolve_director_office,
        resolve_employee_assignment, resolve_executive_institution_scope,
        resolve_hr_office, resolve_xefe_assignment,
        user_has_exact_group,
    )
    if getattr(user, "is_superuser", False) or is_it_admin_user(user):
        return queryset
    # Explicit executive precedence for multi-group accounts.
    if is_executive_institution_user(user):
        scope, _err = resolve_executive_institution_scope(user)
        if scope is None:
            return queryset.none()
        return queryset.filter(schedule__shift__office__in=scope["offices_qs"])
    if is_employee_group_user(user):
        assignment = resolve_employee_assignment(user)
        if assignment is None:
            return queryset.none()
        office = assignment.position.office
        return queryset.filter(
            schedule__employee_assignment__employee=assignment.employee,
            schedule__employee_assignment__position__office=office,
            schedule__shift__office=office,
        )
    if user_has_exact_group(user, XEFE_GROUP):
        assignment = resolve_xefe_assignment(user)
        if assignment is None:
            return queryset.none()
        return queryset.filter(
            schedule__employee_assignment__position__office=assignment.position.office,
            schedule__employee_assignment__position__department=assignment.position.department,
            schedule__shift__office=assignment.position.office,
        )
    if user_has_exact_group(user, DIRECTOR_GROUP):
        office = resolve_director_office(user)
        if office is None:
            return queryset.none()
        return queryset.filter(schedule__employee_assignment__position__office=office, schedule__shift__office=office)
    if is_branch_hr_user(user):
        office = resolve_branch_hr_office(user)
        if office is None:
            return queryset.none()
        return queryset.filter(schedule__employee_assignment__position__office=office, schedule__shift__office=office)
    if is_hr_user(user):
        office = resolve_hr_office(user)
        if office is None:
            return queryset.none()
        return queryset.filter(schedule__employee_assignment__position__office=office, schedule__shift__office=office)
    return queryset.none()


def _scope_diagnostic_querysets(user, failed_verifications, persistence_failures):
    """Apply organizational scope for the diagnostic sections of the Attendance
    Exceptions page.

    Intended audience: Staff HR (own branch), superusers, and IT Admin. Xefe,
    Director, HR Office, Employee and any other role are not in the intended
    audience and fail closed; they retain their existing reports and record
    details.
    """
    from core.permissions import (
        is_it_admin_user,
        is_staff_hr_user,
        resolve_staff_hr_office,
    )

    if getattr(user, "is_superuser", False) or is_it_admin_user(user):
        return failed_verifications, persistence_failures

    if is_staff_hr_user(user):
        office = resolve_staff_hr_office(user)
        if office is None:
            return (
                FaceVerificationLog.objects.none(),
                AttendancePersistenceFailure.objects.none(),
            )
        office_scope = (
            Q(schedule__employee_assignment__position__office=office)
            & Q(schedule__shift__office=office)
        )
        return (
            failed_verifications.filter(office_scope),
            persistence_failures.filter(office_scope),
        )

    # Xefe, Director, HR Office, Employee and any other role are not in the
    # intended audience for this page. Fail closed rather than inferring data
    # scope from the group name or is_staff.
    return (
        FaceVerificationLog.objects.none(),
        AttendancePersistenceFailure.objects.none(),
    )


class AttendanceExceptionsView(AttendanceAccessMixin, TemplateView):
    """Read-only Attendance Exceptions page.

    Access gate: dedicated ``attendance.view_attendance_exceptions``
    permission (superusers pass through established Django behavior). This is
    not gated on ``view_attendancerecord`` alone, since other roles need that
    permission for ordinary attendance reports.

    Underlying data permissions still apply per section:
    verification logs (view_faceverificationlog), persistence failures
    (view_attendancepersistencefailure).
    """

    template_name = "attendance/exceptions.html"
    permission_required = "attendance.view_attendance_exceptions"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        form_data = self.request.GET.copy()

        start_date = parse_date(form_data.get("start_date", "") or "")
        end_date = parse_date(form_data.get("end_date", "") or "")
        employee_id = (form_data.get("employee") or "").strip()
        department_id = (form_data.get("department") or "").strip()

        failed_verifications = FaceVerificationLog.objects.select_related(
            "employee", "device"
        ).all()
        persistence_failures = AttendancePersistenceFailure.objects.select_related(
            "employee", "schedule"
        ).all()

        failed_verifications, persistence_failures = _scope_diagnostic_querysets(
            user, failed_verifications, persistence_failures
        )

        # Read-only filters shared by both diagnostic sections. Opening or
        # filtering the page changes no data.
        if start_date:
            failed_verifications = failed_verifications.filter(occurred_at__date__gte=start_date)
            persistence_failures = persistence_failures.filter(occurred_at__date__gte=start_date)
        if end_date:
            failed_verifications = failed_verifications.filter(occurred_at__date__lte=end_date)
            persistence_failures = persistence_failures.filter(occurred_at__date__lte=end_date)
        if employee_id:
            failed_verifications = failed_verifications.filter(employee_id=employee_id)
            persistence_failures = persistence_failures.filter(employee_id=employee_id)
        if department_id:
            failed_verifications = failed_verifications.filter(
                schedule__employee_assignment__position__department_id=department_id
            )
            persistence_failures = persistence_failures.filter(
                schedule__employee_assignment__position__department_id=department_id
            )

        # Verification logs: failed or inconclusive only, using actual model
        # result values.
        failed_verifications = failed_verifications.filter(
            Q(match_result__in=[
                FaceVerificationLog.MatchResult.NOT_MATCHED,
                FaceVerificationLog.MatchResult.AMBIGUOUS,
                FaceVerificationLog.MatchResult.ERROR,
            ])
            | Q(liveness_result__in=[
                FaceVerificationLog.LivenessResult.FAILED,
                FaceVerificationLog.LivenessResult.INCONCLUSIVE,
                FaceVerificationLog.LivenessResult.NOT_CHECKED,
            ])
        ).order_by("-occurred_at")[:100]

        persistence_failures = persistence_failures.order_by("-occurred_at", "-pk")[:100]

        can_view_verification_logs = user.has_perm("attendance.view_faceverificationlog")
        can_view_persistence_failures = user.has_perm("attendance.view_attendancepersistencefailure")

        # Scoped filter choices: Staff HR sees own branch, all departments.
        today = timezone.localdate()
        active_asn = Q(
            assignments__is_primary=True,
            assignments__start_date__lte=today,
        ) & (Q(assignments__end_date__isnull=True) | Q(assignments__end_date__gte=today))
        active_asn &= (Q(contract_end_date__isnull=True) | Q(contract_end_date__gte=today))
        from core.permissions import is_staff_hr_user, resolve_staff_hr_office
        office = resolve_staff_hr_office(user) if is_staff_hr_user(user) else None
        emp_qs = Employee.objects.filter(active_asn)
        department_choices = []
        if office is not None:
            emp_qs = emp_qs.filter(assignments__position__office=office)
            department_choices = list(
                Department.objects.filter(office=office, is_active=True)
                .order_by("department_name")
                .values("id", "department_name")
            )
        employee_choices = list(
            emp_qs.distinct().order_by("employee_id").values("id", "employee_id", "full_name")[:200]
        )

        context.update(
            page_title="Attendance Exceptions",
            start_date=form_data.get("start_date", ""),
            end_date=form_data.get("end_date", ""),
            employee_filter=employee_id,
            department_filter=department_id,
            employee_choices=employee_choices,
            department_choices=department_choices,
            failed_verifications=failed_verifications if can_view_verification_logs else [],
            persistence_failures=(
                persistence_failures if can_view_persistence_failures else []
            ),
            can_view_verification_logs=can_view_verification_logs,
            can_view_persistence_failures=can_view_persistence_failures,
        )
        return context

class MobileFaceAttendanceView(AttendanceAccessMixin, TemplateView):
    template_name = "attendance/mobile_attendance.html"
    permission_required = "attendance.use_mobile_face_attendance"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        engine_status = get_face_model_status()
        try:
            get_encryption_key()
            encryption_configured = True
            encryption_error = ""
        except RuntimeError as exc:
            encryption_configured = False
            encryption_error = str(exc)
        liveness_status = get_face_liveness_status()
        context.update(
            page_title="Mobile Face Attendance",
            mobile_device_free=True,
            engine_status=engine_status,
            model_version="YuNet/SFace-2021dec",
            encryption_configured=encryption_configured,
            encryption_error=encryption_error,
            liveness_available=bool(liveness_status.get("available")),
            liveness_status=liveness_status,
            server_time=timezone.localtime(timezone.now()),
        )
        return context


class MobileFaceAttendanceVerifyView(FaceAttendanceVerifyView):
    mobile_mode = True
    permission_required = ("attendance.use_mobile_face_attendance",)

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self._json_error(
                "Authentication is required for mobile face attendance.",
                401,
                stage="authentication",
            )
        if getattr(request.user, "employee_profile", None) is None:
            return self._json_error(
                "An employee profile is required for mobile face attendance.",
                403,
                stage="employee",
            )
        permissions = self.get_permission_required()
        if permissions and not request.user.has_perms(permissions):
            return self._json_error(
                "You do not have permission to use mobile face attendance.",
                403,
                stage="authorization",
            )
        return super().dispatch(request, *args, **kwargs)

    def _parse_location(self, request):
        raw_lat = (request.POST.get("latitude") or "").strip()
        raw_lon = (request.POST.get("longitude") or "").strip()
        raw_accuracy = (request.POST.get("accuracy") or request.POST.get("gps_accuracy") or "").strip()
        source = "mobile_browser"
        if raw_lat == "" or raw_lon == "":
            return None, "Location coordinates are required for mobile attendance."
        latitude, latitude_error = self._normalize_coordinate(raw_lat, "Latitude", -90, 90)
        if latitude_error:
            return None, latitude_error
        longitude, longitude_error = self._normalize_coordinate(raw_lon, "Longitude", -180, 180)
        if longitude_error:
            return None, longitude_error
        accuracy = None
        if raw_accuracy:
            if raw_accuracy.lower() in {"true", "false", "none", "null", "nan", "inf", "infinity"}:
                return None, "GPS accuracy must be numeric."
            try:
                accuracy = float(raw_accuracy)
            except (TypeError, ValueError):
                return None, "GPS accuracy must be numeric."
            if not math.isfinite(accuracy):
                return None, "GPS accuracy must be a finite numeric value."
            if accuracy < 0:
                return None, "GPS accuracy cannot be negative."
            if accuracy > 99999999.99:
                return None, "GPS accuracy exceeds the maximum supported value."
        return {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy": accuracy,
            "source": source,
        }, None

    @staticmethod
    def _normalize_coordinate(raw_value, label, minimum, maximum):
        if raw_value is None or not str(raw_value).strip():
            return None, "Location coordinates are required for mobile attendance."
        try:
            coordinate = Decimal(str(raw_value).strip())
        except (InvalidOperation, TypeError, ValueError):
            return None, f"{label} coordinates must be numeric."
        if not coordinate.is_finite():
            return None, f"{label} coordinates must be finite numeric values."
        if coordinate < minimum or coordinate > maximum:
            return None, f"{label} must be between {minimum} and {maximum}."
        try:
            return coordinate.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), None
        except InvalidOperation:
            return None, f"{label} coordinates must be numeric."

    def _location_status(self, location, office):
        if location is None:
            return "not_provided", None, "Location was not provided."
        return (
            AttendanceEvent.LocationStatus.DYNAMIC,
            None,
            "Dynamic field location recorded independently of the organizational office.",
        )

    @staticmethod
    def _haversine_meters(lat1, lon1, lat2, lon2):
        import math
        radius = 6371000.0
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return radius * c

    def post(self, request, *args, **kwargs):
        location, location_error = self._parse_location(request)
        if location_error:
            return self._json_error(location_error, 400, stage="location")
        request._mobile_location = location
        # Force PAD for mobile self-attendance.
        original_require = None
        response = super().post(request, *args, **kwargs)
        return response
