import os
import shutil
import tempfile
from datetime import date, timedelta
from io import BytesIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from PIL import Image

from audittrail.models import AuditLog
from organization.models import Office, Position

from .models import Employee, EmployeeAssignment, EmployeeCertification, UserEmployeeLink, get_linked_employee
from .validators import MAX_CERTIFICATE_FILE_SIZE


class EmployeeSelfServiceTests(TestCase):
    def setUp(self):
        self.employee_group = Group.objects.create(name="employees")
        self.user = get_user_model().objects.create_user(
            username="self-service-employee",
            password="test-password",
        )
        self.user.groups.add(self.employee_group)
        self.employee = Employee.objects.create(
            employee_id="EMP-ESS-1",
            full_name="ESS Employee",
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        UserEmployeeLink.objects.create(user=self.user, employee=self.employee)
        self.other_user = get_user_model().objects.create_user(
            username="other-self-service-employee",
            password="test-password",
        )
        self.other_user.groups.add(self.employee_group)
        self.other_employee = Employee.objects.create(
            employee_id="EMP-ESS-2",
            full_name="Other Employee",
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        UserEmployeeLink.objects.create(user=self.other_user, employee=self.other_employee)
        self.client.force_login(self.user)

    def profile_post_data(self, **overrides):
        data = {
            "full_name": "ESS Employee",
            "preferred_name": "",
            "gender": "",
            "date_of_birth": "",
            "place_of_birth": "",
            "nationality": "Timor-Leste",
            "identity_type": "",
            "identity_number": "",
            "identity_expiry_date": "",
            "personal_phone": "123",
            "personal_email": "ess@example.com",
            "residential_address": "Dili",
            "permanent_address": "",
            "municipality": "",
            "administrative_post": "",
            "suco": "",
            "aldeia": "",
            "marital_status": Employee.MaritalStatus.SINGLE,
            "spouse_name": "",
            "spouse_phone": "",
            "spouse_email": "",
            "emergency_contact_name": "",
            "emergency_contact_relationship": "",
            "emergency_contact_phone": "",
            "emergency_contact_alternative_phone": "",
            "emergency_contact_address": "",
            "emergency_contact_notes": "",
            "education_level": "Bachelor",
            "field_of_study": "Information Technology",
            "certifications": "Existing qualification",
            "certs-TOTAL_FORMS": "0",
            "certs-INITIAL_FORMS": "0",
            "certs-MIN_NUM_FORMS": "0",
            "certs-MAX_NUM_FORMS": "25",
        }
        data.update(overrides)
        return data

    def test_employee_can_edit_only_owned_profile_fields(self):
        response = self.client.post(
            reverse("employees:employee-self-service-profile"),
            self.profile_post_data(
                full_name="Updated ESS Employee",
                employee_id="EMP-HACKED",
                employment_status=Employee.EmploymentStatus.TERMINATED,
            ),
        )
        self.assertRedirects(response, reverse("dashboard"))
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.full_name, "Updated ESS Employee")
        self.assertEqual(self.employee.employee_id, "EMP-ESS-1")
        self.assertEqual(self.employee.employment_status, Employee.EmploymentStatus.ACTIVE)

    def test_employee_cannot_load_another_employee_edit_url(self):
        self.client.force_login(self.other_user)
        response = self.client.get(
            reverse("employees:employee-detail", kwargs={"pk": self.employee.pk})
        )
        self.assertIn(response.status_code, {403, 404})

    def test_employee_can_upload_owned_certificate(self):
        data = self.profile_post_data(
            **{
                "certs-TOTAL_FORMS": "1",
                "certs-INITIAL_FORMS": "0",
                "certs-0-certificate_name": "Python Certificate",
                "certs-0-issuing_organization": "Training Institute",
                "certs-0-certificate_number": "",
                "certs-0-issue_date": "",
                "certs-0-expiry_date": "",
                "certs-0-notes": "",
                "certs-0-document_file": SimpleUploadedFile(
                    "certificate.pdf", b"%PDF-1.4\nsample", content_type="application/pdf"
                ),
            },
        )
        response = self.client.post(
            reverse("employees:employee-self-service-profile"), data
        )
        self.assertRedirects(response, reverse("dashboard"))
        certificate = EmployeeCertification.objects.get(certificate_name="Python Certificate")
        self.assertEqual(certificate.employee_id, self.employee.pk)
        self.assertTrue(certificate.document_file.name)


class EmployeePageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="employee-admin",
            email="employee-admin@example.com",
            password="test-password",
        )
        self.client.force_login(self.user)

    def test_employee_list_searches_database(self):
        Employee.objects.create(
            employee_id="EMP-001",
            full_name='Maria Da Costa',
            hire_date="2024-01-01",
        )
        Employee.objects.create(
            employee_id="EMP-002",
            full_name='Joao Soares',
            hire_date="2024-01-01",
        )

        response = self.client.get(
            reverse("employees:employee-list"), {"q": "Maria"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Maria")
        self.assertNotContains(response, "Joao")

    def test_employee_detail_for_unknown_id_is_404(self):
        response = self.client.get(
            reverse("employees:employee-detail", kwargs={"pk": 9999})
        )

        self.assertEqual(response.status_code, 404)

    def test_employee_form_creates_employee(self):
        office = Office.objects.create(office_code="FORM", office_name="Form Office")
        position = Position.objects.create(
            office=office,
            position_code="FORM-ROLE",
            position_title="Form Position",
        )
        response = self.client.post(
            reverse("employees:employee-create"),
            {
                "user": "",
                "employee_id": "EMP-003",
                "full_name": "Ana Freitas",
                "work_email": "",
                "personal_phone": "",
                "hire_date": "2025-01-01",
                "employment_type": Employee.EmploymentType.PERMANENT,
                "employment_status": Employee.EmploymentStatus.ACTIVE,
                "office": office.pk,
                "department": "",
                "position": position.pk,
                "assignment_start_date": "2025-01-01",
                "termination_date": "",
                "contract_number": "",
                "contract_start_date": "",
                "contract_end_date": "",
                "notes": "",
            },
        )

        employee = Employee.objects.get(employee_id="EMP-003")
        self.assertRedirects(
            response,
            reverse("employees:employee-detail", kwargs={"pk": employee.pk}),
        )
        self.assertEqual(employee.full_name, "Ana Freitas")
        self.assertTrue(employee.assignments.filter(is_primary=True, position=position).exists())

    def test_primary_assignment_is_not_allowed_twice(self):
        employee = Employee.objects.create(
            employee_id="EMP-004",
            full_name="Antonio",
            hire_date="2024-01-01",
        )
        office = Office.objects.create(
            office_code="DIL",
            office_name="Dili Office",
        )
        position = Position.objects.create(
            office=office,
            position_code="MAN",
            position_title="Manager",
        )
        EmployeeAssignment.objects.create(
            employee=employee,
            position=position,
            start_date="2024-01-01",
            is_primary=True,
        )

        response = self.client.post(
            reverse("employees:assignment-create"),
            {
                "employee": employee.pk,
                "position": position.pk,
                "assignment_type": EmployeeAssignment.AssignmentType.PRIMARY,
                "start_date": "2025-01-01",
                "end_date": "",
                "is_primary": "on",
                "assignment_reason": "Promotion",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            EmployeeAssignment.objects.filter(
                employee=employee, start_date="2025-01-01"
            ).exists()
        )

    def test_assignment_list_offers_edit_action_to_authorized_user(self):
        employee = Employee.objects.create(
            employee_id="EMP-007",
            full_name="Joana",
            hire_date="2024-01-01",
        )
        office = Office.objects.create(office_code="DIL", office_name="Dili Office")
        position = Position.objects.create(
            office=office,
            position_code="HR",
            position_title="HR Officer",
        )
        assignment = EmployeeAssignment.objects.create(
            employee=employee,
            position=position,
            start_date="2024-01-01",
        )

        response = self.client.get(reverse("employees:assignment-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse("employees:assignment-update", kwargs={"pk": assignment.pk}),
        )

    def test_employee_deactivation_preserves_assignment_history_and_audits(self):
        employee = Employee.objects.create(
            employee_id="EMP-005",
            full_name="Marta",
            hire_date="2024-01-01",
        )
        office = Office.objects.create(office_code="DIL", office_name="Dili Office")
        position = Position.objects.create(
            office=office,
            position_code="ADM",
            position_title="Administrator",
        )
        assignment = EmployeeAssignment.objects.create(
            employee=employee,
            position=position,
            start_date="2024-01-01",
        )
        url = reverse("employees:employee-deactivate", kwargs={"pk": employee.pk})

        confirmation = self.client.get(url)
        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "EMP-005")
        self.assertContains(
            self.client.get(reverse("employees:employee-list")),
            url,
        )
        self.assertContains(
            self.client.get(reverse("employees:employee-detail", kwargs={"pk": employee.pk})),
            url,
        )
        self.assertTrue(Employee.objects.filter(pk=employee.pk).exists())

        response = self.client.post(url)

        self.assertRedirects(
            response, reverse("employees:employee-detail", kwargs={"pk": employee.pk})
        )
        employee.refresh_from_db()
        self.assertEqual(employee.employment_status, Employee.EmploymentStatus.INACTIVE)
        self.assertTrue(EmployeeAssignment.objects.filter(pk=assignment.pk).exists())
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.UPDATE,
                entity_type="Employee",
                object_id=str(employee.pk),
            ).exists()
        )

    def test_employee_deactivation_is_denied_without_office_scope(self):
        employee = Employee.objects.create(
            employee_id="EMP-006",
            full_name="Antonio",
            hire_date="2024-01-01",
        )
        user = get_user_model().objects.create_user(username="employee-scoped-user")
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="employees",
                codename="change_employee",
            )
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("employees:employee-deactivate", kwargs={"pk": employee.pk})
        )

        self.assertEqual(response.status_code, 403)
        employee.refresh_from_db()
        self.assertEqual(employee.employment_status, Employee.EmploymentStatus.ACTIVE)


