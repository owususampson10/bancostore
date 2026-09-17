# Marks every order already confirmed before receipt tracking existed as
# "receipt sent", so resend_missing_order_receipts never emails those
# customers a second receipt after deploy.
#
# Safe to mark ALL of them: until this release, production sent the receipt
# email inline during confirmation (Task 62's Celery queueing ships in the
# same release as this migration), so no queued receipt job can be waiting
# at deploy time for this backfill to swallow. A design review raised that
# hazard; it applies only if this migration is ever run after queueing is
# already live.
#
# One bulk UPDATE, not a per-row loop, so it holds up on a large table.

from django.db import migrations
from django.db.models import F


def backfill_receipt_email_sent_at(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    Order.objects.filter(
        confirmed_at__isnull=False, receipt_email_sent_at__isnull=True
    ).update(receipt_email_sent_at=F("confirmed_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0009_order_receipt_email_tracking"),
    ]

    operations = [
        migrations.RunPython(backfill_receipt_email_sent_at, migrations.RunPython.noop),
    ]
