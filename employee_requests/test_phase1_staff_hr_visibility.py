from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from attendance.models import EmployeeSchedule, Shift
from employees.models import Employee, EmployeeAssignment
from organization.models import Department, Office, Position

from .models import EmployeeRequest, LeaveType, RequestApproval


User = get_user_model()


class StaffHRVisibilityPhase1Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.employee_group, _ = Group.objects.get_or_create(name="Employess")
        cls.employee_group.permissions.clear()
        cls.xefe_group, _ = Group.objects.get_or_create(name="Xefe_Departamentu")
        cls.xefe_group.permissions.clear()
        cls.director_group, _ = Group.objects.get_or_create(name="Director")
        cls.director_group.permissions.clear()
        cls.staff_hr_group, _ = Group.objects.get_or_create(name="Staff HR")
        cls.staff_hr_group.permissions.clear()

        # Approval capability (Xefe/Director) vs processing workspace (Staff HR)
        for codename in ("view_employeerequest", "change_employeerequest", "view_requestapproval"):
            perm = Permission.objects.get(
                codename=codename, content_type__app_label="employee_requests"
            )
            cls.staff_hr_group.permissions.add(perm)
        for codename in ("view_employeerequest", "add_employeerequest", "change_employeerequest",
                         "view_requestapproval", "change_requestapproval"):
            perm = Permission.objects.get(
                codename=codename, content_type__app_label="employee_requests"
            )
            cls.xefe_group.permissions.add(perm)
            cls.director_group.permissions.add(perm)
            cls.employee_group.permissions.add(perm)

        cls.branch_a = Office.objects.create(office_code="BA", office_name="Branch A", office_type="branch")
        cls.branch_b = Office.objects.create(office_code="BB", office_name="Branch B", office_type="branch")
        cls.dept_a = Department.objects.create(office=cls.branch_a, department_code="DA", department_name="Dept A")
        cls.dept_b = Department.objects.create(office=cls.branch_b, department_code="DB", department_name="Dept B")
        cls.pos_emp_a = Position.objects.create(
            office=cls.branch_a, department=cls.dept_a, position_code="EA", position_title="Employee A"
        )
        cls.pos_emp_b = Position.objects.create(
            office=cls.branch_b, department=cls.dept_b, position_code="EB", position_title="Employee B"
        )
        cls.pos_xefe = Position.objects.create(
            office=cls.branch_a, department=cls.dept_a, position_code="XA",
            position_title="Xefe A", is_management=True,
        )
        cls.pos_dir = Position.objects.create(
            office=cls.branch_a, department=cls.dept_a, position_code="DRA",
            position_title="Director A", is_management=True,
        )
        cls.pos_staff = Position.objects.create(
            office=cls.branch_a, department=cls.dept_a, position_code="SA", position_title="Staff A"
        )
        cls.pos_dir_b = Position.objects.create(
            office=cls.branch_b, department=cls.dept_b, position_code="DRB",
            position_title="Director B", is_management=True,
        )

        cls.employee_a = cls._make_user("emp_a", "EA1", cls.employee_group, cls.pos_emp_a)
        cls.employee_b = cls._make_user("emp_b", "EB1", cls.employee_group, cls.pos_emp_b)
        cls.xefe = cls._make_user("xefe_a", "XA1", cls.xefe_group, cls.pos_xefe)
        cls.director = cls._make_user("dir_a", "DRA1", cls.director_group, cls.pos_dir)
        cls.director_b = cls._make_user("dir_b", "DRB1", cls.director_group, cls.pos_dir_b)
        cls.staff_hr = cls._make_user("staff_a", "SA1", cls.staff_hr_group, cls.pos_staff)

        cls.leave_type = LeaveType.objects.create(code="AL1", name="Annual Leave")

    @classmethod
    def _make_user(cls, username, employee_id, group, position):
        user = User.objects.create_user(username=username, password="test-pass")
        emp = Employee.objects.create(
            user=user, employee_id=employee_id, full_name=username.title(),
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        EmployeeAssignment.objects.create(
            employee=emp, position=position, start_date=date(2024, 1, 1),
            is_primary=True, assignment_type=EmployeeAssignment.AssignmentType.PRIMARY,
            assigned_by=user,
        )
        user.groups.add(group)
        return user

    def _make_request(self, submitter, branch, *, status, processed=False, number, workflow_stage="director"):
        assignment = submitter.employee_profile.assignments.get()
        req = EmployeeRequest.objects.create(
            request_number=number,
            employee_assignment=assignment,
            branch=branch,
            submitted_by=submitter,
            request_type=EmployeeRequest.RequestType.LEAVE,
            leave_type=self.leave_type,
            start_date=date(2026, 1, 10),
            end_date=date(2026, 1, 10),
            reason="Test",
            status=status,
            workflow_stage=workflow_stage,
            final_decision_at=timezone.make_aware(timezone.datetime(2026, 1, 5)) if status == EmployeeRequest.Status.APPROVED else None,
        )
        if processed and status == EmployeeRequest.Status.APPROVED:
            req.schedule_processed_at = timezone.now()
            req.schedule_processed_by = self.staff_hr
            req.save(update_fields=["schedule_processed_at", "schedule_processed_by"])
        return req

    def test_all_requests_shows_processed_and_unprocessed_approved_in_branch(self):
        waiting = self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=False, number="P1-WAIT")
        done = self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="P1-DONE")
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("employee_requests:request-all"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "P1-WAIT")
        self.assertContains(resp, "P1-DONE")
        self.assertContains(resp, "Processing")
        self.assertContains(resp, "processing")  # filter control id/name present

    def test_waiting_filter_shows_only_unprocessed(self):
        self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=False, number="P2-WAIT")
        self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="P2-DONE")
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("employee_requests:request-all"), {"processing": "waiting"})
        self.assertContains(resp, "P2-WAIT")
        self.assertNotContains(resp, "P2-DONE")

    def test_processed_filter_shows_only_processed(self):
        self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=False, number="P3-WAIT")
        self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="P3-DONE")
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("employee_requests:request-all"), {"processing": "processed"})
        self.assertNotContains(resp, "P3-WAIT")
        self.assertContains(resp, "P3-DONE")

    def test_processed_detail_accessible_no_active_processing_ui_action(self):
        done = self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="P4-DONE")
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("employee_requests:request-detail", args=[done.pk]))
        self.assertEqual(resp.status_code, 200)
        # request detail shows process form/button only when can_process_schedule; processed => false
        self.assertNotContains(resp, "Review schedule processing")
        self.assertNotContains(resp, "Confirm schedule processing")
        # GET/POST duplicate protection still enforced by existing endpoint (not re-tested mutatively beyond scope)

    def test_pending_rejected_excluded_from_staff_hr_lists(self):
        self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.PENDING, number="P5-PEND")
        self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.REJECTED, number="P5-REJ")
        self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.UNDER_REVIEW, number="P5-REV")
        self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=False, number="P5-OK")
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("employee_requests:request-all"))
        self.assertNotContains(resp, "P5-PEND")
        self.assertNotContains(resp, "P5-REJ")
        self.assertNotContains(resp, "P5-REV")
        self.assertContains(resp, "P5-OK")

    def test_other_branch_excluded_from_lists_counts_and_detail(self):
        mine = self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=False, number="P6-A")
        other = self._make_request(self.employee_b, self.branch_b, status=EmployeeRequest.Status.APPROVED, processed=False, number="P6-B")
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("employee_requests:request-all"))
        self.assertContains(resp, "P6-A")
        self.assertNotContains(resp, "P6-B")
        # forged office filter cannot expand beyond authorized branch
        # Office filter is not exposed for Staff HR (branch scoped); forged param must not expand access
        resp = self.client.get(reverse("employee_requests:request-all"), {"employee_assignment__position__office_id": self.branch_b.pk})
        self.assertContains(resp, "P6-A")
        self.assertNotContains(resp, "P6-B")
        # schedule processing workspace also scoped
        resp = self.client.get(reverse("employee_requests:schedule-processing-waiting"))
        self.assertContains(resp, "P6-A")
        self.assertNotContains(resp, "P6-B")
        self.assertEqual(self.client.get(reverse("employee_requests:request-detail", args=[other.pk])).status_code, 404)

    def test_forged_branch_param_cannot_expand_scope(self):
        self._make_request(self.employee_b, self.branch_b, status=EmployeeRequest.Status.APPROVED, processed=False, number="P7-B")
        self.client.force_login(self.staff_hr)
        for params in ({"branch": self.branch_b.pk}, {"employee_assignment__position__office_id": self.branch_b.pk}, {"office_id": self.branch_b.pk}, {"status": "approved"}, {"processing": "all", "employee_assignment__position__office_id": self.branch_b.pk}):
            resp = self.client.get(reverse("employee_requests:request-all"), params)
            self.assertNotContains(resp, "P7-B")
            resp = self.client.get(reverse("employee_requests:schedule-processing-waiting"), params)
            self.assertNotContains(resp, "P7-B")

    def test_missing_or_ambiguous_staff_hr_assignment_fails_closed(self):
        # create a temporary Staff HR user with no primary assignment -> resolver fails closed
        u = self._make_user("staff_na", "SNA1", self.staff_hr_group, self.pos_staff)
        EmployeeAssignment.objects.filter(employee=u.employee_profile).delete()
        self.client.force_login(u)
        resp = self.client.get(reverse("employee_requests:request-all"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "REQ-")
        self.assertContains(resp, "No requests found")
        resp = self.client.get(reverse("employee_requests:schedule-processing-waiting"))
        # no eligible processing scope => permission denied for workspace
        self.assertIn(resp.status_code, (302, 403))

    def test_processing_workspace_requires_permissions_and_not_change_requestapproval(self):
        # Staff HR without view_employeerequest loses All Requests (mixin) / workspace
        u = self._make_user("staff_np", "SNP1", self.staff_hr_group, self.pos_staff)
        # remove processing workspace perms but keep group/assignment
        u.user_permissions.clear()
        u.groups.clear()
        g = Group.objects.create(name="Staff HR NP")
        g.permissions.add(Permission.objects.get(codename="view_requestapproval", content_type__app_label="employee_requests"))
        u.groups.add(g)
        self.client.force_login(u)
        self.assertEqual(self.client.get(reverse("employee_requests:request-all")).status_code, 403)
        self.assertEqual(self.client.get(reverse("employee_requests:schedule-processing-waiting")).status_code, 403)
        # our staff_hr fixture has no change_requestapproval needed? ensure they can access workspace
        self.assertFalse(self.staff_hr.has_perm("employee_requests.change_requestapproval"))
        self.client.force_login(self.staff_hr)
        self.assertEqual(self.client.get(reverse("employee_requests:schedule-processing-waiting")).status_code, 200)

    def test_staff_hr_cannot_approve_reject_director_decisions(self):
        # create a real director approval task in branch A
        req = self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.UNDER_REVIEW, number="P10", workflow_stage="director")
        req.forwarded_at = date(2026, 1, 4)
        req.save(update_fields=["forwarded_at"])
        RequestApproval.objects.create(request=req, approver=self.xefe, step_number=1, decision=RequestApproval.Decision.RECOMMENDED)
        approval = RequestApproval.objects.create(request=req, approver=self.director, step_number=2, decision=RequestApproval.Decision.PENDING)
        self.client.force_login(self.staff_hr)
        resp = self.client.post(
            reverse("employee_requests:approval-decision", args=[approval.pk, "approved"]),
            {"decision_note": ""},
        )
        self.assertEqual(resp.status_code, 404)  # approver scoping in view
        approval.refresh_from_db()
        self.assertEqual(approval.decision, RequestApproval.Decision.PENDING)

    def test_xefe_director_approval_inbox_behavior_unchanged(self):
        req = self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.PENDING, number="P11", workflow_stage="xefe_review")
        RequestApproval.objects.create(request=req, approver=self.xefe, step_number=1, decision=RequestApproval.Decision.PENDING)
        self.client.force_login(self.xefe)
        resp = self.client.get(reverse("employee_requests:approval-pending"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "P11")
        self.assertContains(resp, "Only approval tasks assigned to your account are shown.")
        self.assertContains(resp, "Pending Approvals")

    def test_links_use_correct_model_ids(self):
        done = self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="P14")
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("employee_requests:schedule-processing-processed"))
        detail_url = reverse("employee_requests:request-detail", args=[done.pk])
        self.assertContains(resp, detail_url)
        self.assertNotContains(resp, reverse("employee_requests:approval-detail", args=[done.pk]))
        self.assertContains(resp, "View request")

    def test_loading_pages_does_not_change_processing_approval_or_notifications(self):
        from .models import Notification
        done = self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="P15")
        n0 = Notification.objects.count()
        a0 = RequestApproval.objects.count()
        proc_at = done.schedule_processed_at
        self.client.force_login(self.staff_hr)
        self.client.get(reverse("employee_requests:request-all"))
        self.client.get(reverse("employee_requests:schedule-processing-waiting"))
        self.client.get(reverse("employee_requests:schedule-processing-processed"))
        self.client.get(reverse("employee_requests:request-detail", args=[done.pk]))
        done.refresh_from_db()
        self.assertEqual(done.schedule_processed_at, proc_at)
        self.assertEqual(RequestApproval.objects.count(), a0)
        self.assertEqual(Notification.objects.count(), n0)

    def test_processed_legacy_record_visible_without_new_approval_rows(self):
        # Legacy: approved, processed, unusual stage, no approval rows
        legacy = self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="P16-LEG", workflow_stage="xefe_review")
        a0 = RequestApproval.objects.count()
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("employee_requests:request-all"), {"processing": "processed"})
        self.assertContains(resp, "P16-LEG")
        self.assertEqual(RequestApproval.objects.count(), a0)

    def test_sidebar_renders_approval_inbox_for_processing_workspace(self):
        # actual template pipeline via django_adminlte4 context; Staff HR has workspace perms
        self.client.force_login(self.staff_hr)
        resp = self.client.get("/")
        # may redirect if dashboard different; accept 200/302 but follow
        if resp.status_code == 302:
            resp = self.client.get(resp["Location"])
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Approval Inbox")
        self.assertNotContains(resp, "Assignments &amp; Transfers")

    def test_empty_waiting_state_has_guidance_and_processed_link(self):
        self._make_request(self.employee_a, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="P-E1")
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("employee_requests:schedule-processing-waiting"))
        self.assertContains(resp, "No requests await processing")
        self.assertContains(resp, reverse("employee_requests:schedule-processing-processed"))
