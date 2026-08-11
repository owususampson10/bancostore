from django.conf import settings
from django.db import migrations


def set_production_domain(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    Site.objects.update_or_create(
        pk=settings.SITE_ID,
        defaults={"domain": "bancostore.com", "name": "Bancostore"},
    )


def restore_default_domain(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    Site.objects.filter(pk=settings.SITE_ID).update(
        domain="example.com", name="example.com"
    )


class Migration(migrations.Migration):
    # django.contrib.sites' Site model is only ever referenced by Task 37b's
    # sitemap framework -- confirmed unused everywhere else in this codebase
    # by the pre-planning audit -- so its default "example.com" row (visible
    # in a real sitemap.xml request until this migration ran) needed setting
    # to the real production domain (already live since Task 24) for the
    # sitemap's absolute URLs to be correct anywhere but a request literally
    # made to bancostore.com itself.
    dependencies = [
        ("sites", "0002_alter_domain_unique"),
        ("pages", "0002_alter_socialmedialink_platform"),
    ]

    operations = [
        migrations.RunPython(set_production_domain, restore_default_domain),
    ]
