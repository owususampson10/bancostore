from django.db import migrations
from django.db.models import F


def backfill_consumed_reference(apps, schema_editor):
    """Task 67. Before this, a consumed PendingRegistration's own
    payment_reference was always the one that created the account (the
    lookup was by that field alone), so it is the right value to record.
    Without the backfill every replayed webhook for an existing account
    would look like a second payment."""
    PendingRegistration = apps.get_model("distributors", "PendingRegistration")
    PendingRegistration.objects.filter(
        consumed_at__isnull=False, consumed_reference__isnull=True
    ).update(consumed_reference=F("payment_reference"))


class Migration(migrations.Migration):

    dependencies = [
        ("distributors", "0018_task67_payment_issues"),
    ]

    operations = [
        migrations.RunPython(backfill_consumed_reference, migrations.RunPython.noop),
    ]
