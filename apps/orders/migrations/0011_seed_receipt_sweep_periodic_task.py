# Seeds the Celery Beat schedule for resend_missing_order_receipts (Task 62
# follow-up, CodeRabbit PR #93). Same simple, non-self-syncing shape as
# 0004_seed_auto_cancel_periodic_task.py.
#
# 15 minutes: the sweep only acts on receipts at least 30 minutes overdue,
# so running more often would find nothing new.

from django.db import migrations

TASK_NAME = "resend-missing-order-receipts"
TASK_PATH = "apps.orders.tasks.resend_missing_order_receipts"
INTERVAL_MINUTES = 15


def seed_periodic_task(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # "minutes" is hardcoded, not IntervalSchedule.MINUTES -- see 0004.
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
        ("orders", "0010_backfill_receipt_email_sent_at"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(seed_periodic_task, remove_periodic_task),
    ]