# ---------------------------------------------------------------------------
# Employee profile refinement: contract rationalisation, spouse, certificates
# ---------------------------------------------------------------------------

def png_bytes():
    buffer = BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return buffer.getvalue()


PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


class EmployeeFormTestCase(TestCase):
    """Shared helpers. MEDIA_ROOT is a throw-away directory per test."""

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        self.user = get_user_model().objects.create_superuser(
            username="profile-admin", email="profile-admin@example.com", password="test-password"
        )
        self.client.force_login(self.user)
        self.office = Office.objects.create(office_code="HQ", office_name="Head Office")
        self.other_office = Office.objects.create(office_code="BR", office_name="Branch Office")
        self.position = Position.objects.create(office=self.office, position_code="OFF", position_title="Officer")
        self.other_position = Position.objects.create(
            office=self.other_office, position_code="BRM", position_title="Branch Manager"
        )

    def payload(self, **overrides):
        data = {
            "employee_id": "", "full_name": "Test Person", "marital_status": "single",
            "hire_date": "2025-01-01", "employment_type": Employee.EmploymentType.PERMANENT,
            "employment_status": Employee.EmploymentStatus.ACTIVE,
            "office": self.office.pk, "department": "", "position": self.position.pk,
            "assignment_start_date": "2025-01-01",
        }
        data.update(overrides)
        return data

    @staticmethod
    def cert_rows(rows, initial=0):
        """Build certificate formset POST data. Each row is a dict of field values."""
        data = {
            "certs-TOTAL_FORMS": str(len(rows)), "certs-INITIAL_FORMS": str(initial),
            "certs-MIN_NUM_FORMS": "0", "certs-MAX_NUM_FORMS": "25",
        }
        for index, row in enumerate(rows):
            for key, value in row.items():
                data[f"certs-{index}-{key}"] = value
        return data

    def make_employee(self, **kwargs):
        defaults = {"employee_id": "EMP-100", "full_name": "Existing Person", "hire_date": date(2024, 1, 1)}
        defaults.update(kwargs)
        return Employee.objects.create(**defaults)


