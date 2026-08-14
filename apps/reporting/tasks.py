import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from celery import shared_task
from constance import config
from django_celery_beat.models import IntervalSchedule

from bancostore.celery_beat import sync_periodic_task_interval

from .services import compute_daily_rollup

logger = logging.getLogger(__name__)

REPORT_ROLLUP_TASK_NAME = "compute-report-rollup"
REPORT_ROLLUP_LOCK_KEY = "reporting:report_rollup_lock"
# A single day's aggregate computation (a handful of GROUP BY queries),
# not a per-entity loop -- generous but far shorter than the commission
# tasks' own multi-minute budgets (apps/commissions/tasks.py), which
# exist to cover cumulative per-distributor iteration time this task has
# no equivalent of.
REPORT_ROLLUP_LOCK_TIMEOUT_SECONDS = 10 * 60


def _sync_report_rollup_interval():
    sync_periodic_task_interval(
        task_name=REPORT_ROLLUP_TASK_NAME,
        setting_name="REPORT_ROLLUP_INTERVAL_DAYS",
        desired_value=config.REPORT_ROLLUP_INTERVAL_DAYS,
        period=IntervalSchedule.DAYS,
        min_value=1,
    )


@shared_task
def compute_yesterdays_rollup():
    """Task 46b (ADR-0010). Celery Beat driver, triggered every
    REPORT_ROLLUP_INTERVAL_DAYS: computes YESTERDAY's rollup (relative to
    "now" at trigger time), never "today" -- today's orders are still
    actively arriving and changing status, so a genuinely complete day's
    numbers only exist for a day that has fully elapsed. A day with no
    rollup row yet (today, or a day this job hasn't reached) simply has
    no report data yet -- there is no live-query fallback (a deliberate
    ADR-0010 decision: reports always read rollup rows, never scan
    Order/OrderItem directly, so query cost never scales with total
    order-table size regardless of the requested date range).

    A separate, on-demand path exists for recomputing an arbitrary past
    date (a missed/failed night, or correcting a rollup after the fact):
    `python manage.py backfill_report_rollup <date>`, calling the same
    compute_daily_rollup this task calls -- not duplicated here.

    No per-entity loop (unlike calculate_binary_bonus/
    calculate_matching_bonus in apps/commissions/tasks.py), so this
    doesn't reuse their generic `_run_commission_cycle` driver -- that
    shape is built around per-distributor iteration (evaluated/paid/
    failed counts, a per-entity Failure sub-table) this single-day
    aggregate computation has no equivalent of. What IS reused:
    sync_periodic_task_interval (bancostore/celery_beat.py, extracted
    from that same module once this task needed the identical
    interval-schedule-sync behavior) and the overlap-prevention lock
    pattern (a single cache.add, not per-iteration-renewed -- this job's
    own bounded runtime, unlike a per-distributor loop's unbounded
    cumulative time, doesn't need mid-run renewal)."""
    if not cache.add(REPORT_ROLLUP_LOCK_KEY, "1", REPORT_ROLLUP_LOCK_TIMEOUT_SECONDS):
        logger.warning(
            "compute_yesterdays_rollup: previous run still in progress "
            "(lock held) -- skipping this trigger rather than running a "
            "second concurrent computation."
        )
        return {"skipped": True, "reason": "previous run still in progress"}

    try:
        try:
            _sync_report_rollup_interval()
        except Exception:
            logger.exception(
                "compute_yesterdays_rollup: failed to sync the PeriodicTask "
                "interval -- continuing with this run regardless, since a "
                "stale schedule is a lesser problem than skipping the "
                "rollup."
            )

        target_date = (timezone.now() - timedelta(days=1)).date()
        compute_daily_rollup(target_date)
        logger.info("compute_yesterdays_rollup: rollup_date=%s succeeded", target_date)
        return {"rollup_date": target_date.isoformat(), "succeeded": True}
    finally:
        cache.delete(REPORT_ROLLUP_LOCK_KEY)
