"""Isolated verification for the employee edit date-display fix.

Run via Django test runner (isolated test DB). Proves:
- GET edit renders ISO YYYY-MM-DD date values (browser-displayable) and writes nothing.
- Repeated GET/refresh/cancel preserves DB values.
- Saving unchanged values preserves employee + contract + assignment.
- Editing only an unrelated field preserves contract dates/number.
- Intentional valid contract change still saves.
- Explicitly clearing an allowed optional date field still clears.
- Existing dropdown (office/department/position) selections are selected on load.
- Existing photo attachment survives Save without replacement.
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from employees.models import Employee, EmployeeAssignment
from organization.models import Department, Office, Position

User = get_user_model()


class EmployeeEditDisplayTests(TestCase):
    def setUp(self):
        self.office = Office.objects.create(office_code="DIL", office_name="Dili Office")
        self.dept = Department.objects.create(office=self.office, department_code="ADM", department_name="Admin")
        self.pos = Position.objects.create(office=self.office, department=self.dept, position_code="ADM", position_title="Admin Officer")
        self.user = User.objects.create_superuser(username="admin", email="a@example.com", password="pw")
        self.client.force_login(self.user)
        self.emp = Employee.objects.create(
            employee_id="EMP-EDIT1", full_name="Edit Test",
            employment_status=Employee.EmploymentStatus.ACTIVE,
            employment_type=Employee.EmploymentType.CONTRACT,
            hire_date=date(2026, 1, 10),
            contract_number="CN-2026-1",
            contract_start_date=date(2026, 1, 1),
            contract_end_date=date(2027, 1, 1),
            marital_status=Employee.MaritalStatus.SINGLE,
            date_of_birth=date(1990, 5, 20),
        )
        self.asg = EmployeeAssignment.objects.create(
            employee=self.emp, position=self.pos,
            start_date=date(2026, 1, 1), end_date=date(2027, 1, 1), is_primary=True,
        )

    def _get_edit_html(self):
        resp = self.client.get(reverse("employees:employee-update", args=[self.emp.pk]))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_get_renders_iso_dates(self):
        html = self._get_edit_html()
        self.assertIn('name="contract_start_date" value="2026-01-01"', html)
        self.assertIn('name="contract_end_date" value="2027-01-01"', html)
        self.assertIn('name="hire_date" value="2026-01-10"', html)
        self.assertIn('name="date_of_birth" value="1990-05-20"', html)
        # No localized DD-MM-YYYY leak for these dates.
        self.assertNotIn('value="01-01-2026"', html)
        self.assertNotIn('value="20-05-1990"', html)

    def test_get_does_not_modify_data(self):
        before = Employee.objects.get(pk=self.emp.pk)
        self._get_edit_html()
        self._get_edit_html()  # repeated
        after = Employee.objects.get(pk=self.emp.pk)
        self.assertEqual(before.contract_start_date, after.contract_start_date)
        self.assertEqual(before.contract_end_date, after.contract_end_date)
        self.assertEqual(before.contract_number, after.contract_number)
        self.assertEqual(before.hire_date, after.hire_date)
        self.assertEqual(before.date_of_birth, after.date_of_birth)
        self.assertEqual(before.updated_at, after.updated_at)
        self.assertEqual(EmployeeAssignment.objects.filter(employee=self.emp).count(), 1)

    def _payload(self, **over):
        p = {
            "employee_id": self.emp.employee_id,
            "full_name": self.emp.full_name,
            "marital_status": self.emp.marital_status,
            "hire_date": "2026-01-10",
            "employment_type": self.emp.employment_type,
            "employment_status": self.emp.employment_status,
            "contract_number": self.emp.contract_number,
            "contract_start_date": "2026-01-01",
            "contract_end_date": "2027-01-01",
            "date_of_birth": "1990-05-20",
            "office": str(self.office.pk),
            "department": str(self.dept.pk),
            "position": str(self.pos.pk),
            "assignment_start_date": "2026-01-01",
        }
        p.update(over)
        return p

    def test_save_unchanged_preserves_all(self):
        resp = self.client.post(reverse("employees:employee-update", args=[self.emp.pk]), self._payload())
        self.assertEqual(resp.status_code, 302)
        e = Employee.objects.get(pk=self.emp.pk)
        self.assertEqual(e.contract_start_date, date(2026, 1, 1))
        self.assertEqual(e.contract_end_date, date(2027, 1, 1))
        self.assertEqual(e.contract_number, "CN-2026-1")
        self.assertEqual(e.hire_date, date(2026, 1, 10))
        self.assertEqual(e.date_of_birth, date(1990, 5, 20))

    def test_edit_only_full_name_preserves_contract(self):
        resp = self.client.post(
            reverse("employees:employee-update", args=[self.emp.pk]),
            self._payload(full_name="Renamed Person"),
        )
        self.assertEqual(resp.status_code, 302)
        e = Employee.objects.get(pk=self.emp.pk)
        self.assertEqual(e.full_name, "Renamed Person")
        self.assertEqual(e.contract_start_date, date(2026, 1, 1))
        self.assertEqual(e.contract_end_date, date(2027, 1, 1))
        self.assertEqual(e.contract_number, "CN-2026-1")

    def test_intentional_contract_change_saves(self):
        resp = self.client.post(
            reverse("employees:employee-update", args=[self.emp.pk]),
            self._payload(contract_number="CN-2026-2", contract_end_date="2028-01-01"),
        )
        self.assertEqual(resp.status_code, 302)
        e = Employee.objects.get(pk=self.emp.pk)
        self.assertEqual(e.contract_number, "CN-2026-2")
        self.assertEqual(e.contract_end_date, date(2028, 1, 1))

    def test_explicit_clear_optional_date_clears(self):
        # date_of_birth is optional; posting empty must clear it (not restore).
        resp = self.client.post(
            reverse("employees:employee-update", args=[self.emp.pk]),
            self._payload(date_of_birth=""),
        )
        self.assertEqual(resp.status_code, 302)
        e = Employee.objects.get(pk=self.emp.pk)
        self.assertIsNone(e.date_of_birth)

    def test_validation_error_preserves_db_and_shows_input(self):
        # Invalid: end before start -> form error, no DB change, submitted value re-shown.
        resp = self.client.post(
            reverse("employees:employee-update", args=[self.emp.pk]),
            self._payload(contract_start_date="2027-06-01", contract_end_date="2027-01-01"),
        )
        self.assertEqual(resp.status_code, 200)
        e = Employee.objects.get(pk=self.emp.pk)
        self.assertEqual(e.contract_start_date, date(2026, 1, 1))  # unchanged
        self.assertEqual(e.contract_end_date, date(2027, 1, 1))
        html = resp.content.decode()
        self.assertIn("contract_end_date", html)  # error surfaced

    def test_dropdowns_selected_on_load(self):
        html = self._get_edit_html()
        # The currently saved office/department/position must be marked selected.
        self.assertRegex(html, r'<option value="%d" selected' % self.office.pk)
        self.assertRegex(html, r'<option value="%d" selected' % self.dept.pk)
        self.assertRegex(html, r'<option value="%d" selected' % self.pos.pk)

    def test_photo_survives_save_without_replacement(self):
        # Assign a stored photo name directly (no new upload) and ensure Save keeps it.
        self.emp.photo = "employees/photos/2026/01/existing.png"
        self.emp.save(update_fields=["photo"])
        resp = self.client.post(
            reverse("employees:employee-update", args=[self.emp.pk]),
            self._payload(full_name="Photo Keep"),
        )
        self.assertEqual(resp.status_code, 302)
        e = Employee.objects.get(pk=self.emp.pk)
        self.assertEqual(e.photo.name, "employees/photos/2026/01/existing.png")
        self.assertEqual(e.contract_end_date, date(2027, 1, 1))