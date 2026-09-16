from django.db import migrations

# Task 61a/61b. The admin-facing new-order alerts.
#
# Both are seeded with the same wording the hardcoded fallbacks in
# apps/orders/admin_alerts.py use, so the fallback and the live row can
# never silently disagree about what an unedited alert looks like.
#
# The email carries everything needed to act on the order without logging
# in -- reference, total, who to contact and how, what to pack, where it
# goes. An alert that only says "you have an order" would still force a
# login to learn anything, which is the problem this feature exists to
# solve.
#
# The SMS is deliberately short. mNotify bills per 160 characters and
# this fires on every confirmed order, so an item list here would
# multiply the messaging cost of the whole store. Reference, total and
# customer name are what fits and what identifies the order.
_ADMIN_NEW_ORDER_EMAIL_SUBJECT = "New order {{reference}} -- GHS {{total}}"

_ADMIN_NEW_ORDER_EMAIL_BODY = """A customer has paid for an order.

Order:     {{reference}}
Date:      {{order_date}}
Total:     GHS {{total}}

CUSTOMER
{{customer_name}}
{{customer_phone}}
{{customer_email}}

ITEMS
{{items}}

DELIVERY
{{delivery_details}}
"""

_ADMIN_NEW_ORDER_SMS = (
    "New Bancostore order {{reference}} -- GHS {{total}} from {{customer_name}}."
)

_SEED_TEMPLATES = [
    (
        "admin_new_order_email",
        _ADMIN_NEW_ORDER_EMAIL_SUBJECT,
        _ADMIN_NEW_ORDER_EMAIL_BODY,
    ),
    ("admin_new_order_sms", "", _ADMIN_NEW_ORDER_SMS),
]


def seed_admin_order_alert_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    for key, subject, body in _SEED_TEMPLATES:
        # get_or_create, matching migrations 0007 and 0010: re-running
        # must never overwrite wording an admin has since edited.
        NotificationTemplate.objects.get_or_create(
            key=key, defaults={"subject": subject, "body": body}
        )


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0011_add_admin_order_alert_templates"),
    ]

    operations = [
        migrations.RunPython(
            seed_admin_order_alert_templates, migrations.RunPython.noop
        ),
    ]
