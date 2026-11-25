from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone
from datetime import date, timedelta

from attendance.services.workflow import mark_incomplete_sessions


class Command(BaseCommand):
    help = (
        "Reconcile attendance sessions whose schedule window has ended: sessions that checked "
        "in but missed required events are flagged Incomplete (pending HR review); published "
        "schedules with no check-in at all are marked Absent. Never creates events, never "
        "invents timestamps, and never overrides an approved leave, holiday or rest day."
    )

    def add_arguments(self, parser: CommandParser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview how many sessions would be flagged without writing to the database.",
        )
        parser.add_argument(
            "--date",
            help="Reconcile a single work date (YYYY-MM-DD). Defaults to today.",
        )
        parser.add_argument(
            "--start-date",
            help="First work date (inclusive) for catch-up reconciliation (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--end-date",
            help="Last work date (inclusive) for catch-up reconciliation (YYYY-MM-DD).",
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        date_str = options.get("date")
        start_date_str = options.get("start_date")
        end_date_str = options.get("end_date")

        start_date = end_date = None
        if date_str:
            start_date = end_date = date.fromisoformat(date_str)
        if start_date_str:
            start_date = date.fromisoformat(start_date_str)
        if end_date_str:
            end_date = date.fromisoformat(end_date_str)

        now = timezone.now()
        flagged = mark_incomplete_sessions(
            now=now,
            dry_run=dry_run,
            start_date=start_date,
            end_date=end_date,
        )
        verb = "would be" if dry_run else "were"
        kind = "session(s)" if flagged != 1 else "session"
        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"{flagged} {kind} {verb} flagged (dry run, no writes).")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"{flagged} {kind} {verb} flagged."))
