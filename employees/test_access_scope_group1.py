from datetime import time, timedelta
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from attendance.models import EmployeeSchedule, Shift
from core.permissions import DIRECTOR_GROUP, STAFF_HR_GROUP, XEFE_GROUP
from employees.models import Employee, EmployeeAssignment, UserEmployeeLink
from organization.models import Department, Office, Position

EMPLOYEES_GROUP = "Employess"


def _perm_group(name, app_labels, codenames=None):
    group, _ = Group.objects.get_or_create(name=name)
    qs = Permission.objects.filter(content_type__app_label__in=app_labels)
    if codenames:
        qs = qs.filter(codename__in=codenames)
    group.permissions.set(qs)
    return group


def _make_user(username, group_name, employee, password="test-pass-12345"):
    User = get_user_model()
    user = User.objects.create_user(username=username, password=password)
    UserEmployeeLink.objects.create(user=user, employee=employee)
    group = Group.objects.get(name=group_name)
    user.groups.add(group)
    return user


class AccessScopeFirstGroupTests(TestCase):
    """Employee list and employee schedule: filter choices + query scope per role."""

    @classmethod
    def setUpTestData(cls):
        cls.office_a = Office.objects.create(office_code="BRA", office_name="Branch A")
        cls.office_b = Office.objects.create(office_code="BRB", office_name="Branch B")
        cls.dept_a1 = Department.objects.create(office=cls.office_a, department_code="DA1", department_name="Dept A1")
        cls.dept_a2 = Department.objects.create(office=cls.office_a, department_code="DA2", department_name="Dept A2")
        cls.dept_b1 = Department.objects.create(office=cls.office_b, department_code="DB1", department_name="Dept B1")
        cls.pos_a1 = Position.objects.create(office=cls.office_a, department=cls.dept_a1, position_code="PA1", position_title="Pos A1", is_management=True)
        cls.pos_a2 = Position.objects.create(office=cls.office_a, department=cls.dept_a2, position_code="PA2", position_title="Pos A2", is_management=False)
        cls.pos_b1 = Position.objects.create(office=cls.office_b, department=cls.dept_b1, position_code="PB1", position_title="Pos B1", is_management=True)
        cls.shift_a = Shift.objects.create(office=cls.office_a, shift_code="A-MOR", name="Shift A Morning", start_time=time(8, 0), end_time=time(17, 0), break_start_time=time(12, 0), break_end_time=time(13, 0))
        cls.shift_b = Shift.objects.create(office=cls.office_b, shift_code="B-MOR", name="Shift B Morning", start_time=time(8, 0), end_time=time(17, 0), break_start_time=time(12, 0), break_end_time=time(13, 0))
        cls.emp_a = Employee.objects.create(employee_id="EMP-A", full_name="Employee A")
        cls.emp_b = Employee.objects.create(employee_id="EMP-B", full_name="Employee B")
        today = timezone.localdate()
        cls.asg_a = EmployeeAssignment.objects.create(employee=cls.emp_a, position=cls.pos_a1, is_primary=True, start_date=today - timedelta(days=10))
        cls.asg_b = EmployeeAssignment.objects.create(employee=cls.emp_b, position=cls.pos_b1, is_primary=True, start_date=today - timedelta(days=10))

        _perm_group("Director", ["employees", "attendance"], ["view_employee", "view_employeeschedule"])
        _perm_group(EMPLOYEES_GROUP, ["employees", "attendance"], ["view_employee", "view_employeeschedule"])
        _perm_group(STAFF_HR_GROUP, ["employees", "attendance", "organization"], ["view_employee", "view_employeeschedule", "view_office", "view_department", "view_position"])
        _perm_group(XEFE_GROUP, ["employees", "attendance"], ["view_employee", "view_employeeschedule"])

        cls.director = _make_user("dir-user", "Director", cls.emp_a)
        cls.employee = _make_user("emp-user", EMPLOYEES_GROUP, cls.emp_a)
        cls.staff_hr = _make_user("hr-user", STAFF_HR_GROUP, cls.emp_a)
        cls.xefe = _make_user("xefe-user", XEFE_GROUP, cls.emp_a)
        # Two accounts linked to the same employee with different permissions.
        cls.emp_as_director = _make_user("emp-dir", "Director", cls.emp_a)

        cls.schedule_a = EmployeeSchedule.objects.create(
            employee_assignment=cls.asg_a, shift=cls.shift_a, work_date=today,
            schedule_source=EmployeeSchedule.ScheduleSource.MANUAL, status=EmployeeSchedule.Status.SCHEDULED,
            assigned_by=cls.director,
            scheduled_start_at=timezone.now(), scheduled_end_at=timezone.now() + timedelta(hours=9),
        )
        cls.schedule_b = EmployeeSchedule.objects.create(
            employee_assignment=cls.asg_b, shift=cls.shift_b, work_date=today,
            schedule_source=EmployeeSchedule.ScheduleSource.MANUAL, status=EmployeeSchedule.Status.SCHEDULED,
            assigned_by=cls.director,
            scheduled_start_at=timezone.now(), scheduled_end_at=timezone.now() + timedelta(hours=9),
        )

    def setUp(self):
        self.today = timezone.localdate()

    def _choices(self, response, name):
        context_name = "filters" if "filters" in response.context else "filter_controls"
        controls = response.context[context_name]
        for control in controls:
            if control["name"] == name:
                return list(control["choices"])
        return None

    # ---------- Employee list ----------

    def test_director_employee_list_scoped_to_own_office(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        ids = {e.pk for e in response.context["employees"]}
        self.assertEqual(ids, {self.emp_a.pk})
        self.assertNotIn(self.emp_b, response.context["employees"])

    def test_director_position_dropdown_does_not_leak_other_office(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        choices = dict(self._choices(response, "position_id"))
        self.assertIn(self.pos_a1.pk, choices)
        self.assertIn(self.pos_a2.pk, choices)
        self.assertNotIn(self.pos_b1.pk, choices, "Position from Branch B must not appear in Branch A director dropdown")

    def test_director_department_dropdown_scoped_to_own_office(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        choices = dict(self._choices(response, "department_id"))
        self.assertIn(self.dept_a1.pk, choices)
        self.assertIn(self.dept_a2.pk, choices)
        self.assertNotIn(self.dept_b1.pk, choices, "Department from Branch B must not appear in Branch A director dropdown")

    def test_director_office_dropdown_removed(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        names = [c["name"] for c in response.context["filters"]]
        self.assertNotIn("office_id", names)

    def test_director_foreign_department_param_narrows_never_broadens(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("employees:employee-list"), {"department_id": self.dept_b1.pk})
        self.assertEqual(response.status_code, 200)
        ids = {e.pk for e in response.context["employees"]}
        self.assertEqual(ids, set(), "Foreign department from another office must yield empty, not cross-office data")

    def test_director_foreign_position_param_ignored_but_scope_stays_own_office(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("employees:employee-list"), {"position_id": self.pos_b1.pk})
        self.assertEqual(response.status_code, 200)
        ids = {e.pk for e in response.context["employees"]}
        self.assertEqual(ids, {self.emp_a.pk}, "Position filter from another office must not broaden director scope")

    def test_employee_self_service_sees_only_self(self):
        self.client.force_login(self.employee)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["employees"]), [self.emp_a])

    def test_employee_self_service_filter_choices_have_no_office_department_position(self):
        self.client.force_login(self.employee)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        names = [c["name"] for c in response.context["filters"]]
        for name in ("office_id", "department_id", "position_id"):
            self.assertNotIn(name, names)

    def test_anonymous_cannot_open_employee_list(self):
        response = self.client.get(reverse("employees:employee-list"))
        self.assertIn(response.status_code, (302, 403))

    def test_staff_hr_sees_only_own_office(self):
        self.client.force_login(self.staff_hr)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        ids = {e.pk for e in response.context["employees"]}
        self.assertEqual(ids, {self.emp_a.pk})

    def test_two_accounts_same_employee_have_separate_scopes(self):
        # Same employee, one account as pure Employee, one as Director.
        self.client.force_login(self.employee)
        emp_response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(list(emp_response.context["employees"]), [self.emp_a])
        self.client.force_login(self.emp_as_director)
        dir_response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual({e.pk for e in dir_response.context["employees"]}, {self.emp_a.pk})
        # Director account is not blocked by the Employee account's self-only scope.

    def test_director_can_view_own_office_employee_detail(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("employees:employee-detail", args=[self.emp_a.pk]))
        self.assertEqual(response.status_code, 200)

    def test_director_cannot_view_other_office_employee_detail(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("employees:employee-detail", args=[self.emp_b.pk]))
        self.assertEqual(response.status_code, 404)

    # ---------- Employee schedule ----------

    def test_director_schedule_list_scoped_to_own_office(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("attendance:schedule-list"))
        self.assertEqual(response.status_code, 200)
        ids = {s.pk for s in response.context["items"]}
        self.assertEqual(ids, {self.schedule_a.pk})
        self.assertNotIn(self.schedule_b, response.context["items"])

    def test_director_schedule_employee_dropdown_does_not_leak_other_office(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("attendance:schedule-list"))
        self.assertEqual(response.status_code, 200)
        choices = dict(self._choices(response, "employee_id"))
        self.assertIn(self.emp_a.pk, choices)
        self.assertNotIn(self.emp_b.pk, choices, "Employee from Branch B must not appear in Branch A director dropdown")

    def test_director_schedule_shift_dropdown_does_not_leak_other_office(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("attendance:schedule-list"))
        self.assertEqual(response.status_code, 200)
        choices = dict(self._choices(response, "shift_id"))
        self.assertIn(self.shift_a.pk, choices)
        self.assertNotIn(self.shift_b.pk, choices, "Shift from Branch B must not appear in Branch A director dropdown")

    def test_director_schedule_department_dropdown_scoped_to_own_office(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("attendance:schedule-list"))
        self.assertEqual(response.status_code, 200)
        choices = dict(self._choices(response, "department_id"))
        self.assertIn(self.dept_a1.pk, choices)
        self.assertNotIn(self.dept_b1.pk, choices)

    def test_director_schedule_office_dropdown_removed(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("attendance:schedule-list"))
        self.assertEqual(response.status_code, 200)
        names = [c["name"] for c in response.context["filter_controls"]]
        self.assertNotIn("office_id", names)

    def test_director_foreign_employee_schedule_param_narrows_never_broadens(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("attendance:schedule-list"), {"employee_id": self.emp_b.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["items"]), [], "Foreign employee schedule filter must yield empty results")

    def test_employee_sees_only_own_schedules(self):
        self.client.force_login(self.employee)
        response = self.client.get(reverse("attendance:schedule-list"))
        self.assertEqual(response.status_code, 200)
        ids = {s.pk for s in response.context["items"]}
        self.assertEqual(ids, {self.schedule_a.pk})

    def test_employee_schedule_filter_choices_have_no_office_department_employee(self):
        self.client.force_login(self.employee)
        response = self.client.get(reverse("attendance:schedule-list"))
        self.assertEqual(response.status_code, 200)
        names = [c["name"] for c in response.context["filter_controls"]]
        for name in ("office_id", "department_id", "employee_id"):
            self.assertNotIn(name, names)

    def test_anonymous_cannot_open_schedule_list(self):
        response = self.client.get(reverse("attendance:schedule-list"))
        self.assertIn(response.status_code, (302, 403))

    def test_director_cannot_view_other_office_schedule_detail(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("attendance:schedule-detail", args=[self.schedule_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_director_can_view_own_office_schedule_detail(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("attendance:schedule-detail", args=[self.schedule_a.pk]))
        self.assertEqual(response.status_code, 200)

    def test_missing_assignment_fails_closed_for_director(self):
        self.schedule_a.delete()
        self.asg_a.delete()
        self.client.force_login(self.director)
        self.assertEqual(self.client.get(reverse("employees:employee-list")).status_code, 200)
        self.assertEqual(list(self.client.get(reverse("employees:employee-list")).context["employees"]), [])
        self.assertEqual(self.client.get(reverse("attendance:schedule-list")).status_code, 200)
        self.assertEqual(list(self.client.get(reverse("attendance:schedule-list")).context["items"]), [])