class ContractRationalisationTests(EmployeeFormTestCase):
    def test_redundant_fields_are_gone_from_the_model(self):
        names = {field.name for field in Employee._meta.get_fields()}
        for removed in ("contract_type", "contract_status", "probation_status", "contract_notes"):
            self.assertNotIn(removed, names)
        for kept in ("hire_date", "employment_type", "employment_status", "termination_date",
                     "contract_number", "contract_start_date", "contract_end_date", "notes"):
            self.assertIn(kept, names)

    def test_employment_type_choices_are_unchanged(self):
        self.assertEqual(
            [value for value, _ in Employee.EmploymentType.choices],
            ["permanent", "contract", "part_time", "temporary", "intern", "other"],
        )

    def test_contract_end_before_start_is_rejected(self):
        response = self.client.post(reverse("employees:employee-create"), self.payload(
            employee_id="EMP-200", employment_type=Employee.EmploymentType.CONTRACT,
            contract_start_date="2025-06-01", contract_end_date="2025-01-01",
        ))
        self.assertEqual(response.status_code, 200)
        self.assertIn("contract_end_date", response.context["form"].errors)
        self.assertFalse(Employee.objects.filter(employee_id="EMP-200").exists())

    def test_open_ended_contract_is_allowed(self):
        response = self.client.post(reverse("employees:employee-create"), self.payload(
            employee_id="EMP-201", employment_type=Employee.EmploymentType.CONTRACT,
            contract_start_date="2025-06-01", contract_end_date="",
            assignment_start_date="2025-06-01",
        ))
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(Employee.objects.get(employee_id="EMP-201").contract_end_date)

    def test_old_termination_date_is_kept_when_status_is_not_terminated(self):
        employee = self.make_employee(
            employment_status=Employee.EmploymentStatus.TERMINATED, termination_date=date(2024, 6, 1)
        )
        response = self.client.post(reverse("employees:employee-update", args=[employee.pk]), self.payload(
            employee_id=employee.employee_id, full_name=employee.full_name,
            employment_status=Employee.EmploymentStatus.ACTIVE, termination_date="2024-06-01",
        ))
        self.assertEqual(response.status_code, 302)
        employee.refresh_from_db()
        self.assertEqual(employee.employment_status, Employee.EmploymentStatus.ACTIVE)
        self.assertEqual(employee.termination_date, date(2024, 6, 1))

    def test_termination_date_is_validated_only_when_terminated(self):
        response = self.client.post(reverse("employees:employee-create"), self.payload(
            employee_id="EMP-202", employment_status=Employee.EmploymentStatus.TERMINATED,
            hire_date="2025-05-01", termination_date="2025-01-01",
        ))
        self.assertIn("termination_date", response.context["form"].errors)

    def test_form_has_termination_group_for_javascript_toggle(self):
        response = self.client.get(reverse("employees:employee-create"))
        self.assertContains(response, 'data-conditional="termination"')
        self.assertContains(response, 'id="derived-contract-status"')

    def test_detail_shows_termination_date_only_for_terminated(self):
        active = self.make_employee(employee_id="EMP-301", termination_date=date(2023, 1, 1))
        terminated = self.make_employee(
            employee_id="EMP-302", employment_status=Employee.EmploymentStatus.TERMINATED,
            termination_date=date(2024, 2, 2),
        )
        self.assertNotContains(
            self.client.get(reverse("employees:employee-detail", args=[active.pk])), "Termination date"
        )
        self.assertContains(
            self.client.get(reverse("employees:employee-detail", args=[terminated.pk])), "Termination date"
        )


