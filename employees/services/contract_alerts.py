"""Contract-expiry in-app notification service.

Recipients (fail-closed, never widened):

* active Staff HR users whose *verified* branch scope equals the employee's branch
  (current primary assignment office);
* active employee-role accounts linked to that Employee through ``UserEmployeeLink``.

Other administrative accounts that merely share the Employee link are not notified.

Dedup is persistent: :class:`employee_requests.models.ContractExpiryAlert` has a
database unique constraint on (employee, contract end date, recipient, alert type).
The alert row and its :class:`~employee_requests.models.Notification` are created in
one transaction, so a rollback never records a delivered alert.

Only in-app notifications are produced. No email/push delivery is claimed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ALERT_WINDOW_DAYS = 7


@dataclass
class ContractExpiryAlertResult:
    evaluated_count: int = 0
    sent_count: int = 0
    already_sent_count: int = 0
    no_recipient_count: int = 0
    invalid_scope_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)
    employees_without_recipients: list[str] = field(default_factory=list)
    employees_with_invalid_scope: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"evaluated={self.evaluated_count} sent={self.sent_count} already_sent={self.already_sent_count} "
            f"no_recipient={self.no_recipient_count} invalid_scope={self.invalid_scope_count} errors={self.error_count}"
        )


def business_today() -> date:
    """Today's date in the configured business time zone (Asia/Dili by default)."""
    name = getattr(settings, "TIME_ZONE", "") or "Asia/Dili"
    try:
        tz = ZoneInfo(name)
    except Exception:
        tz = timezone.get_current_timezone()
    return timezone.localtime(timezone.now(), tz).date()


def _employee_branch(employee):
    from core.permissions import current_primary_assignment

    assignment = current_primary_assignment(employee)
    if assignment is None:
        return None, None
    return assignment, assignment.position.office


def _employee_recipients(employee, office):
    """Return (recipients, invalid_scope_flag)."""
    from django.contrib.auth import get_user_model

    from core.permissions import (
        STAFF_HR_GROUP,
        is_employee_group_user,
        is_staff_hr_user,
        resolve_staff_hr_office,
    )

    user_model = get_user_model()
    recipients = []

    if office is None:
        return recipients, True

    # Staff HR with a verified branch scope matching this employee's branch.
    staff_hr = (
        user_model.objects.filter(is_active=True, groups__name=STAFF_HR_GROUP)
        .distinct()
        .select_related("employee_account_link__employee")
    )
    for user in staff_hr:
        if is_staff_hr_user(user) and resolve_staff_hr_office(user) is not None:
            if resolve_staff_hr_office(user).pk == office.pk:
                recipients.append(user)

    # Employee-role accounts linked to this employee.
    linked_user_ids = employee.get_linked_user_ids(active_only=True)
    if linked_user_ids:
        linked = user_model.objects.filter(pk__in=linked_user_ids, is_active=True).distinct()
        for user in linked:
            if is_employee_group_user(user):
                recipients.append(user)

    unique = {}
    for user in recipients:
        unique[user.pk] = user
    return list(unique.values()), False


def _notification_link(employee):
    try:
        return reverse("employees:employee-detail", args=[employee.pk])
    except NoReverseMatch:  # pragma: no cover - defensive
        return ""


def _message(employee, remaining_days: int, end_date: date) -> str:
    return (
        f"Contract of {employee.employee_id} — {employee.full_name} ends on {end_date} "
        f"({remaining_days} day{'s' if remaining_days != 1 else ''} remaining). "
        "Review the contract and plan the next assignment in time."
    )


def _deliver(employee, office, recipient, end_date: date, remaining_days: int, alert_type: str):
    from employee_requests.models import ContractExpiryAlert, Notification

    link = _notification_link(employee)
    title = f"Contract expiring in {remaining_days} day{'s' if remaining_days != 1 else ''}: {employee.employee_id}"
    message = _message(employee, remaining_days, end_date)
    if link:
        message = f"{message} Open the employee record: {link}"

    with transaction.atomic():
        # get_or_create relies on the DB unique constraint, not a plain exists() check,
        # so concurrent runs cannot both deliver.
        alert, created = ContractExpiryAlert.objects.get_or_create(
            employee=employee,
            contract_end_date=end_date,
            recipient_user=recipient,
            alert_type=alert_type,
            defaults={"sent_at": timezone.now()},
        )
        if not created:
            return "already_sent"
        notification = Notification.objects.create(
            recipient_user=recipient,
            request=None,
            notification_type=Notification.NotificationType.GENERAL,
            title=title,
            message=message,
            channel=Notification.Channel.IN_APP,
            delivery_status=Notification.DeliveryStatus.SENT,
            sent_at=timezone.now(),
        )
        alert.notification = notification
        alert.save(update_fields=["notification", "sent_at"])
        return "sent"


def send_upcoming_contract_expiry_alerts(*, today=None, dry_run=False) -> ContractExpiryAlertResult:
    """Evaluate employees whose contract ends within 0..7 days and notify recipients once.

    ``dry_run`` reports what would happen without writing anything.
    """
    from employees.models import Employee

    today = today or business_today()
    horizon = today + timedelta(days=ALERT_WINDOW_DAYS)
    result = ContractExpiryAlertResult()

    employees = list(
        Employee.objects.filter(
            contract_end_date__isnull=False,
            contract_end_date__gte=today,
            contract_end_date__lte=horizon,
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        .select_related()
        .order_by("employee_id")
    )

    for employee in employees:
        result.evaluated_count += 1
        end_date = employee.contract_end_date
        remaining = (end_date - today).days
        from employee_requests.models import ContractExpiryAlert

        alert_type = ContractExpiryAlert.AlertType.UPCOMING
        try:
            assignment, office = _employee_branch(employee)
            if office is None:
                result.invalid_scope_count += 1
                result.employees_with_invalid_scope.append(employee.employee_id)
                continue
            recipients, invalid_scope = _employee_recipients(employee, office)
            if invalid_scope:
                result.invalid_scope_count += 1
                result.employees_with_invalid_scope.append(employee.employee_id)
                continue
            if not recipients:
                result.no_recipient_count += 1
                result.employees_without_recipients.append(employee.employee_id)
                continue

            for recipient in recipients:
                if dry_run:
                    result.sent_count += 1
                    continue
                status = _deliver(employee, office, recipient, end_date, remaining, alert_type)
                if status == "sent":
                    result.sent_count += 1
                else:
                    result.already_sent_count += 1
        except Exception as exc:  # pragma: no cover - defensive
            result.error_count += 1
            result.errors.append(f"{employee.employee_id}: {exc}")
            logger.exception("Contract expiry alert failed for employee %s", employee.employee_id)

    logger.info("Contract expiry alerts: %s", result.summary())
    return result