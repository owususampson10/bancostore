"""Shared Celery Beat scheduling helper. Extracted from
apps/commissions/tasks.py (Task 46b, ADR-0010) once a second app
(apps.reporting) needed the identical interval-schedule-sync behavior --
the function itself was already fully generic (task_name/setting_name/
desired_value/period/min_value parameters, no commission-specific logic
inside it), so a cross-app import of what had been a leading-underscore
"private" name was the wrong long-term shape. Matches this codebase's own
repeated "extract shared, don't duplicate" convention (Task 19a's
reverse_ancestor_pv, Task 33's walk_sponsor_chain_downline_ids, Task 45's
bancostore/exports.py) and this file's own sibling
bancostore/concurrency.py's precedent for cross-cutting infrastructure
living at the project root, not inside any one app.
"""

import logging

from django_celery_beat.models import IntervalSchedule, PeriodicTask

logger = logging.getLogger(__name__)


def sync_periodic_task_interval(
    *, task_name, setting_name, desired_value, period, min_value
):
    """Best-effort: keeps a real Celery Beat schedule (a django_celery_beat
    PeriodicTask/IntervalSchedule pair, migration-seeded) in step with an
    admin-editable constance interval setting. Without this, that setting
    would be purely decorative -- constance and django_celery_beat are two
    independent DB-backed config stores that don't know about each other,
    and these interval settings sit in the same admin fieldset as rates/
    caps that ARE live, so an admin has every reason to expect this one is
    too. django_celery_beat's DatabaseScheduler polls a change-timestamp
    every `beat_max_loop_interval` (default 5s, unset in this project) via
    PeriodicTasks.last_change(), so updating the real model here (not a
    migration's historical apps.get_model() version) takes effect within
    seconds, no beat restart needed.
    (Source: django_celery_beat/schedulers.py's DatabaseScheduler.schedule_changed
    and DEFAULT_MAX_INTERVAL, read from the installed package 2026-07-21.)

    Originally `apps.commissions.tasks._sync_periodic_task_interval`
    (2026-07-22, generic across every commission task in that module once
    Task 14's Matching Bonus needed the exact same shape Task 13's Binary
    Bonus already had, differing only in which task, which constance key,
    which schedule period unit, and which floor). Moved here (2026-08-14)
    once Task 46b's report-rollup job needed the identical behavior from a
    completely different app -- see this module's own docstring.

    Silently returns if no PeriodicTask row exists yet (e.g. local dev
    before the seeding migration ran) -- this is a convenience sync, not
    something the calling job itself should ever fail over.

    Only ever touches the `interval` schedule type. django_celery_beat's
    PeriodicTask supports four mutually-exclusive schedule types (interval/
    crontab/solar/clocked -- confirmed against the installed package's
    models.py). If an admin has repointed a task at one of the other three
    via django_celery_beat's own admin (e.g. a crontab restricting it to
    business hours), that's a deliberate choice made through a different,
    equally legitimate admin screen -- overwriting it back to an interval
    schedule every cycle would silently fight the admin. Skip and warn
    instead."""
    try:
        task = PeriodicTask.objects.select_related("interval").get(name=task_name)
    except PeriodicTask.DoesNotExist:
        return

    if task.crontab_id or task.solar_id or task.clocked_id:
        logger.warning(
            "%s: PeriodicTask %r is scheduled via a crontab/solar/clocked "
            "schedule, not an interval -- leaving it alone rather than "
            "overwriting it with %s.",
            task_name,
            task_name,
            setting_name,
        )
        return

    # Floor, not just a > 0 check -- these interval settings have no
    # CONSTANCE_ADDITIONAL_FIELDS bounds beyond min_value (a fat-fingered
    # tiny value is one admin form submission away). These tasks share the
    # default Celery queue/worker pool with everything else in this
    # project (no CELERY_TASK_ROUTES), so an unbounded-low interval is a
    # real, admin-reachable DoS vector on shared infrastructure
    # (2026-07-22 security-and-hardening review), not just a
    # self-inflicted inefficiency.
    if desired_value < min_value:
        logger.warning(
            "%s: %s=%s is below the enforced floor of %s -- leaving the "
            "current schedule (every=%s) in place rather than syncing to "
            "it.",
            task_name,
            setting_name,
            desired_value,
            min_value,
            task.interval.every if task.interval else None,
        )
        return

    current = task.interval
    if (
        current is not None
        and current.every == desired_value
        and current.period == period
    ):
        return

    # get_or_create, not an in-place update of `current` -- an IntervalSchedule
    # row can be shared by multiple PeriodicTasks (django_celery_beat dedupes
    # by (every, period)), so mutating it in place could silently reschedule
    # an unrelated task that happens to already share this exact interval.
    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=desired_value, period=period
    )
    task.interval = schedule
    task.save(update_fields=["interval"])
