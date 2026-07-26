# Seeds the Celery Beat schedule for Task 18d's auto-cancel-unpaid-orders
# batch driver (apps/orders/tasks.py::auto_cancel_unpaid_orders). Mirrors
# apps/distributors/migrations/0004_seed_cleanup_periodic_task.py's own
# simple, non-self-syncing shape (not apps/commissions's interval-syncing
# one) -- PENDING_ORDER_AUTO_CANCEL_HOURS is the age *cutoff* this task
# reads every run, not this task's own *run frequency*, so there is no
# second, admin-editable "how often" setting to keep this schedule in
# sync with (see the task's own docstring for the full reasoning).
#
# 30 minutes: frequent enough that a stale order is caught reasonably
# close to its actual cutoff, without the "every 5-10 minutes" cadence a
# money-moving job like Binary Bonus needs -- this is pure housekeeping,
# not time-sensitive in the same way.

from django.db import migrations

TASK_NAME = "auto-cancel-unpaid-orders"
TASK_PATH = "apps.orders.tasks.auto_cancel_unpaid_orders"
INTERVAL_MINUTES = 30


def seed_periodic_task(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # "minutes" is hardcoded, not IntervalSchedule.MINUTES -- apps.get_model()
    # returns a historical model reconstructed from migration state, which
    # doesn't carry the real model class's constants.
    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=INTERVAL_MINUTES, period="minutes"
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
        ("orders", "0003_ordercyclerun_ordercyclefailure"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(seed_periodic_task, remove_periodic_task),
    ]
