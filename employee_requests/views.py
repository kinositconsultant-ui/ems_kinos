from uuid import uuid4

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models import ProtectedError
from django.http import HttpResponseNotAllowed, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import urlencode
from django.views import View
from django.views.generic import CreateView, DetailView, FormView, ListView, UpdateView
import calendar

from audittrail.models import AuditLog
from audittrail.services import log_action
from employees.models import EmployeeAssignment
from organization.models import Office

from .forms import DecisionForm, LeaveTypeForm, RequestForm
from core.permissions import (
    DIRECTOR_GROUP,
    STAFF_HR_GROUP,
    XEFE_GROUP,
    is_hr_user,
    resolve_hr_office,
)
from .models import EmployeeRequest, LeaveType, Notification, RequestApproval


User = get_user_model()


def authorized_request_reviewers(office=None):
    """Resolve Xefe reviewers for an office and department."""
    from core.permissions import resolve_xefe_assignment, XEFE_GROUP

    reviewers = []
    for reviewer in User.objects.filter(is_active=True, groups__name=XEFE_GROUP).distinct():
        assignment = resolve_xefe_assignment(reviewer)
        if (
            assignment is not None
            and user_can_review_workflow(reviewer)
            and (office is None or assignment.position.office_id == getattr(office, "pk", office))
        ):
            reviewers.append(reviewer)
    return reviewers


def user_can_review_workflow(user):
    return user.has_perm("employee_requests.view_requestapproval")


def eligible_xefe_for_request(request_record):
    from core.permissions import resolve_xefe_assignment

    assignment = request_record.employee_assignment.position
    return [
        user for user in authorized_request_reviewers(office=assignment.office)
        if (
            resolve_xefe_assignment(user) is not None
            and resolve_xefe_assignment(user).position.department_id == assignment.department_id
        )
    ]


def eligible_directors_for_request(request_record):
    from core.permissions import resolve_director_office, DIRECTOR_GROUP

    directors = []
    for director in User.objects.filter(is_active=True, groups__name=DIRECTOR_GROUP).distinct():
        if (
            resolve_director_office(director) is not None
            and resolve_director_office(director).pk == request_record.branch_id
            and director.has_perm("employee_requests.view_requestapproval")
        ):
            directors.append(director)
    return directors


def eligible_staff_hr_for_request(request_record):
    from core.permissions import resolve_staff_hr_office, STAFF_HR_GROUP

    recipients = []
    for user in User.objects.filter(is_active=True, groups__name=STAFF_HR_GROUP).distinct():
        office = resolve_staff_hr_office(user)
        if (
            office is not None
            and office.pk == request_record.branch_id
            and user.has_perm("employee_requests.view_requestapproval")
        ):
            recipients.append(user)
    return recipients

def staff_hr_processing_enabled(user):
    """Authorized Staff HR schedule-processing workspace capability.

    Requires effective permissions for the workspace and an existing
    eligible Staff HR branch scope (fail-closed on missing/ambiguous
    assignment). Does not grant access by is_staff or group name alone.
    """
    from core.permissions import resolve_staff_hr_office, STAFF_HR_GROUP

    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if not user.has_perms(
        [
            "employee_requests.view_employeerequest",
            "employee_requests.change_employeerequest",
        ]
    ):
        return False
    return bool(
        user.groups.filter(name=STAFF_HR_GROUP).exists()
        and resolve_staff_hr_office(user) is not None
    )


def staff_hr_processing_base_queryset(user):
    """Approved requests in the Staff HR user's authorized branch.

    Branch is resolved from account -> linked Employee -> valid current
    primary assignment -> Position -> Office. Missing/ambiguous scope
    fails closed. Includes both processed and unprocessed approved records
    for authorized history visibility; callers may narrow further.
    """
    from core.permissions import resolve_staff_hr_office, STAFF_HR_GROUP

    if not getattr(user, "is_authenticated", False):
        return EmployeeRequest.objects.none()
    if getattr(user, "is_superuser", False):
        return EmployeeRequest.objects.select_related(
            "employee_assignment__employee",
            "employee_assignment__position__office",
            "employee_assignment__position__department",
            "leave_type",
            "schedule_processed_by",
        ).filter(status=EmployeeRequest.Status.APPROVED)
    if not (
        user.has_perms(
            [
                "employee_requests.view_employeerequest",
                "employee_requests.change_employeerequest",
            ]
        )
        and user.groups.filter(name=STAFF_HR_GROUP).exists()
    ):
        return EmployeeRequest.objects.none()
    office = resolve_staff_hr_office(user)
    if office is None:
        return EmployeeRequest.objects.none()
    return EmployeeRequest.objects.select_related(
        "employee_assignment__employee",
        "employee_assignment__position__office",
        "employee_assignment__position__department",
        "leave_type",
        "schedule_processed_by",
    ).filter(
        branch=office,
        status=EmployeeRequest.Status.APPROVED,
    )


def can_access_approval_inbox(user):
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    # Mirror PendingApprovalListView.get() exactly: a reviewer needs both the
    # model permission and membership in a reviewer group (Xefe/Director), or
    # the Staff HR schedule-processing capability. The model permission alone
    # never grants inbox authority, so an Employee-only account that merely
    # holds view_requestapproval is denied here (and the menu stays hidden).
    if staff_hr_processing_enabled(user):
        return True
    return bool(
        user.has_perm("employee_requests.view_requestapproval")
        and user.groups.filter(name__in=[XEFE_GROUP, DIRECTOR_GROUP]).exists()
    )


def can_decide_request_approval(user, approval):
    """Server-side eligibility for Accept/Reject on a RequestApproval row.

    Preserves existing non-HR approver authorization and adds office-scoped
    HR eligibility without granting global change_requestapproval.
    """
    from core.permissions import is_hr_user, resolve_hr_office

    if approval is None or user is None or not getattr(user, "is_authenticated", False):
        return False
    from core.permissions import (
        DIRECTOR_GROUP,
        resolve_director_office,
        resolve_xefe_assignment,
        XEFE_GROUP,
    )
    if approval.decision != RequestApproval.Decision.PENDING:
        return False
    request_record = getattr(approval, "request", None)
    if request_record is None:
        return False
    if request_record.submitted_by_id == user.pk:
        return False
    req_emp = request_record.employee_assignment.employee
    from employees.models import is_user_linked_to_employee
    if is_user_linked_to_employee(user, req_emp):
        return False
    if approval.approver_id != user.pk:
        return False
    if request_record.branch_id != request_record.employee_assignment.position.office_id:
        return False
    if request_record.forwarded_at is None:
        return False
    if not RequestApproval.objects.filter(
        request=request_record,
        step_number=1,
        decision=RequestApproval.Decision.RECOMMENDED,
    ).exists():
        return False
    if user.groups.filter(name=XEFE_GROUP).exists():
        return False
    if user.groups.filter(name=DIRECTOR_GROUP).exists():
        if not user.has_perm("employee_requests.change_requestapproval"):
            return False
        office = resolve_director_office(user)
        if office is None or request_record.branch_id != office.pk:
            return False
        if approval.step_number != 2 or request_record.workflow_stage != "director":
            return False
        return request_record.status == EmployeeRequest.Status.UNDER_REVIEW
    return False


def create_in_app_notification(*, recipient, request_record, notification_type, title, message):
    """Create an in-app notification once for a recipient/request/type combination."""
    if recipient is None:
        return None
    if Notification.objects.filter(
        recipient_user=recipient,
        request=request_record,
        notification_type=notification_type,
        title=title,
    ).exists():
        return None
    return Notification.objects.create(
        recipient_user=recipient,
        request=request_record,
        notification_type=notification_type,
        title=title,
        message=message,
        channel=Notification.Channel.IN_APP,
        delivery_status=Notification.DeliveryStatus.SENT,
        sent_at=timezone.now(),
    )


