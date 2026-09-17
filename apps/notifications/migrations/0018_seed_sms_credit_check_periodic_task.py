# Seeds the Celery Beat schedule for apps.notifications.tasks.check_sms_credit
# (Task 63c). Same simple, non-self-syncing shape as
# 0004_seed_pv_expiry_periodic_task.py.
#
# Hourly: credit drains only as fast as texts are sent, so hourly catches a
# fall below the warning level well before it runs out, and a failed send
# already raises the alarm immediately on its own. The balance check itself
# costs no credit.

from django.db import migrations

TASK_NAME = "check-sms-credit"
TASK_PATH = "apps.notifications.tasks.check_sms_credit"
INTERVAL_MINUTES = 60


def seed_periodic_task(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # "minutes" is hardcoded, not IntervalSchedule.MINUTES -- apps.get_model()
    # returns a historical model, which doesn't carry the real class's
    # constants.
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
        ("notifications", "0017_add_sms_credit_admin_notification"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(seed_periodic_task, remove_periodic_task),
    ]
