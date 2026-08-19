from django.db import migrations


def seed_root_distributor_singleton(apps, schema_editor):
    """Self-healing, not a blind default=None seed: a fresh-context
    adversarial review caught that blindly seeding distributor=None would
    leave the singleton marker unaware of any sponsor-less Distributor row
    that happens to already exist in the database this migration runs
    against (e.g. one created by hand before this feature existed, or via
    Django Admin's default, unrestricted `sponsor` field). If that ever
    happened, bootstrap_root_distributor's own guard -- which only ever
    trusts this marker, not a live query -- would have no way to notice
    and could create a second "official" root. Points the marker at the
    oldest existing sponsor-less distributor (lowest pk) if one is found,
    matching this project's own convention of preferring the row that's
    actually been there longest as the authoritative one."""
    RootDistributor = apps.get_model("distributors", "RootDistributor")
    Distributor = apps.get_model("distributors", "Distributor")

    existing_roots = list(
        Distributor.objects.filter(sponsor__isnull=True).order_by("pk")[:2]
    )
    if len(existing_roots) > 1:
        print(
            "WARNING: multiple sponsor-less Distributor rows already exist "
            "-- pointing RootDistributor at the oldest one (pk="
            f"{existing_roots[0].pk}). Investigate the others manually; "
            "this migration cannot safely resolve that ambiguity."
        )
    found_root = existing_roots[0] if existing_roots else None

    RootDistributor.objects.get_or_create(pk=1, defaults={"distributor": found_root})


def remove_root_distributor_singleton(apps, schema_editor):
    RootDistributor = apps.get_model("distributors", "RootDistributor")
    RootDistributor.objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("distributors", "0016_add_root_distributor_singleton"),
    ]

    operations = [
        migrations.RunPython(
            seed_root_distributor_singleton, remove_root_distributor_singleton
        ),
    ]
