from django.db import migrations


def backfill_checkouts(apps, schema_editor):
    """Task 68b. Every distributor with a current starter-pack reference gets
    a checkout row for it, so a payment landing on it after this deploy
    resolves through the new history rather than falling through as
    unmatched. Older, already-overwritten references are unrecoverable --
    they were never stored anywhere."""
    Distributor = apps.get_model("distributors", "Distributor")
    StarterPackCheckout = apps.get_model("distributors", "StarterPackCheckout")

    rows = [
        StarterPackCheckout(
            distributor_id=distributor.pk,
            reference=distributor.starter_pack_payment_reference,
            amount_pesewas=distributor.starter_pack_price_pesewas or 0,
            choice=distributor.starter_pack_choice or "",
        )
        for distributor in Distributor.objects.exclude(
            starter_pack_payment_reference__isnull=True
        ).exclude(starter_pack_payment_reference="")
    ]
    StarterPackCheckout.objects.bulk_create(rows, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ("distributors", "0024_starter_pack_checkout"),
    ]

    operations = [
        migrations.RunPython(backfill_checkouts, migrations.RunPython.noop),
    ]
