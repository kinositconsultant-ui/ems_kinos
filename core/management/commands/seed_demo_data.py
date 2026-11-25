from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from attendance.models import (
    AttendanceDevice,
    AttendanceEvent,
    AttendancePolicy,
    AttendanceRecord,
    EmployeeSchedule,
    Holiday,
    Shift,
)
from audittrail.models import AuditLog
from employee_requests.models import EmployeeRequest, LeaveType, Notification, RequestApproval
from employees.models import Employee, EmployeeAssignment
from organization.models import Department, InstitutionProfile, Office, Position


class Command(BaseCommand):
    help = "Seed demo data for the local development interface without overwriting real data."

    def add_arguments(self, parser):
        parser.add_argument("--confirm-demo-data", action="store_true", dest="confirm_demo_data")

    def handle(self, *args, **options):
        if not options.get("confirm_demo_data"):
            self.stdout.write("No demo data created. Re-run with --confirm-demo-data to seed synthetic records.")
            return

        if not settings.DEBUG:
            raise CommandError("The seed_demo_data command can only run while settings.DEBUG is True.")

        engine = settings.DATABASES.get("default", {}).get("ENGINE", "")
        if not engine.endswith("sqlite3"):
            raise CommandError("The seed_demo_data command is restricted to SQLite development databases.")

        table_names = set(connection.introspection.table_names())
        required_tables = {
            "auth_user",
            "organization_institutionprofile",
            "organization_office",
            "organization_department",
            "organization_position",
            "employees_employee",
            "employees_employeeassignment",
            "attendance_shift",
            "attendance_attendancepolicy",
            "attendance_attendancedevice",
            "attendance_employeeschedule",
            "attendance_attendancerecord",
            "attendance_attendanceevent",
            "attendance_holiday",
            "employee_requests_leavetype",
            "employee_requests_employeerequest",
            "employee_requests_requestapproval",
            "employee_requests_notification",
            "audittrail_auditlog",
        }
        missing = sorted(required_tables - table_names)
        if missing:
            raise CommandError(
                "Required database tables are not available yet. Run the project checks/migrations before seeding demo data. Missing: "
                + ", ".join(missing)
            )

        self._run_seed()

    def _run_seed(self):
        summary = {
            "institution_profile": {"created": 0, "skipped": 0},
            "offices": {"created": 0, "skipped": 0},
            "departments": {"created": 0, "skipped": 0},
            "positions": {"created": 0, "skipped": 0},
            "employees": {"created": 0, "skipped": 0},
            "employee_assignments": {"created": 0, "skipped": 0},
            "shifts": {"created": 0, "skipped": 0},
            "attendance_policies": {"created": 0, "skipped": 0},
            "attendance_devices": {"created": 0, "skipped": 0},
            "employee_schedules": {"created": 0, "skipped": 0},
            "attendance_records": {"created": 0, "skipped": 0},
            "attendance_events": {"created": 0, "skipped": 0},
            "holidays": {"created": 0, "skipped": 0},
            "leave_types": {"created": 0, "skipped": 0},
            "employee_requests": {"created": 0, "skipped": 0},
            "request_approvals": {"created": 0, "skipped": 0},
            "notifications": {"created": 0, "skipped": 0},
            "audit_logs": {"created": 0, "skipped": 0},
            "demo_users": {"created": 0, "skipped": 0},
        }

        try:
            with transaction.atomic():
                admin_user = self._get_or_create_demo_staff_user("seed_demo_admin", prefix="System")
                demo_users = self._ensure_demo_users()
                self._seed_institution(summary)
                offices = self._seed_offices(summary)
                departments = self._seed_departments(offices, summary)
                positions = self._seed_positions(offices, departments, summary)
                employees = self._seed_employees(positions, summary)
                assignments = self._seed_employee_assignments(employees, positions, admin_user, summary)
                shifts = self._seed_shifts(offices, summary)
                policies = self._seed_policies(offices, admin_user, summary)
                devices = self._seed_devices(offices, summary)
                schedules = self._seed_schedules(assignments, shifts, summary)
                attendance_records = self._seed_attendance_records(schedules, summary)
                self._seed_attendance_events(attendance_records, summary)
                self._seed_holidays(offices, admin_user, summary)
                leave_types = self._seed_leave_types(summary)
                requests = self._seed_employee_requests(assignments, leave_types, demo_users, summary)
                self._seed_request_approvals(requests, demo_users, summary)
                self._seed_notifications(requests, demo_users, summary)
                self._seed_audit_logs(offices, demo_users, admin_user, summary)
                self._print_summary(summary)
        except Exception as exc:  # pragma: no cover - handled by management command result
            raise CommandError(f"Demo seed failed and was rolled back: {exc}") from exc

    def _get_or_create_demo_staff_user(self, username, prefix="Demo"):
        user_model = get_user_model()
        user, created = user_model.objects.get_or_create(
            username=username,
            defaults={
                "email": f"{username}@demo.local",
                "first_name": prefix,
                "last_name": "User",
                "is_active": True,
                "is_staff": True,
                "is_superuser": True,
            },
        )
        if created:
            user.set_password("demo-password-not-for-login")
            user.save(update_fields=["password", "is_staff", "is_superuser", "is_active", "first_name", "last_name", "email"])
        return user

    def _ensure_demo_users(self):
        User = get_user_model()
        users = []
        for index in range(1, 11):
            username = f"seed_demo_user_{index:02d}"
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": f"{username}@example.invalid",
                    "first_name": "Demo",
                    "last_name": f"User {index:02d}",
                    "is_active": False,
                    "is_staff": False,
                },
            )
            if created:
                user.set_unusable_password()
                user.save(update_fields=["password", "is_active", "first_name", "last_name", "email", "is_staff"])
                users.append(user)
            else:
                users.append(user)
        return users

    def _seed_institution(self, summary):
        institution, created = InstitutionProfile.objects.get_or_create(
            id=1,
            defaults={
                "name": "Instituisaun Demo Timor-Leste",
                "legal_name": "Fictional Attendance Demo Institution",
                "institution_code": "DEMO-ATTENDANCE-01",
                "institution_type": "government",
                "registration_number": "DEMO-REG-001",
                "email": "demo@instituisaun-demo.tl",
                "phone": "+6700000001",
                "address": "Avenida Demo, Dili, Timor-Leste",
                "timezone": "Asia/Dili",
            },
        )
        if created:
            summary["institution_profile"]["created"] = 1
        else:
            summary["institution_profile"]["skipped"] = 1

    def _seed_offices(self, summary):
        office_specs = [
            {"office_code": "DEMO-HQ", "office_name": "DEMO Headquarters - Dili", "office_type": Office.OfficeType.HEADQUARTERS, "municipality": "Dili", "administrative_post": "Nain Feto", "suco": "Motael", "aldeia": "Aldeia Demo 1", "address": "Rua Governu Demo, Dili, Timor-Leste", "phone": "+6700000010", "email": "hq.demo@instituisaun-demo.tl", "latitude": "-8.5568", "longitude": "125.5799", "timezone": "Asia/Dili", "is_active": True},
            {"office_code": "DEMO-BR-01", "office_name": "DEMO Branch Office - Baucau", "office_type": Office.OfficeType.BRANCH, "municipality": "Baucau", "administrative_post": "Baucau", "suco": "Vii", "aldeia": "Aldeia Demo 2", "address": "Rua Demo Baucau, Timor-Leste", "phone": "+6700000101", "email": "baucau.demo@instituisaun-demo.tl", "latitude": "-8.4600", "longitude": "126.4170", "timezone": "Asia/Dili", "is_active": True},
            {"office_code": "DEMO-BR-02", "office_name": "DEMO Branch Office - Ermera", "office_type": Office.OfficeType.BRANCH, "municipality": "Ermera", "administrative_post": "Ermera", "suco": "Letefoho", "aldeia": "Aldeia Demo 3", "address": "Rua Demo Ermera, Timor-Leste", "phone": "+6700000102", "email": "ermera.demo@instituisaun-demo.tl", "latitude": "-8.7600", "longitude": "125.3919", "timezone": "Asia/Dili", "is_active": True},
            {"office_code": "DEMO-BR-03", "office_name": "DEMO Branch Office - Liquiçá", "office_type": Office.OfficeType.BRANCH, "municipality": "Liquiçá", "administrative_post": "Liquiçá", "suco": "Bazartete", "aldeia": "Aldeia Demo 4", "address": "Rua Demo Liquiçá, Timor-Leste", "phone": "+6700000103", "email": "liquica.demo@instituisaun-demo.tl", "latitude": "-8.6550", "longitude": "125.3500", "timezone": "Asia/Dili", "is_active": True},
            {"office_code": "DEMO-BR-04", "office_name": "DEMO Branch Office - Aileu", "office_type": Office.OfficeType.BRANCH, "municipality": "Aileu", "administrative_post": "Aileu", "suco": "Aileu Vila", "aldeia": "Aldeia Demo 5", "address": "Rua Demo Aileu, Timor-Leste", "phone": "+6700000104", "email": "aileu.demo@instituisaun-demo.tl", "latitude": "-8.7071", "longitude": "125.5656", "timezone": "Asia/Dili", "is_active": True},
            {"office_code": "DEMO-BR-05", "office_name": "DEMO Branch Office - Viqueque", "office_type": Office.OfficeType.BRANCH, "municipality": "Viqueque", "administrative_post": "Viqueque", "suco": "Ossu", "aldeia": "Aldeia Demo 6", "address": "Rua Demo Viqueque, Timor-Leste", "phone": "+6700000105", "email": "viqueque.demo@instituisaun-demo.tl", "latitude": "-8.8400", "longitude": "126.3640", "timezone": "Asia/Dili", "is_active": True},
            {"office_code": "DEMO-BR-06", "office_name": "DEMO Branch Office - Manatuto", "office_type": Office.OfficeType.BRANCH, "municipality": "Manatuto", "administrative_post": "Manatuto", "suco": "Manatuto", "aldeia": "Aldeia Demo 7", "address": "Rua Demo Manatuto, Timor-Leste", "phone": "+6700000106", "email": "manatuto.demo@instituisaun-demo.tl", "latitude": "-8.5084", "longitude": "126.0124", "timezone": "Asia/Dili", "is_active": True},
            {"office_code": "DEMO-BR-07", "office_name": "DEMO Branch Office - Oecusse", "office_type": Office.OfficeType.BRANCH, "municipality": "Oecusse", "administrative_post": "Oecusse", "suco": "Pante Makasar", "aldeia": "Aldeia Demo 8", "address": "Rua Demo Oecusse, Timor-Leste", "phone": "+6700000107", "email": "oecusse.demo@instituisaun-demo.tl", "latitude": "-9.1967", "longitude": "124.3567", "timezone": "Asia/Dili", "is_active": True},
            {"office_code": "DEMO-BR-08", "office_name": "DEMO Branch Office - Bobonaro", "office_type": Office.OfficeType.BRANCH, "municipality": "Bobonaro", "administrative_post": "Maliana", "suco": "Maliana", "aldeia": "Aldeia Demo 9", "address": "Rua Demo Bobonaro, Timor-Leste", "phone": "+6700000108", "email": "bubonaro.demo@instituisaun-demo.tl", "latitude": "-8.9975", "longitude": "125.2173", "timezone": "Asia/Dili", "is_active": True},
            {"office_code": "DEMO-BR-09", "office_name": "DEMO Branch Office - Lautém", "office_type": Office.OfficeType.BRANCH, "municipality": "Lautém", "administrative_post": "Lospalos", "suco": "Lospalos", "aldeia": "Aldeia Demo 10", "address": "Rua Demo Lautém, Timor-Leste", "phone": "+6700000109", "email": "lautem.demo@instituisaun-demo.tl", "latitude": "-8.5190", "longitude": "127.0011", "timezone": "Asia/Dili", "is_active": True},
        ]

        headquarters = None
        created_offices = {}
        for index, spec in enumerate(office_specs):
            parent = None
            if index != 0:
                parent = created_offices["DEMO-HQ"]
            office, created = Office.objects.get_or_create(
                office_code=spec["office_code"],
                defaults={
                    "parent_office": parent,
                    **spec,
                },
            )
            if created:
                summary["offices"]["created"] += 1
                created_offices[spec["office_code"]] = office
            else:
                summary["offices"]["skipped"] += 1
                created_offices[spec["office_code"]] = office
            if spec["office_code"] == "DEMO-HQ":
                headquarters = office
        return created_offices

    def _seed_departments(self, offices, summary):
        office_department_map = {}
        for office_code, office in offices.items():
            dept_names = [
                ("DEMO-MGMT", "Management / Office Direction", "Leadership and office governance."),
                ("DEMO-HR", "Human Resources & Administration", "People operations, admin support, and records."),
                ("DEMO-OPS", "Operations & Technical", "Operational coordination and technical delivery."),
            ]
            office_departments = {}
            for code, name, description in dept_names:
                dept, created = Department.objects.get_or_create(
                    office=office,
                    department_code=f"{office_code}-{code}",
                    defaults={
                        "department_name": name,
                        "description": description,
                        "is_active": True,
                    },
                )
                if created:
                    summary["departments"]["created"] += 1
                else:
                    summary["departments"]["skipped"] += 1
                office_departments[name] = dept
            office_department_map[office_code] = office_departments
        return office_department_map

    def _seed_positions(self, offices, departments, summary):
        positions_by_office = {}
        for office_code, office in offices.items():
            office_positions = {}
            if office_code == "DEMO-HQ":
                director = self._create_position(
                    office,
                    departments[office_code]["Management / Office Direction"],
                    "DEMO-DG-01",
                    "Director General",
                    "executive",
                    level=1,
                    is_management=True,
                    reports_to=None,
                    description="DEMO DATA — synthetic executive leadership position.",
                )
                office_positions["director"] = director
                office_positions["hr"] = self._create_position(
                    office,
                    departments[office_code]["Human Resources & Administration"],
                    "DEMO-HR-01",
                    "HR Manager",
                    "management",
                    level=2,
                    is_management=True,
                    reports_to=director,
                    description="DEMO DATA — synthetic HR leadership position.",
                )
                office_positions["operations"] = self._create_position(
                    office,
                    departments[office_code]["Operations & Technical"],
                    "DEMO-OPS-01",
                    "Operations Manager",
                    "operational",
                    level=2,
                    is_management=True,
                    reports_to=director,
                    description="DEMO DATA — synthetic operations leadership position.",
                )
            else:
                branch_director = self._create_position(
                    office,
                    departments[office_code]["Management / Office Direction"],
                    f"{office_code}-BD-01",
                    "Branch Director",
                    "executive",
                    level=1,
                    is_management=True,
                    reports_to=positions_by_office["DEMO-HQ"]["director"],
                    description="DEMO DATA — synthetic branch leadership position.",
                )
                office_positions["director"] = branch_director
                office_positions["hr"] = self._create_position(
                    office,
                    departments[office_code]["Human Resources & Administration"],
                    f"{office_code}-HR-01",
                    "HR Officer",
                    "administrative",
                    level=2,
                    is_management=False,
                    reports_to=branch_director,
                    description="DEMO DATA — synthetic HR support position.",
                )
                office_positions["operations"] = self._create_position(
                    office,
                    departments[office_code]["Operations & Technical"],
                    f"{office_code}-OPS-01",
                    "Operations Officer",
                    "operational",
                    level=2,
                    is_management=False,
                    reports_to=branch_director,
                    description="DEMO DATA — synthetic operations support position.",
                )
            positions_by_office[office_code] = office_positions
        summary["positions"]["created"] = sum(
            Position.objects.filter(office__office_code__startswith="DEMO").count() for _ in [0]
        )
        return positions_by_office

    def _create_position(self, office, department, code, title, category, level, is_management, reports_to, description):
        position, created = Position.objects.get_or_create(
            office=office,
            position_code=code,
            defaults={
                "department": department,
                "reports_to_position": reports_to,
                "position_title": title,
                "position_category": category,
                "position_level": level,
                "is_management": is_management,
                "description": description,
                "is_active": True,
            },
        )
        return position

    def _seed_employees(self, positions, summary):
        employees = []
        for office_code, office_positions in positions.items():
            for role_order, position in enumerate([office_positions["director"], office_positions["hr"], office_positions["operations"]], start=1):
                first_names = ["Afonso", "Helena", "Joao", "Marta", "Luis", "Nadia", "Rui", "Cecilia", "Domingos", "Elisa"]
                last_names = ["Da Costa", "Soares", "Lopes", "Araújo", "Freitas", "Nunes", "Almeida", "Gomes", "Martins", "Silva"]
                employee_index = len(employees) + 1
                first_name = first_names[(employee_index - 1) % len(first_names)]
                last_name = last_names[(employee_index - 1) % len(last_names)]
                employee_id = f"DEMO-EMP-{employee_index:03d}"
                employee, created = Employee.objects.get_or_create(
                    employee_id=employee_id,
                    defaults={
                        "legacy_external_identifier": f"DEMO-EXT-{employee_index:03d}",
                        "full_name": f"{first_name} {last_name}",
                        "work_email": f"{first_name.lower()}.{last_name.lower().replace(' ', '')}@demo.local",
                        "personal_phone": f"+670{7000000 + employee_index}",
                        "hire_date": date.today() - timedelta(days=365 * 2 + employee_index),
                        "employment_type": Employee.EmploymentType.PERMANENT,
                        "employment_status": Employee.EmploymentStatus.ACTIVE,
                        "notes": "DEMO DATA — synthetic employee record for local interface testing.",
                    },
                )
                if created:
                    summary["employees"]["created"] += 1
                else:
                    summary["employees"]["skipped"] += 1
                employees.append({"employee": employee, "position": position})
        return employees

    def _seed_employee_assignments(self, employees, positions, admin_user, summary):
        assignments = []
        for item in employees:
            employee = item["employee"]
            position = item["position"]
            existing = EmployeeAssignment.objects.filter(
                employee=employee,
                is_primary=True,
                end_date__isnull=True,
            )
            if not existing.exists():
                assignment = EmployeeAssignment.objects.create(
                    employee=employee,
                    position=position,
                    assignment_type=EmployeeAssignment.AssignmentType.PRIMARY,
                    start_date=date.today() - timedelta(days=300),
                    end_date=None,
                    is_primary=True,
                    assigned_by=admin_user,
                    assignment_reason="DEMO DATA — synthetic primary assignment for interface testing.",
                )
                summary["employee_assignments"]["created"] += 1
                assignments.append(assignment)
            else:
                assignment = existing.first()
                summary["employee_assignments"]["skipped"] += 1
                assignments.append(assignment)
        return assignments

    def _seed_shifts(self, offices, summary):
        shift_entries = {}
        # Deterministic shift assignment per office so re-runs are idempotent and
        # each office always receives the same shift template. DEMO-BR-01 (Baucau)
        # uses the Morning Shift template (08:30-17:30) to match its published
        # schedules; other offices cycle through the standard templates.
        shift_specs_by_office = {
            "DEMO-HQ": ("DEMO-STD", "Morning Shift", time(8, 30), time(17, 0), False, time(12, 0), time(12, 30)),
            "DEMO-BR-01": ("DEMO-STD", "Morning Shift", time(8, 30), time(17, 30), False, time(12, 0), time(12, 30)),
            "DEMO-BR-02": ("DEMO-NIGHT", "Night Shift", time(22, 0), time(6, 0), True, time(1, 0), time(1, 30)),
            "DEMO-BR-03": ("DEMO-STD", "Morning Shift", time(8, 30), time(17, 0), False, time(12, 0), time(12, 30)),
            "DEMO-BR-04": ("DEMO-OPS", "Operations Shift", time(7, 30), time(16, 30), False, time(12, 0), time(12, 30)),
            "DEMO-BR-05": ("DEMO-NIGHT", "Night Shift", time(22, 0), time(6, 0), True, time(1, 0), time(1, 30)),
            "DEMO-BR-06": ("DEMO-STD", "Morning Shift", time(8, 30), time(17, 0), False, time(12, 0), time(12, 30)),
            "DEMO-BR-07": ("DEMO-OPS", "Operations Shift", time(7, 30), time(16, 30), False, time(12, 0), time(12, 30)),
            "DEMO-BR-08": ("DEMO-NIGHT", "Night Shift", time(22, 0), time(6, 0), True, time(1, 0), time(1, 30)),
            "DEMO-BR-09": ("DEMO-STD", "Morning Shift", time(8, 30), time(17, 30), False, time(12, 0), time(12, 30)),
        }
        for office_code, office in offices.items():
            spec = shift_specs_by_office[office_code]
            shift_code, name, start_time, end_time, crosses, break_start, break_end = spec
            shift, created = Shift.objects.get_or_create(
                office=office,
                shift_code=f"{office_code}-{shift_code}",
                defaults={
                    "name": name,
                    "start_time": start_time,
                    "end_time": end_time,
                    "break_start_time": break_start,
                    "break_end_time": break_end,
                    "grace_period_minutes": 10,
                    "early_checkin_window_minutes": 30,
                    "is_active": True,
                },
            )
            if created:
                summary["shifts"]["created"] += 1
            else:
                summary["shifts"]["skipped"] += 1
            shift_entries[office_code] = shift
        return shift_entries

    def _seed_policies(self, offices, admin_user, summary):
        for office_code, office in offices.items():
            policy_name = f"DEMO POLICY - {office_code}"
            policy, created = AttendancePolicy.objects.get_or_create(
                office=office,
                effective_from=date.today() - timedelta(days=45),
                defaults={
                    "policy_name": policy_name,
                    "early_checkin_window_minutes": 30,
                    "absent_after_minutes": 180,
                    "require_liveness": True,
                    "max_failed_attempts": 5,
                    "failed_attempt_lockout_minutes": 15,
                    "effective_until": date.today() + timedelta(days=180),
                    "is_active": True,
                    "updated_by": admin_user,
                },
            )
            if created:
                summary["attendance_policies"]["created"] += 1
            else:
                summary["attendance_policies"]["skipped"] += 1
        return list(AttendancePolicy.objects.filter(office__office_code__startswith="DEMO"))

    def _seed_devices(self, offices, summary):
        for office_code, office in offices.items():
            device, created = AttendanceDevice.objects.get_or_create(
                office=office,
                device_code=f"DEMO-DEV-{office_code}",
                defaults={
                    "device_name": f"DEMO Device - {office.office_name}",
                    "device_type": AttendanceDevice.DeviceType.KIOSK,
                    "location_description": f"Main lobby for {office.office_name}",
                    "credential_hash": "",
                    "status": AttendanceDevice.Status.ACTIVE,
                    "last_seen_at": None,
                    "last_authenticated_at": None,
                },
            )
            if created:
                summary["attendance_devices"]["created"] += 1
            else:
                summary["attendance_devices"]["skipped"] += 1
        return list(AttendanceDevice.objects.filter(office__office_code__startswith="DEMO"))

    def _seed_schedules(self, assignments, shifts, summary):
        created_schedules = []
        assignment_lookup = {}
        for assignment in assignments:
            assignment_lookup[assignment.position.office_id] = assignment
        for office_id, shift in shifts.items():
            assignment = assignment_lookup.get(shift.office_id)
            if assignment is None:
                continue
            for work_day_offset, is_historical in enumerate([7, 5, 3, 1, 0]):
                work_date = date.today() - timedelta(days=work_day_offset if is_historical else 0)
                if is_historical:
                    continue
                schedule, created = EmployeeSchedule.objects.get_or_create(
                    employee_assignment=assignment,
                    scheduled_start_at=self._make_aware_datetime(work_date, shift.start_time),
                    defaults={
                        "shift": shift,
                        "work_date": work_date,
                        "scheduled_end_at": self._make_aware_datetime(work_date, shift.end_time) if not shift.crosses_midnight else self._make_aware_datetime(work_date + timedelta(days=1), shift.end_time),
                        "status": EmployeeSchedule.Status.SCHEDULED,
                        "notes": "DEMO DATA — synthetic schedule for local interface testing only.",
                    },
                )
                if created:
                    summary["employee_schedules"]["created"] += 1
                    created_schedules.append(schedule)
                else:
                    summary["employee_schedules"]["skipped"] += 1
        for office_id, shift in shifts.items():
            assignment = assignment_lookup.get(shift.office_id)
            if assignment is None:
                continue
            for days_ago in [14, 12, 10, 8, 6, 4, 2, 1]:
                work_date = date.today() - timedelta(days=days_ago)
                schedule_start = self._make_aware_datetime(work_date, shift.start_time)
                if shift.crosses_midnight:
                    schedule_end = self._make_aware_datetime(work_date + timedelta(days=1), shift.end_time)
                else:
                    schedule_end = self._make_aware_datetime(work_date, shift.end_time)
                schedule, created = EmployeeSchedule.objects.get_or_create(
                    employee_assignment=assignment,
                    scheduled_start_at=schedule_start,
                    defaults={
                        "shift": shift,
                        "work_date": work_date,
                        "scheduled_end_at": schedule_end,
                        "status": EmployeeSchedule.Status.SCHEDULED,
                        "notes": "DEMO DATA — historical schedule for testing scenarios.",
                    },
                )
                if created:
                    summary["employee_schedules"]["created"] += 1
                    created_schedules.append(schedule)
                else:
                    summary["employee_schedules"]["skipped"] += 1
        return created_schedules

    def _seed_attendance_records(self, schedules, summary):
        created_records = []
        for index, schedule in enumerate(schedules):
            if index < 10:
                status = ["present", "late", "incomplete", "absent", "pending_review"][index % 5]
                record = AttendanceRecord.objects.get_or_create(
                    schedule=schedule,
                    defaults={
                        "status": status,
                        "review_status": AttendanceRecord.ReviewStatus.APPROVED if status == "present" else AttendanceRecord.ReviewStatus.PENDING if status == "pending_review" else AttendanceRecord.ReviewStatus.NOT_REQUIRED,
                        "remarks": "DEMO DATA — simulated attendance; not generated by real face recognition.",
                        "total_work_minutes": 420 if status == "present" else 360 if status == "late" else 180 if status == "incomplete" else 0,
                        "late_minutes": 15 if status == "late" else 0,
                        "early_leave_minutes": 5 if status == "incomplete" else 0,
                    },
                )[0]
                if AttendanceRecord.objects.filter(schedule=schedule).count() == 1:  # ensure only the first create counts
                    summary["attendance_records"]["created"] += 1
                else:
                    summary["attendance_records"]["skipped"] += 1
                created_records.append(record)
            else:
                record, created = AttendanceRecord.objects.get_or_create(
                    schedule=schedule,
                    defaults={
                        "status": AttendanceRecord.Status.SCHEDULED,
                        "review_status": AttendanceRecord.ReviewStatus.NOT_REQUIRED,
                        "remarks": "",
                    },
                )
                if created:
                    summary["attendance_records"]["created"] += 1
                else:
                    summary["attendance_records"]["skipped"] += 1
                created_records.append(record)
        return created_records

    def _seed_attendance_events(self, records, summary):
        for index, record in enumerate(records[:10]):
            if record.status == AttendanceRecord.Status.SCHEDULED:
                continue
            if record.first_check_in_at:
                event = AttendanceEvent.objects.create(
                    attendance_record=record,
                    event_type=AttendanceEvent.EventType.CHECK_IN,
                    occurred_at=record.first_check_in_at,
                    source=AttendanceEvent.Source.SYSTEM,
                    created_by_user=None,
                    reason="DEMO DATA — synthetic attendance event; not generated by a real camera or face recognition engine.",
                )
                summary["attendance_events"]["created"] += 1
            if record.last_check_out_at and record.last_check_out_at > record.first_check_in_at:
                AttendanceEvent.objects.create(
                    attendance_record=record,
                    event_type=AttendanceEvent.EventType.CHECK_OUT,
                    occurred_at=record.last_check_out_at,
                    source=AttendanceEvent.Source.SYSTEM,
                    created_by_user=None,
                    reason="DEMO DATA — synthetic attendance event; not generated by a real camera or face recognition engine.",
                )
                summary["attendance_events"]["created"] += 1

    def _seed_holidays(self, offices, admin_user, summary):
        holiday_names = [
            "DEMO - Office Holiday - Dili",
            "DEMO - Office Holiday - Baucau",
            "DEMO - Office Holiday - Ermera",
            "DEMO - Office Holiday - Liquiçá",
            "DEMO - Office Holiday - Aileu",
            "DEMO - Office Holiday - Viqueque",
            "DEMO - Office Holiday - Manatuto",
            "DEMO - Office Holiday - Oecusse",
            "DEMO - Office Holiday - Bobonaro",
            "DEMO - Office Holiday - Lautém",
        ]
        for index, office in enumerate(offices.values()):
            start_date = date.today() + timedelta(days=index + 1)
            end_date = start_date + timedelta(days=1)
            holiday, created = Holiday.objects.get_or_create(
                office=office,
                name=holiday_names[index],
                defaults={
                    "start_date": start_date,
                    "end_date": end_date,
                    "holiday_type": Holiday.HolidayType.OFFICE,
                    "description": "DEMO DATA — synthetic office holiday only for local UI testing.",
                    "is_active": True,
                    "created_by": admin_user,
                },
            )
            if created:
                summary["holidays"]["created"] += 1
            else:
                summary["holidays"]["skipped"] += 1

    def _seed_leave_types(self, summary):
        leave_specs = [
            ("DEMO-AL", "Annual Leave", "DEMO DATA — annual leave sample.", True, True, False, Decimal("12.00")),
            ("DEMO-ML", "Medical Leave", "DEMO DATA — medical leave sample.", True, True, True, Decimal("10.00")),
            ("DEMO-PL", "Personal Leave", "DEMO DATA — personal leave sample.", True, True, False, Decimal("5.00")),
            ("DEMO-FL", "Family Leave", "DEMO DATA — family leave sample.", True, True, True, Decimal("7.00")),
            ("DEMO-EL", "Emergency Leave", "DEMO DATA — emergency leave sample.", True, True, True, Decimal("3.00")),
            ("DEMO-UL", "Unpaid Leave", "DEMO DATA — unpaid leave sample.", False, True, False, None),
            ("DEMO-SL", "Study Leave", "DEMO DATA — study leave sample.", True, True, False, Decimal("6.00")),
            ("DEMO-CL", "Compassionate Leave", "DEMO DATA — compassionate leave sample.", True, True, True, Decimal("5.00")),
            ("DEMO-PL2", "Parental Leave", "DEMO DATA — parental leave sample.", True, True, False, Decimal("8.00")),
            ("DEMO-SP", "Special Permission", "DEMO DATA — special permission sample.", False, False, False, Decimal("2.00")),
        ]
        leave_types = []
        for code, name, description, is_paid, requires_approval, requires_attachment, max_days in leave_specs:
            leave_type, created = LeaveType.objects.get_or_create(
                code=code,
                defaults={
                    "name": name,
                    "description": description,
                    "is_paid": is_paid,
                    "requires_approval": requires_approval,
                    "requires_attachment": requires_attachment,
                    "max_days_per_year": max_days,
                    "is_active": True,
                },
            )
            if created:
                summary["leave_types"]["created"] += 1
            else:
                summary["leave_types"]["skipped"] += 1
            leave_types.append(leave_type)
        return leave_types

    def _seed_employee_requests(self, assignments, leave_types, demo_users, summary):
        request_specs = []
        for index, assignment in enumerate(assignments[:10], start=1):
            leave_type = leave_types[index % len(leave_types)]
            request_type = [
                EmployeeRequest.RequestType.LEAVE,
                EmployeeRequest.RequestType.ABSENCE_PERMISSION,
                EmployeeRequest.RequestType.LATE_ARRIVAL,
                EmployeeRequest.RequestType.EARLY_DEPARTURE,
                EmployeeRequest.RequestType.ATTENDANCE_CORRECTION,
                EmployeeRequest.RequestType.OVERTIME,
                EmployeeRequest.RequestType.BUSINESS_TRIP,
                EmployeeRequest.RequestType.LEAVE,
                EmployeeRequest.RequestType.ABSENCE_PERMISSION,
                EmployeeRequest.RequestType.LATE_ARRIVAL,
            ][index - 1]
            start_date = date.today() - timedelta(days=10 + index)
            end_date = start_date + timedelta(days=1 if request_type == EmployeeRequest.RequestType.LEAVE else 0)
            request_number = f"DEMO-REQ-{index:03d}"
            request_specs.append((request_number, assignment, request_type, leave_type, start_date, end_date, demo_users[index % len(demo_users)]))
        requests = []
        for request_number, assignment, request_type, leave_type, start_date, end_date, submitted_by in request_specs:
            request, created = EmployeeRequest.objects.get_or_create(
                request_number=request_number,
                defaults={
                    "employee_assignment": assignment,
                    "leave_type": leave_type if request_type == EmployeeRequest.RequestType.LEAVE else None,
                    "request_type": request_type,
                    "start_date": start_date,
                    "end_date": end_date,
                    "reason": "DEMO DATA — synthetic employee request for interface testing only.",
                    "status": [
                        EmployeeRequest.Status.PENDING,
                        EmployeeRequest.Status.UNDER_REVIEW,
                        EmployeeRequest.Status.APPROVED,
                        EmployeeRequest.Status.REJECTED,
                        EmployeeRequest.Status.CANCELLED,
                    ][(int(request_number[-3:]) - 1) % 5],
                    "submitted_by": submitted_by,
                    "processing_note": "DEMO DATA — synthetic processing note; not a real institutional decision.",
                },
            )
            if created:
                summary["employee_requests"]["created"] += 1
            else:
                summary["employee_requests"]["skipped"] += 1
            requests.append(request)
        return requests

    def _seed_request_approvals(self, requests, demo_users, summary):
        for index, request in enumerate(requests, start=1):
            decision = RequestApproval.Decision.PENDING
            if request.status in [EmployeeRequest.Status.APPROVED, EmployeeRequest.Status.REJECTED]:
                decision = RequestApproval.Decision.APPROVED if request.status == EmployeeRequest.Status.APPROVED else RequestApproval.Decision.REJECTED
            approval, created = RequestApproval.objects.get_or_create(
                request=request,
                step_number=1,
                defaults={
                    "approver": demo_users[index % len(demo_users)],
                    "decision": decision,
                    "decision_note": "DEMO DATA — synthetic approval step for local review only.",
                    "decided_at": timezone.now() - timedelta(days=1) if decision != RequestApproval.Decision.PENDING else None,
                },
            )
            if created:
                summary["request_approvals"]["created"] += 1
            else:
                summary["request_approvals"]["skipped"] += 1

    def _seed_notifications(self, requests, demo_users, summary):
        for index, request in enumerate(requests, start=1):
            user = demo_users[index % len(demo_users)]
            notification, created = Notification.objects.get_or_create(
                recipient_user=user,
                request=request,
                title=f"[DEMO] {request.request_number} - {request.status}",
                defaults={
                    "notification_type": Notification.NotificationType.REQUEST_SUBMITTED if request.status == EmployeeRequest.Status.PENDING else Notification.NotificationType.REQUEST_APPROVED if request.status == EmployeeRequest.Status.APPROVED else Notification.NotificationType.REQUEST_REJECTED if request.status == EmployeeRequest.Status.REJECTED else Notification.NotificationType.GENERAL,
                    "message": "DEMO DATA — synthetic notification record for local UI testing only.",
                    "channel": Notification.Channel.IN_APP,
                    "delivery_status": Notification.DeliveryStatus.QUEUED,
                },
            )
            if created:
                summary["notifications"]["created"] += 1
            else:
                summary["notifications"]["skipped"] += 1

    def _seed_audit_logs(self, offices, demo_users, admin_user, summary):
        for index, office in enumerate(offices.values(), start=1):
            actor = demo_users[index % len(demo_users)] if demo_users else admin_user
            activity, created = AuditLog.objects.get_or_create(
                office=office,
                entity_type=f"DEMO-{office.office_code}-ENTITY",
                action=AuditLog.Action.CREATE,
                object_id=f"DEMO-{index:03d}",
                defaults={
                    "actor_user": actor,
                    "before_data": {"source": "demo"},
                    "after_data": {"status": "created"},
                    "reason": "DEMO DATA — synthetic sample; not a real administrative event.",
                    "ip_address": "127.0.0.1",
                },
            )
            if created:
                summary["audit_logs"]["created"] += 1
            else:
                summary["audit_logs"]["skipped"] += 1

    def _print_summary(self, summary):
        self.stdout.write(self.style.SUCCESS("Demo data seeded successfully."))
        self.stdout.write("InstitutionProfile created/skipped: %s / %s" % (summary["institution_profile"]["created"], summary["institution_profile"]["skipped"]))
        self.stdout.write("Offices new: %s" % summary["offices"]["created"])
        self.stdout.write("Departments new: %s" % summary["departments"]["created"])
        self.stdout.write("Positions new: %s" % summary["positions"]["created"])
        self.stdout.write("Employees new: %s" % summary["employees"]["created"])
        self.stdout.write("EmployeeAssignments new: %s" % summary["employee_assignments"]["created"])
        self.stdout.write("Shifts new: %s" % summary["shifts"]["created"])
        self.stdout.write("AttendanceDevices new: %s" % summary["attendance_devices"]["created"])
        self.stdout.write("AttendancePolicies new: %s" % summary["attendance_policies"]["created"])
        self.stdout.write("EmployeeSchedules new: %s" % summary["employee_schedules"]["created"])
        self.stdout.write("AttendanceRecords new: %s" % summary["attendance_records"]["created"])
        self.stdout.write("AttendanceEvents demo new: %s" % summary["attendance_events"]["created"])
        self.stdout.write("Holidays new: %s" % summary["holidays"]["created"])
        self.stdout.write("LeaveTypes new: %s" % summary["leave_types"]["created"])
        self.stdout.write("EmployeeRequests new: %s" % summary["employee_requests"]["created"])
        self.stdout.write("RequestApprovals new: %s" % summary["request_approvals"]["created"])
        self.stdout.write("Notifications new: %s" % summary["notifications"]["created"])
        self.stdout.write("AuditLogs demo new: %s" % summary["audit_logs"]["created"])
        self.stdout.write("User dependencies demo new: %s" % summary["demo_users"]["created"])
        self.stdout.write("FaceTemplates not created.")
        self.stdout.write("FaceVerificationLogs not created.")
        self.stdout.write("Data DEMO bersifat sintetis dan hanya untuk pengujian lokal. Jangan gunakan sebagai data operasional/absensi resmi.")

    def _make_aware_datetime(self, day, time_value):
        return timezone.make_aware(datetime.combine(day, time_value), timezone.get_current_timezone())