class DerivedContractStatusTests(SimpleTestCase):
    today = date(2026, 10, 16)

    def status(self, **kwargs):
        return Employee(**kwargs).get_derived_contract_status(today=self.today)

    def test_terminated_wins(self):
        self.assertEqual(
            self.status(employment_status="terminated", contract_end_date=date(2030, 1, 1)),
            Employee.DerivedContractStatus.TERMINATED,
        )

    def test_expired_contract_does_not_change_employment_status(self):
        employee = Employee(employment_status="active", contract_end_date=self.today - timedelta(days=1))
        self.assertEqual(employee.get_derived_contract_status(today=self.today), "expired")
        self.assertEqual(employee.employment_status, Employee.EmploymentStatus.ACTIVE)

    def test_active_upcoming_open_ended_and_not_set(self):
        self.assertEqual(
            self.status(contract_start_date=date(2026, 1, 1), contract_end_date=date(2027, 1, 1)), "active"
        )
        self.assertEqual(self.status(contract_start_date=date(2026, 1, 1)), "active")
        self.assertEqual(self.status(contract_end_date=self.today), "active")
        self.assertEqual(self.status(contract_start_date=date(2027, 1, 1)), "upcoming")
        self.assertEqual(self.status(), "not_set")

    def test_no_status_column_is_stored(self):
        self.assertNotIn("contract_status", {f.name for f in Employee._meta.concrete_fields})


