# Seeds the Celery Beat schedule for Task 16f PR 2's Friday payout batch
# driver (apps/withdrawal/tasks.py::process_withdrawal_payouts). Mirrors
# apps/commissions/migrations/0001_seed_binary_bonus_periodic_task.py's
# pattern, but with a CrontabSchedule (a specific weekday) instead of an
# IntervalSchedule -- no prior CrontabSchedule precedent exists in this
# codebase before this migration.
#
# The schedule seeded here (Friday, 00:00) matches WITHDRAWAL_DAY's
# constance default ("friday") -- but like the interval-based tasks, this
# doesn't stay static after seeding: the task itself re-syncs the crontab
# from the live WITHDRAWAL_DAY setting on every run (see
# _sync_withdrawal_day_crontab in tasks.py).
#
# Shipping-and-launch decision (2026-07-24, doubt-driven-development plan,
# step 6): seeded enabled=True (django_celery_beat's own PeriodicTask
# default), matching Binary/Matching Bonus's own precedent -- not left to
# silently default either way. Paystack Transfers are still blocked on
# this sandbox account's tier (project_paystack_transfer_account_tier_
# blocked memory), so every row this task attempts will currently fail at
# the live Paystack call -- but it fails safely: nothing here debits a
# wallet twice (the debit already happened at approval time, Task 16d),
# a failed row is isolated and recorded in WithdrawalCycleFailure rather
# than corrupting the cycle, and claim_for_payout's pinned reference means
# a retry next Friday resumes cleanly rather than re-attempting from
# scratch.

from django.db import migrations

TASK_NAME = "process-withdrawal-payouts"
TASK_PATH = "apps.withdrawal.tasks.process_withdrawal_payouts"


def seed_periodic_task(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="0",
        day_of_month="*",
        month_of_year="*",
        day_of_week="5",  # Friday -- 0/7=Sunday, 1=Monday, ... per django_celery_beat
    )
    PeriodicTask.objects.get_or_create(
        name=TASK_NAME,
        defaults={"crontab": schedule, "task": TASK_PATH},
    )


def remove_periodic_task(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("withdrawal", "0005_withdrawalcyclefailure_withdrawalcyclerun_and_more"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(seed_periodic_task, remove_periodic_task),
    ]