class RequestAccessMixin(LoginRequiredMixin, PermissionRequiredMixin):
    raise_exception = True


class LeaveTypeListView(RequestAccessMixin, ListView):
    model = LeaveType
    template_name = "employee_requests/leave_type_list.html"
    context_object_name = "leave_types"
    permission_required = "employee_requests.view_leavetype"
    paginate_by = 20

    def get_queryset(self):
        queryset = LeaveType.objects.all()
        query = self.request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(Q(code__icontains=query) | Q(name__icontains=query))
        if self.request.GET.get("is_active"):
            queryset = queryset.filter(is_active=self.request.GET["is_active"])
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            page_title="Leave Types",
            search_query=self.request.GET.get("q", ""),
            can_add=self.request.user.has_perm("employee_requests.add_leavetype"),
        )
        context["table_rows"] = [
            {
                "item": item,
                "has_requests": item.requests.exists(),
                "can_delete": (
                    self.request.user.has_perm("employee_requests.delete_leavetype")
                    and not item.requests.exists()
                ),
                "can_deactivate": (
                    item.is_active
                    and item.requests.exists()
                    and self.request.user.has_perm("employee_requests.change_leavetype")
                ),
            }
            for item in context["leave_types"]
        ]
        return context


class LeaveTypeDetailView(RequestAccessMixin, DetailView):
    model = LeaveType
    template_name = "employee_requests/leave_type_detail.html"
    permission_required = "employee_requests.view_leavetype"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        has_requests = self.object.requests.exists()
        context.update(
            can_edit=self.request.user.has_perm("employee_requests.change_leavetype"),
            has_requests=has_requests,
            can_delete=(
                not has_requests
                and self.request.user.has_perm("employee_requests.delete_leavetype")
            ),
            can_deactivate=(
                self.object.is_active
                and has_requests
                and self.request.user.has_perm("employee_requests.change_leavetype")
            ),
        )
        return context


class LeaveTypeActionView(RequestAccessMixin, View):
    action = ""
    permission_required = ""

    def get_object(self):
        return get_object_or_404(LeaveType, pk=self.kwargs["pk"])

    def get(self, request, *args, **kwargs):
        item = self.get_object()
        in_use = item.requests.exists()
        if self.action == "delete" and in_use:
            messages.error(request, "A leave type used by requests cannot be deleted.")
            return redirect("employee_requests:leave-type-detail", pk=item.pk)
        if self.action == "deactivate" and not item.is_active:
            messages.error(request, "This leave type is already inactive.")
            return redirect("employee_requests:leave-type-detail", pk=item.pk)
        return render(
            request,
            "core/action_confirm.html",
            {
                "page_title": (
                    "Delete leave type" if self.action == "delete"
                    else "Deactivate leave type"
                ),
                "prompt": (
                    "Permanently delete this unused leave type:"
                    if self.action == "delete"
                    else "Deactivate this leave type while retaining request history:"
                ),
                "object_label": f"{item.code} — {item.name}",
                "warning": (
                    "This action cannot be undone."
                    if self.action == "delete"
                    else "Existing requests will retain their leave type; this action cannot be reversed here."
                ),
                "confirm_label": (
                    "Delete permanently" if self.action == "delete"
                    else "Deactivate leave type"
                ),
                "back_url": reverse(
                    "employee_requests:leave-type-detail", args=[item.pk]
                ),
            },
        )

    def post(self, request, *args, **kwargs):
        item = self.get_object()
        if self.action == "delete" and item.requests.exists():
            messages.error(request, "A leave type used by requests cannot be deleted.")
            return redirect("employee_requests:leave-type-detail", pk=item.pk)
        if self.action == "deactivate" and not item.is_active:
            messages.error(request, "This leave type is already inactive.")
            return redirect("employee_requests:leave-type-detail", pk=item.pk)
        try:
            with transaction.atomic():
                if self.action == "delete":
                    log_action(
                        actor_user=request.user,
                        action=AuditLog.Action.DELETE,
                        entity_type="LeaveType",
                        object_id=item.pk,
                        before_data={"code": item.code, "name": item.name},
                        reason="Unused leave type deleted from the management interface.",
                        ip_address=request.META.get("REMOTE_ADDR"),
                    )
                    item.delete()
                else:
                    log_action(
                        actor_user=request.user,
                        action=AuditLog.Action.UPDATE,
                        entity_type="LeaveType",
                        object_id=item.pk,
                        before_data={"is_active": item.is_active},
                        after_data={"is_active": False},
                        reason="Leave type deactivated; request history retained.",
                        ip_address=request.META.get("REMOTE_ADDR"),
                    )
                    item.is_active = False
                    item.save(update_fields=["is_active", "updated_at"])
        except (ProtectedError, IntegrityError):
            messages.error(
                request,
                "This leave type is referenced by existing data and cannot be deleted. Deactivate it instead.",
            )
            return redirect("employee_requests:leave-type-detail", pk=item.pk)
        messages.success(
            request,
            f"Leave type {item.code} was "
            f"{'deleted' if self.action == 'delete' else 'deactivated'}.",
        )
        return redirect("employee_requests:leave-type-list")


class LeaveTypeDeleteView(LeaveTypeActionView):
    action = "delete"
    permission_required = "employee_requests.delete_leavetype"


class LeaveTypeDeactivateView(LeaveTypeActionView):
    action = "deactivate"
    permission_required = "employee_requests.change_leavetype"


class LeaveTypeFormContextMixin:
    template_name = "employee_requests/leave_type_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        return context

    def form_valid(self, form):
        messages.success(self.request, "Leave type saved.")
        return super().form_valid(form)


class LeaveTypeCreateView(RequestAccessMixin, LeaveTypeFormContextMixin, CreateView):
    model = LeaveType
    form_class = LeaveTypeForm
    permission_required = "employee_requests.add_leavetype"
    page_title = "Create Leave Type"
    success_url = reverse_lazy("employee_requests:leave-type-list")


class LeaveTypeUpdateView(RequestAccessMixin, LeaveTypeFormContextMixin, UpdateView):
    model = LeaveType
    form_class = LeaveTypeForm
    permission_required = "employee_requests.change_leavetype"
    page_title = "Edit Leave Type"

    def get_success_url(self):
        return reverse("employee_requests:leave-type-detail", args=[self.object.pk])


