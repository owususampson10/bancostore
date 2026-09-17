from django.db import migrations

TASK_NAME = "reconcile-paystack-payments"
TASK_PATH = "apps.distributors.tasks.reconcile_paystack_payments"


def seed_periodic_task(apps, schema_editor):
    """Task 67. Daily, matching apps/compliance's audit cleanup seed."""
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # "days" is hardcoded, not IntervalSchedule.DAYS -- apps.get_model()
    # returns a historical model reconstructed from migration state, which
    # doesn't carry the real model class's constants.
    schedule, _ = IntervalSchedule.objects.get_or_create(every=1, period="days")
    PeriodicTask.objects.get_or_create(
        name=TASK_NAME,
        defaults={"interval": schedule, "task": TASK_PATH},
    )


def remove_periodic_task(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("distributors", "0019_backfill_consumed_reference"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(seed_periodic_task, remove_periodic_task),
    ]
