"""Read-only audit of contract-generated schedules against assignment/contract coverage.

Detects future contract-generated schedules that fall outside the effective coverage
of the assignment that owns them (assignment start/end, bounded by the employee's
contract end date), and reports protection conditions and a proposed action.

This command is ALWAYS read-only in this task: it never creates, cancels, edits or
deletes any row. ``--dry-run`` is accepted for explicitness and is the default; there
is no write/repair mode.

Usage:
    python manage.py audit_contract_schedules
    python manage.py audit_contract_schedules --dry-run
    python manage.py audit_contract_schedules --employee EMP-001
"""
from datetime import date

from django.core.management.base import BaseCommand
from django.utils import timezone

from employees.models import Employee, EmployeeAssignment


class Command(BaseCommand):
    help = (
        "Read-only audit of contract-generated schedules against assignment and "
        "contract coverage. Reports out-of-coverage schedules, protection conditions "
        "and a proposed action. Never modifies data."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--employee",
            dest="employee",
            default=None,
            help="Limit the audit to a single employee_id (e.g. EMP-001).",
        )
        parser.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            default=True,
            help="Read-only preview (default; accepted for explicitness).",
        )

    def handle(self, *args, **options):
        today = timezone.localdate()
        employee_id = (options.get("employee") or "").strip()

        employees = Employee.objects.all()
        if employee_id:
            employees = employees.filter(employee_id=employee_id)
        employees = employees.order_by("employee_id")

        self.stdout.write(
            f"Contract schedule coverage audit — business date {today} (read-only)\n"
            + "=" * 70
        )

        total_assignments_flagged = 0
        total_schedules_out = 0
        total_protected = 0
        employees_with_findings = 0

        for employee in employees:
            contract_start = employee.contract_start_date
            contract_end = employee.contract_end_date
            assignments = list(
                employee.assignments.select_related("position__office").order_by("start_date")
            )
            if not assignments and not employee.contract_end_date:
                continue

            assignment_findings = []
            for a in assignments:
                reasons = []
                if contract_start and a.start_date and a.start_date < contract_start:
                    reasons.append(
                        f"starts {a.start_date} before contract start {contract_start} "
                        "(historical continuity — review, do not auto-edit)"
                    )
                if contract_end and a.end_date and a.end_date > contract_end:
                    reasons.append(
                        f"ends {a.end_date} after contract end {contract_end} (inconsistent)"
                    )
                if a.end_date is None and contract_end is not None:
                    reasons.append(
                        f"open-ended; effective end bounded to contract end {contract_end}"
                    )
                if reasons:
                    assignment_findings.append((a, reasons))
                    total_assignments_flagged += 1

            # Contract-generated schedules owned by this employee.
            schedule_rows = self._schedule_findings(employee, today)
            out_rows = [row for row in schedule_rows if row["out_of_coverage"]]
            protected_rows = [row for row in out_rows if row["protected"]]
            total_schedules_out += len(out_rows)
            total_protected += len(protected_rows)

            if not assignment_findings and not out_rows:
                continue
            employees_with_findings += 1

            self.stdout.write(f"\nEmployee {employee.employee_id} — {employee.full_name}")
            self.stdout.write(
                f"  contract: {contract_start} .. {contract_end}  "
                f"(derived status: {employee.get_derived_contract_status()})"
            )

            if assignment_findings:
                self.stdout.write("  Assignments needing review:")
                for a, reasons in assignment_findings:
                    eff = a.effective_end_date
                    self.stdout.write(
                        f"    - assignment #{a.pk} [{a.start_date} .. {a.end_date or 'open'}] "
                        f"effective_end={eff} primary={a.is_primary}"
                    )
                    for reason in reasons:
                        self.stdout.write(f"        * {reason}")

            if out_rows:
                self.stdout.write(
                    f"  Out-of-coverage contract schedules ({len(out_rows)}, "
                    f"{len(protected_rows)} protected):"
                )
                for row in out_rows:
                    protection = "PROTECTED" if row["protected"] else "CANCELLABLE"
                    self.stdout.write(
                        f"    - schedule #{row['pk']} work_date={row['work_date']} "
                        f"status={row['status']} source={row['source']} "
                        f"assignment=#{row['assignment_pk']} [{row['assignment']}..{row['assignment_end']}] "
                        f"-> {protection}"
                    )
                    self.stdout.write(f"        reason: {row['reason']}")
                    if row["protected"]:
                        self.stdout.write(f"        protected by: {row['protection']}")
                    self.stdout.write(
                        f"        proposed action: "
                        f"{'report only (do not modify)' if row['protected'] else 'cancel future schedule (preview only)'}"
                    )

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write(
            f"Summary: {employees_with_findings} employee(s) with findings, "
            f"{total_assignments_flagged} assignment(s) flagged, "
            f"{total_schedules_out} out-of-coverage contract schedule(s) "
            f"({total_protected} protected)."
        )
        if employee_id and employees_with_findings == 0:
            self.stdout.write(f"No findings for employee {employee_id}.")
        self.stdout.write(
            "No data was modified. This command is read-only (no write/repair mode)."
        )

    def _schedule_findings(self, employee, today):
        """Return contract-schedule rows with coverage/protection details."""
        from attendance.models import EmployeeSchedule

        rows = []
        queryset = (
            EmployeeSchedule.objects.filter(
                employee_assignment__employee=employee,
                schedule_source=EmployeeSchedule.ScheduleSource.CONTRACT,
                work_date__gte=today,
            )
            .select_related("employee_assignment", "attendance_record")
            .order_by("work_date", "scheduled_start_at")
        )
        for schedule in queryset:
            a = schedule.employee_assignment
            eff_end = a.effective_end_date
            contract_end = employee.contract_end_date

            reason = None
            if a.start_date and schedule.work_date < a.start_date:
                reason = f"before assignment start ({a.start_date})"
            elif eff_end is not None and schedule.work_date > eff_end:
                if contract_end is not None and schedule.work_date > contract_end:
                    reason = (
                        f"after contract end ({contract_end}); assignment effective_end={eff_end}"
                    )
                else:
                    reason = f"after assignment effective end ({eff_end})"

            attended = hasattr(schedule, "attendance_record")
            manual = bool(schedule.notes and schedule.notes.strip().lower() == "manual")
            overtime = (schedule.approved_overtime_minutes or 0) > 0
            holiday_cancelled = schedule.cancelled_by_holiday_id is not None
            protected = attended or manual or overtime or holiday_cancelled

            protection = []
            if attended:
                protection.append("has attendance record")
            if manual:
                protection.append("manual notes")
            if overtime:
                protection.append("approved overtime")
            if holiday_cancelled:
                protection.append("cancelled by holiday (do not reactivate)")

            rows.append(
                {
                    "pk": schedule.pk,
                    "work_date": schedule.work_date,
                    "status": schedule.status,
                    "source": schedule.schedule_source,
                    "assignment_pk": a.pk,
                    "assignment": a.start_date,
                    "assignment_end": a.end_date,
                    "out_of_coverage": reason is not None,
                    "reason": reason or "within coverage",
                    "protected": protected,
                    "protection": "; ".join(protection),
                }
            )
        return rows