class RequestListView(RequestAccessMixin, ListView):
    model = EmployeeRequest
    template_name = "employee_requests/request_list.html"
    context_object_name = "requests"
    permission_required = "employee_requests.view_employeerequest"
    paginate_by = 20
    pending_only = False

    def get_queryset(self):
        queryset = EmployeeRequest.objects.select_related(
            "employee_assignment__employee",
            "employee_assignment__position__office",
            "leave_type",
            "processed_by",
        ).prefetch_related("approval_steps")
        from core.permissions import is_hr_user, resolve_hr_office
        from core.permissions import (
            DIRECTOR_GROUP, XEFE_GROUP, STAFF_HR_GROUP,
            resolve_director_office, resolve_xefe_assignment, resolve_staff_hr_office,
        )

        hr_office = resolve_hr_office(self.request.user) if is_hr_user(self.request.user) else None
        employee = getattr(self.request.user, "employee_profile", None)
        role_names = {DIRECTOR_GROUP, XEFE_GROUP, STAFF_HR_GROUP}
        is_workflow_role = bool(set(self.request.user.groups.values_list("name", flat=True)) & role_names)
        if not self.request.user.is_staff and not is_hr_user(self.request.user) and not is_workflow_role:
            if employee is None:
                queryset = queryset.none()
            else:
                queryset = queryset.filter(
                    Q(submitted_by=self.request.user)
                    | Q(employee_assignment__employee=employee)
                )
        # HR branch scoping.
        if is_hr_user(self.request.user):
            if hr_office is None:
                return EmployeeRequest.objects.none()
            queryset = queryset.filter(employee_assignment__position__office=hr_office)
        if getattr(self, "my_requests", False):
            queryset = queryset.filter(submitted_by=self.request.user)
        if self.pending_only:
            queryset = queryset.filter(
                status__in=[
                    EmployeeRequest.Status.PENDING,
                    EmployeeRequest.Status.UNDER_REVIEW,
                ]
            )
        query = self.request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(
                Q(request_number__icontains=query)
                | Q(employee_assignment__employee__employee_id__icontains=query)
                | Q(employee_assignment__employee__full_name__icontains=query)
            )
        from core.permissions import is_branch_hr_user
        from core.permissions import is_director_user, is_xefe_user
        director_only = is_director_user(self.request.user)
        xefe_only = is_xefe_user(self.request.user)
        for field in ("request_type", "status", "employee_assignment__position__office_id"):
            if field == "employee_assignment__position__office_id" and (is_hr_user(self.request.user) or is_branch_hr_user(self.request.user) or director_only or xefe_only):
                continue
            value = self.request.GET.get(field)
            if value:
                queryset = queryset.filter(**{field: value})
        for field in ("start_date", "end_date"):
            value = self.request.GET.get(field)
            if value:
                lookup = "submitted_at__date__gte" if field == "start_date" else "submitted_at__date__lte"
                queryset = queryset.filter(**{lookup: value})
        if self.request.user.groups.filter(name=DIRECTOR_GROUP).exists():
            office = resolve_director_office(self.request.user)
            queryset = queryset.filter(branch=office) if office else queryset.none()
        if self.request.user.groups.filter(name=XEFE_GROUP).exists():
            assignment = resolve_xefe_assignment(self.request.user)
            queryset = (
                queryset.filter(
                    employee_assignment__position__office=assignment.position.office,
                    employee_assignment__position__department=assignment.position.department,
                    approval_steps__approver=self.request.user,
                    approval_steps__step_number=1,
                )
                if assignment else queryset.none()
            )
        if self.request.user.groups.filter(name=STAFF_HR_GROUP).exists():
            office = resolve_staff_hr_office(self.request.user)
            if office is None:
                return queryset.none()
            # Staff HR sees authorized-branch approved requests (processed and
            # unprocessed) for history. "All" is the default; optional explicit
            # processing filter narrows without expanding scope.
            queryset = queryset.filter(
                branch=office,
                status=EmployeeRequest.Status.APPROVED,
            )
            processing = self.request.GET.get("processing", "all")
            if processing == "waiting":
                queryset = queryset.filter(schedule_processed_at__isnull=True)
            elif processing == "processed":
                queryset = queryset.filter(schedule_processed_at__isnull=False)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from core.permissions import STAFF_HR_GROUP, is_hr_user, resolve_hr_office
        from core.permissions import is_director_user, is_xefe_user, resolve_director_office
        show_processing_filter = (
            self.request.user.is_superuser
            or self.request.user.groups.filter(name=STAFF_HR_GROUP).exists()
        ) and not getattr(self, "my_requests", False) and not self.pending_only
        processing = self.request.GET.get("processing", "all") if show_processing_filter else "all"
        if processing not in ("all", "waiting", "processed"):
            processing = "all"
        director_only = is_director_user(self.request.user)
        xefe_only = is_xefe_user(self.request.user)
        if director_only:
            _doffice = resolve_director_office(self.request.user)
            _offices_qs = Office.objects.filter(pk=_doffice.pk) if _doffice is not None else Office.objects.none()
        else:
            _offices_qs = (Office.objects.filter(pk=resolve_hr_office(self.request.user).pk) if is_hr_user(self.request.user) and resolve_hr_office(self.request.user) is not None else Office.objects.order_by("office_name"))
        removed_names = set()
        if director_only or xefe_only:
            removed_names.add("employee_assignment__position__office_id")
        context.update(
            page_title="Pending Requests" if self.pending_only else "All Requests",
            page_description=(
                "Approved requests in your authorized branch, including processed history."
                if show_processing_filter and not self.request.user.is_superuser
                else "Requests are processed through assigned approval steps."
            ),
            search_query=self.request.GET.get("q", ""),
            can_add=self.request.user.has_perm("employee_requests.add_employeerequest"),
            request_types=EmployeeRequest.RequestType.choices,
            request_statuses=EmployeeRequest.Status.choices,
            offices=_offices_qs,
            director_only=director_only,
            xefe_only=xefe_only,
            querystring=urlencode({
                key: value for key, value in self.request.GET.items()
                if key != "page" and key not in removed_names
            }),
            start_date=self.request.GET.get("start_date", ""),
            end_date=self.request.GET.get("end_date", ""),
            show_processing_filter=show_processing_filter,
            processing_filter=processing,
            processing_choices=[
                ("all", "All"),
                ("waiting", "Waiting for Processing"),
                ("processed", "Processed"),
            ],
        )
        context["table_rows"] = [
            {
                "request": item,
                "cancel_url": reverse(
                    "employee_requests:request-cancel", args=[item.pk]
                ),
                "approval_pk": next(
                    (
                        approval.pk
                        for approval in item.approval_steps.all()
                        if approval.approver_id == self.request.user.pk
                        and approval.decision == RequestApproval.Decision.PENDING
                    ),
                    None,
                ),
                "can_cancel": (
                    item.status == EmployeeRequest.Status.PENDING
                    and not item.approval_steps.all()
                    and (
                        item.submitted_by_id == self.request.user.pk
                        or (
                            self.request.user.is_superuser
                            and self.request.user.has_perm(
                                "employee_requests.change_employeerequest"
                            )
                        )
                    )
                ),
                "can_decide": can_decide_request_approval(self.request.user, next(
                    (a for a in item.approval_steps.all()
                     if a.approver_id == self.request.user.pk
                     and a.decision == RequestApproval.Decision.PENDING),
                    None,
                )),
            }
            for item in context["requests"]
        ]
        return context


class PendingRequestListView(RequestListView):
    pending_only = True


class MyRequestListView(RequestListView):
    my_requests = True

    def dispatch(self, request, *args, **kwargs):
        # My Requests is employee self-service. A pure Xefe_Departamentu account
        # is a reviewer, not an employee submitter, so it must not reach this
        # route. Superusers and accounts that also hold the Employess group keep
        # their existing behaviour (see RequestCreateView dispatch precedent).
        if (
            not request.user.is_superuser
            and not request.user.groups.filter(name="Employess").exists()
            and request.user.groups.filter(name__in=[XEFE_GROUP, STAFF_HR_GROUP]).exists()
        ):
            raise PermissionDenied(
                "My Requests is employee self-service. Reviewer and Staff HR accounts use the "
                "Approval Inbox instead."
            )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "My Requests"
        return context


