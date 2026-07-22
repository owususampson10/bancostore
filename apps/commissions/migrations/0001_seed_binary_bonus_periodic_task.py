# Seeds the Celery Beat schedule for Task 13's Binary Bonus batch driver
# (apps/commissions/tasks.py::calculate_binary_bonus). Mirrors the pattern
# in apps/distributors/migrations/0004_seed_cleanup_periodic_task.py.
#
# The interval hardcoded here (10 minutes) matches BINARY_BONUS_INTERVAL_
# MINUTES' constance default -- but unlike the cleanup task this precedent
# is based on, this schedule doesn't stay static after seeding: the task
# itself re-syncs IntervalSchedule.every from the live constance value on
# every run (see _sync_periodic_task_interval in tasks.py), since that
# setting sits in the same admin-editable fieldset as commission rates
# that already take effect live, and silently ignoring it would be a real
# operator footgun on a job that moves real money.

from django.db import migrations

TASK_NAME = "calculate-binary-bonus"
TASK_PATH = "apps.commissions.tasks.calculate_binary_bonus"
DEFAULT_INTERVAL_MINUTES = 10


def seed_periodic_task(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # "minutes" is hardcoded, not IntervalSchedule.MINUTES -- apps.get_model()
    # returns a historical model reconstructed from migration state, which
    # doesn't carry the real model class's constants.
    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=DEFAULT_INTERVAL_MINUTES, period="minutes"
    )
    PeriodicTask.objects.get_or_create(
        name=TASK_NAME,
        defaults={"interval": schedule, "task": TASK_PATH},
    )


def remove_periodic_task(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(seed_periodic_task, remove_periodic_task),
    ]
