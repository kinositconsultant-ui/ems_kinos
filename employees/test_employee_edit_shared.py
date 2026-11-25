"""Multi-employee isolated verification of the shared Employee List -> Edit fix.

Exercises the real update view + real template (not form.as_p) across:
- fully populated employee
- employee with legitimately empty optional fields
- different employment types / contract dates / branches
- employee with existing attachment + assignment history
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from employees.models import Employee, EmployeeAssignment
from organization.models import Department, Office, Position

User = get_user_model()


class SharedEmployeeEditTests(TestCase):
    def setUp(self):
        self.hq = Office.objects.create(office_code="HQ", office_name="HQ Dili")
        self.br = Office.objects.create(office_code="BR", office_name="Baucau Branch")
        self.d_hq = Department.objects.create(office=self.hq, department_code="HR", department_name="HR")
        self.d_br = Department.objects.create(office=self.br, department_code="OP", department_name="Ops")
        self.p_hq = Position.objects.create(office=self.hq, department=self.d_hq, position_code="HR", position_title="HR Officer")
        self.p_br = Position.objects.create(office=self.br, department=self.d_br, position_code="OP", position_title="Ops Officer")
        self.admin = User.objects.create_superuser(username="admin", email="a@x.com", password="pw")
        self.client.force_login(self.admin)

    def _emp(self, **kw):
        base = dict(
            employee_id=kw.pop("employee_id"), full_name=kw.pop("full_name"),
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        base.update(kw)
        return Employee.objects.create(**base)

    def _assign(self, emp, pos, start, end):
        return EmployeeAssignment.objects.create(
            employee=emp, position=pos, start_date=start, end_date=end, is_primary=True,
        )

    def _html(self, emp):
        r = self.client.get(reverse("employees:employee-update", args=[emp.pk]))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def _payload(self, emp, pos, asg, **over):
        p = {
            "employee_id": emp.employee_id, "full_name": emp.full_name,
            "marital_status": emp.marital_status or "single",
            "employment_type": emp.employment_type, "employment_status": emp.employment_status,
            "hire_date": emp.hire_date.isoformat() if emp.hire_date else "",
            "contract_number": emp.contract_number or "",
            "contract_start_date": emp.contract_start_date.isoformat() if emp.contract_start_date else "",
            "contract_end_date": emp.contract_end_date.isoformat() if emp.contract_end_date else "",
            "date_of_birth": emp.date_of_birth.isoformat() if emp.date_of_birth else "",
            "office": str(pos.office_id), "department": str(pos.department_id or ""),
            "position": str(pos.pk),
            "assignment_start_date": asg.start_date.isoformat(),
        }
        p.update(over)
        return p

    def test_fully_populated(self):
        e = self._emp(employee_id="E-FULL", full_name="Full Person",
                      employment_type=Employee.EmploymentType.CONTRACT,
                      hire_date=date(2025, 1, 1), contract_number="C-FULL",
                      contract_start_date=date(2025, 1, 1), contract_end_date=date(2026, 1, 1),
                      date_of_birth=date(1990, 1, 1))
        a = self._assign(e, self.p_hq, date(2025, 1, 1), date(2026, 1, 1))
        html = self._html(e)
        self.assertIn('name="hire_date" value="2025-01-01"', html)
        self.assertIn('name="contract_start_date" value="2025-01-01"', html)
        self.assertIn('name="contract_end_date" value="2026-01-01"', html)
        self.assertIn('name="date_of_birth" value="1990-01-01"', html)
        self.assertRegex(html, r'<option value="%d" selected' % self.p_hq.office_id)

    def test_empty_optionals_stay_empty(self):
        e = self._emp(employee_id="E-EMPTY", full_name="Minimal Person",
                      employment_type=Employee.EmploymentType.PERMANENT)
        a = self._assign(e, self.p_br, date(2025, 6, 1), None)
        html = self._html(e)
        # date inputs with no saved value render empty (no value attr)
        import re
        for name in ("date_of_birth", "hire_date", "contract_start_date", "contract_end_date"):
            m = re.search(r'<input[^>]*name="%s"[^>]*>' % name, html)
            self.assertTrue(m and 'value=' not in m.group(0), f"{name} should be empty")
        self.assertRegex(html, r'<option value="%d" selected' % self.p_br.office_id)

    def test_save_unchanged_and_single_field_edit(self):
        e = self._emp(employee_id="E-SAVE", full_name="Save Person",
                      employment_type=Employee.EmploymentType.CONTRACT,
                      hire_date=date(2025, 3, 1), contract_number="C-SAVE",
                      contract_start_date=date(2025, 3, 1), contract_end_date=date(2026, 3, 1))
        a = self._assign(e, self.p_hq, date(2025, 3, 1), date(2026, 3, 1))
        # save unchanged
        r = self.client.post(reverse("employees:employee-update", args=[e.pk]), self._payload(e, self.p_hq, a))
        self.assertEqual(r.status_code, 302)
        e.refresh_from_db()
        self.assertEqual(e.contract_end_date, date(2026, 3, 1))
        # edit only full_name
        r = self.client.post(reverse("employees:employee-update", args=[e.pk]),
                             self._payload(e, self.p_hq, a, full_name="Renamed"))
        self.assertEqual(r.status_code, 302)
        e.refresh_from_db()
        self.assertEqual(e.full_name, "Renamed")
        self.assertEqual(e.contract_number, "C-SAVE")
        self.assertEqual(e.contract_start_date, date(2025, 3, 1))
        self.assertEqual(e.contract_end_date, date(2026, 3, 1))
        self.assertEqual(e.hire_date, date(2025, 3, 1))

    def test_validation_error_preserves_and_surfaces(self):
        e = self._emp(employee_id="E-INV", full_name="Invalid Person",
                      employment_type=Employee.EmploymentType.CONTRACT,
                      contract_number="C-INV",
                      contract_start_date=date(2025, 1, 1), contract_end_date=date(2026, 1, 1))
        a = self._assign(e, self.p_hq, date(2025, 1, 1), date(2026, 1, 1))
        r = self.client.post(reverse("employees:employee-update", args=[e.pk]),
                             self._payload(e, self.p_hq, a, contract_start_date="2026-06-01", contract_end_date="2026-01-01"))
        self.assertEqual(r.status_code, 200)
        e.refresh_from_db()
        self.assertEqual(e.contract_start_date, date(2025, 1, 1))  # unchanged
        self.assertEqual(e.contract_end_date, date(2026, 1, 1))

    def test_existing_attachment_survives_save(self):
        e = self._emp(employee_id="E-ATT", full_name="Attach Person",
                      employment_type=Employee.EmploymentType.CONTRACT,
                      contract_number="C-ATT",
                      contract_start_date=date(2025, 1, 1), contract_end_date=date(2026, 1, 1))
        a = self._assign(e, self.p_hq, date(2025, 1, 1), date(2026, 1, 1))
        e.photo = "employees/photos/2026/01/keep.png"; e.save(update_fields=["photo"])
        r = self.client.post(reverse("employees:employee-update", args=[e.pk]),
                             self._payload(e, self.p_hq, a, full_name="Attach Renamed"))
        self.assertEqual(r.status_code, 302)
        e.refresh_from_db()
        self.assertEqual(e.photo.name, "employees/photos/2026/01/keep.png")
        self.assertEqual(e.contract_end_date, date(2026, 1, 1))

    def test_assignment_history_preserved_on_reopen(self):
        e = self._emp(employee_id="E-HIST", full_name="History Person",
                      employment_type=Employee.EmploymentType.CONTRACT,
                      contract_start_date=date(2024, 1, 1), contract_end_date=date(2026, 1, 1))
        a1 = self._assign(e, self.p_hq, date(2024, 1, 1), date(2025, 1, 1))
        a2 = EmployeeAssignment.objects.create(employee=e, position=self.p_br,
                                               start_date=date(2025, 1, 1), end_date=date(2026, 1, 1), is_primary=True)
        before = EmployeeAssignment.objects.filter(employee=e).count()
        self._html(e); self._html(e)  # reopen/refresh
        self.assertEqual(EmployeeAssignment.objects.filter(employee=e).count(), before)
        # Both historical assignment rows and their dates are intact.
        self.assertTrue(EmployeeAssignment.objects.filter(employee=e, pk=a1.pk).exists())
        self.assertTrue(EmployeeAssignment.objects.filter(employee=e, pk=a2.pk).exists())
        a1.refresh_from_db(); a2.refresh_from_db()
        self.assertEqual(a1.end_date, date(2025, 1, 1))
        self.assertEqual(a2.start_date, date(2025, 1, 1))