from django.db import migrations

from constance import config


def seed_ir_id_sequence(apps, schema_editor):
    IrIdSequence = apps.get_model("distributors", "IrIdSequence")
    # constance's config proxy hits its own storage (a separate system from
    # Django's migration graph), so this is safe to read here regardless of
    # migration order -- same as reading config.X anywhere else at runtime.
    IrIdSequence.objects.get_or_create(
        pk=1, defaults={"next_number": max(config.IR_ID_STARTING_NUMBER, 1)}
    )


def remove_ir_id_sequence(apps, schema_editor):
    IrIdSequence = apps.get_model("distributors", "IrIdSequence")
    IrIdSequence.objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("distributors", "0010_iridsequence_distributor_kyc_rejection_reason"),
    ]

    operations = [
        migrations.RunPython(seed_ir_id_sequence, remove_ir_id_sequence),
    ]
