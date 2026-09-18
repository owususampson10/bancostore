"""Task 68j. A deploy-time warning when a payment problem would have nobody
to tell.

PAYMENT_ISSUE_ALERT_EMAIL falls back to ADMIN_ORDER_ALERT_EMAIL, and both
default to blank -- at which point the email alert returns silently and only
the admin bell rings. Found by the Task 68 audit: this is real degradation
that nothing announced, and production ran that way until 2026-09-18.

A WARNING that `manage.py check` prints, never an error: a store still being
set up must be able to start, and the bell still works. The deploy runbook
already runs `manage.py check` before anything is stopped, so this surfaces
exactly where a deployer will read it.
"""

import logging

from django.core.checks import Warning, register

logger = logging.getLogger(__name__)

PAYMENT_ALERT_EMAIL_WARNING_ID = "bancostore.W001"


@register()
def payment_alert_email_is_set(app_configs, **kwargs):
    from constance import config

    try:
        configured = (config.PAYMENT_ISSUE_ALERT_EMAIL or "").strip() or (
            config.ADMIN_ORDER_ALERT_EMAIL or ""
        ).strip()
    except Exception:
        # constance reads through Redis; an outage at check time says
        # nothing about how the platform is configured, so it must not
        # produce a misleading warning either way.
        logger.warning("payment_alert_email_is_set: could not read the settings")
        return []

    if configured:
        return []
    return [
        Warning(
            "No email address is set for payment alerts, so a payment that "
            "needs attention will only appear on the admin bell.",
            hint=(
                "Set Payment Issue Alert Email (or Admin Order Alert Email) "
                "in Platform Settings."
            ),
            id=PAYMENT_ALERT_EMAIL_WARNING_ID,
        )
    ]
