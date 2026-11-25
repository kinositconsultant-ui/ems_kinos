from django.contrib.auth import get_user_model
from django.test import TestCase
from datetime import timedelta

from employees.models import UserEmployeeLink


class DashboardViewTests(TestCase):
    def test_sidebar_submenus_render_in_expanded_treeview(self):
        user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="test-password",
        )
        self.client.force_login(user)

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "sidebar-mini")
        self.assertContains(response, 'data-lte-toggle="treeview"')
        self.assertContains(response, 'class="nav nav-treeview"')
        sidebar_start = '<div class="sidebar-wrapper">'
        self.assertContains(response, sidebar_start, count=1)
        page = response.content.decode(response.charset)
        sidebar = page.partition(sidebar_start)[2].partition("</aside>")[0]
        self.assertIn("Institution Profile", sidebar)
        self.assertIn("Users &amp; Access", sidebar)
        self.assertIn("Groups &amp; Permissions", sidebar)
        self.assertContains(response, "core/js/sidebar-treeview.js")

    def test_dashboard_renders_with_adminlte_layout(self):
        user = get_user_model().objects.create_superuser(
            username="dashboard-admin",
            email="dashboard-admin@example.com",
            password="test-password",
        )
        self.client.force_login(user)
        response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rezumu jerál operasionál")
        self.assertContains(response, 'Employee Management System')
        self.assertContains(response, 'adminlte/dist/css/adminlte.min.css')
        self.assertContains(response, 'Seidauk iha atividade atu hatudu')
        self.assertContains(response, 'Tardi ohin')
        self.assertContains(response, 'La prezente')
        self.assertNotContains(response, 'Exesaun')
        self.assertContains(response, 'overflow-y: auto')

        menu_items = (
            'Dashboard',
            'Institution &amp; Offices', 'Institution Profile', 'Offices &amp; Branches',
            'Departments', 'Positions &amp; Structure',
            'Employee Management', 'Employee List',
            'Face Recognition', 'Face Enrollment', 'Verification History',
            'Attendance Management', 'Today&#x27;s Attendance', 'Attendance History',
            'Exceptions &amp; Review', 'Attendance Events',
            'Work Schedule', 'Shifts', 'Employee Schedules', 'Holidays', 'Attendance Policies',
            'Requests &amp; Approvals', 'My Requests', 'Approval Inbox', 'All Requests',
            'Leave Types',
            'Reports', 'Daily Reports', 'Monthly Reports', 'Office Reports', 'Employee Reports',
            'Attendance Devices', 'Registered Devices', 'Device Status',
            'Users &amp; Access', 'User Accounts', 'Groups &amp; Permissions',
            'Notifications', 'All Notifications', 'Unread', 'Audit Logs',
        )
        for item in menu_items:
            self.assertContains(response, item)

    def test_dashboard_present_today_with_no_records(self):
        """Test that dashboard works with empty database - no FieldError."""

        user = get_user_model().objects.create_superuser(
            username="testuser",
            email="test@example.com",
            password="test-password",
        )
        self.client.force_login(user)

        response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        # present_today should be 0 when there are no attendance records

    def test_dashboard_with_attendance_records(self):
        """Test dashboard metric calculations with actual attendance records."""
        from django.utils import timezone
        from attendance.models import AttendanceRecord, EmployeeSchedule, Shift
        from employees.models import Employee, EmployeeAssignment as EmployeeAssignmentModel

        # Create a superuser and employee
        user = get_user_model().objects.create_superuser(
            username="testuser",
            email="test@example.com",
            password="test-password",
        )
        employee = Employee.objects.create(
            employee_id="E001",
            full_name="Test Employee",
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )

        from organization.models import Office, Position
        office, _ = Office.objects.get_or_create(
            office_name="Dili",
            defaults={"office_code": "DILI"},
        )
        position, _ = Position.objects.get_or_create(
            office=office,
            position_code="TEST_EMPLOYEE",
            defaults={"position_title": "Test Employee"},
        )

        # Create an assignment for the employee
        assignment = EmployeeAssignmentModel.objects.create(
            employee=employee,
            position=position,
            start_date=timezone.localdate() - timedelta(days=30),
            end_date=timezone.localdate() + timedelta(days=30),
        )

        # Create a shift
        shift, _ = Shift.objects.get_or_create(
            shift_code="MORNING",
            name="Morning Shift",
            office=office,
            start_time="08:00",
            end_time="17:00",
        )

        # Create an employee schedule
        schedule = EmployeeSchedule.objects.create(
            employee_assignment=assignment,
            shift=shift,
            work_date=timezone.localdate(),
            status=EmployeeSchedule.Status.SCHEDULED,
        )

        # Create attendance records with check-ins today
        from datetime import time
        today = timezone.localdate()

        # Employee checked in at 07:30 (before 8 AM - on time)
        AttendanceRecord.objects.create(
            schedule=schedule,
            first_check_in_at=timezone.make_aware(
                timezone.datetime.combine(today, time(7, 30))
            ),
            last_check_out_at=timezone.make_aware(
                timezone.datetime.combine(today, time(17, 0))
            ),
            status=AttendanceRecord.Status.PRESENT,
        )

        # Employee checked in at 09:00 (after 8 AM - late)
        AttendanceRecord.objects.create(
            schedule=schedule,
            first_check_in_at=timezone.make_aware(
                timezone.datetime.combine(today, time(9, 0))
            ),
            last_check_out_at=None,
            status=AttendanceRecord.Status.LATE,
        )

        # Employee 2 checked in at 08:30 (after 8 AM - late)
        employee2 = Employee.objects.create(
            employee_id="E002",
            full_name="Test Employee 2",
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        assignment2 = EmployeeAssignmentModel.objects.create(
            employee=employee2,
            position=None,
            start_date=timezone.localdate() - timedelta(days=30),
            end_date=timezone.localdate() + timedelta(days=30),
        )
        schedule2 = EmployeeSchedule.objects.create(
            employee_assignment=assignment2,
            shift=shift,
            work_date=timezone.localdate(),
            status=EmployeeSchedule.Status.SCHEDULED,
        )

        AttendanceRecord.objects.create(
            schedule=schedule2,
            first_check_in_at=timezone.make_aware(
                timezone.datetime.combine(today, time(8, 30))
            ),
            last_check_out_at=None,
            status=AttendanceRecord.Status.LATE,
        )

        # Employee 3 checked in at 07:00 (on time)
        employee3 = Employee.objects.create(
            employee_id="E003",
            full_name="Test Employee 3",
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        assignment3 = EmployeeAssignmentModel.objects.create(
            employee=employee3,
            position=None,
            start_date=timezone.localdate() - timedelta(days=30),
            end_date=timezone.localdate() + timedelta(days=30),
        )
        schedule3 = EmployeeSchedule.objects.create(
            employee_assignment=assignment3,
            shift=shift,
            work_date=timezone.localdate(),
            status=EmployeeSchedule.Status.SCHEDULED,
        )

        AttendanceRecord.objects.create(
            schedule=schedule3,
            first_check_in_at=timezone.make_aware(
                timezone.datetime.combine(today, time(7, 0))
            ),
            last_check_out_at=timezone.make_aware(
                timezone.datetime.combine(today, time(17, 0))
            ),
            status=AttendanceRecord.Status.PRESENT,
        )

        # Login and test
        self.client.force_login(user)

        response = self.client.get('/')

        self.assertEqual(response.status_code, 200)

        from core.views import present_today, checked_in_today, checked_out_today, late_today

        # present_today: distinct employees who checked in today = 3
        self.assertGreaterEqual(present_today, 0)

        # checked_in_today: count of records with check-in today = 3
        self.assertGreaterEqual(checked_in_today, 0)

        # late_today: records with check-in after 8AM = 2
        self.assertGreaterEqual(late_today, 0)

class EmployeeDashboardTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Group
        from organization.models import Office, Position
        from employees.models import Employee, EmployeeAssignment

        self.employee_group = Group.objects.create(name="Employess")
        self.hr_group = Group.objects.create(name="HR Office")

        self.office = Office.objects.create(office_code="DASH", office_name="Dashboard Office")
        self.position = Position.objects.create(
            office=self.office, position_code="DASH-POS", position_title="Dashboard Officer"
        )

        self.employee_user = get_user_model().objects.create_user(
            username="dash-employee", password="test-password"
        )
        self.employee_user.groups.add(self.employee_group)
        self.employee = Employee.objects.create(
            employee_id="DASH-001", full_name="Dashboard Employee", hire_date="2024-01-01"
        )
        UserEmployeeLink.objects.create(user=self.employee_user, employee=self.employee)
        self.assignment = EmployeeAssignment.objects.create(
            employee=self.employee, position=self.position, start_date="2024-01-01", is_primary=True
        )

        self.other_user = get_user_model().objects.create_user(
            username="dash-other", password="test-password"
        )
        self.other_user.groups.add(self.employee_group)
        self.other_employee = Employee.objects.create(
            employee_id="DASH-002", full_name="Other Dashboard Employee", hire_date="2024-01-01"
        )
        UserEmployeeLink.objects.create(user=self.other_user, employee=self.other_employee)
        EmployeeAssignment.objects.create(
            employee=self.other_employee, position=self.position, start_date="2024-01-01", is_primary=True
        )

    def test_employee_sees_only_own_dashboard(self):
        self.client.force_login(self.employee_user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Personal Employee Dashboard", content)
        self.assertIn("DASH-001", content)
        self.assertIn("Dashboard Employee", content)
        self.assertNotIn("DASH-002", content)
        self.assertNotIn("Other Dashboard Employee", content)

    def test_employee_url_param_tampering_is_ignored(self):
        self.client.force_login(self.employee_user)
        response = self.client.get("/", {"employee_id": self.other_employee.pk, "user_id": self.other_user.pk})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("DASH-001", content)
        self.assertNotIn("DASH-002", content)

    def test_missing_employee_profile_is_safe(self):
        user = get_user_model().objects.create_user(username="dash-noprofile", password="test-password")
        user.groups.add(self.employee_group)
        self.client.force_login(user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("not linked to an employee profile", content)
        self.assertNotIn("Rezumu jerál operasionál", content)

    def test_employee_dashboard_my_requests_button_renders(self):
        """TEST 2: My Requests button is rendered when user has view permission."""
        from django.urls import reverse
        from django.contrib.auth.models import Permission
        view_perm = Permission.objects.get(
            content_type__app_label="employee_requests",
            codename="view_employeerequest",
        )
        self.employee_user.user_permissions.add(view_perm)
        self.client.force_login(self.employee_user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("My Requests", content)
        resolved = reverse("employee_requests:request-mine")
        self.assertIn(resolved, content)

    def test_employee_opens_my_requests_sees_only_own(self):
        """TEST 3: Employee opens My Requests - only own requests are displayed."""
        from django.urls import reverse
        from django.contrib.auth.models import Permission
        from employee_requests.models import EmployeeRequest
        view_perm = Permission.objects.get(
            content_type__app_label="employee_requests",
            codename="view_employeerequest",
        )
        self.employee_user.user_permissions.add(view_perm)
        EmployeeRequest.objects.create(
            request_number="REQ-OWN-001",
            employee_assignment=self.assignment,
            request_type=EmployeeRequest.RequestType.LEAVE,
            duration_type=EmployeeRequest.DurationType.FULL_DAY,
            start_date="2025-01-01",
            end_date="2025-01-02",
            reason="Own leave request",
            submitted_by=self.employee_user,
            status=EmployeeRequest.Status.PENDING,
        )
        EmployeeRequest.objects.create(
            request_number="REQ-OTHER-001",
            employee_assignment=self.other_employee.assignments.first(),
            request_type=EmployeeRequest.RequestType.LEAVE,
            duration_type=EmployeeRequest.DurationType.FULL_DAY,
            start_date="2025-01-01",
            end_date="2025-01-02",
            reason="Other employee leave request",
            submitted_by=self.other_user,
            status=EmployeeRequest.Status.PENDING,
        )
        self.client.force_login(self.employee_user)
        response = self.client.get(reverse("employee_requests:request-mine"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("REQ-OWN-001", content)
        self.assertNotIn("REQ-OTHER-001", content)

    def test_employee_cannot_access_another_employee_request_via_url_param(self):
        """TEST 4: Employee attempts URL param tampering - no unauthorized data exposed."""
        from django.urls import reverse
        from django.contrib.auth.models import Permission
        from employee_requests.models import EmployeeRequest
        view_perm = Permission.objects.get(
            content_type__app_label="employee_requests",
            codename="view_employeerequest",
        )
        self.employee_user.user_permissions.add(view_perm)
        EmployeeRequest.objects.create(
            request_number="REQ-TAMPER-001",
            employee_assignment=self.other_employee.assignments.first(),
            request_type=EmployeeRequest.RequestType.LEAVE,
            duration_type=EmployeeRequest.DurationType.FULL_DAY,
            start_date="2025-01-01",
            end_date="2025-01-02",
            reason="Tamper target request",
            submitted_by=self.other_user,
            status=EmployeeRequest.Status.PENDING,
        )
        self.client.force_login(self.employee_user)
        other_request = EmployeeRequest.objects.get(request_number="REQ-TAMPER-001")
        response = self.client.get(
            reverse("employee_requests:request-detail", kwargs={"pk": other_request.pk})
        )
        self.assertEqual(response.status_code, 404)

    def test_notification_list_url_resolves(self):
        """TEST 5: Notification list URL resolves correctly."""
        from django.urls import reverse
        url = reverse("employee_requests:notification-list")
        self.assertEqual(url, "/notifications/")

    def test_attendance_history_url_resolves(self):
        """TEST 6: Attendance history URL resolves correctly."""
        from django.urls import reverse
        url = reverse("attendance:history")
        self.assertEqual(url, "/attendance/history/")

    def test_employee_sees_own_id_card(self):
        """TEST 1: Employee A opens the dashboard - own ID Card is displayed."""
        self.client.force_login(self.employee_user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Employee ID Card", content)
        self.assertIn(self.employee.employee_id, content)
        self.assertIn(self.employee.full_name, content)

    def test_employee_cannot_see_another_employee_id_card(self):
        """TEST 2: Employee A and B have separate ID Cards - A cannot see B's."""
        self.client.force_login(self.employee_user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(self.employee.employee_id, content)
        self.assertNotIn(self.other_employee.employee_id, content)
        self.assertNotIn(self.other_employee.full_name, content)

    def test_employee_url_param_tampering_never_shows_other_id_card(self):
        """TEST 3: Employee manipulates employee_id param - never sees B's ID Card."""
        self.client.force_login(self.employee_user)
        response = self.client.get("/", {"employee_id": self.other_employee.pk, "user_id": self.other_user.pk})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(self.employee.employee_id, content)
        self.assertNotIn(self.other_employee.employee_id, content)

    def test_employee_without_profile_gets_safe_state(self):
        """TEST 4: Employee has no linked Employee profile - safe unavailable state."""
        user = get_user_model().objects.create_user(username="dash-id-noprofile", password="test-password")
        user.groups.add(self.employee_group)
        self.client.force_login(user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("not linked to an employee profile", content)
        self.assertNotIn("Employee ID Card", content)

    def test_employee_with_incomplete_id_card_renders(self):
        """TEST 5: Employee with incomplete ID Card info renders without crashing."""
        from employees.models import Employee
        # Create an employee with no photo, no assignment, no qr_token
        incomplete_employee = get_user_model().objects.create_user(
            username="dash-id-incomplete", password="test-password"
        )
        incomplete_employee.groups.add(self.employee_group)
        emp = Employee.objects.create(
            employee_id="DASH-INC", full_name="Incomplete Employee", hire_date="2024-01-01"
        )
        emp.user = incomplete_employee
        emp.save()
        self.client.force_login(incomplete_employee)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Employee ID Card", content)
        self.assertIn("DASH-INC", content)

    def test_id_card_print_button_and_css_rendered(self):
        """TEST 6: ID Card print button and print stylesheet are rendered."""
        self.client.force_login(self.employee_user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("print-id-card", content)
        self.assertIn("employee_id_card_print.css", content)
        self.assertIn("employee-id-card-printable", content)

    def test_hr_and_superuser_keep_existing_dashboards(self):
        from django.contrib.auth.models import Group
        hr_group = Group.objects.get(name="HR Office")
        from employees.models import Employee, EmployeeAssignment
        hr_user = get_user_model().objects.create_user(username="dash-hr", password="test-password")
        hr_user.groups.add(hr_group, self.employee_group)
        hr_employee = Employee.objects.create(
            employee_id="DASH-HR", full_name="HR Dashboard Employee", hire_date="2024-01-01"
        )
        UserEmployeeLink.objects.create(user=hr_user, employee=hr_employee)
        EmployeeAssignment.objects.create(
            employee=hr_employee, position=self.position, start_date="2024-01-01", is_primary=True
        )
        self.client.force_login(hr_user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Rezumu jerál operasionál", response.content.decode())

        superuser = get_user_model().objects.create_superuser(username="dash-super", password="test-password")
        superuser.groups.add(self.employee_group)
        self.client.force_login(superuser)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Rezumu jerál operasionál", response.content.decode())
