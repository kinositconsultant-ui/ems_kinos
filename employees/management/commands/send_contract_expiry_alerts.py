"""Send contract-expiry in-app notifications (daily task).

Usage:
    python manage.py send_contract_expiry_alerts
    python manage.py send_contract_expiry_alerts --dry-run

Safe to run repeatedly: each (employee, contract end date, recipient, alert type)
is delivered at most once, enforced by a database unique constraint.
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from employees.services.contract_alerts import (
    ALERT_WINDOW_DAYS,
    business_today,
    send_upcoming_contract_expiry_alerts,
)


class Command(BaseCommand):
    help = "Send in-app contract-expiry notifications for contracts ending within 0-7 days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be sent without writing any notification.",
        )
        parser.add_argument(
            "--today",
            dest="today",
            default="",
            help="Override the evaluation date (YYYY-MM-DD) in the business time zone. For testing only.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        today = business_today()
        raw = (options.get("today") or "").strip()
        if raw:
            try:
                today = date.fromisoformat(raw)
            except ValueError:
                raise CommandError(f"Invalid --today value: {raw!r}. Use YYYY-MM-DD.")

        self.stdout.write(
            f"Contract expiry check for {today} (window 0-{ALERT_WINDOW_DAYS} days, Asia/Dili)."
            + (" Dry run: nothing will be written." if dry_run else "")
        )
        result = send_upcoming_contract_expiry_alerts(today=today, dry_run=dry_run)
        self.stdout.write(self.style.SUCCESS(result.summary()))
        for employee_id in result.employees_with_invalid_scope:
            self.stdout.write(self.style.WARNING(f"Invalid branch scope: {employee_id}"))
        for employee_id in result.employees_without_recipients:
            self.stdout.write(self.style.WARNING(f"No eligible recipient: {employee_id}"))
        for error in result.errors:
            self.stderr.write(self.style.ERROR(error))