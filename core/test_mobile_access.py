"""Server-side authorization tests for the Employee mobile (Android APK) area.

These tests use isolated test data (Django TestCase transactions) and never
touch the development database. They verify:

- An eligible Employee-role account with a valid UserEmployeeLink can reach the
  mobile dashboard.
- Anonymous access is redirected to the existing login flow.
- Non-Employee and unlinked accounts are denied mobile entry.
- An Employee cannot reach another Employee's protected data through the mobile
  area (object-level scope is inherited from the reused views).
- Logout invalidates the session so the mobile area is no longer accessible.
- Existing desktop role access for HR/Director/Xefe/Staff HR/superusers is
  unchanged (they are denied the mobile area, but retain desktop dashboard).
- The desktop admin sidebar is not rendered inside the mobile area.
"""

from datetime import date, time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from attendance.models import AttendanceRecord, EmployeeSchedule, Shift
from employees.models import Employee, EmployeeAssignment, UserEmployeeLink
from employee_requests.models import EmployeeRequest, LeaveType
from organization.models import Department, Office, Position

User = get_user_model()


class MobileAccessTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.emp_group, _ = Group.objects.get_or_create(name="Employess")
        cls.xefe_group, _ = Group.objects.get_or_create(name="Xefe_Departamentu")
        cls.dir_group, _ = Group.objects.get_or_create(name="Director")
        cls.shr_group, _ = Group.objects.get_or_create(name="Staff HR")

        cls.office_a = Office.objects.create(office_code="MA", office_name="Mobile Branch A", office_type="branch")
        cls.office_b = Office.objects.create(office_code="MB", office_name="Mobile Branch B", office_type="branch")
        cls.dept_a = Department.objects.create(office=cls.office_a, department_code="MDA", department_name="Dept A")
        cls.pos_emp_a = Position.objects.create(office=cls.office_a, department=cls.dept_a, position_code="MEA", position_title="Employee A")
        cls.pos_emp_b = Position.objects.create(office=cls.office_b, department=None, position_code="MEB", position_title="Employee B")

        # Grant the Employee group the self-service permissions the mobile area
        # reuses (matches the desktop employee experience).
        cls.emp_group.permissions.set(
            Permission.objects.filter(
                content_type__app_label__in=["employee_requests", "attendance"],
            )
        )

        cls.emp_a = cls._make_user("m_emp_a", "MEA1", cls.emp_group, cls.pos_emp_a)
        cls.emp_b = cls._make_user("m_emp_b", "MEB1", cls.emp_group, cls.pos_emp_b)
        cls.hr = cls._make_user("m_hr", "MHR1", cls.shr_group, cls.pos_emp_a)
        cls.dir = cls._make_user("m_dir", "MDIR1", cls.dir_group, cls.pos_emp_a)

    @classmethod
    def _make_user(cls, username, employee_id, group, position):
        user = User.objects.create_user(username=username, password="test-pass")
        emp = Employee.objects.create(employee_id=employee_id, full_name=username, employment_status=Employee.EmploymentStatus.ACTIVE)
        UserEmployeeLink.objects.create(user=user, employee=emp)
        EmployeeAssignment.objects.create(
            employee=emp, position=position, start_date=date(2024, 1, 1),
            is_primary=True, assignment_type=EmployeeAssignment.AssignmentType.PRIMARY,
        )
        user.groups.add(group)
        return user

    @property
    def emp_link_a(self):
        return UserEmployeeLink.objects.get(user=self.emp_a).employee

    @property
    def emp_link_b(self):
        return UserEmployeeLink.objects.get(user=self.emp_b).employee


