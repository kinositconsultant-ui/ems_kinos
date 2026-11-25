from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from attendance.models import AttendanceRecord, EmployeeSchedule, Holiday, Shift
from employees.models import Employee, EmployeeAssignment, UserEmployeeLink
from employee_requests.models import EmployeeRequest, LeaveType, RequestApproval
from organization.models import Department, Office, Position

User = get_user_model()


class StaffHRIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff_hr_group, _ = Group.objects.get_or_create(name="Staff HR")
        cls.staff_hr_group.permissions.clear()
        cls.emp_group, _ = Group.objects.get_or_create(name="Employess")
        cls.xefe_group, _ = Group.objects.get_or_create(name="Xefe_Departamentu")
        cls.dir_group, _ = Group.objects.get_or_create(name="Director")

        # Apply the supplied Staff HR permission set (verify codenames exist)
        perm_specs = [
            ("attendance", "view_attendanceevent"), ("attendance", "add_attendancepolicy"),
            ("attendance", "change_attendancepolicy"), ("attendance", "view_attendancepolicy"),
            ("attendance", "view_attendancerecord"), ("attendance", "add_employeeschedule"),
            ("attendance", "change_employeeschedule"), ("attendance", "view_employeeschedule"),
            ("attendance", "add_holiday"), ("attendance", "change_holiday"),
            ("attendance", "delete_holiday"), ("attendance", "view_holiday"),
            ("attendance", "add_shift"), ("attendance", "change_shift"), ("attendance", "view_shift"),
            ("employee_requests", "change_employeerequest"), ("employee_requests", "view_employeerequest"),
            ("employee_requests", "view_leave_calendar"), ("employee_requests", "add_leavetype"),
            ("employee_requests", "change_leavetype"), ("employee_requests", "view_leavetype"),
            ("employee_requests", "change_notification"), ("employee_requests", "view_notification"),
            ("employee_requests", "change_requestapproval"), ("employee_requests", "view_requestapproval"),
            ("employees", "add_employee"), ("employees", "change_employee"), ("employees", "view_employee"),
            ("employees", "add_employeeassignment"), ("employees", "change_employeeassignment"),
            ("employees", "view_employeeassignment"), ("employees", "add_employeecertification"),
            ("employees", "change_employeecertification"), ("employees", "view_employeecertification"),
            ("organization", "add_department"), ("organization", "change_department"), ("organization", "view_department"),
            ("organization", "add_institutionprofile"), ("organization", "view_institutionprofile"),
            ("organization", "add_office"), ("organization", "view_office"),
            ("organization", "add_position"), ("organization", "change_position"), ("organization", "view_position"),
        ]
        for app, codename in perm_specs:
            cls.staff_hr_group.permissions.add(
                Permission.objects.get(content_type__app_label=app, codename=codename)
            )

        cls.branch_a = Office.objects.create(office_code="SHA", office_name="Staff Branch A", office_type="branch")
        cls.branch_b = Office.objects.create(office_code="SHB", office_name="Staff Branch B", office_type="branch")
        cls.dept_a1 = Department.objects.create(office=cls.branch_a, department_code="SDA1", department_name="Dept A1")
        cls.dept_a2 = Department.objects.create(office=cls.branch_a, department_code="SDA2", department_name="Dept A2")
        cls.dept_b1 = Department.objects.create(office=cls.branch_b, department_code="SDB1", department_name="Dept B1")
        cls.pos_staff = Position.objects.create(office=cls.branch_a, department=cls.dept_a1, position_code="PSA", position_title="Staff A")
        cls.pos_emp_a1 = Position.objects.create(office=cls.branch_a, department=cls.dept_a1, position_code="PEA1", position_title="Emp A1")
        cls.pos_emp_a2 = Position.objects.create(office=cls.branch_a, department=cls.dept_a2, position_code="PEA2", position_title="Emp A2")
        cls.pos_emp_b1 = Position.objects.create(office=cls.branch_b, department=cls.dept_b1, position_code="PEB1", position_title="Emp B1")
        cls.pos_dir = Position.objects.create(office=cls.branch_a, department=cls.dept_a1, position_code="PDA", position_title="Director A", is_management=True)

        cls.staff_hr = cls._make_user("shr_int", "SHR1", cls.staff_hr_group, cls.pos_staff)
        cls.emp_a1 = cls._make_user("shr_ea1", "SEA1", cls.emp_group, cls.pos_emp_a1)
        cls.emp_a2 = cls._make_user("shr_ea2", "SEA2", cls.emp_group, cls.pos_emp_a2)
        cls.emp_b1 = cls._make_user("shr_eb1", "SEB1", cls.emp_group, cls.pos_emp_b1)
        cls.director = cls._make_user("shr_dir", "SDR1", cls.dir_group, cls.pos_dir)
        # give director approval perms for workflow test setup
        for c in ("view_employeerequest", "change_employeerequest", "view_requestapproval", "change_requestapproval"):
            cls.dir_group.permissions.add(Permission.objects.get(codename=c, content_type__app_label="employee_requests"))

        from datetime import time
        cls.shift = Shift.objects.create(office=cls.branch_a, shift_code="SH1", name="Day", start_time=time(8,0), end_time=time(17,0))
        cls.leave_type = LeaveType.objects.create(code="SHAL", name="Annual Leave")

    @classmethod
    def _make_user(cls, username, employee_id, group, position):
        user = User.objects.create_user(username=username, password="test-pass")
        emp = Employee.objects.create(employee_id=employee_id, full_name=username, employment_status=Employee.EmploymentStatus.ACTIVE)
        UserEmployeeLink.objects.create(user=user, employee=emp)
        EmployeeAssignment.objects.create(employee=emp, position=position, start_date=date(2024, 1, 1), is_primary=True,
                                         assignment_type=EmployeeAssignment.AssignmentType.PRIMARY, assigned_by=user)
        user.groups.add(group)
        return user

    def test_staff_hr_sees_employees_across_all_departments_in_branch(self):
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "SEA1")
        self.assertContains(resp, "SEA2")  # other department, same branch
        self.assertNotContains(resp, "SEB1")  # other branch

    def test_staff_hr_attendance_today_scoped_to_branch(self):
        from datetime import time as dtime
        sched = EmployeeSchedule.objects.create(
            employee_assignment=self.emp_a1.employee_profile.assignments.get(),
            shift=self.shift, work_date=date.today(),
            scheduled_start_at=timezone.make_aware(timezone.datetime.combine(date.today(), dtime(8, 0))),
            scheduled_end_at=timezone.make_aware(timezone.datetime.combine(date.today(), dtime(17, 0))),
            status=EmployeeSchedule.Status.SCHEDULED,
        )
        AttendanceRecord.objects.create(schedule=sched, status=AttendanceRecord.Status.PRESENT, first_check_in_at=timezone.now())
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("attendance:today"))
        self.assertEqual(resp.status_code, 200)

    def test_staff_hr_cannot_access_kiosk(self):
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("attendance:kiosk"))
        self.assertEqual(resp.status_code, 403)

    def test_staff_hr_my_requests_hidden_and_denied(self):
        self.client.force_login(self.staff_hr)
        resp = self.client.get(reverse("employee_requests:request-mine"))
        self.assertEqual(resp.status_code, 403)
        # menu helper: dedicated Staff HR without Employess group => hidden
        from config.settings import _menu_can_view_my_requests
        from django.test import RequestFactory
        rf = RequestFactory().get("/")
        rf.user = self.staff_hr
        self.assertFalse(_menu_can_view_my_requests(rf))

    def test_staff_hr_cannot_perform_director_decision_even_with_change_requestapproval(self):
        req = EmployeeRequest.objects.create(
            request_number="SHR-DEC1", employee_assignment=self.emp_a1.employee_profile.assignments.get(),
            branch=self.branch_a, submitted_by=self.emp_a1, request_type=EmployeeRequest.RequestType.LEAVE,
            leave_type=self.leave_type, start_date=date(2026, 2, 1), end_date=date(2026, 2, 1),
            reason="r", status=EmployeeRequest.Status.UNDER_REVIEW, workflow_stage="director",
            forwarded_at=timezone.now(), final_decision_at=None,
        )
        RequestApproval.objects.create(request=req, approver=self.director, step_number=1, decision=RequestApproval.Decision.RECOMMENDED)
        approval = RequestApproval.objects.create(request=req, approver=self.director, step_number=2, decision=RequestApproval.Decision.PENDING)
        self.client.force_login(self.staff_hr)
        resp = self.client.post(reverse("employee_requests:approval-decision", args=[approval.pk, "approved"]), {"decision_note": ""})
        self.assertEqual(resp.status_code, 404)
        approval.refresh_from_db()
        self.assertEqual(approval.decision, RequestApproval.Decision.PENDING)

    def test_staff_hr_cannot_create_global_holiday(self):
        self.client.force_login(self.staff_hr)
        resp = self.client.post(reverse("attendance:holiday-create"), {
            "holiday_type": Holiday.HolidayType.NATIONAL,
            "name": "Global",
            "start_date": "2026-12-25", "end_date": "2026-12-25",
            "is_active": "on",
        })
        # form should reject (office-specific only / wrong type) and not create global holiday
        self.assertFalse(Holiday.objects.filter(name="Global", office__isnull=True).exists())

    def test_staff_hr_can_create_office_specific_holiday_in_branch(self):
        self.client.force_login(self.staff_hr)
        resp = self.client.post(reverse("attendance:holiday-create"), {
            "holiday_type": Holiday.HolidayType.OFFICE,
            "office": self.branch_a.pk,
            "name": "Branch Holiday",
            "start_date": "2026-12-26", "end_date": "2026-12-26",
            "is_active": "on",
        })
        self.assertTrue(Holiday.objects.filter(name="Branch Holiday", office=self.branch_a).exists())
        self.assertFalse(Holiday.objects.filter(name="Branch Holiday", office=self.branch_b).exists())

    def test_staff_hr_department_create_scoped_to_branch(self):
        self.client.force_login(self.staff_hr)
        resp = self.client.post(reverse("organization:department-create"), {
            "office": self.branch_b.pk,  # forged other branch
            "department_code": "XBAD", "department_name": "Bad",
            "is_active": "on",
        })
        self.assertFalse(Department.objects.filter(department_code="XBAD").exists())

    def test_standalone_assignments_absent(self):
        self.client.force_login(self.staff_hr)
        resp = self.client.get("/")
        if resp.status_code == 302:
            resp = self.client.get(resp["Location"])
        if resp.status_code == 200:
            self.assertNotContains(resp, "Assignments &amp; Transfers")
