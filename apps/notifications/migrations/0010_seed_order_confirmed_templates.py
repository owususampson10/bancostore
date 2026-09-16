from django.db import migrations

# Task 60. Seeds the two keys Task 48c missed. The SMS body is the real
# current wording, copied verbatim from the hardcoded f-string in
# apps.orders.services::_send_confirmation_notifications that this task's
# own wiring replaces -- an admin who never touches the admin portal sees
# no change in what customers receive by SMS.
#
# The email body is genuinely new: the f-string it replaces was a single
# line with no item list, no totals breakdown and no delivery details,
# which is not a receipt. {{items}} is pre-rendered server-side by
# apps.orders.receipts.render_items_block; the renderer has no loop, by
# design (see apps.notifications.rendering's module docstring).
#
# Kept plain text, not HTML: this body is admin-editable, and rendering
# admin-entered markup in a customer's mail client is a needless
# injection surface for no real gain. Plain text also renders reliably
# everywhere.
_ORDER_CONFIRMED_SMS = (
    "Your Bancostore order (GHS {{total}}) is confirmed! " "Reference: {{reference}}"
)

_ORDER_CONFIRMED_EMAIL_SUBJECT = "Your Bancostore order {{reference}} is confirmed"

_ORDER_CONFIRMED_EMAIL_BODY = """Hi {{customer_name}},

Your order is confirmed. Thank you for shopping with Bancostore.

Order:  {{reference}}
Date:   {{order_date}}

ITEMS
{{items}}

Subtotal        GHS {{subtotal}}
Discount        GHS {{discount_amount}}
Delivery        GHS {{delivery_fee}}
TOTAL           GHS {{total}}

DELIVERY
{{delivery_details}}

WHAT HAPPENS NEXT
We'll send you an SMS each time your order status changes.

Thank you,
The Bancostore Team
"""

_SEED_TEMPLATES = [
    ("order_confirmed_sms", "", _ORDER_CONFIRMED_SMS),
    (
        "order_confirmed_email",
        _ORDER_CONFIRMED_EMAIL_SUBJECT,
        _ORDER_CONFIRMED_EMAIL_BODY,
    ),
]


def seed_order_confirmed_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    for key, subject, body in _SEED_TEMPLATES:
        # get_or_create, matching migration 0007's own convention: re-running
        # this must never overwrite wording an admin has since edited.
        NotificationTemplate.objects.get_or_create(
            key=key, defaults={"subject": subject, "body": body}
        )


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0009_add_order_confirmed_templates"),
    ]

    operations = [
        migrations.RunPython(seed_order_confirmed_templates, migrations.RunPython.noop),
    ]
