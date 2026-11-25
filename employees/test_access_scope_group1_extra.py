from datetime import timedelta, time
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from attendance.models import EmployeeSchedule, Shift
from core.permissions import DIRECTOR_GROUP, STAFF_HR_GROUP, XEFE_GROUP, IT_ADMIN_GROUP
from employees.models import Employee, EmployeeAssignment, UserEmployeeLink
from organization.models import Department, Office, Position

EMPLOYEES_GROUP = "Employess"


def _group(name, app_labels, codenames=None):
    group, _ = Group.objects.get_or_create(name=name)
    qs = Permission.objects.filter(content_type__app_label__in=app_labels)
    if codenames:
        qs = qs.filter(codename__in=codenames)
    group.permissions.set(qs)
    return group


def _user(username, employee, password="test-pass-12345"):
    User = get_user_model()
    u = User.objects.create_user(username=username, password=password)
    UserEmployeeLink.objects.create(user=u, employee=employee)
    return u


class AccessScopeGroup1ExtraTests(TestCase):
    """Verifikasi tambahan: position_choices fallback, Xefe, Director tanpa assignment,
    dan karyawan dengan riwayat transfer cabang."""

    @classmethod
    def setUpTestData(cls):
        cls.office_a = Office.objects.create(office_code="XA", office_name="Branch XA")
        cls.office_b = Office.objects.create(office_code="XB", office_name="Branch XB")
        cls.dept_a1 = Department.objects.create(office=cls.office_a, department_code="XA1", department_name="Dept XA1")
        cls.dept_a2 = Department.objects.create(office=cls.office_a, department_code="XA2", department_name="Dept XA2")
        cls.dept_b1 = Department.objects.create(office=cls.office_b, department_code="XB1", department_name="Dept XB1")
        cls.pos_a1 = Position.objects.create(office=cls.office_a, department=cls.dept_a1, position_code="XPA1",
                                             position_title="Pos XA1 Lead", is_management=True)
        cls.pos_a2 = Position.objects.create(office=cls.office_a, department=cls.dept_a2, position_code="XPA2",
                                             position_title="Pos XA2", is_management=False)
        cls.pos_b1 = Position.objects.create(office=cls.office_b, department=cls.dept_b1, position_code="XPB1",
                                             position_title="Pos XB1 Lead", is_management=True)
        cls.shift_a = Shift.objects.create(office=cls.office_a, shift_code="XA", name="Shift XA", start_time=time(8),
                                           end_time=time(17), break_start_time=time(12), break_end_time=time(13))
        cls.shift_b = Shift.objects.create(office=cls.office_b, shift_code="XB", name="Shift XB", start_time=time(8),
                                           end_time=time(17), break_start_time=time(12), break_end_time=time(13))
        cls.emp_a = Employee.objects.create(employee_id="XEA", full_name="Emp XA")
        cls.emp_a2 = Employee.objects.create(employee_id="XEA2", full_name="Emp XA2")
        cls.emp_b = Employee.objects.create(employee_id="XEB", full_name="Emp XB")
        cls.emp_no_asg = Employee.objects.create(employee_id="XEN", full_name="Emp No Assignment")
        cls.emp_transfer = Employee.objects.create(employee_id="XET", full_name="Emp Transfer")
        today = timezone.localdate()
        cls.asg_a = EmployeeAssignment.objects.create(employee=cls.emp_a, position=cls.pos_a1, is_primary=True,
                                                      start_date=today - timedelta(days=10))
        cls.asg_a2 = EmployeeAssignment.objects.create(employee=cls.emp_a2, position=cls.pos_a2, is_primary=True,
                                                       start_date=today - timedelta(days=10))
        cls.asg_b = EmployeeAssignment.objects.create(employee=cls.emp_b, position=cls.pos_b1, is_primary=True,
                                                      start_date=today - timedelta(days=10))
        # Riwayat transfer: emp_transfer pernah di office B (lalu), kini di office A (saat ini).
        EmployeeAssignment.objects.create(
            employee=cls.emp_transfer, position=cls.pos_b1, is_primary=True,
            start_date=today - timedelta(days=60), end_date=today - timedelta(days=30),
        )
        cls.asg_transfer = EmployeeAssignment.objects.create(
            employee=cls.emp_transfer, position=cls.pos_a1, is_primary=True,
            start_date=today - timedelta(days=10),
        )

        _group("Director", ["employees", "attendance"], ["view_employee", "view_employeeschedule"])
        _group(EMPLOYEES_GROUP, ["employees", "attendance"], ["view_employee", "view_employeeschedule"])
        _group(STAFF_HR_GROUP, ["employees", "attendance", "organization"],
               ["view_employee", "view_employeeschedule", "view_office", "view_department", "view_position"])
        _group(XEFE_GROUP, ["employees", "attendance"], ["view_employee", "view_employeeschedule"])
        _group(IT_ADMIN_GROUP, ["employees", "attendance"], ["view_employee", "view_employeeschedule"])

        cls.director = _user("xdir", cls.emp_a)
        cls.director.groups.add(Group.objects.get(name="Director"))
        cls.director_no_asg = _user("xdirnoasg", cls.emp_no_asg)
        cls.director_no_asg.groups.add(Group.objects.get(name="Director"))
        cls.xefe = _user("xxefe", cls.emp_a)
        cls.xefe.groups.add(Group.objects.get(name=XEFE_GROUP))
        cls.employee = _user("xemp", cls.emp_a)
        cls.employee.groups.add(Group.objects.get(name=EMPLOYEES_GROUP))
        cls.view_only = _user("xview", cls.emp_a)
        view_perm = Permission.objects.get(content_type__app_label="employees", codename="view_employee")
        cls.view_only.user_permissions.add(view_perm)
        cls.superuser = get_user_model().objects.create_superuser("xsu", "su@example.com", "pass")
        cls.it_admin = _user("xit", cls.emp_a)
        cls.it_admin.groups.add(Group.objects.get(name=IT_ADMIN_GROUP))

        cls.schedule_a = EmployeeSchedule.objects.create(
            employee_assignment=cls.asg_a, shift=cls.shift_a, work_date=today,
            schedule_source=EmployeeSchedule.ScheduleSource.MANUAL, status=EmployeeSchedule.Status.SCHEDULED,
            scheduled_start_at=timezone.now(), scheduled_end_at=timezone.now() + timedelta(hours=9),
        )
        cls.schedule_a2 = EmployeeSchedule.objects.create(
            employee_assignment=cls.asg_a2, shift=cls.shift_a, work_date=today,
            schedule_source=EmployeeSchedule.ScheduleSource.MANUAL, status=EmployeeSchedule.Status.SCHEDULED,
            scheduled_start_at=timezone.now(), scheduled_end_at=timezone.now() + timedelta(hours=9),
        )
        cls.schedule_b = EmployeeSchedule.objects.create(
            employee_assignment=cls.asg_b, shift=cls.shift_b, work_date=today,
            schedule_source=EmployeeSchedule.ScheduleSource.MANUAL, status=EmployeeSchedule.Status.SCHEDULED,
            scheduled_start_at=timezone.now(), scheduled_end_at=timezone.now() + timedelta(hours=9),
        )
        cls.schedule_transfer = EmployeeSchedule.objects.create(
            employee_assignment=cls.asg_transfer, shift=cls.shift_a, work_date=today,
            schedule_source=EmployeeSchedule.ScheduleSource.MANUAL, status=EmployeeSchedule.Status.SCHEDULED,
            scheduled_start_at=timezone.now(), scheduled_end_at=timezone.now() + timedelta(hours=9),
        )

    def setUp(self):
        self.today = timezone.localdate()

    def _control_choices(self, response, name):
        for context_key in ("filters", "filter_controls"):
            try:
                controls = response.context[context_key]
            except KeyError:
                continue
            for control in controls:
                if control["name"] == name:
                    return list(control["choices"])
        return None

    # ---------- Item 1: position_choices fallback ----------

    def test_position_choices_superuser_receives_all_positions(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        choices = dict(self._control_choices(response, "position_id"))
        self.assertIn(self.pos_b1.pk, choices)
        self.assertEqual(len(choices), 3)

    def test_position_choices_it_admin_receives_all_positions(self):
        self.client.force_login(self.it_admin)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        choices = dict(self._control_choices(response, "position_id"))
        self.assertIn(self.pos_b1.pk, choices)

    def test_position_choices_xefe_scoped_to_led_office_department(self):
        self.client.force_login(self.xefe)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        choices = dict(self._control_choices(response, "position_id"))
        self.assertIn(self.pos_a1.pk, choices)
        self.assertNotIn(self.pos_a2.pk, choices, "Posisi departemen lain yang sama tidak boleh ada dalam dropdown Xefe")
        self.assertNotIn(self.pos_b1.pk, choices, "Posisi cabang lain tidak boleh ada dalam dropdown Xefe")

    def test_position_choices_director_without_valid_assignment_is_empty(self):
        self.client.force_login(self.director_no_asg)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        choices = dict(self._control_choices(response, "position_id"))
        self.assertEqual(choices, {}, "Direktur tanpa assignment valid tidak boleh mendapatkan opsi posisi global")
        self.assertEqual(list(response.context["employees"]), [])

    def test_view_only_permission_without_role_does_not_receive_global_position_choices(self):
        self.client.force_login(self.view_only)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        choices = dict(self._control_choices(response, "position_id"))
        self.assertEqual(choices, {}, "Pengguna hanya punya permission view tanpa role/cakupan tidak boleh opsi posisi global")

    # ---------- Item 2: Xefe ----------

    def test_xefe_employee_list_led_department_only(self):
        self.client.force_login(self.xefe)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        ids = {e.pk for e in response.context["employees"]}
        # Xefe memimpin dept_a1 (office A): emp_a dan emp_transfer (keduanya saat ini di dept_a1/office A) terlihat.
        self.assertEqual(ids, {self.emp_a.pk, self.emp_transfer.pk})
        self.assertNotIn(self.emp_a2, response.context["employees"])
        self.assertNotIn(self.emp_b, response.context["employees"])

    def test_xefe_employee_filter_choices_scoped_separately_from_query(self):
        self.client.force_login(self.xefe)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        # Pilihan filter (department_id disembunyikan untuk Xefe, office_id disembunyikan).
        names = [c["name"] for c in response.context["filters"]]
        self.assertNotIn("office_id", names)
        self.assertNotIn("department_id", names)
        position_choices = dict(self._control_choices(response, "position_id"))
        self.assertIn(self.pos_a1.pk, position_choices)
        self.assertNotIn(self.pos_b1.pk, position_choices)
        # Hasil query terpisah dari pilihan: emp_a dan emp_transfer (kedua di dept_a1/office A).
        self.assertEqual(
            list(response.context["employees"]), [self.emp_a, self.emp_transfer]
        )

    def test_xefe_employee_detail_led_department_allowed(self):
        self.client.force_login(self.xefe)
        self.assertEqual(self.client.get(reverse("employees:employee-detail", args=[self.emp_a.pk])).status_code, 200)

    def test_xefe_employee_detail_other_department_same_branch_denied(self):
        self.client.force_login(self.xefe)
        self.assertEqual(self.client.get(reverse("employees:employee-detail", args=[self.emp_a2.pk])).status_code, 404)

    def test_xefe_employee_detail_other_branch_denied(self):
        self.client.force_login(self.xefe)
        self.assertEqual(self.client.get(reverse("employees:employee-detail", args=[self.emp_b.pk])).status_code, 404)

    def test_xefe_schedule_list_led_department_only(self):
        self.client.force_login(self.xefe)
        response = self.client.get(reverse("attendance:schedule-list"))
        self.assertEqual(response.status_code, 200)
        ids = {s.pk for s in response.context["items"]}
        self.assertEqual(ids, {self.schedule_a.pk}, "Xefe jadwal hanya departemen yang dipimpin")
        self.assertNotIn(self.schedule_a2, response.context["items"])
        self.assertNotIn(self.schedule_b, response.context["items"])

    def test_xefe_schedule_filter_choices_scoped_separately_from_query(self):
        self.client.force_login(self.xefe)
        response = self.client.get(reverse("attendance:schedule-list"))
        self.assertEqual(response.status_code, 200)
        names = [c["name"] for c in response.context["filter_controls"]]
        self.assertNotIn("office_id", names)
        self.assertNotIn("department_id", names)
        shift_choices = dict(self._control_choices(response, "shift_id"))
        self.assertIn(self.shift_a.pk, shift_choices)
        self.assertNotIn(self.shift_b.pk, shift_choices, "Shift cabang lain tidak boleh dalam dropdown Xefe")
        self.assertEqual(list(response.context["items"]), [self.schedule_a])

    def test_xefe_schedule_detail_led_allowed_other_branch_denied(self):
        self.client.force_login(self.xefe)
        self.assertEqual(self.client.get(reverse("attendance:schedule-detail", args=[self.schedule_a.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("attendance:schedule-detail", args=[self.schedule_b.pk])).status_code, 404)

    # ---------- Item 3: Director tanpa assignment + transfer history ----------

    def test_director_without_valid_assignment_fails_closed(self):
        self.client.force_login(self.director_no_asg)
        self.assertEqual(self.client.get(reverse("employees:employee-list")).status_code, 200)
        self.assertEqual(list(self.client.get(reverse("employees:employee-list")).context["employees"]), [])
        self.assertEqual(self.client.get(reverse("attendance:schedule-list")).status_code, 200)
        self.assertEqual(list(self.client.get(reverse("attendance:schedule-list")).context["items"]), [])
        self.assertEqual(self.client.get(reverse("employees:employee-detail", args=[self.emp_a.pk])).status_code, 404)

    def test_employee_transfer_history_references_current_assignment_only(self):
        # Direktur office A melihat emp_transfer (assignment SAAT INI di office A),
        # meskipun ada riwayat di office B.
        self.client.force_login(self.director)
        response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(response.status_code, 200)
        ids = {e.pk for e in response.context["employees"]}
        self.assertIn(self.emp_transfer.pk, ids)
        self.assertIn(self.emp_a.pk, ids)
        self.assertNotIn(self.emp_b, response.context["employees"])

    def test_director_foreign_office_id_ignored_scope_stays_own_office(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("employees:employee-list"), {"office_id": self.office_b.pk})
        self.assertEqual(response.status_code, 200)
        ids = {e.pk for e in response.context["employees"]}
        # Parameter office_id asing diabaikan: cakupan tetap kantor sendiri, tidak meluas ke cabang B.
        self.assertEqual(ids, {self.emp_a.pk, self.emp_a2.pk, self.emp_transfer.pk})
        self.assertNotIn(self.emp_b, response.context["employees"])

    def test_foreign_department_param_director_narrows_never_broadens(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("employees:employee-list"), {"department_id": self.dept_b1.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["employees"]), [], "Departemen cabang lain harus kosong, bukan global")

    def test_two_accounts_same_employee_scopes_remain_separate(self):
        self.client.force_login(self.employee)
        emp_response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual(list(emp_response.context["employees"]), [self.emp_a])
        self.client.force_login(self.director)
        dir_response = self.client.get(reverse("employees:employee-list"))
        self.assertEqual({e.pk for e in dir_response.context["employees"]}, {self.emp_a.pk, self.emp_a2.pk, self.emp_transfer.pk})
        # Akun Employee tidak mewarisi cakupan Direktur dan sebaliknya.
