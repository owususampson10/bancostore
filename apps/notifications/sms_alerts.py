"""Task 63c. Tell the admin when SMS credit is low, or has run out.

Found 2026-09-17: the mNotify account had 0 credits and every SMS --
distributors' login and password-reset codes, customers' order texts -- had
been failing with HTTP 402, with nothing but a traceback in the server log
to show for it.

Two ways in, one alert:
- apps.notifications.tasks.check_sms_credit reads the balance every hour and
  warns below SMS_LOW_CREDIT_THRESHOLD (user-confirmed default: 200);
- apps.notifications.sms raises the alarm the moment a send is refused for
  lack of credit.

Never by SMS, for obvious reasons: by email (SMS_CREDIT_ALERT_EMAIL, blank to
skip) and the admin bell.
"""

import logging

from django.core.cache import cache
from django.core.mail import send_mail

from constance import config

from .email import get_sender_email
from .models import AdminNotification
from .services import send_admin_notification

logger = logging.getLogger(__name__)

# "Low" and "run out" are different news, so each has its own once-a-day
# limit: running out must never be swallowed by an earlier low warning.
LOW_CREDIT_ALERT_KEY = "notifications:sms_credit_alert:low"
OUT_OF_CREDIT_ALERT_KEY = "notifications:sms_credit_alert:out"
ALERT_INTERVAL_SECONDS = 24 * 60 * 60


def alert_admin_about_sms_credit(*, credits: int) -> None:
    """Emails the admin and rings the bell, at most once a day per kind.
    Never raises: it is called from inside failing SMS sends, and the caller
    must still learn that its own send failed."""
    out_of_credit = credits <= 0
    try:
        key = OUT_OF_CREDIT_ALERT_KEY if out_of_credit else LOW_CREDIT_ALERT_KEY
        if not cache.add(key, 1, ALERT_INTERVAL_SECONDS):
            return
    except Exception:
        # Redis unreachable: skip rather than risk alerting on every send.
        logger.exception(
            "alert_admin_about_sms_credit: could not check the alert limit"
        )
        return

    if out_of_credit:
        subject = "Bancostore SMS credit has run out"
        summary = (
            "SMS credit has run out -- texts to distributors and customers are "
            "failing. Top up mNotify."
        )
    else:
        subject = f"Bancostore SMS credit is low ({credits} left)"
        summary = (
            f"SMS credit is low: {credits} credits left. Top up mNotify so login "
            "codes and order texts keep sending."
        )

    emailed = _email_admin(subject, summary)
    rang = _ring_bell(summary)
    if not (emailed or rang):
        # CodeRabbit (PR #94): nobody was told, so don't start the
        # once-a-day limit -- let the next failed send or hourly check try
        # again.
        try:
            cache.delete(key)
        except Exception:
            logger.exception(
                "alert_admin_about_sms_credit: could not clear the alert limit"
            )


def reset_sms_credit_alerts() -> None:
    """Called once credit is healthy again, so the next drop warns straight
    away instead of waiting out the previous day's limit."""
    try:
        cache.delete_many([LOW_CREDIT_ALERT_KEY, OUT_OF_CREDIT_ALERT_KEY])
    except Exception:
        logger.exception("reset_sms_credit_alerts: could not clear the alert limit")


def _email_admin(subject, summary) -> bool:
    """True if the email was sent. False if it failed or no address is set."""
    try:
        recipient = (config.SMS_CREDIT_ALERT_EMAIL or "").strip()
        if not recipient:
            return False
        send_mail(
            subject=subject,
            message=(
                f"{summary}\n\n"
                "While credit is out, distributors can't receive login codes by "
                "text (password reset codes go by email where the account has "
                "one), and customers don't get order texts.\n\n"
                "If your mNotify wallet has money in it, it may need converting "
                "into SMS credit from the mNotify dashboard."
            ),
            from_email=get_sender_email(),
            recipient_list=[recipient],
        )
    except Exception:
        logger.exception("alert_admin_about_sms_credit: the email alert failed")
        return False
    return True


def _ring_bell(summary) -> bool:
    """True if the bell row was recorded. send_admin_notification returns
    None, never raising, when it couldn't be."""
    try:
        return (
            send_admin_notification(AdminNotification.EventType.SMS_CREDIT, summary)
            is not None
        )
    except Exception:
        logger.exception("alert_admin_about_sms_credit: the bell notification failed")
        return False
