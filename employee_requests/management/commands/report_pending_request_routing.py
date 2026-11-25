from django.core.management.base import BaseCommand

from employee_requests.models import EmployeeRequest, RequestApproval


class Command(BaseCommand):
    help = "Reports pending requests that may need manual routing to Xefe or Director."

    def handle(self, *args, **options):
        pending = EmployeeRequest.objects.filter(
            status__in=[
                EmployeeRequest.Status.PENDING,
                EmployeeRequest.Status.UNDER_REVIEW,
            ]
        ).select_related("employee_assignment__position__office")
        found = 0
        for request_record in pending:
            legacy_hr_tasks = request_record.approval_steps.filter(
                approver__groups__name="HR Office",
                decision=RequestApproval.Decision.PENDING,
            )
            if legacy_hr_tasks.exists() or request_record.workflow_stage in ("", "xefe_review"):
                found += 1
                office = request_record.employee_assignment.position.office
                self.stdout.write(
                    f"{request_record.request_number}: {request_record.status}/{request_record.workflow_stage}; "
                    f"branch={office.office_code}; route to one eligible Xefe for "
                    f"{request_record.employee_assignment.position.department_id} in stage 'xefe_review', "
                    "then one branch Director; legacy tasks were not rewritten."
                )
        if not found:
            self.stdout.write("No pending requests require routing review.")