class RequestDetailView(RequestAccessMixin, DetailView):
    model = EmployeeRequest
    template_name = "employee_requests/request_detail.html"
    permission_required = "employee_requests.view_employeerequest"

    def get_queryset(self):
        queryset = EmployeeRequest.objects.select_related(
            "employee_assignment__employee",
            "employee_assignment__position__office",
            "employee_assignment__position__department",
            "attendance_record",
            "leave_type",
            "submitted_by",
            "processed_by",
        ).prefetch_related("approval_steps__approver", "notifications")
        from core.permissions import is_hr_user, resolve_hr_office
        from core.permissions import (
            DIRECTOR_GROUP, XEFE_GROUP, STAFF_HR_GROUP,
            resolve_director_office, resolve_xefe_assignment, resolve_staff_hr_office,
        )

        employee = getattr(self.request.user, "employee_profile", None)
        role_names = {DIRECTOR_GROUP, XEFE_GROUP, STAFF_HR_GROUP}
        is_workflow_role = bool(set(self.request.user.groups.values_list("name", flat=True)) & role_names)
        if not self.request.user.is_staff and not is_hr_user(self.request.user) and not is_workflow_role:
            if employee is None:
                queryset = queryset.none()
            else:
                queryset = queryset.filter(
                    Q(submitted_by=self.request.user)
                    | Q(employee_assignment__employee=employee)
                )
        if is_hr_user(self.request.user):
            hr_office = resolve_hr_office(self.request.user)
            if hr_office is None:
                return EmployeeRequest.objects.none()
            queryset = queryset.filter(employee_assignment__position__office=hr_office)
        if self.request.user.groups.filter(name=DIRECTOR_GROUP).exists():
            office = resolve_director_office(self.request.user)
            queryset = queryset.filter(branch=office) if office else queryset.none()
        if self.request.user.groups.filter(name=XEFE_GROUP).exists():
            assignment = resolve_xefe_assignment(self.request.user)
            queryset = (
                queryset.filter(
                    employee_assignment__position__office=assignment.position.office,
                    employee_assignment__position__department=assignment.position.department,
                    approval_steps__approver=self.request.user,
                    approval_steps__step_number=1,
                )
                if assignment else queryset.none()
            )
        if self.request.user.groups.filter(name=STAFF_HR_GROUP).exists():
            office = resolve_staff_hr_office(self.request.user)
            queryset = (
                queryset.filter(
                    branch=office,
                    status=EmployeeRequest.Status.APPROVED,
                )
                if office
                else queryset.none()
            )
        return queryset

    def _get_review_task(self, request_record):
        from core.permissions import XEFE_GROUP, DIRECTOR_GROUP, resolve_xefe_assignment

        user = self.request.user
        if not user.has_perms([
            "employee_requests.view_requestapproval",
            "employee_requests.change_requestapproval",
        ]):
            return None

        tasks = request_record.approval_steps.filter(
            approver=user,
            decision=RequestApproval.Decision.PENDING,
        )
        if user.groups.filter(name=XEFE_GROUP).exists():
            assignment = resolve_xefe_assignment(user)
            position = request_record.employee_assignment.position
            if (
                assignment is None
                or request_record.status != EmployeeRequest.Status.PENDING
                or request_record.workflow_stage != "xefe_review"
                or request_record.forwarded_at is not None
                or request_record.branch_id != position.office_id
                or assignment.position.office_id != position.office_id
                or assignment.position.department_id != position.department_id
            ):
                return None
            return tasks.filter(step_number=1).first()

        if user.groups.filter(name=DIRECTOR_GROUP).exists():
            task = tasks.filter(step_number=2).first()
            if task is not None and can_decide_request_approval(user, task):
                return task
        return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request_record = self.object
        review_task = self._get_review_task(request_record)
        from core.permissions import resolve_staff_hr_office
        staff_hr_office = resolve_staff_hr_office(self.request.user)
        attachment_name = (
            request_record.attachment.name.rsplit("/", 1)[-1]
            if request_record.attachment else ""
        )
        context.update(
            attachment_name=attachment_name,
            approval_steps=request_record.approval_steps.select_related("approver"),
            related_notifications=request_record.notifications.filter(
                recipient_user=self.request.user
            ),
            can_edit=(
                request_record.status == EmployeeRequest.Status.PENDING
                and request_record.forwarded_at is None
                and request_record.submitted_by_id == self.request.user.pk
                and self.request.user.has_perm("employee_requests.change_employeerequest")
            ),
            can_cancel=(
                request_record.status == EmployeeRequest.Status.PENDING
                and not request_record.approval_steps.exists()
                and (
                    request_record.submitted_by_id == self.request.user.pk
                    or (
                        self.request.user.is_superuser
                        and self.request.user.has_perm(
                            "employee_requests.change_employeerequest"
                        )
                    )
                )
            ),
            show_task_link=review_task is not None,
            task_url=(
                reverse("employee_requests:approval-detail", args=[review_task.pk])
                if review_task is not None else ""
            ),
            task_role=(
                "xefe" if review_task is not None and review_task.step_number == 1
                else "director" if review_task is not None else ""
            ),
            task_caption=(
                "Review and forward" if review_task is not None
                and review_task.step_number == 1 else "Review and decide"
            ),
            # Hide My Requests for Xefe, preserve Employess, superuser exception
            show_my_requests=(
                not self.request.user.groups.filter(
                    name="Xefe_Departamentu"
                ).exists()
                or self.request.user.is_superuser
            ),
            can_process_schedule=(
                staff_hr_office is not None
                and self.request.user.groups.filter(
                    name="Staff HR"
                ).exists()
                and self.request.user.has_perm(
                    "employee_requests.change_employeerequest"
                )
                and request_record.status
                == EmployeeRequest.Status.APPROVED
                and request_record.schedule_processed_at is None
                and request_record.branch_id == staff_hr_office.pk
                and request_record.employee_assignment.position.office_id
                == staff_hr_office.pk
            ),
        )
        return context


