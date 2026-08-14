from django.db import migrations


def seed_escrow_ledger(apps, schema_editor):
    EscrowLedger = apps.get_model("compliance", "EscrowLedger")
    # Pre-seeded, never lazily created -- mirrors
    # apps.distributors.migrations.0011_seed_ir_id_sequence's own
    # "always exists" convention for a singleton sequence/ledger row. A
    # doubt-driven-development finding: get_or_create(pk=1) at first-use
    # time has a narrow but real bootstrapping race under truly
    # concurrent first callers; pre-seeding removes that window entirely.
    EscrowLedger.objects.get_or_create(pk=1)


def remove_escrow_ledger(apps, schema_editor):
    EscrowLedger = apps.get_model("compliance", "EscrowLedger")
    EscrowLedger.objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("compliance", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_escrow_ledger, remove_escrow_ledger),
    ]
