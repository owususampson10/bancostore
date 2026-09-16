from django.db import migrations

# Task 62. The only admin-editable parts of the branded HTML receipt.
#
# The wording matches the plain-text receipt's own intro and closing lines
# (seeded by migration 0010), so a store whose admin never touches these
# sees the same words in both the HTML and plain-text parts of the email.
#
# Deliberately short text blocks, not HTML: they are HTML-escaped when
# placed into templates/emails/order_receipt.html, so an admin can change
# the words but cannot inject markup into a customer's mail client.
_SEED_TEMPLATES = [
    (
        "order_receipt_intro",
        "",
        "Your order is confirmed. Thank you for shopping with Bancostore.",
    ),
    (
        "order_receipt_closing",
        "",
        "We'll send you an SMS each time your order status changes.",
    ),
]


def seed_order_receipt_text_blocks(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    for key, subject, body in _SEED_TEMPLATES:
        # get_or_create, matching 0007/0010/0012: re-running must never
        # overwrite wording an admin has since edited.
        NotificationTemplate.objects.get_or_create(
            key=key, defaults={"subject": subject, "body": body}
        )


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0015_add_order_receipt_text_blocks"),
    ]

    operations = [
        migrations.RunPython(seed_order_receipt_text_blocks, migrations.RunPython.noop),
    ]
