from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee, EmployeeAssignment, UserEmployeeLink
from organization.models import Department, Office, Position

from .models import EmployeeRequest, LeaveType, Notification, RequestApproval


User = get_user_model()


class LeaveCalendarTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cal_perm = Permission.objects.get(
            codename="view_leave_calendar", content_type__app_label="employee_requests"
        )
        cls.view_req_perm = Permission.objects.get(
            codename="view_employeerequest", content_type__app_label="employee_requests"
        )
        cls.employee_group, _ = Group.objects.get_or_create(name="Employess")
        cls.employee_group.permissions.clear()
        cls.employee_group.permissions.clear()
        cls.xefe_group, _ = Group.objects.get_or_create(name="Xefe_Departamentu")
        cls.xefe_group.permissions.clear()
        cls.xefe_group.permissions.clear()
        cls.director_group, _ = Group.objects.get_or_create(name="Director")
        cls.director_group.permissions.clear()
        cls.director_group.permissions.clear()
        cls.staff_hr_group, _ = Group.objects.get_or_create(name="Staff HR")
        cls.staff_hr_group.permissions.clear()
        cls.staff_hr_group.permissions.clear()
        for g in (cls.xefe_group, cls.director_group, cls.staff_hr_group):
            for c in ("view_employeerequest", "change_employeerequest", "view_requestapproval"):
                g.permissions.add(Permission.objects.get(codename=c, content_type__app_label="employee_requests"))

        cls.branch_a = Office.objects.create(office_code="CA", office_name="Cal Branch A", office_type="branch")
        cls.branch_b = Office.objects.create(office_code="CB", office_name="Cal Branch B", office_type="branch")
        cls.dept_a1 = Department.objects.create(office=cls.branch_a, department_code="DA1", department_name="Dept A1")
        cls.dept_a2 = Department.objects.create(office=cls.branch_a, department_code="DA2", department_name="Dept A2")
        cls.dept_b1 = Department.objects.create(office=cls.branch_b, department_code="DB1", department_name="Dept B1")
        cls.pos_emp_a1 = Position.objects.create(office=cls.branch_a, department=cls.dept_a1, position_code="CEA1", position_title="Emp A1")
        cls.pos_emp_a2 = Position.objects.create(office=cls.branch_a, department=cls.dept_a2, position_code="CEA2", position_title="Emp A2")
        cls.pos_emp_b1 = Position.objects.create(office=cls.branch_b, department=cls.dept_b1, position_code="CEB1", position_title="Emp B1")
        cls.pos_xefe = Position.objects.create(office=cls.branch_a, department=cls.dept_a1, position_code="CXA1", position_title="Xefe A1", is_management=True)
        cls.pos_dir = Position.objects.create(office=cls.branch_a, department=cls.dept_a1, position_code="CDRA", position_title="Director A", is_management=True)
        cls.pos_staff = Position.objects.create(office=cls.branch_a, department=cls.dept_a1, position_code="CSA1", position_title="Staff A")
        cls.pos_dir_b = Position.objects.create(office=cls.branch_b, department=cls.dept_b1, position_code="CDRB", position_title="Director B", is_management=True)

        cls.leave_sick = LeaveType.objects.create(code="SICK", name="Sick Leave", category=LeaveType.Category.SICK)
        cls.leave_annual = LeaveType.objects.create(code="ANN", name="Annual Leave", category=LeaveType.Category.ANNUAL)

        cls.emp_a1 = cls._make_user("c_emp_a1", "CA11", cls.employee_group, cls.pos_emp_a1)
        cls.emp_a2 = cls._make_user("c_emp_a2", "CA21", cls.employee_group, cls.pos_emp_a2)
        cls.emp_b1 = cls._make_user("c_emp_b1", "CB11", cls.employee_group, cls.pos_emp_b1)
        cls.xefe = cls._make_user("c_xefe", "CXA1", cls.xefe_group, cls.pos_xefe)
        cls.director = cls._make_user("c_dir", "CDRA", cls.director_group, cls.pos_dir)
        cls.director_b = cls._make_user("c_dirb", "CDRB", cls.director_group, cls.pos_dir_b)
        cls.staff_hr = cls._make_user("c_staff", "CSA1", cls.staff_hr_group, cls.pos_staff)

        for u in (cls.emp_a1, cls.emp_a2, cls.emp_b1, cls.xefe, cls.director, cls.director_b, cls.staff_hr):
            u.user_permissions.add(cls.cal_perm)

    @classmethod
    def _make_user(cls, username, employee_id, group, position):
        user = User.objects.create_user(username=username, password="test-pass")
        emp = Employee.objects.create(employee_id=employee_id, full_name=username.replace("_", " ").title(),
                                     employment_status=Employee.EmploymentStatus.ACTIVE)
        UserEmployeeLink.objects.create(user=user, employee=emp)
        EmployeeAssignment.objects.create(employee=emp, position=position, start_date=date(2024, 1, 1), is_primary=True,
                                         assignment_type=EmployeeAssignment.AssignmentType.PRIMARY, assigned_by=user)
        user.groups.add(group)
        return user

    def _leave(self, submitter, branch, *, status, processed, number, start, end, leave_type=None,
               duration=EmployeeRequest.DurationType.FULL_DAY, workflow_stage="director", request_type=None):
        assignment = submitter.employee_profile.assignments.get()
        req = EmployeeRequest.objects.create(
            request_number=number, employee_assignment=assignment, branch=branch, submitted_by=submitter,
            request_type=request_type or EmployeeRequest.RequestType.LEAVE,
            leave_type=leave_type if (request_type or EmployeeRequest.RequestType.LEAVE) == EmployeeRequest.RequestType.LEAVE else None,
            start_date=start, end_date=end, reason="SENSITIVE-REASON-DO-NOT-LEAK",
            duration_type=duration, status=status, workflow_stage=workflow_stage,
            final_decision_at=timezone.make_aware(timezone.datetime(2026, 1, 2)) if status == EmployeeRequest.Status.APPROVED else None,
        )
        if processed and status == EmployeeRequest.Status.APPROVED:
            req.schedule_processed_at = timezone.now()
            req.schedule_processed_by = self.staff_hr
            req.save(update_fields=["schedule_processed_at", "schedule_processed_by"])
        return req

    def test_only_approved_processed_leave_displayed(self):
        self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="CAL-OK", start=date(2026, 6, 10), end=date(2026, 6, 10), leave_type=self.leave_sick)
        self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=False, number="CAL-UNP", start=date(2026, 6, 11), end=date(2026, 6, 11), leave_type=self.leave_sick)
        self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.PENDING, processed=False, number="CAL-PEND", start=date(2026, 6, 12), end=date(2026, 6, 12), leave_type=self.leave_sick)
        self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.REJECTED, processed=False, number="CAL-REJ", start=date(2026, 6, 13), end=date(2026, 6, 13), leave_type=self.leave_sick)
        self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="CAL-NONL", start=date(2026, 6, 14), end=date(2026, 6, 14), leave_type=None, request_type=EmployeeRequest.RequestType.BUSINESS_TRIP)
        self.client.force_login(self.director)
        resp = self.client.get(reverse("employee_requests:leave-calendar"), {"year": 2026, "month": 6})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        self.assertNotContains(resp, "SENSITIVE-REASON-DO-NOT-LEAK")

    def test_all_leave_types_eligible_without_private_type_labels(self):
        self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="CT1", start=date(2026, 7, 1), end=date(2026, 7, 1), leave_type=self.leave_sick)
        self._leave(self.emp_a2, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="CT2", start=date(2026, 7, 2), end=date(2026, 7, 2), leave_type=self.leave_annual)
        self.client.force_login(self.director)
        resp = self.client.get(reverse("employee_requests:leave-calendar"), {"year": 2026, "month": 7})
        self.assertEqual(resp.context["total_count"], 2)
        self.assertContains(resp, "Leave")
        self.assertNotContains(resp, "Sick Leave")
        self.assertNotContains(resp, "Annual Leave")
        j = self.client.get(reverse("employee_requests:leave-calendar-events"), {"year": 2026, "month": 7})
        self.assertEqual(j.status_code, 200)
        data = j.json()["events"]
        self.assertTrue(all(e["label"] == "Leave" for e in data))
        self.assertTrue(all("Sick" not in str(e.values()) and "Annual" not in str(e.values()) for e in data))

    def test_employee_scope_own_branch_department(self):
        self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="S1", start=date(2026, 8, 1), end=date(2026, 8, 1), leave_type=self.leave_annual)
        self._leave(self.emp_a2, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="S2", start=date(2026, 8, 2), end=date(2026, 8, 2), leave_type=self.leave_annual)
        self._leave(self.emp_b1, self.branch_b, status=EmployeeRequest.Status.APPROVED, processed=True, number="S3", start=date(2026, 8, 3), end=date(2026, 8, 3), leave_type=self.leave_annual)
        self.client.force_login(self.emp_a1)
        resp = self.client.get(reverse("employee_requests:leave-calendar"), {"year": 2026, "month": 8})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        self.assertContains(resp, "C Emp A1")
        self.assertNotContains(resp, "C Emp A2")
        self.assertNotContains(resp, "C Emp B1")

    def test_xefe_scope_led_department(self):
        self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="X1", start=date(2026, 8, 5), end=date(2026, 8, 5), leave_type=self.leave_annual)
        self._leave(self.emp_a2, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="X2", start=date(2026, 8, 6), end=date(2026, 8, 6), leave_type=self.leave_annual)
        self.client.force_login(self.xefe)
        resp = self.client.get(reverse("employee_requests:leave-calendar"), {"year": 2026, "month": 8})
        self.assertEqual(resp.context["total_count"], 1)
        self.assertContains(resp, "C Emp A1")
        self.assertNotContains(resp, "C Emp A2")

    def test_staff_hr_director_see_all_departments_in_authorized_branch(self):
        self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="B1", start=date(2026, 9, 1), end=date(2026, 9, 1), leave_type=self.leave_annual)
        self._leave(self.emp_a2, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="B2", start=date(2026, 9, 2), end=date(2026, 9, 2), leave_type=self.leave_annual)
        self._leave(self.emp_b1, self.branch_b, status=EmployeeRequest.Status.APPROVED, processed=True, number="B3", start=date(2026, 9, 3), end=date(2026, 9, 3), leave_type=self.leave_annual)
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("employee_requests:leave-calendar"), {"year": 2026, "month": 9})
        self.assertEqual(resp.context["total_count"], 2)
        self.assertNotContains(resp, "C Emp B1")
        self.client.force_login(self.director)
        resp = self.client.get(reverse("employee_requests:leave-calendar"), {"year": 2026, "month": 9})
        self.assertEqual(resp.context["total_count"], 2)
        self.assertNotContains(resp, "C Emp B1")

    def test_missing_calendar_permission_denies_access(self):
        u = User.objects.create_user("no-cal", password="test-pass")
        self.client.force_login(u)
        self.assertEqual(self.client.get(reverse("employee_requests:leave-calendar")).status_code, 403)
        self.assertEqual(self.client.get(reverse("employee_requests:leave-calendar-events")).status_code, 403)
        self.assertEqual(self.client.get(reverse("employee_requests:leave-calendar-event-detail", args=[1])).status_code, 403)

    def test_calendar_permission_alone_no_private_request_detail_access(self):
        req = self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="PRIV1", start=date(2026, 10, 1), end=date(2026, 10, 1), leave_type=self.leave_sick)
        u = User.objects.create_user("calonly", password="test-pass")
        u.user_permissions.add(self.cal_perm)
        self.client.force_login(u)
        resp = self.client.get(reverse("employee_requests:request-detail", args=[req.pk]))
        self.assertIn(resp.status_code, (302, 403, 404))

    def test_missing_or_ambiguous_scope_fails_closed(self):
        u = User.objects.create_user("noscope", password="test-pass")
        u.user_permissions.add(self.cal_perm)
        self.client.force_login(u)
        resp = self.client.get(reverse("employee_requests:leave-calendar"))
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "authorized leave-calendar scope", status_code=403)

    def test_forged_filters_cannot_leak_records(self):
        self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="F1", start=date(2026, 11, 1), end=date(2026, 11, 1), leave_type=self.leave_annual)
        self._leave(self.emp_b1, self.branch_b, status=EmployeeRequest.Status.APPROVED, processed=True, number="F2", start=date(2026, 11, 2), end=date(2026, 11, 2), leave_type=self.leave_annual)
        self.client.force_login(self.director)
        for params in [{"branch": self.branch_b.pk, "year": 2026, "month": 11}, {"department": self.dept_b1.pk, "year": 2026, "month": 11}, {"employee": self.emp_b1.employee_profile.pk, "year": 2026, "month": 11}]:
            resp = self.client.get(reverse("employee_requests:leave-calendar"), params)
            self.assertNotContains(resp, "C Emp B1")
            j = self.client.get(reverse("employee_requests:leave-calendar-events"), params)
            for ev in j.json().get("events", []):
                self.assertNotIn("C Emp B1", ev["employee_name"])

    def test_privacy_no_sensitive_fields_in_html_or_json(self):
        req = self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="SEC1", start=date(2026, 12, 1), end=date(2026, 12, 1), leave_type=self.leave_sick)
        self.client.force_login(self.director)
        resp = self.client.get(reverse("employee_requests:leave-calendar"), {"year": 2026, "month": 12})
        self.assertNotContains(resp, "SENSITIVE-REASON-DO-NOT-LEAK")
        self.assertNotContains(resp, "Sick Leave")
        self.assertNotContains(resp, "request-detail")
        j = self.client.get(reverse("employee_requests:leave-calendar-events"), {"year": 2026, "month": 12}).json()
        s = str(j)
        self.assertNotIn("SENSITIVE-REASON-DO-NOT-LEAK", s)
        self.assertNotIn("Sick Leave", s)
        self.assertNotIn("attachment", s.lower())
        self.assertTrue(all(set(ev.keys()) <= {"id","employee_name","department","label","start_date","end_date","duration","branch","status"} for ev in j["events"]))
        d = self.client.get(reverse("employee_requests:leave-calendar-event-detail", args=[req.pk])).json()
        self.assertNotIn("SENSITIVE-REASON-DO-NOT-LEAK", str(d))

    def test_duration_labels_no_fabrication(self):
        self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="D1", start=date(2027, 1, 5), end=date(2027, 1, 5), leave_type=self.leave_annual, duration=EmployeeRequest.DurationType.FULL_DAY)
        self._leave(self.emp_a2, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="D2", start=date(2027, 1, 6), end=date(2027, 1, 6), leave_type=self.leave_annual, duration=EmployeeRequest.DurationType.HALF_DAY)
        self.client.force_login(self.director)
        resp = self.client.get(reverse("employee_requests:leave-calendar"), {"year": 2027, "month": 1, "view": "list"})
        self.assertContains(resp, "Full day")
        self.assertContains(resp, "Half day (recorded)")
        content = resp.content.decode()
        self.assertNotRegex(content, r"\b(AM|PM)\b(?=.*recorded|.*Full day|.*Half day)")
        self.assertNotIn("Half day (recorded) AM", content)
        self.assertNotIn("Half day (recorded) PM", content)

    def test_cross_month_overlap_and_year_boundary(self):
        self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="SPAN", start=date(2026, 12, 30), end=date(2027, 1, 2), leave_type=self.leave_annual)
        self.client.force_login(self.director)
        dec = self.client.get(reverse("employee_requests:leave-calendar-events"), {"year": 2026, "month": 12}).json()["events"]
        jan = self.client.get(reverse("employee_requests:leave-calendar-events"), {"year": 2027, "month": 1}).json()["events"]
        self.assertTrue(any(e["start_date"] == "2026-12-30" for e in dec))
        self.assertTrue(any(e["start_date"] == "2026-12-30" for e in jan))

    def test_legacy_processed_leave_displays_without_rewriting(self):
        req = self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="LEG-CAL", start=date(2027, 2, 3), end=date(2027, 2, 3), leave_type=self.leave_annual, workflow_stage="xefe_review")
        before = (req.status, req.workflow_stage, req.schedule_processed_at, RequestApproval.objects.count())
        self.client.force_login(self.director)
        self.client.get(reverse("employee_requests:leave-calendar"), {"year": 2027, "month": 2})
        req.refresh_from_db()
        self.assertEqual(before, (req.status, req.workflow_stage, req.schedule_processed_at, RequestApproval.objects.count()))

    def test_get_requests_do_not_modify_business_records(self):
        req = self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="MUT0", start=date(2027, 3, 4), end=date(2027, 3, 4), leave_type=self.leave_annual)
        n0 = Notification.objects.count()
        proc = req.schedule_processed_at
        self.client.force_login(self.director)
        self.client.get(reverse("employee_requests:leave-calendar"), {"year": 2027, "month": 3})
        self.client.get(reverse("employee_requests:leave-calendar-events"), {"year": 2027, "month": 3})
        self.client.get(reverse("employee_requests:leave-calendar-event-detail", args=[req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.schedule_processed_at, proc)
        self.assertEqual(Notification.objects.count(), n0)

    def test_sidebar_uses_calendar_permission_and_assignments_still_absent(self):
        self.client.force_login(self.staff_hr)
        resp = self.client.get("/")
        if resp.status_code == 302:
            resp = self.client.get(resp["Location"])
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Leave Calendar")
        self.assertNotContains(resp, "Assignments &amp; Transfers")
        u = User.objects.create_user("nocalnav", password="test-pass")
        self.client.force_login(u)
        resp = self.client.get("/")
        if resp.status_code == 302:
            resp = self.client.get(resp["Location"])
        if resp.status_code == 200:
            self.assertNotContains(resp, "leave-calendar")

    def test_malformed_params_do_not_500(self):
        self._leave(self.emp_a1, self.branch_a, status=EmployeeRequest.Status.APPROVED, processed=True, number="MAL1", start=date(2027, 4, 1), end=date(2027, 4, 1), leave_type=self.leave_annual)
        self.client.force_login(self.director)
        for params in [{"year": "x", "month": "y"}, {"department": "abc", "year": 2027, "month": 4}, {"month": 13, "year": 2027}, {"year": 99999, "month": 4}]:
            self.assertEqual(self.client.get(reverse("employee_requests:leave-calendar"), params).status_code, 200)
            self.assertEqual(self.client.get(reverse("employee_requests:leave-calendar-events"), params).status_code, 200)