class SpouseTests(EmployeeFormTestCase):
    def test_form_marks_spouse_inputs_as_conditional(self):
        response = self.client.get(reverse("employees:employee-create"))
        self.assertContains(response, 'data-conditional="spouse"', count=3)
        self.assertContains(response, 'id="spouse-hidden-notice"')

    def test_detail_shows_spouse_only_when_married(self):
        married = self.make_employee(
            employee_id="EMP-401", marital_status="married", spouse_name="Maria Spouse"
        )
        single = self.make_employee(
            employee_id="EMP-402", marital_status="single", spouse_name="Hidden Spouse"
        )
        self.assertContains(self.client.get(reverse("employees:employee-detail", args=[married.pk])), "Maria Spouse")
        self.assertNotContains(self.client.get(reverse("employees:employee-detail", args=[single.pk])), "Hidden Spouse")

    def test_stored_spouse_data_survives_switching_to_non_married(self):
        employee = self.make_employee(
            marital_status="married", spouse_name="Maria Spouse", spouse_phone="7700", spouse_email="m@example.com"
        )
        response = self.client.post(reverse("employees:employee-update", args=[employee.pk]), self.payload(
            employee_id=employee.employee_id, full_name=employee.full_name, marital_status="single",
            spouse_name="", spouse_phone="", spouse_email="",  # what a hidden / emptied input would send
        ))
        self.assertEqual(response.status_code, 302)
        employee.refresh_from_db()
        self.assertEqual(employee.marital_status, "single")
        self.assertEqual(
            (employee.spouse_name, employee.spouse_phone, employee.spouse_email),
            ("Maria Spouse", "7700", "m@example.com"),
        )

    def test_server_ignores_spouse_input_for_non_married(self):
        employee = self.make_employee(marital_status="single", spouse_name="Kept")
        self.client.post(reverse("employees:employee-update", args=[employee.pk]), self.payload(
            employee_id=employee.employee_id, full_name=employee.full_name, marital_status="single",
            spouse_name="Injected", spouse_email="not-an-email",
        ))
        employee.refresh_from_db()
        self.assertEqual(employee.spouse_name, "Kept")

    def test_married_employee_can_save_spouse_details(self):
        response = self.client.post(reverse("employees:employee-create"), self.payload(
            employee_id="EMP-403", marital_status="married", spouse_name="Ana", spouse_phone="123",
        ))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Employee.objects.get(employee_id="EMP-403").spouse_name, "Ana")

    def test_emergency_contact_is_independent_of_spouse(self):
        employee = self.make_employee(marital_status="married", spouse_name="Ana")
        self.client.post(reverse("employees:employee-update", args=[employee.pk]), self.payload(
            employee_id=employee.employee_id, full_name=employee.full_name, marital_status="married",
            spouse_name="Ana", emergency_contact_name="Brother", emergency_contact_relationship="Sibling",
        ))
        employee.refresh_from_db()
        self.assertEqual(employee.emergency_contact_name, "Brother")
        self.assertEqual(employee.spouse_name, "Ana")


class AddEditEmployeeTests(EmployeeFormTestCase):
    def test_add_and_edit_employee(self):
        response = self.client.post(reverse("employees:employee-create"), self.payload(employee_id="EMP-500"))
        employee = Employee.objects.get(employee_id="EMP-500")
        self.assertRedirects(response, reverse("employees:employee-detail", args=[employee.pk]))
        response = self.client.post(reverse("employees:employee-update", args=[employee.pk]), self.payload(
            employee_id="EMP-500", full_name="Renamed Person", notes="Some notes",
        ))
        self.assertEqual(response.status_code, 302)
        employee.refresh_from_db()
        self.assertEqual((employee.full_name, employee.notes), ("Renamed Person", "Some notes"))

    def test_form_renders_accordion_sections_and_first_is_open(self):
        response = self.client.get(reverse("employees:employee-create"))
        titles = [section["title"] for section in response.context["sections"]]
        self.assertEqual(titles, [
            "Personal Information", "Contact & Address", "Family & Emergency Contact",
            "Employment & Contract", "Office / Branch Assignment", "Education & Qualifications",
        ])
        self.assertEqual([s["open"] for s in response.context["sections"]], [True] + [False] * 5)

    def test_section_with_error_is_opened(self):
        response = self.client.post(reverse("employees:employee-create"), self.payload(
            employee_id="EMP-501", employment_type=Employee.EmploymentType.CONTRACT,
            contract_start_date="2025-06-01", contract_end_date="2025-01-01",
        ))
        sections = {s["key"]: s for s in response.context["sections"]}
        self.assertTrue(sections["employment"]["open"])
        self.assertTrue(sections["employment"]["has_errors"])
        self.assertFalse(sections["contact"]["open"])

    def test_office_assignment_is_created_and_transfer_closes_the_old_one(self):
        self.client.post(reverse("employees:employee-create"), self.payload(employee_id="EMP-502"))
        employee = Employee.objects.get(employee_id="EMP-502")
        self.assertTrue(employee.assignments.filter(is_primary=True, position=self.position).exists())
        response = self.client.post(reverse("employees:employee-update", args=[employee.pk]), self.payload(
            employee_id="EMP-502", office=self.other_office.pk, position=self.other_position.pk,
            assignment_start_date="2025-06-01",
        ))
        self.assertEqual(response.status_code, 302)
        open_primary = employee.assignments.filter(is_primary=True, end_date__isnull=True)
        self.assertEqual(open_primary.count(), 1)
        self.assertEqual(open_primary.get().position, self.other_position)
        self.assertEqual(employee.assignments.get(position=self.position).end_date, date(2025, 5, 31))

    def test_position_must_belong_to_selected_office(self):
        response = self.client.post(reverse("employees:employee-create"), self.payload(
            employee_id="EMP-503", position=self.other_position.pk,
        ))
        self.assertIn("position", response.context["form"].errors)

    def test_post_without_certificate_management_form_still_works(self):
        response = self.client.post(reverse("employees:employee-create"), self.payload(employee_id="EMP-504"))
        self.assertEqual(response.status_code, 302)


