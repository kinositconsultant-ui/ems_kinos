from datetime import date, datetime

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse

from audittrail.models import AuditLog
from attendance.models import EmployeeSchedule
from employees.models import Employee, EmployeeAssignment, UserEmployeeLink, get_linked_employee
from organization.models import Department, Office, Position

from .models import EmployeeRequest, LeaveType, Notification, RequestApproval


User = get_user_model()


class EmployeeRequestWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.employee_group = Group.objects.create(name="Employess")
        cls.xefe_group = Group.objects.create(name="Xefe_Departamentu")
        cls.director_group = Group.objects.create(name="Director")
        cls.staff_hr_group = Group.objects.create(name="Staff HR")
        cls.hr_office_group = Group.objects.create(name="HR Office")

        for codename in (
            "view_employeerequest",
            "add_employeerequest",
            "change_employeerequest",
            "view_requestapproval",
            "change_requestapproval",
        ):
            permission = Permission.objects.get(codename=codename)
            for group in (cls.employee_group, cls.xefe_group, cls.director_group, cls.staff_hr_group):
                group.permissions.add(permission)

        cls.branch_a = Office.objects.create(
            office_code="BR-A", office_name="Branch A", office_type="branch"
        )
        cls.branch_b = Office.objects.create(
            office_code="BR-B", office_name="Branch B", office_type="branch"
        )
        cls.department_a = Department.objects.create(
            office=cls.branch_a, department_code="OPS-A", department_name="Operations A"
        )
        cls.department_a2 = Department.objects.create(
            office=cls.branch_a, department_code="FIN-A", department_name="Finance A"
        )
        cls.department_b = Department.objects.create(
            office=cls.branch_b, department_code="OPS-B", department_name="Operations B"
        )
        cls.employee_position = Position.objects.create(
            office=cls.branch_a, department=cls.department_a,
            position_code="EMP-A", position_title="Employee"
        )
        cls.xefe_position = Position.objects.create(
            office=cls.branch_a, department=cls.department_a,
            position_code="XEF-A", position_title="Department Xefe",
            is_management=True,
        )
        cls.director_position = Position.objects.create(
            office=cls.branch_a, department=cls.department_a,
            position_code="DIR-A", position_title="Director",
            is_management=True,
        )
        cls.staff_position = Position.objects.create(
            office=cls.branch_a, department=cls.department_a,
            position_code="STA-A", position_title="Staff HR",
        )

        cls.employee = cls.make_employee("employee", "EMPLOYEE", cls.employee_group, cls.employee_position)
        cls.xefe = cls.make_employee("xefe", "XEFE", cls.xefe_group, cls.xefe_position)
        cls.director = cls.make_employee("director", "DIRECTOR", cls.director_group, cls.director_position)
        cls.staff_hr = cls.make_employee("staffhr", "STAFFHR", cls.staff_hr_group, cls.staff_position)

        cls.leave_type = LeaveType.objects.create(
            code="AL", name="Annual Leave", category=LeaveType.Category.ANNUAL
        )

    @classmethod
    def make_employee(cls, username, employee_id, group, position):
        user = User.objects.create_user(username=username, password="test-pass")
        employee = Employee.objects.create(
            employee_id=employee_id, full_name=username.title(),
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        UserEmployeeLink.objects.create(user=user, employee=employee)
        EmployeeAssignment.objects.create(
            employee=employee, position=position, start_date=date.today(),
            is_primary=True, assignment_type=EmployeeAssignment.AssignmentType.PRIMARY,
            assigned_by=user,
        )
        user.groups.add(group)
        return user

    def make_request(self):
        assignment = get_linked_employee(self.employee).assignments.get()
        request_record = EmployeeRequest.objects.create(
            request_number=f"REQ-{EmployeeRequest.objects.count() + 1}",
            employee_assignment=assignment,
            branch=self.branch_a,
            submitted_by=self.employee,
            request_type=EmployeeRequest.RequestType.LEAVE,
            leave_type=self.leave_type,
            start_date=date.today(),
            end_date=date.today(),
            reason="Family leave",
        )
        RequestApproval.objects.create(
            request=request_record, approver=self.xefe, step_number=1,
        )
        return request_record

    def forward(self, request_record):
        approval = request_record.approval_steps.get(step_number=1)
        self.client.force_login(self.xefe)
        response = self.client.post(
            reverse("employee_requests:approval-forward", args=[approval.pk]),
            {"decision_note": " "},
        )
        self.assertEqual(response.status_code, 302)
        request_record.refresh_from_db()
        return request_record.approval_steps.get(step_number=2)

    def test_xefe_personal_self_service_is_denied_and_employee_remains_allowed(self):
        self.client.force_login(self.xefe)
        for url_name in ("request-mine", "request-create"):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse("employee_requests:" + url_name))
                self.assertEqual(response.status_code, 403)
        self.client.force_login(self.employee)
        self.assertEqual(
            self.client.get(reverse("employee_requests:request-mine")).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("employee_requests:request-create")).status_code,
            200,
        )

    def test_submission_forwarding_and_repeated_forwarding(self):
        request_record = self.make_request()
        self.client.force_login(self.xefe)
        approval = request_record.approval_steps.get(step_number=1)
        self.assertEqual(
            self.client.get(reverse("employee_requests:approval-detail", args=[approval.pk])).status_code,
            200,
        )
        request_record.refresh_from_db()
        self.assertEqual(request_record.workflow_stage, "xefe_review")
        self.assertEqual(request_record.approval_steps.filter(step_number=2).count(), 0)

        director_approval = self.forward(request_record)
        self.assertEqual(request_record.approval_steps.filter(step_number=2).count(), 1)
        self.assertEqual(
            Notification.objects.filter(
                request=request_record,
                recipient_user=self.director,
            ).count(),
            1,
        )
        self.client.post(
            reverse("employee_requests:approval-forward", args=[approval.pk]),
            {"decision_note": ""},
        )
        self.assertEqual(request_record.approval_steps.filter(step_number=2).count(), 1)
        self.assertEqual(director_approval.request_id, request_record.pk)

    def test_director_decision_before_forward_is_denied(self):
        request_record = self.make_request()
        approval = RequestApproval.objects.create(
            request=request_record, approver=self.director, step_number=2,
        )
        request_record.status = EmployeeRequest.Status.UNDER_REVIEW
        request_record.workflow_stage = "director"
        request_record.save(update_fields=["status", "workflow_stage"])
        self.client.force_login(self.director)
        response = self.client.post(
            reverse("employee_requests:approval-decision", args=[approval.pk, "approved"]),
            {"decision_note": ""},
        )
        self.assertIn(response.status_code, (302, 403))
        request_record.refresh_from_db()
        self.assertEqual(request_record.status, EmployeeRequest.Status.UNDER_REVIEW)

    def test_director_approval_and_rejection_notifications(self):
        request_record = self.make_request()
        approval = self.forward(request_record)
        approval = RequestApproval.objects.get(pk=approval.pk)
        self.client.force_login(self.director)
        response = self.client.post(
            reverse("employee_requests:approval-decision", args=[approval.pk, "approved"]),
            {"decision_note": ""},
        )
        self.assertEqual(response.status_code, 302)
        request_record.refresh_from_db()
        self.assertEqual(request_record.status, EmployeeRequest.Status.APPROVED)
        self.assertTrue(Notification.objects.filter(
            request=request_record, recipient_user=self.employee,
            notification_type=Notification.NotificationType.REQUEST_APPROVED,
        ).exists())
        self.assertTrue(Notification.objects.filter(
            request=request_record, recipient_user=self.staff_hr,
            notification_type=Notification.NotificationType.REQUEST_APPROVED,
        ).exists())

        rejected_request = self.make_request()
        rejected_approval = self.forward(rejected_request)
        self.client.force_login(self.director)
        self.client.post(
            reverse("employee_requests:approval-decision", args=[rejected_approval.pk, "rejected"]),
            {"decision_note": ""},
        )
        rejected_request.refresh_from_db()
        self.assertEqual(rejected_request.status, EmployeeRequest.Status.REJECTED)
        self.assertFalse(Notification.objects.filter(
            request=rejected_request,
            recipient_user__groups__name="Staff HR",
        ).exists())

    def test_staff_hr_queue_and_processed_leave(self):
        from datetime import time as dtime
        from attendance.models import EmployeeSchedule, Shift
        request_record = self.make_request()
        assignment = request_record.employee_assignment
        shift = Shift.objects.create(office=self.branch_a, name="LeaveProc", start_time=dtime(8, 0), end_time=dtime(17, 0))
        EmployeeSchedule.objects.create(
            employee_assignment=assignment, shift=shift, work_date=request_record.start_date,
            scheduled_start_at=timezone.make_aware(datetime.combine(request_record.start_date, dtime(8, 0))),
            scheduled_end_at=timezone.make_aware(datetime.combine(request_record.start_date, dtime(17, 0))),
            status=EmployeeSchedule.Status.SCHEDULED,
        )
        approval = self.forward(request_record)
        request_record.status = EmployeeRequest.Status.APPROVED
        request_record.save(update_fields=["status"])
        approval.decision = RequestApproval.Decision.APPROVED
        approval.save(update_fields=["decision"])

        self.client.force_login(self.staff_hr)
        response = self.client.get(reverse("employee_requests:request-all"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, request_record.request_number)
        preview = self.client.get(reverse("employee_requests:request-process-preview", args=[request_record.pk]))
        fp = preview.context["preview"].fingerprint
        response = self.client.post(
            reverse("employee_requests:request-process-result", args=[request_record.pk]),
            {"confirm": "1", "fingerprint": fp},
        )
        self.assertEqual(response.status_code, 302)
        request_record.refresh_from_db()
        self.assertIsNotNone(request_record.schedule_processed_at)
        self.assertEqual(request_record.schedule_processed_by, self.staff_hr)

    def test_xefe_create_post_is_denied_and_employee_post_creates_only_xefe_task(self):
        self.client.force_login(self.xefe)
        self.assertEqual(
            self.client.post(reverse("employee_requests:request-create"), {}).status_code,
            403,
        )

        self.client.force_login(self.employee)
        response = self.client.post(
            reverse("employee_requests:request-create"),
            {
                "request_category": "leave",
                "employee_assignment": get_linked_employee(self.employee).assignments.get().pk,
                "request_type": EmployeeRequest.RequestType.LEAVE,
                "duration_type": EmployeeRequest.DurationType.FULL_DAY,
                "leave_type": self.leave_type.pk,
                "start_date": date.today(),
                "end_date": date.today(),
                "reason": "Submitted for real workflow routing",
            },
        )
        self.assertEqual(response.status_code, 302)
        request_record = EmployeeRequest.objects.get(submitted_by=self.employee)
        approvals = RequestApproval.objects.filter(request=request_record)
        self.assertEqual(approvals.count(), 1)
        self.assertEqual(approvals.get().approver, self.xefe)
        self.assertFalse(approvals.filter(step_number=2).exists())
        self.assertTrue(Notification.objects.filter(
            request=request_record,
            recipient_user=self.xefe,
            notification_type=Notification.NotificationType.REQUEST_SUBMITTED,
        ).exists())
        self.assertFalse(Notification.objects.filter(
            request=request_record,
            recipient_user=self.director,
        ).exists())
        self.assertFalse(Notification.objects.filter(
            request=request_record,
            recipient_user=self.staff_hr,
        ).exists())

    def test_forward_notes_and_scope_denials(self):
        for note in ("", "   ", "Please review this request"):
            with self.subTest(note=note):
                request_record = self.make_request()
                approval = request_record.approval_steps.get(step_number=1)
                self.client.force_login(self.xefe)
                response = self.client.post(
                    reverse("employee_requests:approval-forward", args=[approval.pk]),
                    {"decision_note": note},
                )
                self.assertEqual(response.status_code, 302)
                request_record.refresh_from_db()
                approval.refresh_from_db()
                self.assertEqual(approval.decision_note, note.strip())
                self.assertEqual(request_record.xefe_review_note, note.strip())
                self.assertEqual(request_record.workflow_stage, "director")

        other_xefe = self.make_employee(
            "other-xefe",
            "OTHERXEFE",
            self.xefe_group,
            Position.objects.create(
                office=self.branch_a,
                department=self.department_a2,
                position_code="XEF-A2",
                position_title="Other Department Xefe",
                is_management=True,
            ),
        )
        request_record = self.make_request()
        approval = request_record.approval_steps.get(step_number=1)
        self.client.force_login(other_xefe)
        self.assertIn(
            self.client.post(
                reverse("employee_requests:approval-forward", args=[approval.pk]),
                {"decision_note": ""},
            ).status_code,
            (403, 404),
        )

    def test_xefe_cannot_decide_and_hr_office_is_excluded(self):
        request_record = self.make_request()
        director_approval = self.forward(request_record)
        request_record.status = EmployeeRequest.Status.UNDER_REVIEW
        request_record.save(update_fields=["status"])
        xefe_approval = request_record.approval_steps.get(step_number=1)
        self.client.force_login(self.xefe)
        self.assertIn(
            self.client.post(
                reverse(
                    "employee_requests:approval-decision",
                    args=[xefe_approval.pk, "approved"],
                ),
                {"decision_note": ""},
            ).status_code,
            (302, 403),
        )

        hr_office = User.objects.create_user(username="hr-office", password="test-pass")
        hr_office.groups.add(self.hr_office_group)
        self.client.force_login(hr_office)
        self.assertNotEqual(
            self.client.post(
                reverse(
                    "employee_requests:approval-decision",
                    args=[director_approval.pk, "approved"],
                ),
                {"decision_note": ""},
            ).status_code,
            200,
        )

    def test_assigned_director_and_no_schedule_changes_on_decision(self):
        request_record = self.make_request()
        director_approval = self.forward(request_record)

        unassigned_director = self.make_employee(
            "other-director",
            "OTHERDIR",
            self.director_group,
            Position.objects.create(
                office=self.branch_b,
                department=self.department_b,
                position_code="DIR-B",
                position_title="Branch B Director",
                is_management=True,
            ),
        )
        self.client.force_login(unassigned_director)
        response = self.client.post(
            reverse(
                "employee_requests:approval-decision",
                args=[director_approval.pk, "approved"],
            ),
            {"decision_note": ""},
        )
        self.assertIn(response.status_code, (302, 403, 404))
        request_record.refresh_from_db()
        self.assertEqual(request_record.status, EmployeeRequest.Status.UNDER_REVIEW)
        self.assertIsNone(request_record.schedule_processed_at)

        self.client.force_login(self.director)
        self.client.post(
            reverse(
                "employee_requests:approval-decision",
                args=[director_approval.pk, "rejected"],
            ),
            {"decision_note": ""},
        )
        request_record.refresh_from_db()
        self.assertEqual(request_record.status, EmployeeRequest.Status.REJECTED)
        self.assertIsNone(request_record.schedule_processed_at)
        self.assertTrue(Notification.objects.filter(
            request=request_record,
            recipient_user=self.employee,
            notification_type=Notification.NotificationType.REQUEST_REJECTED,
        ).exists())
        self.assertFalse(Notification.objects.filter(
            request=request_record,
            recipient_user__groups__name="Staff HR",
        ).exists())

        director_approval.refresh_from_db()
        self.client.post(
            reverse(
                "employee_requests:approval-decision",
                args=[director_approval.pk, "approved"],
            ),
            {"decision_note": ""},
        )
        request_record.refresh_from_db()
        self.assertEqual(request_record.status, EmployeeRequest.Status.REJECTED)

    def test_rejection_without_staff_hr_and_approval_later_staff_hr_queue(self):
        staff_hr = self.staff_hr
        staff_hr.groups.remove(self.staff_hr_group)

        rejected_request = self.make_request()
        rejected_approval = self.forward(rejected_request)
        self.client.force_login(self.director)
        self.client.post(
            reverse(
                "employee_requests:approval-decision",
                args=[rejected_approval.pk, "rejected"],
            ),
            {"decision_note": ""},
        )
        rejected_request.refresh_from_db()
        self.assertEqual(rejected_request.status, EmployeeRequest.Status.REJECTED)
        self.assertFalse(Notification.objects.filter(
            request=rejected_request,
            recipient_user__groups__name="Staff HR",
        ).exists())

        approved_request = self.make_request()
        approved_approval = self.forward(approved_request)
        self.client.force_login(self.director)
        self.client.post(
            reverse(
                "employee_requests:approval-decision",
                args=[approved_approval.pk, "approved"],
            ),
            {"decision_note": ""},
        )
        approved_request.refresh_from_db()
        self.assertEqual(approved_request.status, EmployeeRequest.Status.APPROVED)
        staff_hr.groups.add(self.staff_hr_group)
        self.client.force_login(staff_hr)
        response = self.client.get(reverse("employee_requests:request-all"))
        self.assertContains(response, approved_request.request_number)

    def test_staff_hr_processing_scope_completed_detail_and_duplicate_denial(self):
        request_record = self.make_request()
        director_approval = self.forward(request_record)
        request_record.status = EmployeeRequest.Status.APPROVED
        request_record.save(update_fields=["status"])
        director_approval.decision = RequestApproval.Decision.APPROVED
        director_approval.save(update_fields=["decision"])

        from datetime import time as dtime
        from attendance.models import EmployeeSchedule, Shift
        assignment = request_record.employee_assignment
        shift = Shift.objects.create(office=self.branch_a, name="DupProc", start_time=dtime(8, 0), end_time=dtime(17, 0))
        EmployeeSchedule.objects.create(
            employee_assignment=assignment, shift=shift, work_date=request_record.start_date,
            scheduled_start_at=timezone.make_aware(datetime.combine(request_record.start_date, dtime(8, 0))),
            scheduled_end_at=timezone.make_aware(datetime.combine(request_record.start_date, dtime(17, 0))),
            status=EmployeeSchedule.Status.SCHEDULED,
        )
        self.client.force_login(self.staff_hr)
        process_url = reverse(
            "employee_requests:request-process-result",
            args=[request_record.pk],
        )
        preview = self.client.get(reverse("employee_requests:request-process-preview", args=[request_record.pk]))
        fp = preview.context["preview"].fingerprint
        self.assertEqual(self.client.post(process_url, {"confirm": "1", "fingerprint": fp}).status_code, 302)
        request_record.refresh_from_db()
        first_processed_at = request_record.schedule_processed_at
        processed_by = request_record.schedule_processed_by
        self.client.post(process_url, {"confirm": "1", "fingerprint": fp})
        request_record.refresh_from_db()
        self.assertEqual(request_record.schedule_processed_at, first_processed_at)
        self.assertEqual(request_record.schedule_processed_by, processed_by)
        self.assertEqual(
            self.client.get(
                reverse("employee_requests:request-detail", args=[request_record.pk])
            ).status_code,
            200,
        )

        pending_request = self.make_request()
        self.client.post(
            reverse(
                "employee_requests:request-process-result",
                args=[pending_request.pk],
            ),
            {"confirm": "1", "fingerprint": "stale"},
        )
        pending_request.refresh_from_db()
        self.assertIsNone(pending_request.schedule_processed_at)

    def test_superuser_navigation_and_access(self):
        superuser = User.objects.create_superuser(
            username="workflow-superuser",
            email="workflow@example.com",
            password="test-pass",
        )
        self.client.force_login(superuser)
        response = self.client.get(reverse("employee_requests:request-mine"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(reverse("employee_requests:request-create")).status_code,
            200,
        )
        superuser.groups.add(self.xefe_group)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_self_approval_and_incomplete_forwarding_are_denied(self):
        request_record = self.make_request()
        request_record.submitted_by = self.director
        request_record.save(update_fields=["submitted_by"])
        director_approval = RequestApproval.objects.create(
            request=request_record,
            approver=self.director,
            step_number=2,
        )
        request_record.forwarded_at = None
        request_record.workflow_stage = "director"
        request_record.status = EmployeeRequest.Status.UNDER_REVIEW
        request_record.save(update_fields=["forwarded_at", "workflow_stage", "status"])
        self.client.force_login(self.director)
        response = self.client.post(
            reverse(
                "employee_requests:approval-decision",
                args=[director_approval.pk, "approved"],
            ),
            {"decision_note": ""},
        )
        self.assertIn(response.status_code, (302, 403))
        request_record.refresh_from_db()
        self.assertEqual(request_record.status, EmployeeRequest.Status.UNDER_REVIEW)
        self.assertFalse(Notification.objects.filter(
            request=request_record,
            notification_type=Notification.NotificationType.REQUEST_APPROVED,
        ).exists())

class EmployeeRequestEditSecurityTests(TestCase):
    """Unauthorized EmployeeRequest editing and review-integrity tests.

    Group permissions mirror the manually maintained production groups:
    Xefe_Departamentu and Director hold change_requestapproval; Staff HR
    holds change_employeerequest. Employees hold view/add rights for their
    own requests only. Nothing here modifies production users or groups;
    every fixture lives in the isolated test database.
    """

    @classmethod
    def make_employee(cls, username, employee_id, group, position):
        user = User.objects.create_user(username=username, password="test-pass")
        employee = Employee.objects.create(
            employee_id=employee_id,
            full_name=username.title(),
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        UserEmployeeLink.objects.create(user=user, employee=employee)
        EmployeeAssignment.objects.create(
            employee=employee,
            position=position,
            start_date=date.today(),
            is_primary=True,
            assignment_type=EmployeeAssignment.AssignmentType.PRIMARY,
            assigned_by=user,
        )
        user.groups.add(group)
        return user

    @classmethod
    def setUpTestData(cls):
        cls.employee_group = Group.objects.create(name="Employess")
        cls.xefe_group = Group.objects.create(name="Xefe_Departamentu")
        cls.director_group = Group.objects.create(name="Director")
        cls.staff_hr_group = Group.objects.create(name="Staff HR")

        view_employeerequest = Permission.objects.get(codename="view_employeerequest")
        add_employeerequest = Permission.objects.get(codename="add_employeerequest")
        change_employeerequest = Permission.objects.get(codename="change_employeerequest")
        view_requestapproval = Permission.objects.get(codename="view_requestapproval")
        change_requestapproval = Permission.objects.get(codename="change_requestapproval")

        # Employees submit and view their own requests only.
        cls.employee_group.permissions.add(view_employeerequest, add_employeerequest)
        # Workflow review/decision permissions.
        cls.xefe_group.permissions.add(view_requestapproval, change_requestapproval)
        cls.director_group.permissions.add(view_requestapproval, change_requestapproval)
        # Staff HR's processing permission must not confer generic edit rights.
        cls.staff_hr_group.permissions.add(change_employeerequest)

        cls.branch_a = Office.objects.create(
            office_code="BR-A", office_name="Branch A", office_type="branch"
        )
        cls.department_a = Department.objects.create(
            office=cls.branch_a,
            department_code="OPS-A",
            department_name="Operations A",
        )
        cls.employee_position = Position.objects.create(
            office=cls.branch_a,
            department=cls.department_a,
            position_code="EMP-A",
            position_title="Employee",
        )
        cls.employee_b_position = Position.objects.create(
            office=cls.branch_a,
            department=cls.department_a,
            position_code="EMP-B",
            position_title="Employee B",
        )
        cls.xefe_position = Position.objects.create(
            office=cls.branch_a,
            department=cls.department_a,
            position_code="XEF-A",
            position_title="Department Xefe",
            is_management=True,
        )
        cls.director_position = Position.objects.create(
            office=cls.branch_a,
            department=cls.department_a,
            position_code="DIR-A",
            position_title="Director",
            is_management=True,
        )
        cls.employee_a = cls.make_employee(
            "employee_a", "EMPLOYEEA", cls.employee_group, cls.employee_position
        )
        cls.employee_b = cls.make_employee(
            "employee_b", "EMPLOYEEB", cls.employee_group, cls.employee_b_position
        )
        # Emulates the reviewed snapshot: an Employess account holding
        # change_employeerequest. That permission must never authorize
        # editing another employee's request.
        cls.employee_a.user_permissions.add(change_employeerequest)

        cls.xefe = cls.make_employee("xefe", "XEFE", cls.xefe_group, cls.xefe_position)
        cls.director = cls.make_employee(
            "director", "DIRECTOR", cls.director_group, cls.director_position
        )
        cls.staff_hr = cls.make_employee(
            "staffhr", "STAFFHR", cls.staff_hr_group, cls.employee_position
        )

        cls.leave_type = LeaveType.objects.create(
            code="AL", name="Annual Leave", category=LeaveType.Category.ANNUAL
        )

    def make_request(self):
        assignment = get_linked_employee(self.employee_a).assignments.get()
        request_record = EmployeeRequest.objects.create(
            request_number=f"REQ-{EmployeeRequest.objects.count() + 1}",
            employee_assignment=assignment,
            branch=self.branch_a,
            submitted_by=self.employee_a,
            request_type=EmployeeRequest.RequestType.LEAVE,
            leave_type=self.leave_type,
            start_date=date.today(),
            end_date=date.today(),
            reason="Family leave",
        )
        RequestApproval.objects.create(
            request=request_record, approver=self.xefe, step_number=1,
        )
        return request_record

    def forward(self, request_record):
        approval = request_record.approval_steps.get(step_number=1)
        self.client.force_login(self.xefe)
        response = self.client.post(
            reverse("employee_requests:approval-forward", args=[approval.pk]),
            {"decision_note": " "},
        )
        self.assertEqual(response.status_code, 302)
        request_record.refresh_from_db()
        return request_record.approval_steps.get(step_number=2)
    def edit_data(self, **override):
        data = {
            "employee_assignment": get_linked_employee(self.employee_a).assignments.get().pk,
            "request_type": EmployeeRequest.RequestType.LEAVE,
            "duration_type": EmployeeRequest.DurationType.FULL_DAY,
            "leave_type": self.leave_type.pk,
            "start_date": date.today(),
            "end_date": date.today(),
            "reason": "Family leave",
            "request_category": "leave",
        }
        data.update(override)
        return data

    def test_foreign_employee_cannot_get_or_takeover_another_employee_request(self):
        request_record = self.make_request()
        self.client.force_login(self.employee_b)
        edit_url = reverse("employee_requests:request-update", args=[request_record.pk])

        self.assertEqual(self.client.get(edit_url).status_code, 404)

        assignment_b = get_linked_employee(self.employee_b).assignments.get()
        response = self.client.post(
            edit_url,
            self.edit_data(
                employee_assignment=assignment_b.pk,
                reason="Takeover attempt by employee_b",
            ),
        )
        self.assertEqual(response.status_code, 404)

        request_record.refresh_from_db()
        self.assertEqual(
            request_record.employee_assignment_id,
            get_linked_employee(self.employee_a).assignments.get().pk,
        )
        self.assertEqual(request_record.branch_id, self.branch_a.pk)
        self.assertEqual(request_record.reason, "Family leave")
        self.assertEqual(request_record.submitted_by_id, self.employee_a.pk)

    def test_owner_can_edit_own_pending_request_before_review(self):
        request_record = self.make_request()
        self.client.force_login(self.employee_a)
        edit_url = reverse("employee_requests:request-update", args=[request_record.pk])

        self.assertEqual(self.client.get(edit_url).status_code, 200)

        assignment_a = get_linked_employee(self.employee_a).assignments.get()
        response = self.client.post(
            edit_url,
            self.edit_data(reason="Family leave (updated)"),
        )
        self.assertEqual(response.status_code, 302)
        request_record.refresh_from_db()
        self.assertEqual(request_record.reason, "Family leave (updated)")
        self.assertEqual(request_record.employee_assignment_id, assignment_a.pk)
        self.assertEqual(request_record.branch_id, self.branch_a.pk)
        self.assertEqual(request_record.status, EmployeeRequest.Status.PENDING)
        self.assertIsNone(request_record.forwarded_at)
        self.assertTrue(
            request_record.approval_steps.filter(
                step_number=1,
                approver=self.xefe,
                decision=RequestApproval.Decision.PENDING,
            ).exists()
        )

    def test_post_forward_edits_are_blocked(self):
        request_record = self.make_request()
        self.forward(request_record)
        request_record.refresh_from_db()
        self.assertEqual(request_record.status, EmployeeRequest.Status.UNDER_REVIEW)
        self.assertIsNotNone(request_record.forwarded_at)

        self.client.force_login(self.employee_a)
        edit_url = reverse("employee_requests:request-update", args=[request_record.pk])
        self.assertEqual(self.client.get(edit_url).status_code, 404)

        response = self.client.post(
            edit_url,
            self.edit_data(reason="Attempted post-forward edit"),
        )
        self.assertEqual(response.status_code, 404)

        request_record.refresh_from_db()
        self.assertEqual(request_record.status, EmployeeRequest.Status.UNDER_REVIEW)
        self.assertEqual(request_record.reason, "Family leave")
        self.assertEqual(
            request_record.employee_assignment_id,
            get_linked_employee(self.employee_a).assignments.get().pk,
        )
        self.assertEqual(request_record.branch_id, self.branch_a.pk)

    def test_staff_hr_processing_permission_does_not_grant_foreign_editing(self):
        request_record = self.make_request()
        self.assertTrue(self.staff_hr.has_perm("employee_requests.change_employeerequest"))

        self.client.force_login(self.staff_hr)
        edit_url = reverse("employee_requests:request-update", args=[request_record.pk])

        self.assertEqual(self.client.get(edit_url).status_code, 404)
        response = self.client.post(
            edit_url,
            self.edit_data(reason="Staff HR unauthorized edit"),
        )
        self.assertEqual(response.status_code, 404)

        request_record.refresh_from_db()
        self.assertEqual(request_record.reason, "Family leave")
        self.assertEqual(request_record.submitted_by_id, self.employee_a.pk)
        self.assertEqual(request_record.status, EmployeeRequest.Status.PENDING)

    def test_rejection_leaves_prior_approval_records_unchanged(self):
        request_record = self.make_request()
        director_approval = self.forward(request_record)
        step_one = request_record.approval_steps.get(step_number=1)
        self.assertEqual(step_one.decision, RequestApproval.Decision.RECOMMENDED)

        self.client.force_login(self.director)
        response = self.client.post(
            reverse(
                "employee_requests:approval-decision",
                args=[director_approval.pk, "rejected"],
            ),
            {"decision_note": "Needs more information."},
        )
        self.assertEqual(response.status_code, 302)

        request_record.refresh_from_db()
        step_one.refresh_from_db()
        director_approval.refresh_from_db()
        self.assertEqual(request_record.status, EmployeeRequest.Status.REJECTED)
        # The prior Xefe recommendation is not invalidated or altered by the rejection.
        self.assertEqual(step_one.decision, RequestApproval.Decision.RECOMMENDED)
        self.assertEqual(director_approval.decision, RequestApproval.Decision.REJECTED)
        self.assertEqual(request_record.approval_steps.count(), 2)

