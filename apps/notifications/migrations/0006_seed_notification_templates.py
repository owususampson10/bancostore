from django.db import migrations

# Task 48a. Real current wording, copied verbatim from the hardcoded
# f-strings this migration's own send sites still use today (48b/48c
# wire the actual reads) -- {{name}} placeholders map 1:1 to
# apps.notifications.template_registry.PLACEHOLDERS_BY_KEY.
_SEED_TEMPLATES = [
    (
        "otp_code",
        "",
        "Your Bancostore verification code is {{code}}. It expires in "
        "{{expiry_minutes}} minutes.",
    ),
    (
        "withdrawal_approved",
        "",
        "Your Bancostore withdrawal of GHS {{net_amount}} has been "
        "approved. Payout processes on {{withdrawal_day}} -- we'll "
        "notify you once it's paid.",
    ),
    (
        "withdrawal_rejected",
        "",
        "Your Bancostore withdrawal request of GHS {{amount}} was not "
        "approved. Reason: {{reason}}",
    ),
    (
        "withdrawal_paid",
        "",
        "Good news! Your Bancostore withdrawal of GHS {{net_amount}} has "
        "been paid to your mobile money account.",
    ),
    (
        "withdrawal_reversed",
        "",
        "Your Bancostore withdrawal of GHS {{net_amount}} could not be "
        "completed and has been returned to your wallet. You can request "
        "a new withdrawal anytime.",
    ),
    (
        "kyc_approved",
        "",
        "Your KYC verification has been approved!",
    ),
    (
        "kyc_rejected",
        "",
        "Your KYC verification was rejected: {{reason}}",
    ),
]


def seed_notification_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    # Pre-seeded, never lazily created -- matches this project's own
    # established "fixed row set exists from the start" convention
    # (IrIdSequence, EscrowLedger). A send site that finds no row for
    # its key falls back to a safe hardcoded default (see
    # apps.notifications.rendering.render_or_default) rather than
    # failing the underlying business operation, so this migration
    # being re-run or a row being manually deleted is never catastrophic
    # -- get_or_create keeps it idempotent regardless.
    for key, subject, body in _SEED_TEMPLATES:
        NotificationTemplate.objects.get_or_create(
            key=key, defaults={"subject": subject, "body": body}
        )


class Migration(migrations.Migration):

    dependencies = [
        (
            "notifications",
            "0005_notificationtemplate_historicalnotificationtemplate",
        ),
    ]

    operations = [
        # No real reverse -- these rows are safe to leave in place (see
        # apps.compliance.migrations.0002_seed_escrow_ledger's identical
        # reasoning for a pre-seeded row set with no destructive
        # foreign-key dependents forcing a real teardown).
        migrations.RunPython(seed_notification_templates, migrations.RunPython.noop),
    ]
