from django.core.management.base import BaseCommand
from django.utils import timezone

from core.permissions import is_hr_user, resolve_hr_office
from employee_requests.models import EmployeeRequest, RequestApproval
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = "Backfill HR office approval rows for eligible pending requests (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report without changes.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        User = get_user_model()
        created = 0
        skipped = 0
        pending = EmployeeRequest.objects.filter(
            status__in=[EmployeeRequest.Status.PENDING, EmployeeRequest.Status.UNDER_REVIEW]
        ).select_related("employee_assignment__position__office")
        for req in pending:
            office = req.employee_assignment.position.office
            for hr_user in User.objects.filter(is_active=True, groups__name="HR Office"):
                if not hr_user.has_perm("employee_requests.view_requestapproval"):
                    continue
                if not is_hr_user(hr_user):
                    continue
                hr_office = resolve_hr_office(hr_user)
                if hr_office is None or hr_office.pk != office.pk:
                    continue
                # Self-approval exclusion.
                if req.submitted_by_id == hr_user.pk:
                    continue
                try:
                    from employees.models import is_user_linked_to_employee
                    if is_user_linked_to_employee(hr_user, req.employee_assignment.employee):
                        continue
                except Exception:
                    pass
                if hr_user.groups.filter(name="direktur_utama").exists():
                    continue
                exists = RequestApproval.objects.filter(request=req, approver=hr_user).exists()
                if exists:
                    skipped += 1
                    continue
                if dry_run:
                    self.stdout.write(f"[dry-run] would create approval for request {req.pk} -> hr user {hr_user.pk}")
                    created += 1
                    continue
                RequestApproval.objects.get_or_create(
                    request=req, approver=hr_user, step_number=1
                )
                created += 1
        self.stdout.write(self.style.SUCCESS(f"Created: {created}, skipped existing: {skipped}"))