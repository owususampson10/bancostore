from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from apps.reporting.services import compute_daily_rollup


class Command(BaseCommand):
    """Task 46b (ADR-0010). The on-demand path for recomputing a
    specific past day's DailyOrderRollup/DailyProductSales rows -- for a
    missed or failed nightly compute_yesterdays_rollup run, or for
    correcting a rollup after the fact (e.g. an order in that day's
    range was cancelled/refunded after the original rollup ran; see
    DailyOrderRollup's own docstring for that accepted staleness). Calls
    the exact same compute_daily_rollup the scheduled task calls -- an
    idempotent upsert, safe to run any number of times for the same
    date."""

    help = "Recompute a specific date's sales/revenue report rollup (Task 46b)."

    def add_arguments(self, parser):
        parser.add_argument(
            "date",
            help="Date to recompute, in YYYY-MM-DD format.",
        )

    def handle(self, *args, **options):
        try:
            target_date = datetime.strptime(options["date"], "%Y-%m-%d").date()
        except ValueError:
            raise CommandError(
                f"Invalid date {options['date']!r} -- expected YYYY-MM-DD."
            )

        compute_daily_rollup(target_date)
        self.stdout.write(
            self.style.SUCCESS(f"Recomputed report rollup for {target_date}.")
        )
