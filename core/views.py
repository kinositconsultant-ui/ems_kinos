from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee, EmployeeAssignment
from employees.views import build_id_card
from attendance.models import AttendanceRecord
from employee_requests.models import EmployeeRequest
from organization.models import InstitutionProfile

from core.mobile_access import mobile_employee_required


def _mobile_context(request):
    """Shared context for the mobile Employee area.

    Only self-service data is provided. The sidebar is intentionally omitted
    (the mobile area has its own navigation), so administrative menus can never
    be inherited from another group membership.
    """
    return {
        "mobile_employee_area": True,
        "mobile_home_url": reverse("mobile-home"),
        "mobile_logout_url": reverse("mobile-logout"),
        "mobile_attendance_url": reverse("mobile-attendance"),
        "mobile_requests_url": reverse("mobile-requests"),
        "mobile_notifications_url": reverse("mobile-notifications"),
        "mobile_profile_url": reverse("mobile-profile"),
    }


@mobile_employee_required
def mobile_home(request):
    """Employee mobile entry: reuses the existing Employee dashboard body."""
    from employees.models import get_linked_employee

    request._mobile_employee_area = True
    employee = get_linked_employee(request.user)
    today = timezone.localdate()
    assignment, assignment_ambiguous = _resolve_valid_primary_assignment(employee, today)

    today_record = None
    month_total = 0
    month_checked_in = 0
    month_late_count = 0
    try:
        today_record = (
            AttendanceRecord.objects.filter(
                schedule__employee_assignment__employee=employee,
                schedule__work_date=today,
            )
            .select_related("schedule__shift", "schedule__employee_assignment")
            .order_by("-id")
            .first()
        )
        month_start = today.replace(day=1)
        month_qs = AttendanceRecord.objects.filter(
            schedule__employee_assignment__employee=employee,
            schedule__work_date__gte=month_start,
            schedule__work_date__lte=today,
        )
        month_total = month_qs.count()
        month_checked_in = month_qs.exclude(first_check_in_at__isnull=True).count()
        month_late_count = month_qs.filter(late_minutes__gt=0).count()
    except Exception:
        pass

    personal_requests = (
        EmployeeRequest.objects.filter(employee_assignment__employee=employee)
        .select_related("employee_assignment__position__office", "leave_type")
        .order_by("-submitted_at")
    )
    pending_requests_count = personal_requests.filter(
        status=EmployeeRequest.Status.PENDING
    ).count()
    under_review_count = personal_requests.filter(
        status=EmployeeRequest.Status.UNDER_REVIEW
    ).count()
    approved_requests_count = personal_requests.filter(
        status=EmployeeRequest.Status.APPROVED
    ).count()
    rejected_requests_count = personal_requests.filter(
        status=EmployeeRequest.Status.REJECTED
    ).count()
    recent_requests = list(personal_requests[:5])

    institution = InstitutionProfile.objects.filter(pk=1).first()
    certifications = list(employee.employee_certifications.all())
    id_card = build_id_card(employee, assignment)

    context = {
        "employee_dashboard": True,
        "employee_profile_missing": False,
        "today": today,
        "employee": employee,
        "assignment": assignment,
        "assignment_ambiguous": assignment_ambiguous,
        "today_record": today_record,
        "month_total": month_total,
        "month_checked_in": month_checked_in,
        "month_late_count": month_late_count,
        "month_late_minutes": 0,
        "attendance_supported": True,
        "pending_requests_count": pending_requests_count,
        "under_review_count": under_review_count,
        "approved_requests_count": approved_requests_count,
        "rejected_requests_count": rejected_requests_count,
        "recent_requests": recent_requests,
        "institution": institution,
        "profile_certifications": certifications,
        "id_card": id_card,
        "page_title": "Dashboard",
    }
    context.update(_mobile_context(request))
    return render(request, "core/mobile/mobile_home.html", context)


def _render_mobile_view(request, view_class, active):
    """Render an existing authorized view inside the Employee mobile shell.

    Reuses the existing view's permission and object-level scope checks, then
    sets a request flag (read by the shared ``mobile_employee_area`` context
    processor) so the desktop admin sidebar is suppressed. This guarantees
    mixed-role accounts cannot inherit administrative menus in the mobile area.
    """
    request._mobile_employee_area = True
    response = view_class.as_view()(request)
    if hasattr(response, "context_data"):
        response.context_data.update(_mobile_context(request))
        response.context_data["active"] = active
    return response


@mobile_employee_required
def mobile_profile(request):
    """Employee mobile entry: reuses the existing self-service profile view."""
    from employees.views import EmployeeSelfServiceProfileView

    return _render_mobile_view(request, EmployeeSelfServiceProfileView, "profile")


@mobile_employee_required
def mobile_requests(request):
    """Employee mobile entry: reuses the existing My Requests list."""
    from employee_requests.views import MyRequestListView

    return _render_mobile_view(request, MyRequestListView, "requests")


