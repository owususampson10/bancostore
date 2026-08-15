from django.db import migrations

# Task 48c. Real current wording, copied verbatim from the hardcoded
# f-strings this migration's own send sites still use today (the actual
# code wiring lands in this same sub-task) -- {{name}} placeholders map
# 1:1 to apps.notifications.template_registry.PLACEHOLDERS_BY_KEY.
_SEED_TEMPLATES = [
    (
        "binary_bonus_credited",
        "",
        "You earned GHS {{amount}} Binary Bonus!",
    ),
    (
        "downline_joined",
        "",
        "{{name}} just joined your team!",
    ),
    (
        "direct_referral_bonus_credited_sms",
        "",
        "You've earned GHS {{amount}} Direct Referral Bonus from "
        "{{referred_name}}'s purchase. Check your Bancostore wallet!",
    ),
    (
        "direct_referral_bonus_credited_inapp",
        "",
        "You earned GHS {{amount}} Direct Referral Bonus from "
        "{{referred_name}}'s purchase!",
    ),
    (
        "pv_expiring",
        "",
        "You have {{pv}} PV expiring around {{expiry_date}} -- use it "
        "before it's lost!",
    ),
    (
        "order_status_update_sms",
        "",
        "Your Bancostore order {{reference}} is now {{status}}.",
    ),
    (
        "order_status_update_email",
        "Your Bancostore order is {{status}}",
        "Your order {{reference}} is now {{status}}.",
    ),
]


def seed_more_notification_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    for key, subject, body in _SEED_TEMPLATES:
        NotificationTemplate.objects.get_or_create(
            key=key, defaults={"subject": subject, "body": body}
        )


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0006_seed_notification_templates"),
    ]

    operations = [
        migrations.RunPython(
            seed_more_notification_templates, migrations.RunPython.noop
        ),
    ]