class RequestCancelView(LoginRequiredMixin, View):
    raise_exception = True

    def get_object(self):
        return get_object_or_404(
            EmployeeRequest.objects.select_related(
                "employee_assignment__position__office"
            ).prefetch_related("approval_steps"),
            pk=self.kwargs["pk"],
        )

    def _can_cancel(self, request_record):
        return (
            request_record.status == EmployeeRequest.Status.PENDING
            and not request_record.approval_steps.exists()
            and (
                request_record.submitted_by_id == self.request.user.pk
                or (
                    self.request.user.is_superuser
                    and self.request.user.has_perm(
                        "employee_requests.change_employeerequest"
                    )
                )
            )
        )

    def get(self, request, *args, **kwargs):
        request_record = self.get_object()
        if not self._can_cancel(request_record):
            raise PermissionDenied(
                "Only the submitter can cancel a pending request before approval steps are assigned."
            )
        return render(
            request,
            "core/action_confirm.html",
            {
                "page_title": "Cancel request",
                "prompt": "Cancel this pending employee request while retaining its official history:",
                "object_label": request_record.request_number,
                "warning": "The request will remain in the system with Cancelled status; this action cannot be reversed here.",
                "confirm_label": "Cancel request",
                "back_url": reverse(
                    "employee_requests:request-detail", args=[request_record.pk]
                ),
            },
        )

    def post(self, request, *args, **kwargs):
        request_record = self.get_object()
        if not self._can_cancel(request_record):
            raise PermissionDenied(
                "Only the submitter can cancel a pending request before approval steps are assigned."
            )
        with transaction.atomic():
            request_record = EmployeeRequest.objects.select_for_update().select_related(
                "employee_assignment__position__office"
            ).get(pk=request_record.pk)
            if not self._can_cancel(request_record):
                raise PermissionDenied("This request can no longer be cancelled.")
            previous_status = request_record.status
            request_record.status = EmployeeRequest.Status.CANCELLED
            request_record.save(update_fields=["status", "updated_at"])
            log_action(
                actor_user=request.user,
                office=request_record.employee_assignment.position.office,
                action=AuditLog.Action.UPDATE,
                entity_type="EmployeeRequest",
                object_id=request_record.pk,
                before_data={"status": previous_status},
                after_data={"status": request_record.status},
                reason="Pending request cancelled by its submitter.",
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        messages.success(request, f"Request {request_record.request_number} was cancelled.")
        return redirect("employee_requests:request-detail", pk=request_record.pk)


class RequestFormContextMixin:
    template_name = "employee_requests/request_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        employee = getattr(self.request.user, "employee_profile", None)
        kwargs["employee"] = employee if employee is not None and not self.request.user.is_staff else None
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            page_title=self.page_title,
            back_url=reverse("employee_requests:request-all"),
        )
        return context

    def form_valid(self, form):
        if form.instance.pk:
            if form.instance.status != EmployeeRequest.Status.PENDING or (
                form.instance.forwarded_at is not None
            ):
                raise PermissionDenied(
                    "Only pending requests that have not been forwarded for review can be edited."
                )
            if form.instance.submitted_by_id != self.request.user.pk:
                raise PermissionDenied("You can only edit your own requests.")
            if not self.request.user.has_perm("employee_requests.change_employeerequest"):
                raise PermissionDenied
            if "employee_assignment" in form.changed_data:
                raise PermissionDenied(
                    "Assignment changes are not allowed on existing submitted requests. "
                    "Request routing must be updated through the explicit workflow."
                )
        else:
            form.instance.request_number = f"REQ-{timezone.now():%Y%m%d}-{uuid4().hex[:10].upper()}"
            form.instance.submitted_by = self.request.user
        is_update = bool(form.instance.pk)

        reviewers = []
        submitter_employee_ids = set()
        assignment = None
        if not is_update:
            try:

                assignment = getattr(form.instance, "employee_assignment", None) or form.cleaned_data.get("employee_assignment")
                if assignment is not None:
                    submitter_employee_ids = set(assignment.employee.get_linked_user_ids(active_only=True))
            except Exception:
                submitter_employee_ids = set()
            try:
                office = (getattr(form.instance, "employee_assignment", None) or form.cleaned_data.get("employee_assignment")).position.office
            except Exception:
                office = None
            form.instance.branch = office
            reviewers = eligible_xefe_for_request(form.instance) if assignment is not None else []
            reviewers = [u for u in reviewers if u.pk not in submitter_employee_ids and u.pk != self.request.user.pk]
            if len(reviewers) != 1:
                messages.error(
                    self.request,
                    "Request routing is not configured: exactly one eligible Xefe must be "
                    "assigned to the request's office and department.",
                )
                return self.form_invalid(form)

        with transaction.atomic():
            response = super().form_valid(form)
            if not is_update:
                # Re-derive from the persisted object for trusted server-side values.
                try:

                    submitter_employee_ids = set(self.object.employee_assignment.employee.get_linked_user_ids(active_only=True))
                except Exception:
                    submitter_employee_ids = set()
                reviewers = eligible_xefe_for_request(self.object)
                reviewers = [u for u in reviewers if u.pk not in submitter_employee_ids and u.pk != self.request.user.pk]
                if len(reviewers) != 1:
                    raise PermissionDenied(
                        "Request routing configuration changed before the request could be saved."
                    )
                for reviewer in reviewers:
                    RequestApproval.objects.get_or_create(
                        request=self.object,
                        approver=reviewer,
                        step_number=1,
                    )
                for reviewer in reviewers:
                    create_in_app_notification(
                        recipient=reviewer,
                        request_record=self.object,
                        notification_type=Notification.NotificationType.REQUEST_SUBMITTED,
                        title=f"Request {self.object.request_number} submitted",
                        message=f"Request {self.object.request_number} is awaiting your review.",
                    )
            log_action(
                actor_user=self.request.user,
                office=self.object.employee_assignment.position.office,
                action=AuditLog.Action.UPDATE if is_update else AuditLog.Action.CREATE,
                entity_type="EmployeeRequest",
                object_id=self.object.pk,
                before_data={} if not hasattr(self, "_before") else self._before,
                after_data={
                    "request_number": self.object.request_number,
                    "request_type": self.object.request_type,
                    "status": self.object.status,
                },
                reason=self.object.reason,
                ip_address=self.request.META.get("REMOTE_ADDR"),
            )
        messages.success(self.request, "Request saved.")
        return response


class RequestCreateView(RequestAccessMixin, RequestFormContextMixin, CreateView):
    model = EmployeeRequest
    form_class = RequestForm
    permission_required = "employee_requests.add_employeerequest"
    page_title = "New Employee Request"

    def get_success_url(self):
        return reverse("employee_requests:request-detail", args=[self.object.pk])

    def dispatch(self, request, *args, **kwargs):
        if (
            request.user.is_authenticated
            and not request.user.is_staff
            and getattr(request.user, "employee_profile", None) is None
        ):
            raise PermissionDenied("An employee profile is required to submit a request.")
        if request.user.is_authenticated and not request.user.is_superuser:
            if not request.user.groups.filter(name="Employess").exists():
                raise PermissionDenied("Only accounts in the Employess group may submit requests.")
        return super().dispatch(request, *args, **kwargs)


class RequestUpdateView(RequestAccessMixin, RequestFormContextMixin, UpdateView):
    model = EmployeeRequest
    form_class = RequestForm
    page_title = "Edit Employee Request"

    def has_permission(self):
        # Authentication is handled by LoginRequiredMixin. Authorization is
        # enforced by object ownership and editable request state in
        # get_queryset(), dispatch() and form_valid(); no model permission --
        # including Staff HR's change_employeerequest -- grants editing of
        # another employee's request.
        return self.request.user.is_authenticated

    def get_queryset(self):
        queryset = super().get_queryset().filter(
            status=EmployeeRequest.Status.PENDING,
            forwarded_at__isnull=True,
            submitted_by=self.request.user,
        )
        return queryset

    def get_success_url(self):
        return reverse("employee_requests:request-detail", args=[self.object.pk])

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        self._before = {
            "request_number": obj.request_number,
            "request_type": obj.request_type,
            "status": obj.status,
        }
        return obj

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        if getattr(self, "object", None) and self.object.submitted_by_id != request.user.pk:
            raise PermissionDenied("You can only edit your own requests.")
        return response


class ApprovalListView(RequestAccessMixin, ListView):
    model = RequestApproval
    template_name = "employee_requests/approval_list.html"
    context_object_name = "approvals"
    permission_required = "employee_requests.view_requestapproval"
    paginate_by = 20
    pending_only = False

    def get_queryset(self):
        queryset = RequestApproval.objects.select_related(
            "request__employee_assignment__employee", "approver"
        )
        from core.permissions import DIRECTOR_GROUP, XEFE_GROUP, resolve_xefe_assignment
        if (
            self.request.user.groups.filter(name__in=[XEFE_GROUP, DIRECTOR_GROUP]).exists()
            and self.request.user.has_perm("employee_requests.view_requestapproval")
        ):
            queryset = queryset.filter(approver=self.request.user)
        else:
            queryset = queryset.none()
        if self.request.user.groups.filter(name=XEFE_GROUP).exists():
            assignment = resolve_xefe_assignment(self.request.user)
            queryset = (
                queryset.filter(
                    step_number=1,
                    request__employee_assignment__position__office=assignment.position.office,
                    request__employee_assignment__position__department=assignment.position.department,
                    request__branch_id=assignment.position.office_id,
                )
                if assignment else queryset.none()
            )
        if self.pending_only:
            queryset = queryset.filter(decision=RequestApproval.Decision.PENDING)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Pending Approvals" if self.pending_only else "Approval History"
        return context


class PendingApprovalListView(LoginRequiredMixin, View):
    """Approval Inbox landing.

    - Xefe/Director: existing RequestApproval tasks (unchanged).
    - Staff HR processing workspace (view+change_employeerequest, eligible
      branch scope): Schedule Processing summary with Waiting/Processed links.
      No RequestApproval rows are created or queried for Staff HR.
    """

    raise_exception = True

    def get(self, request, *args, **kwargs):
        user = request.user
        has_approval = user.has_perm("employee_requests.view_requestapproval")
        has_processing = staff_hr_processing_enabled(user)
        if not (user.is_superuser or has_approval or has_processing):
            raise PermissionDenied
        from core.permissions import DIRECTOR_GROUP, XEFE_GROUP
        show_approval = user.is_superuser or bool(
            has_approval
            and user.groups.filter(name__in=[XEFE_GROUP, DIRECTOR_GROUP]).exists()
        )
        if show_approval and has_processing:
            waiting_qs = staff_hr_processing_base_queryset(user).filter(
                schedule_processed_at__isnull=True
            )
            processed_qs = staff_hr_processing_base_queryset(user).filter(
                schedule_processed_at__isnull=False
            )
            return render(
                request,
                "employee_requests/inbox_landing.html",
                {
                    "page_title": "Approval Inbox",
                    "show_approval_tasks": True,
                    "show_schedule_processing": True,
                    "waiting_count": waiting_qs.count(),
                    "processed_count": processed_qs.count(),
                },
            )
        if show_approval:
            return ApprovalListView.as_view(pending_only=True)(request, *args, **kwargs)
        if has_processing:
            return redirect("employee_requests:schedule-processing-waiting")
        raise PermissionDenied


