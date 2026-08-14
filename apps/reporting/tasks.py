import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from celery import shared_task
from constance import config
from django_celery_beat.models import IntervalSchedule

from bancostore.celery_beat import sync_periodic_task_interval

from .models import DailyOrderRollup
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
# How far back this task looks for a missing DailyOrderRollup row, every
# run. Bounded (never "since the dawn of time") so cost stays flat
# regardless of how long this project has been live -- larger than
# MAX_ROLLUP_DAYS_PER_RUN so there's real headroom to notice and recover
# from a multi-week gap (a stuck Celery Beat, a long-unnoticed failure)
# without needing any operator intervention beyond letting it run.
GAP_LOOKBACK_DAYS = 45
# A CodeRabbit-caught real gap in an earlier version of this task: it
# used to resume from ReportRollupRun's MAX(succeeded rollup_date) --
# corruptible by an out-of-band `backfill_report_rollup <date>` run for a
# date NEWER than an unresolved automated failure, which would silently
# skip the older gap forever (backfill_report_rollup calls the exact
# same compute_daily_rollup and writes an indistinguishable
# ReportRollupRun row, so there was no way to tell "resumed past a real
# gap" from "caught up cleanly"). Fixed by dropping that watermark
# entirely: this task now checks DailyOrderRollup's own rows directly
# for what's actually missing within GAP_LOOKBACK_DAYS, so there is no
# cursor left to corrupt -- a manual backfill for any date just fills in
# that one date, nothing more.
#
# Capped, not unbounded, so a very long gap (e.g. Celery Beat down for
# months, or this task's very first run ever against a live system) can't
# make a single run blow past REPORT_ROLLUP_LOCK_TIMEOUT_SECONDS; the
# remainder self-heals over subsequent scheduled runs.
MAX_ROLLUP_DAYS_PER_RUN = 30


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
    REPORT_ROLLUP_INTERVAL_DAYS: computes every calendar day within
    GAP_LOOKBACK_DAYS of YESTERDAY (relative to "now" at trigger time)
    that DailyOrderRollup has no row for yet, never "today" -- today's
    orders are still actively arriving and changing status, so a
    genuinely complete day's numbers only exist for a day that has fully
    elapsed. Stateless by design: which dates are missing is read fresh
    from DailyOrderRollup itself every run, not from any remembered
    cursor -- see GAP_LOOKBACK_DAYS' own comment for why a cursor-based
    design was tried and dropped. This is what makes an admin-configured
    REPORT_ROLLUP_INTERVAL_DAYS > 1, a missed trigger, or an out-of-band
    manual backfill all self-healing in the same simple way: every
    missing day within the window gets its own rollup row, most-recent-
    missing-first (so a long-past gap can never starve "yesterday" of
    ever being computed), capped at MAX_ROLLUP_DAYS_PER_RUN so one run
    can't blow past REPORT_ROLLUP_LOCK_TIMEOUT_SECONDS. A day with no
    rollup row yet (today, or a day this job hasn't reached) simply has
    no report data yet -- there is no live-query fallback (a deliberate
    ADR-0010 decision: reports always read rollup rows, never scan
    Order/OrderItem directly, so query cost never scales with total
    order-table size regardless of the requested date range).

    A separate, on-demand path exists for recomputing an arbitrary past
    date OUTSIDE GAP_LOOKBACK_DAYS (correcting a very old rollup after
    the fact): `python manage.py backfill_report_rollup <date>`, calling
    the same compute_daily_rollup this task calls -- not duplicated
    here.

    No per-entity loop (unlike calculate_binary_bonus/
    calculate_matching_bonus in apps/commissions/tasks.py), so this
    doesn't reuse their generic `_run_commission_cycle` driver -- that
    shape is built around per-distributor iteration (evaluated/paid/
    failed counts, a per-entity Failure sub-table) this multi-day
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
        window_start = target_date - timedelta(days=GAP_LOOKBACK_DAYS - 1)
        already_rolled_up = set(
            DailyOrderRollup.objects.filter(
                date__gte=window_start, date__lte=target_date
            ).values_list("date", flat=True)
        )

        missing_dates_newest_first = []
        current = target_date
        while current >= window_start:
            if current not in already_rolled_up:
                missing_dates_newest_first.append(current)
            current -= timedelta(days=1)

        truncated = len(missing_dates_newest_first) > MAX_ROLLUP_DAYS_PER_RUN
        if truncated:
            remaining = len(missing_dates_newest_first) - MAX_ROLLUP_DAYS_PER_RUN
            logger.warning(
                "compute_yesterdays_rollup: %d missing day(s) within the "
                "%d-day lookback window exceeds the %d-day-per-run cap -- "
                "processing the most recent %d now, the remaining %d "
                "(older) will catch up on subsequent scheduled runs.",
                len(missing_dates_newest_first),
                GAP_LOOKBACK_DAYS,
                MAX_ROLLUP_DAYS_PER_RUN,
                MAX_ROLLUP_DAYS_PER_RUN,
                remaining,
            )

        dates_to_process = sorted(missing_dates_newest_first[:MAX_ROLLUP_DAYS_PER_RUN])

        for rollup_date in dates_to_process:
            compute_daily_rollup(rollup_date)
            logger.info(
                "compute_yesterdays_rollup: rollup_date=%s succeeded", rollup_date
            )

        return {
            "rollup_dates": [d.isoformat() for d in dates_to_process],
            "succeeded": True,
            "truncated": truncated,
        }
    finally:
        cache.delete(REPORT_ROLLUP_LOCK_KEY)