class MobileEntryGateTests(MobileAccessTestBase):
    def test_anonymous_redirects_to_login(self):
        for name in ["mobile-home", "mobile-attendance", "mobile-requests", "mobile-notifications", "mobile-profile"]:
            with self.subTest(name=name):
                resp = self.client.get(reverse(name))
                self.assertEqual(resp.status_code, 302)
                self.assertIn(reverse("login"), resp.url)

    def test_employee_role_with_link_reaches_home(self):
        self.client.force_login(self.emp_a)
        resp = self.client.get(reverse("mobile-home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.emp_link_a.full_name)

    def test_employee_reaches_all_mobile_sections(self):
        self.client.force_login(self.emp_a)
        for name in ["mobile-home", "mobile-attendance", "mobile-requests", "mobile-notifications", "mobile-profile"]:
            with self.subTest(name=name):
                resp = self.client.get(reverse(name))
                self.assertEqual(resp.status_code, 200)

    def test_non_employee_staff_hr_denied_mobile_entry(self):
        self.client.force_login(self.hr)
        resp = self.client.get(reverse("mobile-home"))
        self.assertEqual(resp.status_code, 403)

    def test_non_employee_director_denied_mobile_entry(self):
        self.client.force_login(self.dir)
        resp = self.client.get(reverse("mobile-home"))
        self.assertEqual(resp.status_code, 403)

    def test_superuser_denied_mobile_entry(self):
        admin = User.objects.create_superuser(username="m_admin", password="test-pass")
        self.client.force_login(admin)
        resp = self.client.get(reverse("mobile-home"))
        self.assertEqual(resp.status_code, 403)

    def test_employee_group_without_link_denied(self):
        # Remove the authoritative link; Employee role alone is not enough.
        UserEmployeeLink.objects.filter(user=self.emp_a).delete()
        self.client.force_login(self.emp_a)
        resp = self.client.get(reverse("mobile-home"))
        self.assertEqual(resp.status_code, 403)

    def test_link_only_without_employee_role_denied(self):
        # A valid UserEmployeeLink but no Employee group membership must fail.
        unlinked_group_user = User.objects.create_user(username="m_linkonly", password="test-pass")
        emp = Employee.objects.create(employee_id="MLINK1", full_name="Link Only", employment_status=Employee.EmploymentStatus.ACTIVE)
        UserEmployeeLink.objects.create(user=unlinked_group_user, employee=emp)
        self.client.force_login(unlinked_group_user)
        resp = self.client.get(reverse("mobile-home"))
        self.assertEqual(resp.status_code, 403)

    def test_mobile_area_does_not_render_admin_sidebar(self):
        self.client.force_login(self.emp_a)
        resp = self.client.get(reverse("mobile-home"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'class="app-sidebar')

    def test_reused_mobile_pages_do_not_render_admin_sidebar(self):
        self.client.force_login(self.emp_a)
        for name in ["mobile-requests", "mobile-notifications", "mobile-attendance", "mobile-profile"]:
            with self.subTest(name=name):
                resp = self.client.get(reverse(name))
                self.assertEqual(resp.status_code, 200)
                self.assertNotContains(resp, 'class="app-sidebar')


class MobileObjectScopeTests(MobileAccessTestBase):
    def setUp(self):
        # Give each employee one attendance schedule + request.
        self.shift = Shift.objects.create(
            office=self.office_a, shift_code="MSH", name="Mobile Shift",
            start_time=time(8, 0), end_time=time(17, 0),
        )
        self.req_a = self._make_request(self.emp_a, self.emp_link_a)
        self.req_b = self._make_request(self.emp_b, self.emp_link_b)

    def _make_request(self, user, employee):
        assignment = EmployeeAssignment.objects.get(employee=employee, is_primary=True)
        leave_type = LeaveType.objects.create(code=f"ML{employee.pk}", name="Annual Leave")
        return EmployeeRequest.objects.create(
            request_number=f"REQ-{employee.employee_id}",
            employee_assignment=assignment,
            leave_type=leave_type,
            request_type=EmployeeRequest.RequestType.LEAVE,
            start_date=timezone.localdate(),
            end_date=timezone.localdate(),
            submitted_by=user,
            status=EmployeeRequest.Status.PENDING,
        )

    def test_employee_cannot_read_other_employees_request_detail(self):
        self.client.force_login(self.emp_a)
        resp = self.client.get(reverse("employee_requests:request-detail", args=[self.req_b.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_employee_can_read_own_request_detail(self):
        self.client.force_login(self.emp_a)
        resp = self.client.get(reverse("employee_requests:request-detail", args=[self.req_a.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_my_requests_list_only_own_requests(self):
        self.client.force_login(self.emp_a)
        resp = self.client.get(reverse("mobile-requests"))
        self.assertEqual(resp.status_code, 200)
        numbers = [r.request_number for r in resp.context["requests"]]
        self.assertIn(self.req_a.request_number, numbers)
        self.assertNotIn(self.req_b.request_number, numbers)


class MobileLogoutTests(MobileAccessTestBase):
    def test_logout_invalidates_session(self):
        self.client.force_login(self.emp_a)
        self.assertEqual(self.client.get(reverse("mobile-home")).status_code, 200)
        resp = self.client.post(reverse("mobile-logout"))
        self.assertEqual(resp.status_code, 302)
        # Session is gone; mobile area now requires login again.
        resp2 = self.client.get(reverse("mobile-home"))
        self.assertEqual(resp2.status_code, 302)
        self.assertIn(reverse("login"), resp2.url)


class DesktopRegressionTests(MobileAccessTestBase):
    """Existing desktop role access must remain unchanged."""

    def test_employee_group_dashboard_still_renders(self):
        self.client.force_login(self.emp_a)
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)

    def test_staff_hr_dashboard_still_renders(self):
        self.client.force_login(self.hr)
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)

    def test_director_dashboard_still_renders(self):
        self.client.force_login(self.dir)
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)

    def test_superuser_dashboard_still_renders(self):
        admin = User.objects.create_superuser(username="m_admin2", password="test-pass")
        self.client.force_login(admin)
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)

    def test_desktop_login_page_unaffected(self):
        resp = self.client.get(reverse("login"))
        self.assertEqual(resp.status_code, 200)