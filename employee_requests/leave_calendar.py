"""Leave calendar: authorized, read-only view of approved+processed LEAVE requests."""
from __future__ import annotations

import calendar
from datetime import date

from core.permissions import (
    DIRECTOR_GROUP,
    STAFF_HR_GROUP,
    XEFE_GROUP,
    is_employee_group_user,
    is_executive_institution_user,
    is_hr_user,
    resolve_director_office,
    resolve_employee_assignment,
    resolve_executive_institution_scope,
    resolve_hr_office,
    resolve_staff_hr_office,
    resolve_xefe_assignment,
    user_has_exact_group,
)
from organization.models import Department, Office

from .models import EmployeeRequest

CALENDAR_PERMISSION = "employee_requests.view_leave_calendar"


def has_calendar_permission(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    return bool(getattr(user, "is_superuser", False) or user.has_perm(CALENDAR_PERMISSION))


def _calendar_role_keys(user):
    roles = set()
    if is_executive_institution_user(user):
        roles.add("executive")
    if is_employee_group_user(user):
        roles.add("employee")
    if user_has_exact_group(user, XEFE_GROUP):
        roles.add("xefe")
    if user_has_exact_group(user, DIRECTOR_GROUP):
        roles.add("director")
    if user_has_exact_group(user, STAFF_HR_GROUP):
        roles.add("staff_hr")
    if is_hr_user(user):
        roles.add("hr_office")
    return roles


def resolve_leave_calendar_scope(user):
    if not getattr(user, "is_authenticated", False):
        return None, "Authentication is required."
    if getattr(user, "is_superuser", False):
        return {"kind": "superuser"}, None
    roles = _calendar_role_keys(user)
    if not roles:
        return None, "Your account does not have an authorized leave-calendar scope."
    # Explicit precedence for executive multi-group accounts.
    if "executive" in roles:
        ex_scope, ex_err = resolve_executive_institution_scope(user)
        if ex_scope is None:
            return None, ex_err or "Ministro/Vice institution scope is not configured for leave calendar."
        return {"kind": "executive_institution", "offices_qs": ex_scope["offices_qs"], "institution": ex_scope["institution"]}, None
    if len(roles) > 1:
        return (
            None,
            "Your account has multiple leave-calendar roles with different scopes. "
            "An explicit multi-group scope policy is required before calendar access can be granted.",
        )
    role = next(iter(roles))
    if role == "employee":
        assignment = resolve_employee_assignment(user)
        if assignment is None:
            return None, "Your employee assignment could not be resolved for calendar access."
        pos = assignment.position
        return {"kind": "employee", "office_id": pos.office_id, "department_id": pos.department_id,
                "office": pos.office, "department": pos.department}, None
    if role == "xefe":
        assignment = resolve_xefe_assignment(user)
        if assignment is None:
            return None, "Your Xefe assignment/department scope could not be resolved."
        pos = assignment.position
        return {"kind": "xefe", "office_id": pos.office_id, "department_id": pos.department_id,
                "office": pos.office, "department": pos.department}, None
    if role == "director":
        office = resolve_director_office(user)
        if office is None:
            return None, "Your Director branch scope could not be resolved."
        return {"kind": "director", "office_id": office.pk, "office": office}, None
    if role == "staff_hr":
        office = resolve_staff_hr_office(user)
        if office is None:
            return None, "Your Staff HR branch scope could not be resolved."
        return {"kind": "staff_hr", "office_id": office.pk, "office": office}, None
    if role == "hr_office":
        office = resolve_hr_office(user)
        if office is None:
            return None, "Your HR Office scope could not be resolved."
        return {"kind": "hr_office", "office_id": office.pk, "office": office}, None
    return None, "Calendar scope could not be resolved."


def _base_leave_qs():
    return EmployeeRequest.objects.select_related(
        "employee_assignment__employee",
        "employee_assignment__position__office",
        "employee_assignment__position__department",
        "branch",
    ).filter(
        request_type=EmployeeRequest.RequestType.LEAVE,
        status=EmployeeRequest.Status.APPROVED,
        schedule_processed_at__isnull=False,
    )


def _apply_scope(qs, scope):
    kind = scope["kind"]
    if kind == "superuser":
        return qs
    if kind == "executive_institution":
        return qs.filter(branch__in=scope["offices_qs"])
    if kind in ("employee", "xefe"):
        return qs.filter(
            branch_id=scope["office_id"],
            employee_assignment__position__department_id=scope["department_id"],
        )
    if kind in ("director", "staff_hr", "hr_office"):
        return qs.filter(branch_id=scope["office_id"])
    return qs.none()


def _parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def month_bounds(year, month):
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def authorized_leave_calendar_queryset(user, *, start=None, end=None, params=None):
    scope, error = resolve_leave_calendar_scope(user)
    if error:
        return EmployeeRequest.objects.none(), scope, error
    qs = _apply_scope(_base_leave_qs(), scope)
    if start is not None and end is not None:
        qs = qs.filter(start_date__lte=end, end_date__gte=start)
    params = params or {}
    kind = scope["kind"]
    dept_id = _parse_int(params.get("department"))
    if dept_id is not None:
        if kind in ("employee", "xefe"):
            if dept_id == scope.get("department_id"):
                qs = qs.filter(employee_assignment__position__department_id=dept_id)
        elif kind == "superuser":
            qs = qs.filter(employee_assignment__position__department_id=dept_id)
        elif kind == "executive_institution":
            qs = qs.filter(branch__in=scope["offices_qs"], employee_assignment__position__department_id=dept_id)
        else:
            qs = qs.filter(branch_id=scope["office_id"], employee_assignment__position__department_id=dept_id)
    emp_id = _parse_int(params.get("employee"))
    if emp_id is not None:
        qs = qs.filter(employee_assignment__employee_id=emp_id)
    office_id = _parse_int(params.get("branch") or params.get("office"))
    if office_id is not None and kind in ("superuser", "executive_institution"):
        if kind == "executive_institution":
            if scope["offices_qs"].filter(pk=office_id).exists():
                qs = qs.filter(branch_id=office_id)
        else:
            qs = qs.filter(branch_id=office_id)
    return qs, scope, None


def duration_label(request_record) -> str:
    dtype = request_record.duration_type
    if dtype == EmployeeRequest.DurationType.FULL_DAY:
        return "Full day"
    if dtype == EmployeeRequest.DurationType.HALF_DAY:
        return "Half day (recorded)"
    if dtype == EmployeeRequest.DurationType.HOURLY:
        days = request_record.requested_days
        if days is None:
            return "Hourly (recorded)"
        return f"Hourly ({days:g} hour{'s' if days != 1 else ''} recorded)"
    return "Recorded duration"


def serialize_leave_event(request_record):
    assignment = request_record.employee_assignment
    employee = getattr(assignment, "employee", None)
    position = getattr(assignment, "position", None)
    department = getattr(position, "department", None) if position else None
    branch = getattr(request_record, "branch", None) or (getattr(position, "office", None) if position else None)
    return {
        "id": request_record.pk,
        "employee_name": getattr(employee, "full_name", "") or "",
        "department": getattr(department, "department_name", "") or "—",
        "label": "Leave",
        "start_date": request_record.start_date.isoformat() if request_record.start_date else "",
        "end_date": request_record.end_date.isoformat() if request_record.end_date else "",
        "duration": duration_label(request_record),
        "branch": getattr(branch, "office_name", "") or "—",
        "status": "Processed",
    }


def calendar_filter_choices(qs, scope):
    kind = scope["kind"] if scope else ""
    offices = Office.objects.none()
    departments = Department.objects.none()
    if kind == "superuser":
        offices = Office.objects.filter(is_active=True).order_by("office_name")
        departments = Department.objects.filter(is_active=True).select_related("office").order_by("office__office_name", "department_name")
    elif kind == "executive_institution":
        offices = scope["offices_qs"]
        departments = Department.objects.filter(office__in=offices, is_active=True).select_related("office").order_by("office__office_name", "department_name")
    elif kind in ("director", "staff_hr", "hr_office"):
        offices = Office.objects.filter(pk=scope["office_id"])
        departments = Department.objects.filter(office_id=scope["office_id"], is_active=True).order_by("department_name")
    elif kind in ("employee", "xefe"):
        offices = Office.objects.filter(pk=scope["office_id"])
        departments = Department.objects.filter(pk=scope["department_id"]) if scope.get("department_id") else Department.objects.none()
    emp_rows = (
        qs.values("employee_assignment__employee_id", "employee_assignment__employee__full_name")
        .distinct()
        .order_by("employee_assignment__employee__full_name")
    )
    employees = [
        {"id": r["employee_assignment__employee_id"], "name": r["employee_assignment__employee__full_name"] or ""}
        for r in emp_rows
        if r["employee_assignment__employee_id"]
    ]
    return offices, departments, employees