class ApprovalDetailView(RequestAccessMixin, DetailView):
    model = RequestApproval
    template_name = "employee_requests/approval_detail.html"
    permission_required = "employee_requests.view_requestapproval"

    def get_queryset(self):
        queryset = RequestApproval.objects.select_related(
            "request__employee_assignment__employee",
            "request__employee_assignment__position__office",
            "request__leave_type",
            "approver",
        )
        if self.request.user.has_perm("employee_requests.change_requestapproval") or is_hr_user(self.request.user):
            queryset = queryset.filter(approver=self.request.user)
        else:
            queryset = queryset.filter(request__submitted_by=self.request.user)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        approval = self.object
        context.update(
            decision_form=DecisionForm(),
            can_decide=can_decide_request_approval(self.request.user, approval),
            is_xefe=self.request.user.groups.filter(name="Xefe_Departamentu").exists(),
        )
        return context


def process_request_decision(*, request, approval, decision, note=""):
    with transaction.atomic():
        approval = RequestApproval.objects.select_for_update().select_related(
            "request__employee_assignment__employee",
            "request__employee_assignment__position__office",
        ).get(pk=approval.pk)
        request_record = EmployeeRequest.objects.select_for_update().get(
            pk=approval.request_id
        )
        previous_status = request_record.status
        if approval.decision != RequestApproval.Decision.PENDING:
            raise PermissionDenied("This approval step has already been processed.")
        if approval.step_number != 2 or request_record.workflow_stage != "director":
            raise PermissionDenied("Only an assigned Director decision is final.")
        if request_record.status not in (
            EmployeeRequest.Status.PENDING,
            EmployeeRequest.Status.UNDER_REVIEW,
        ):
            raise PermissionDenied("This request is no longer awaiting a decision.")
        if request_record.forwarded_at is None:
            raise PermissionDenied("This request has not been forwarded by Xefe.")
        if not RequestApproval.objects.filter(
            request=request_record,
            step_number=1,
            decision=RequestApproval.Decision.RECOMMENDED,
        ).exists():
            raise PermissionDenied("The Xefe review step has not been completed.")
        if request_record.submitted_by_id == request.user.pk:
            raise PermissionDenied("A requester cannot approve or reject their own request.")
        from employees.models import is_user_linked_to_employee
        req_emp = request_record.employee_assignment.employee
        if is_user_linked_to_employee(request.user, req_emp):
            raise PermissionDenied("A requester cannot approve or reject their own request.")
        if RequestApproval.objects.filter(
            request=request_record,
            step_number__lt=approval.step_number,
            decision=RequestApproval.Decision.PENDING,
        ).exists():
            raise PermissionDenied("A previous approval step must be completed first.")

        now = timezone.now()
        approval.decision = decision
        approval.decision_note = note or ""
        approval.decided_at = now
        approval.save(update_fields=["decision", "decision_note", "decided_at"])

        RequestApproval.objects.filter(
            request=request_record,
            step_number=approval.step_number,
            decision=RequestApproval.Decision.PENDING,
        ).exclude(pk=approval.pk).update(
            decision=RequestApproval.Decision.SKIPPED,
            decided_at=now,
            decision_note="Superseded by another reviewer's decision.",
        )

        request_record.processed_by = request.user
        request_record.processed_at = now
        request_record.processing_note = approval.decision_note
        if decision == RequestApproval.Decision.REJECTED:
            request_record.status = EmployeeRequest.Status.REJECTED
            request_record.final_decision_at = now
        else:
            has_pending_step = RequestApproval.objects.filter(
                request=request_record,
                decision=RequestApproval.Decision.PENDING,
            ).exists()
            request_record.status = (
                EmployeeRequest.Status.UNDER_REVIEW
                if has_pending_step else EmployeeRequest.Status.APPROVED
            )
            request_record.final_decision_at = None if has_pending_step else now
        request_record.save(
            update_fields=[
                "status", "processed_by", "processed_at", "processing_note",
                "final_decision_at", "updated_at",
            ]
        )
        log_action(
            actor_user=request.user,
            office=request_record.employee_assignment.position.office,
            action=(
                AuditLog.Action.APPROVE
                if decision == RequestApproval.Decision.APPROVED
                else AuditLog.Action.REJECT
            ),
            entity_type="EmployeeRequest",
            object_id=request_record.pk,
            before_data={
                "status": previous_status,
                "approval_decision": RequestApproval.Decision.PENDING,
            },
            after_data={
                "status": request_record.status,
                "approval_decision": approval.decision,
            },
            reason=approval.decision_note,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        if request_record.status == EmployeeRequest.Status.APPROVED:
            create_in_app_notification(
                recipient=request_record.submitted_by,
                request_record=request_record,
                notification_type=Notification.NotificationType.REQUEST_APPROVED,
                title=f"Request {request_record.request_number} approved",
                message=f"Your request {request_record.request_number} has been approved.",
            )
            for recipient in eligible_staff_hr_for_request(request_record):
                create_in_app_notification(
                    recipient=recipient,
                    request_record=request_record,
                    notification_type=Notification.NotificationType.REQUEST_APPROVED,
                    title=f"Request {request_record.request_number} approved",
                    message=f"Approved request {request_record.request_number} is ready for Staff HR processing.",
                )
        elif request_record.status == EmployeeRequest.Status.REJECTED:
            note_text = approval.decision_note.strip()
            create_in_app_notification(
                recipient=request_record.submitted_by,
                request_record=request_record,
                notification_type=Notification.NotificationType.REQUEST_REJECTED,
                title=f"Request {request_record.request_number} rejected",
                message=(
                    f"Your request {request_record.request_number} was rejected. "
                    f"Reason: {note_text}"
                    if note_text
                    else f"Your request {request_record.request_number} was rejected."
                ),
            )
        elif request_record.status == EmployeeRequest.Status.UNDER_REVIEW:
            create_in_app_notification(
                recipient=request_record.submitted_by,
                request_record=request_record,
                notification_type=Notification.NotificationType.REQUEST_UPDATED,
                title=f"Request {request_record.request_number} updated",
                message=f"Your request status is now {request_record.get_status_display()}.",
            )
            pending_steps = RequestApproval.objects.filter(
                request=request_record,
                decision=RequestApproval.Decision.PENDING,
            )
            step_numbers = list(pending_steps.values_list("step_number", flat=True))
            next_step_number = min(step_numbers) if step_numbers else None
            if next_step_number is not None:
                for next_approval in pending_steps.filter(step_number=next_step_number):
                    create_in_app_notification(
                        recipient=next_approval.approver,
                        request_record=request_record,
                        notification_type=Notification.NotificationType.REQUEST_SUBMITTED,
                        title=f"Request {request_record.request_number} awaiting your review",
                        message=f"Request {request_record.request_number} is ready for your approval.",
                    )
    return approval, request_record

class RequestForwardView(RequestAccessMixin, View):
    permission_required = "employee_requests.change_requestapproval"

    def post(self, request, pk):
        from core.permissions import resolve_xefe_assignment, XEFE_GROUP

        approval = get_object_or_404(
            RequestApproval.objects.select_related(
                "request__employee_assignment__position__office",
                "request__employee_assignment__position__department",
            ),
            pk=pk,
            approver=request.user,
        )
        request_record = approval.request
        assignment = resolve_xefe_assignment(request.user)
        request_assignment = request_record.employee_assignment.position
        if (
            not request.user.groups.filter(name=XEFE_GROUP).exists()
            or assignment is None
            or assignment.position.office_id != request_assignment.office_id
            or assignment.position.department_id != request_assignment.department_id
            or request_record.branch_id != request_assignment.office_id
            or approval.step_number != 1
        ):
            raise PermissionDenied("You are not authorized to forward this request.")
        with transaction.atomic():
            approval = RequestApproval.objects.select_for_update().select_related("request").get(pk=approval.pk)
            request_record = EmployeeRequest.objects.select_for_update().get(pk=request_record.pk)
            if approval.decision != RequestApproval.Decision.PENDING:
                raise PermissionDenied("This request has already been forwarded or reviewed.")
            if request_record.status != EmployeeRequest.Status.PENDING or request_record.workflow_stage != "xefe_review":
                raise PermissionDenied("The request is not awaiting Xefe review.")
            directors = eligible_directors_for_request(request_record)
            if len(directors) != 1:
                raise PermissionDenied(
                    "Request routing is not configured: exactly one eligible Director "
                    "must be assigned to this request's branch."
                )
            note = request.POST.get("decision_note", "").strip()
            now = timezone.now()
            approval.decision = RequestApproval.Decision.RECOMMENDED
            approval.decision_note = note
            approval.decided_at = now
            approval.save(update_fields=["decision", "decision_note", "decided_at"])
            RequestApproval.objects.update_or_create(
                request=request_record,
                approver=directors[0],
                step_number=2,
                defaults={"decision": RequestApproval.Decision.PENDING, "decision_note": ""},
            )
            request_record.workflow_stage = "director"
            request_record.status = EmployeeRequest.Status.UNDER_REVIEW
            request_record.xefe_review_note = note
            request_record.forwarded_at = now
            request_record.save(update_fields=[
                "workflow_stage", "status", "xefe_review_note", "forwarded_at", "updated_at",
            ])
            log_action(
                actor_user=request.user,
                office=request_record.employee_assignment.position.office,
                action=AuditLog.Action.UPDATE,
                entity_type="EmployeeRequest",
                object_id=request_record.pk,
                before_data={"status": EmployeeRequest.Status.PENDING},
                after_data={"status": EmployeeRequest.Status.UNDER_REVIEW, "workflow_stage": "director"},
                reason=note or "Forwarded to Director",
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            create_in_app_notification(
                recipient=directors[0],
                request_record=request_record,
                notification_type=Notification.NotificationType.REQUEST_SUBMITTED,
                title=f"Request {request_record.request_number} forwarded",
                message=f"Request {request_record.request_number} is awaiting Director review.",
            )
        messages.success(request, "Request forwarded to Director.")
        return redirect("employee_requests:approval-detail", pk=approval.pk)


class StaffScheduleProcessPreviewView(RequestAccessMixin, DetailView):
    """Read-only Staff HR preview of schedule effects before confirmation."""

    model = EmployeeRequest
    template_name = "employee_requests/schedule_processing_preview.html"
    permission_required = "employee_requests.view_employeerequest"

    def get_queryset(self):
        return staff_hr_processing_base_queryset(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from .schedule_processing import build_schedule_processing_preview
        preview = build_schedule_processing_preview(self.object, actor_user=self.request.user)
        context.update(
            page_title="Review Schedule Processing",
            preview=preview,
            can_confirm=(
                self.request.user.has_perm("employee_requests.change_employeerequest")
                and preview.can_apply
            ),
        )
        return context


class StaffScheduleProcessView(RequestAccessMixin, View):
    permission_required = "employee_requests.change_employeerequest"

    def get(self, request, pk):
        return redirect("employee_requests:request-process-preview", pk=pk)

    def post(self, request, pk):
        from core.permissions import resolve_staff_hr_office
        from .schedule_processing import apply_schedule_processing

        request_record = get_object_or_404(
            EmployeeRequest.objects.select_related(
                "employee_assignment__position__office",
                "employee_assignment__position__department",
                "employee_assignment__employee",
                "branch",
            ),
            pk=pk,
        )
        office = resolve_staff_hr_office(request.user)
        if not staff_hr_processing_enabled(request.user) or office is None:
            raise PermissionDenied("You are not authorized to process schedule results.")
        if request_record.branch_id != office.pk or request_record.employee_assignment.position.office_id != office.pk:
            raise Http404()
        if request_record.status != EmployeeRequest.Status.APPROVED:
            messages.error(request, "Only Director-approved requests can be processed.")
            return redirect("employee_requests:request-detail", pk=pk)
        if request.POST.get("confirm") != "1":
            return redirect("employee_requests:request-process-preview", pk=pk)

        fingerprint = (request.POST.get("fingerprint") or "").strip()
        if not fingerprint:
            messages.error(request, "Review the current schedule-processing preview and confirm from that page before applying changes.")
            return redirect("employee_requests:request-process-preview", pk=pk)
        try:
            obj, state, results = apply_schedule_processing(
                request_record, actor_user=request.user, office=office,
                ip_address=request.META.get("REMOTE_ADDR"), fingerprint=fingerprint,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("employee_requests:request-process-preview", pk=pk)

        if state == "already_processed":
            messages.info(request, "Schedule processing has already been recorded for this request.")
        else:
            messages.success(request, f"Staff HR schedule processing recorded ({len(results)} schedule(s) affected).")
        return redirect("employee_requests:request-detail", pk=obj.pk)

class ApprovalDecisionView(RequestAccessMixin, View):
    permission_required = "employee_requests.view_requestapproval"

    def post(self, request, pk, decision):
        if request.user.groups.filter(name="direktur_utama").exists():
            raise PermissionDenied("Direktur Utama may view requests but cannot process them.")
        if decision not in (
            RequestApproval.Decision.APPROVED,
            RequestApproval.Decision.REJECTED,
        ):
            return HttpResponseNotAllowed(["POST"])
        form = DecisionForm(request.POST)
        if not form.is_valid():
            messages.error(request, "The decision note is invalid.")
            return redirect("employee_requests:approval-detail", pk=pk)

        approval = get_object_or_404(
            RequestApproval, pk=pk, approver=request.user
        )
        from core.permissions import is_hr_user, resolve_hr_office

        if is_hr_user(request.user):
            hr_office = resolve_hr_office(request.user)
            if hr_office is None or approval.request.employee_assignment.position.office_id != hr_office.pk:
                raise Http404()
        if not can_decide_request_approval(request.user, approval):
            raise PermissionDenied("You are not authorized to decide this request.")
        try:
            approval, request_record = process_request_decision(
                request=request,
                approval=approval,
                decision=decision,
                note=form.cleaned_data["decision_note"],
            )
        except PermissionDenied as exc:
            messages.error(request, str(exc))
            return redirect("employee_requests:approval-detail", pk=pk)

        messages.success(request, f"Request decision recorded: {approval.get_decision_display()}.")
        return redirect("employee_requests:approval-detail", pk=approval.pk)


class NotificationListView(RequestAccessMixin, ListView):
    model = Notification
    template_name = "employee_requests/notification_list.html"
    context_object_name = "notifications"
    permission_required = "employee_requests.view_notification"
    paginate_by = 20
    unread_only = False

    def get_queryset(self):
        queryset = Notification.objects.filter(
            recipient_user=self.request.user
        ).select_related("request")
        if self.unread_only:
            queryset = queryset.filter(read_at__isnull=True)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Unread Notifications" if self.unread_only else "Notifications"
        context["notification_rows"] = [
            {
                "item": item,
                "action": "unread" if item.read_at else "read",
            }
            for item in context["notifications"]
        ]
        return context


class UnreadNotificationListView(NotificationListView):
    unread_only = True


class NotificationDetailView(RequestAccessMixin, DetailView):
    model = Notification
    template_name = "employee_requests/notification_detail.html"
    permission_required = "employee_requests.view_notification"

    def get_queryset(self):
        return Notification.objects.filter(
            recipient_user=self.request.user
        ).select_related("request")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["action"] = "unread" if self.object.read_at else "read"
        return context


class NotificationReadAction(RequestAccessMixin, View):
    permission_required = "employee_requests.change_notification"

    def post(self, request, pk, action):
        if action not in ("read", "unread"):
            return HttpResponseNotAllowed(["POST"])
        notification = get_object_or_404(
            Notification, pk=pk, recipient_user=request.user
        )
        notification.read_at = (
            timezone.now() if action == "read" else None
        )
        notification.save(update_fields=["read_at"])
        messages.success(
            request,
            "Notification marked as read." if action == "read" else "Notification marked as unread.",
        )
        return redirect("employee_requests:notification-detail", pk=notification.pk)

class ScheduleProcessingListView(RequestAccessMixin, ListView):
    """Staff HR Schedule Processing workspace (EmployeeRequest-based).

    Lists approved requests in the user's authorized branch. Uses
    EmployeeRequest records and links to request detail; never creates
    RequestApproval rows and never grants approval authority.
    """

    model = EmployeeRequest
    template_name = "employee_requests/schedule_processing_list.html"
    context_object_name = "requests"
    paginate_by = 20
    processing_state = "waiting"  # waiting|processed

    def has_permission(self):
        user = self.request.user
        if not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False):
            return True
        return staff_hr_processing_enabled(user)

    def get_queryset(self):
        base = staff_hr_processing_base_queryset(self.request.user)
        if self.processing_state == "processed":
            return base.filter(schedule_processed_at__isnull=False).order_by(
                "-schedule_processed_at", "-final_decision_at", "-submitted_at"
            )
        return base.filter(schedule_processed_at__isnull=True).order_by(
            "-final_decision_at", "-submitted_at"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        counts_base = staff_hr_processing_base_queryset(self.request.user)
        waiting_count = counts_base.filter(schedule_processed_at__isnull=True).count()
        processed_count = counts_base.filter(schedule_processed_at__isnull=False).count()
        is_waiting = self.processing_state != "processed"
        context.update(
            page_title="Schedule Processing",
            processing_state="waiting" if is_waiting else "processed",
            is_waiting=is_waiting,
            waiting_count=waiting_count,
            processed_count=processed_count,
        )
        return context


# ---------------------------------------------------------------------------
# Leave Calendar (read-only). Uses approved+processed LEAVE EmployeeRequests.
# Permission: employee_requests.view_leave_calendar. Scope via existing org
# resolvers (fail-closed). GETs never mutate business records.
# ---------------------------------------------------------------------------

class LeaveCalendarView(LoginRequiredMixin, View):
    template_name = "employee_requests/leave_calendar.html"
    raise_exception = True

    def get(self, request):
        from .leave_calendar import (
            CALENDAR_PERMISSION,
            authorized_leave_calendar_queryset,
            calendar_filter_choices,
            duration_label,
            has_calendar_permission,
            month_bounds,
            serialize_leave_event,
        )
        if not has_calendar_permission(request.user):
            raise PermissionDenied
        today = timezone.localdate()
        try:
            year = int(request.GET.get("year", today.year))
            month = int(request.GET.get("month", today.month))
            if month < 1 or month > 12:
                raise ValueError
        except (TypeError, ValueError):
            year, month = today.year, today.month
        # normalize year bounds reasonably
        if year < 2000 or year > 2100:
            year = today.year
        start, end = month_bounds(year, month)
        view_mode = request.GET.get("view", "calendar")
        if view_mode not in ("calendar", "list"):
            view_mode = "calendar"
        base_qs, scope, error = authorized_leave_calendar_queryset(
            request.user, start=start, end=end, params=request.GET
        )
        if error:
            return render(request, self.template_name, {
                "page_title": "Leave Calendar",
                "scope_error": error,
                "events": [],
                "days": [],
                "year": year, "month": month,
                "month_label": start.strftime("%B %Y"),
                "today": today, "view_mode": view_mode,
            }, status=403)
        # choices from scope-constrained base (month-bounded filtered set is fine for employee list)
        offices, departments, employees = calendar_filter_choices(base_qs, scope)
        events_qs = base_qs.order_by("start_date", "employee_assignment__employee__full_name", "id")
        events = [serialize_leave_event(r) for r in events_qs[:1000]]
        # month grid (Mon-Sun)
        cal = calendar.Calendar(firstweekday=0)
        weeks = []
        for week in cal.monthdatescalendar(year, month):
            row = []
            for d in week:
                day_events = [e for e in events if e["start_date"] <= d.isoformat() <= e["end_date"]]
                row.append({"date": d, "in_month": d.month == month, "is_today": d == today, "events": day_events[:8], "more": max(0, len(day_events)-8)})
            weeks.append(row)
        # nav
        prev_m = month - 1 or 12
        prev_y = year - 1 if month == 1 else year
        next_m = month % 12 + 1
        next_y = year + 1 if month == 12 else year
        # keep filters in nav/links
        keep = {k: v for k, v in request.GET.items() if k in ("department", "employee", "branch", "office", "view") and v}
        context = {
            "page_title": "Leave Calendar",
            "events": events,
            "weeks": weeks,
            "year": year, "month": month,
            "month_label": start.strftime("%B %Y"),
            "prev_year": prev_y, "prev_month": prev_m,
            "next_year": next_y, "next_month": next_m,
            "today": today, "view_mode": view_mode,
            "scope": scope,
            "offices": offices, "departments": departments, "employees_filter": employees,
            "show_branch_filter": scope and scope.get("kind") == "superuser",
            "selected_department": request.GET.get("department", ""),
            "selected_employee": request.GET.get("employee", ""),
            "selected_branch": request.GET.get("branch") or request.GET.get("office") or "",
            "keep_params": urlencode(keep),
            "scope_error": None,
            "total_count": events_qs.count(),
        }
        return render(request, self.template_name, context)


class LeaveCalendarEventsView(LoginRequiredMixin, View):
    """Allowlisted JSON events for the calendar (same scope as page)."""
    raise_exception = True

    def get(self, request):
        from .leave_calendar import (
            authorized_leave_calendar_queryset,
            has_calendar_permission,
            month_bounds,
            serialize_leave_event,
        )
        if not has_calendar_permission(request.user):
            return JsonResponse({"detail": "Forbidden"}, status=403)
        today = timezone.localdate()
        try:
            year = int(request.GET.get("year", today.year))
            month = int(request.GET.get("month", today.month))
            if month < 1 or month > 12:
                raise ValueError
        except (TypeError, ValueError):
            year, month = today.year, today.month
        if year < 2000 or year > 2100:
            year = today.year
        start, end = month_bounds(year, month)
        qs, scope, error = authorized_leave_calendar_queryset(request.user, start=start, end=end, params=request.GET)
        if error:
            return JsonResponse({"detail": error}, status=403)
        events = [serialize_leave_event(r) for r in qs.order_by("start_date", "id")[:1000]]
        return JsonResponse({"events": events, "count": len(events)})


class LeaveCalendarEventDetailView(LoginRequiredMixin, View):
    """Allowlisted single-event detail (same scope). No private request fields."""
    raise_exception = True

    def get(self, request, pk):
        from .leave_calendar import (
            authorized_leave_calendar_queryset,
            has_calendar_permission,
            serialize_leave_event,
        )
        if not has_calendar_permission(request.user):
            return JsonResponse({"detail": "Forbidden"}, status=403)
        qs, scope, error = authorized_leave_calendar_queryset(request.user, params=request.GET)
        if error:
            return JsonResponse({"detail": error}, status=403)
        # scope enforced by queryset (includes branch/dept constraints); pk lookup inside it
        try:
            obj = qs.get(pk=pk)
        except (EmployeeRequest.DoesNotExist, ValueError, TypeError):
            return JsonResponse({"detail": "Not found"}, status=404)
        return JsonResponse({"event": serialize_leave_event(obj)})