@mobile_employee_required
def mobile_notifications(request):
    """Employee mobile entry: reuses the existing own-notifications list."""
    from employee_requests.views import NotificationListView

    return _render_mobile_view(request, NotificationListView, "notifications")


@mobile_employee_required
def mobile_attendance(request):
    """Employee mobile entry: reuses the existing own attendance history."""
    from attendance.views import AttendanceRecordListView

    return _render_mobile_view(request, AttendanceRecordListView, "attendance")


@login_required
def mobile_logout(request):
    """End the Django session for the mobile area and return to the login page.

    Uses the same ``django.contrib.auth.logout`` machinery as the desktop
    logout so session invalidation, CSRF and audit behavior stay consistent.
    """
    from django.contrib.auth import logout
    from django.conf import settings

    logout(request)
    return redirect(f"{settings.LOGIN_URL}")


@login_required
def dashboard(request):
    from core.permissions import is_employee_group_user, is_executive_institution_user

    # Preserve executive (Ministro/Vice) institution reporting policy even when
    # an account is also in the employee group; multi-group precedence is explicit.
    if is_executive_institution_user(request.user):
        from core.services.dashboard import build_dashboard_context
        context = build_dashboard_context(request)
        return render(request, 'core/dashboard.html', context)

    if is_employee_group_user(request.user):
        return _employee_dashboard(request)

    from core.services.dashboard import build_dashboard_context

    context = build_dashboard_context(request)
    return render(request, 'core/dashboard.html', context)


def _resolve_valid_primary_assignment(employee, today):
    candidates = list(
        EmployeeAssignment.objects.filter(
            employee=employee,
            is_primary=True,
            start_date__lte=today,
        )
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .filter(Q(employee__contract_end_date__isnull=True) | Q(employee__contract_end_date__gte=today))
        .select_related("position__office", "position__department")
    )
    if len(candidates) == 1:
        return candidates[0], False
    return None, len(candidates) > 1


def _employee_dashboard(request):
    today = timezone.localdate()
    from employees.models import get_linked_employee
    employee = get_linked_employee(request.user)

    if employee is None:
        context = {
            "employee_dashboard": True,
            "employee_profile_missing": True,
            "today": today,
        }
        return render(request, "core/dashboard.html", context)

    assignment, assignment_ambiguous = _resolve_valid_primary_assignment(employee, today)

    today_record = None
    month_total = 0
    month_checked_in = 0
    month_checked_out = 0
    month_late_count = 0
    month_late_minutes = 0
    attendance_supported = True
    try:
        today_record = (
            AttendanceRecord.objects.filter(
                schedule__employee_assignment__employee=employee,
                schedule__work_date=today,
            )
            .select_related("schedule__shift", "schedule__employee_assignment")
            .order_by("-id")
            .first()
        )
        month_start = today.replace(day=1)
        month_qs = AttendanceRecord.objects.filter(
            schedule__employee_assignment__employee=employee,
            schedule__work_date__gte=month_start,
            schedule__work_date__lte=today,
        )
        month_total = month_qs.count()
        month_checked_in = month_qs.exclude(first_check_in_at__isnull=True).count()
        month_checked_out = month_qs.filter(last_check_out_at__isnull=False).count()
        month_late_count = month_qs.filter(late_minutes__gt=0).count()
        month_late_minutes = month_qs.aggregate(total=Sum("late_minutes"))["total"] or 0
    except Exception:
        attendance_supported = False

    personal_requests = (
        EmployeeRequest.objects.filter(employee_assignment__employee=employee)
        .select_related("employee_assignment__position__office", "leave_type")
        .order_by("-submitted_at")
    )
    pending_requests_count = personal_requests.filter(
        status=EmployeeRequest.Status.PENDING
    ).count()
    under_review_count = personal_requests.filter(
        status=EmployeeRequest.Status.UNDER_REVIEW
    ).count()
    approved_requests_count = personal_requests.filter(
        status=EmployeeRequest.Status.APPROVED
    ).count()
    rejected_requests_count = personal_requests.filter(
        status=EmployeeRequest.Status.REJECTED
    ).count()
    recent_requests = list(personal_requests[:5])

    institution = InstitutionProfile.objects.filter(pk=1).first()
    certifications = list(employee.employee_certifications.all())
    id_card = build_id_card(employee, assignment)
    context = {
        "employee_dashboard": True,
        "employee_profile_missing": False,
        "today": today,
        "employee": employee,
        "assignment": assignment,
        "assignment_ambiguous": assignment_ambiguous,
        "today_record": today_record,
        "month_total": month_total,
        "month_checked_in": month_checked_in,
        "month_checked_out": month_checked_out,
        "month_late_count": month_late_count,
        "month_late_minutes": month_late_minutes,
        "attendance_supported": attendance_supported,
        "pending_requests_count": pending_requests_count,
        "under_review_count": under_review_count,
        "approved_requests_count": approved_requests_count,
        "rejected_requests_count": rejected_requests_count,
        "recent_requests": recent_requests,
        "institution": institution,
        "profile_certifications": certifications,
        "id_card": id_card,
    }
    return render(request, "core/dashboard.html", context)
