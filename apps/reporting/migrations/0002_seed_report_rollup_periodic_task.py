# Seeds the Celery Beat schedule for Task 46b's report-rollup batch driver
# (apps/reporting/tasks.py::compute_yesterdays_rollup). Mirrors the pattern
# in apps/commissions/migrations/0001_seed_binary_bonus_periodic_task.py.
#
# The interval hardcoded here (1 day) matches REPORT_ROLLUP_INTERVAL_DAYS'
# constance default -- but the task itself re-syncs IntervalSchedule.every
# from the live constance value on every run
# (bancostore.celery_beat.sync_periodic_task_interval), so this schedule
# doesn't stay static after seeding.

from django.db import migrations

TASK_NAME = "compute-report-rollup"
TASK_PATH = "apps.reporting.tasks.compute_yesterdays_rollup"
DEFAULT_INTERVAL_DAYS = 1


def seed_periodic_task(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # "days" is hardcoded, not IntervalSchedule.DAYS -- apps.get_model()
    # returns a historical model reconstructed from migration state, which
    # doesn't carry the real model class's constants.
    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=DEFAULT_INTERVAL_DAYS, period="days"
    )
    PeriodicTask.objects.get_or_create(
        name=TASK_NAME,
        defaults={"interval": schedule, "task": TASK_PATH},
    )


def remove_periodic_task(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("reporting", "0001_initial"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(seed_periodic_task, remove_periodic_task),
    ]
