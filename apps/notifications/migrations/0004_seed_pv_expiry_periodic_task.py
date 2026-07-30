# Seeds the Celery Beat schedule for Task 21d-iii's PV-expiry
# notification batch driver (apps/notifications/tasks.py::
# send_pv_expiry_notifications). Mirrors apps/orders/migrations/
# 0004_seed_auto_cancel_periodic_task.py's own simple, non-self-syncing
# shape (not apps/commissions's interval-syncing one) -- PV_EXPIRY_
# WARNING_DAYS is the warning-window *threshold* this task reads every
# run, not this task's own *run frequency*, so there is no second,
# admin-editable "how often" setting to keep this schedule in sync with
# (see the task's own docstring for the full reasoning).
#
# 24 hours: PV_EXPIRY_WARNING_DAYS defaults to 14 days, so a daily check
# gives ample notice before expiry -- this is not time-sensitive the way
# a money-moving job like Binary Bonus is.

from django.db import migrations

TASK_NAME = "send-pv-expiry-notifications"
TASK_PATH = "apps.notifications.tasks.send_pv_expiry_notifications"
INTERVAL_HOURS = 24


def seed_periodic_task(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # "hours" is hardcoded, not IntervalSchedule.HOURS -- apps.get_model()
    # returns a historical model reconstructed from migration state, which
    # doesn't carry the real model class's constants.
    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=INTERVAL_HOURS, period="hours"
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
        ("notifications", "0003_notificationcyclerun_notificationcyclefailure"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(seed_periodic_task, remove_periodic_task),
    ]
