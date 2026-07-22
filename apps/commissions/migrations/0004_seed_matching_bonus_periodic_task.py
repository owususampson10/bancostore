# Seeds the Celery Beat schedule for Task 14's Matching Bonus batch driver
# (apps/commissions/tasks.py::calculate_matching_bonus). Mirrors
# 0001_seed_binary_bonus_periodic_task.py exactly, at a 7-day interval
# instead of 10 minutes -- the task itself re-syncs IntervalSchedule.every
# from the live MATCHING_BONUS_INTERVAL_DAYS constance value on every run
# (see _sync_matching_bonus_interval in tasks.py), same reasoning as Binary
# Bonus's.

from django.db import migrations

TASK_NAME = "calculate-matching-bonus"
TASK_PATH = "apps.commissions.tasks.calculate_matching_bonus"
DEFAULT_INTERVAL_DAYS = 7


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
        ("commissions", "0003_generalize_cycle_audit_models"),
    ]

    operations = [
        migrations.RunPython(seed_periodic_task, remove_periodic_task),
    ]