class CertificationTests(EmployeeFormTestCase):
    def test_add_multiple_certificates_in_one_submission(self):
        data = self.payload(employee_id="EMP-600")
        data.update(self.cert_rows([
            {"certificate_name": "First Aid", "issuing_organization": "Red Cross",
             "issue_date": "2024-01-01", "expiry_date": "2026-01-01",
             "document_file": SimpleUploadedFile("first-aid.pdf", PDF_BYTES, "application/pdf")},
            {"certificate_name": "Diploma", "issuing_organization": "University",
             "document_file": SimpleUploadedFile("diploma.png", png_bytes(), "image/png")},
            {"certificate_name": "No file yet"},
        ]))
        response = self.client.post(reverse("employees:employee-create"), data)
        self.assertEqual(response.status_code, 302)
        employee = Employee.objects.get(employee_id="EMP-600")
        certs = {c.certificate_name: c for c in employee.employee_certifications.all()}
        self.assertEqual(set(certs), {"First Aid", "Diploma", "No file yet"})
        # every document stays attached to its own metadata
        self.assertEqual(certs["First Aid"].original_filename, "first-aid.pdf")
        self.assertEqual(certs["Diploma"].original_filename, "diploma.png")
        self.assertTrue(certs["First Aid"].document_file.name.endswith(".pdf"))
        self.assertTrue(os.path.exists(certs["Diploma"].document_file.path))
        self.assertFalse(certs["No file yet"].document_file)

    def test_employee_can_have_more_than_one_certificate_over_time(self):
        employee = self.make_employee()
        for name in ("A", "B", "C"):
            EmployeeCertification.objects.create(employee=employee, certificate_name=name)
        self.assertEqual(employee.employee_certifications.count(), 3)

    def test_disallowed_extension_is_rejected(self):
        data = self.payload(employee_id="EMP-601")
        data.update(self.cert_rows([{
            "certificate_name": "Bad", "document_file": SimpleUploadedFile("run.exe", b"MZ\x90", "application/pdf"),
        }]))
        response = self.client.post(reverse("employees:employee-create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Employee.objects.filter(employee_id="EMP-601").exists())
        self.assertFalse(EmployeeCertification.objects.exists())

    def test_extension_is_not_trusted_without_matching_content(self):
        for name, content in (("fake.pdf", b"<script>alert(1)</script>"), ("fake.png", b"%PDF-1.4"),
                              ("fake.jpg", png_bytes())):
            data = self.payload(employee_id="EMP-602")
            data.update(self.cert_rows([{
                "certificate_name": "Fake", "document_file": SimpleUploadedFile(name, content, "image/png"),
            }]))
            response = self.client.post(reverse("employees:employee-create"), data)
            self.assertEqual(response.status_code, 200, name)
        self.assertFalse(EmployeeCertification.objects.exists())

    def test_oversized_document_is_rejected(self):
        data = self.payload(employee_id="EMP-603")
        big = b"%PDF-" + b"0" * MAX_CERTIFICATE_FILE_SIZE
        data.update(self.cert_rows([{
            "certificate_name": "Huge", "document_file": SimpleUploadedFile("huge.pdf", big, "application/pdf"),
        }]))
        self.assertEqual(self.client.post(reverse("employees:employee-create"), data).status_code, 200)
        self.assertFalse(EmployeeCertification.objects.exists())

    def test_expiry_before_issue_is_rejected(self):
        data = self.payload(employee_id="EMP-604")
        data.update(self.cert_rows([{
            "certificate_name": "Backwards", "issue_date": "2025-05-01", "expiry_date": "2025-01-01",
        }]))
        response = self.client.post(reverse("employees:employee-create"), data)
        self.assertEqual(response.status_code, 200)
        sections = {s["key"]: s for s in response.context["sections"]}
        self.assertTrue(sections["education"]["open"])

    def _employee_with_certificate(self):
        employee = self.make_employee()
        cert = EmployeeCertification.objects.create(
            employee=employee, certificate_name="Stored", issuing_organization="Org", notes="old",
            document_file=SimpleUploadedFile("stored.pdf", PDF_BYTES), original_filename="stored.pdf",
        )
        return employee, cert

    def _edit_payload(self, employee, cert, **cert_overrides):
        row = {"id": cert.pk, "certificate_name": "Stored", "issuing_organization": "Org", "notes": "old"}
        row.update(cert_overrides)
        data = self.payload(employee_id=employee.employee_id, full_name=employee.full_name)
        data.update(self.cert_rows([row], initial=1))
        return data

    def test_editing_metadata_keeps_the_stored_file(self):
        employee, cert = self._employee_with_certificate()
        stored_name, stored_path = cert.document_file.name, cert.document_file.path
        response = self.client.post(
            reverse("employees:employee-update", args=[employee.pk]),
            self._edit_payload(employee, cert, notes="updated notes"),
        )
        self.assertEqual(response.status_code, 302)
        cert.refresh_from_db()
        self.assertEqual(cert.notes, "updated notes")
        self.assertEqual(cert.document_file.name, stored_name)
        self.assertTrue(os.path.exists(stored_path))

    def test_editing_employee_without_certificate_data_keeps_certificates(self):
        employee, cert = self._employee_with_certificate()
        self.client.post(reverse("employees:employee-update", args=[employee.pk]),
                         self.payload(employee_id=employee.employee_id, full_name="New Name"))
        cert.refresh_from_db()
        self.assertTrue(os.path.exists(cert.document_file.path))

    def test_replacing_document_removes_old_file_only_after_commit(self):
        employee, cert = self._employee_with_certificate()
        old_path = cert.document_file.path
        data = self._edit_payload(employee, cert)
        data["certs-0-document_file"] = SimpleUploadedFile("new.png", png_bytes(), "image/png")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("employees:employee-update", args=[employee.pk]), data)
        self.assertEqual(response.status_code, 302)
        cert.refresh_from_db()
        self.assertEqual(cert.original_filename, "new.png")
        self.assertTrue(os.path.exists(cert.document_file.path))
        self.assertFalse(os.path.exists(old_path))

    def test_rejected_replacement_keeps_old_file(self):
        employee, cert = self._employee_with_certificate()
        old_path = cert.document_file.path
        data = self._edit_payload(employee, cert)
        data["certs-0-document_file"] = SimpleUploadedFile("bad.exe", b"MZ", "application/pdf")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(reverse("employees:employee-update", args=[employee.pk]), data).status_code, 200)
        self.assertTrue(os.path.exists(old_path))

    def test_deleting_certificate_removes_record_and_file(self):
        employee, cert = self._employee_with_certificate()
        path = cert.document_file.path
        data = self._edit_payload(employee, cert)
        data["certs-0-DELETE"] = "on"
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("employees:employee-update", args=[employee.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(EmployeeCertification.objects.filter(pk=cert.pk).exists())
        self.assertFalse(os.path.exists(path))

    def test_detail_lists_certificates_with_protected_link(self):
        employee, cert = self._employee_with_certificate()
        response = self.client.get(reverse("employees:employee-detail", args=[employee.pk]))
        self.assertContains(response, "Stored")
        self.assertContains(response, reverse("employees:certification-download", args=[cert.pk]))
        self.assertNotContains(response, cert.document_file.url)  # no direct MEDIA_URL link

    def test_download_requires_permission(self):
        employee, cert = self._employee_with_certificate()
        url = reverse("employees:certification-download", args=[cert.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        limited = get_user_model().objects.create_user(username="limited")
        limited.user_permissions.add(Permission.objects.get(codename="view_employee"))
        self.client.force_login(limited)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_user_without_certificate_permissions_cannot_change_certificates(self):
        employee, cert = self._employee_with_certificate()
        editor = get_user_model().objects.create_user(username="editor")
        editor.user_permissions.add(*Permission.objects.filter(
            codename__in=["view_employee", "change_employee", "add_employeeassignment"]
        ))
        self.client.force_login(editor)
        response = self.client.get(reverse("employees:employee-update", args=[employee.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["cert_formset"])
        data = self._edit_payload(employee, cert, notes="tampered")
        data["certs-0-DELETE"] = "on"
        self.client.post(reverse("employees:employee-update", args=[employee.pk]), data)
        cert.refresh_from_db()
        self.assertEqual(cert.notes, "old")

    def test_user_without_delete_permission_cannot_delete(self):
        employee, cert = self._employee_with_certificate()
        editor = get_user_model().objects.create_user(username="editor2")
        editor.user_permissions.add(*Permission.objects.filter(codename__in=[
            "view_employee", "change_employee", "view_employeecertification", "change_employeecertification",
            "add_employeecertification",
        ]))
        self.client.force_login(editor)
        data = self._edit_payload(employee, cert)
        data["certs-0-DELETE"] = "on"
        response = self.client.post(reverse("employees:employee-update", args=[employee.pk]), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(EmployeeCertification.objects.filter(pk=cert.pk).exists())


class IdCardTests(EmployeeFormTestCase):
    def test_form_contains_live_preview_markup(self):
        response = self.client.get(reverse("employees:employee-create"))
        for element_id in ("employee-id-card", "id-card-name", "id-card-id", "id-card-office",
                           "id-card-department", "id-card-position", "id-card-photo"):
            self.assertContains(response, f'id="{element_id}"')

    def test_card_data_excludes_sensitive_information(self):
        employee = self.make_employee(
            identity_number="SECRET-ID-999", residential_address="Secret Street 1",
            date_of_birth=date(1990, 5, 5), personal_phone="7777-1111", marital_status="married",
            spouse_name="Secret Spouse", emergency_contact_name="Secret Contact", contract_number="CN-SECRET",
        )
        EmployeeAssignment.objects.create(employee=employee, position=self.position, start_date="2024-01-01")
        response = self.client.get(reverse("employees:employee-detail", args=[employee.pk]))
        card = response.context["id_card"]
        self.assertEqual(set(card), {"name", "employee_id", "office", "department", "position", "photo_url", "qr_url"})
        self.assertEqual((card["office"], card["position"]), ("Head Office", "Officer"))
        card_html = response.content.decode().split('id="employee-id-card"', 1)[1].split("Face enrollment status", 1)[0]
        for secret in ("SECRET-ID-999", "Secret Street", "May 5, 1990", "1990-05-05", "7777-1111", "Secret Spouse", "Secret Contact", "CN-SECRET"):
            self.assertNotIn(secret, card_html)

    def test_detail_has_print_button_and_print_stylesheet(self):
        employee = self.make_employee()
        response = self.client.get(reverse("employees:employee-detail", args=[employee.pk]))
        self.assertContains(response, 'id="print-id-card"')
        self.assertContains(response, "employee_id_card_print.css")
        self.assertContains(response, "employee-id-card-printable")


class ModelCompatibilityTests(SimpleTestCase):
    """Other apps (Attendance, Face Enrollment, Employee Requests) rely on these members."""

    def test_public_employee_api_is_unchanged(self):
        names = {f.name for f in Employee._meta.get_fields()}
        for name in ("id", "user", "employee_id", "full_name", "photo", "qr_token", "assignments",
                     "employment_status", "employment_type", "hire_date", "work_email", "personal_phone"):
            self.assertIn(name, names)
        self.assertTrue(hasattr(Employee, "qr_code_data_url"))
        self.assertTrue(hasattr(Employee, "generate_employee_id"))


class AuthoritativeEmployeeLinkTests(TestCase):
    def test_legacy_employee_user_is_not_a_fallback(self):
        user = get_user_model().objects.create_user(username="legacy-only")
        employee = Employee.objects.create(
            employee_id="EMP-LINK-1",
            full_name="Linked Employee",
            user=user,
        )
        self.assertIsNone(get_linked_employee(user))
        UserEmployeeLink.objects.create(user=user, employee=employee)
        self.assertEqual(get_linked_employee(user), employee)
