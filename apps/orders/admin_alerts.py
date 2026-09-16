"""Task 61. Tells the admin an order has been paid for.

Before this, a confirmed order notified the customer (SMS + email, via
apps.orders.services._send_confirmation_notifications) and nobody else.
The admin learned about it only by logging in and reading the Admin
Dashboard's "Orders Awaiting Action" card. Pull, never push -- so if
nobody logged in on a Saturday, Saturday's paid orders sat unseen while
the customer waited.

Every channel here is independently guarded and builds its own context
INSIDE that guard. This is called from confirm_order_payment after money,
stock and PV have already committed, and inside
retry_on_lock_contention's _attempt -- so an exception escaping would
either make a successful order look rolled back or re-run an
already-committed transaction. That exact failure was caught by
CodeRabbit on PR #91 in _send_confirmation_notifications; the same shape
applies here for the same reason.

Recipients are constance settings with a blank default meaning disabled,
matching COMPLIANCE_ALERT_EMAIL and LOCKOUT_ALERT_EMAIL rather than
inventing a new convention. Deliberately NOT "every staff user": that
would turn a routine order into mail for every admin account that ever
existed, with no way to opt out short of deactivating the account.
"""

import logging

from django.core.mail import send_mail

from constance import config

from apps.notifications.email import get_sender_email
from apps.notifications.models import NotificationTemplate
from apps.notifications.rendering import render_email_or_default, render_or_default
from apps.notifications.sms import send_sms
from apps.orders.receipts import build_receipt_context

logger = logging.getLogger(__name__)

_DEFAULT_EMAIL_SUBJECT = "New order {{reference}} -- GHS {{total}}"

_DEFAULT_EMAIL_BODY = """A customer has paid for an order.

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

# Task 61b: one SMS segment is the budget -- see _send_admin_sms_alert.
_DEFAULT_SMS_BODY = (
    "New Bancostore order {{reference}} -- GHS {{total}} from {{customer_name}}."
)


def _stripped_or_none(value):
    """A whitespace-only setting is not a configured value. Mirrors
    apps/pages/views.py's own helper of the same name, which exists
    because a blank-looking constance string would otherwise read as
    "set" and be used verbatim."""
    value = (value or "").strip()
    return value or None


def build_admin_alert_context(order) -> dict:
    """Task 60's customer receipt context plus the fields only an admin
    needs: who to contact and how. The customer's own receipt has no
    reason to repeat their contact details back at them; an admin alert
    is useless without them, since acting on an order means reaching the
    buyer.

    Every value is a plain string -- the renderer substitutes str(value),
    so a model instance leaking in would render as a repr.
    """
    context = build_receipt_context(order)
    context["customer_phone"] = str(order.phone_number)
    context["customer_email"] = order.email or "(none given)"
    context["order_status"] = order.get_status_display()
    return context


def send_admin_order_alert(order) -> None:
    """Tell the admin about a newly paid order, by email and/or SMS.

    Each channel has its own recipient setting and its own guard, and
    builds its own context inside that guard. One channel failing must
    never skip the other, and nothing here may raise -- see the module
    docstring for why that matters at this call site specifically.
    """
    _send_admin_email_alert(order)
    _send_admin_sms_alert(order)


def _send_admin_email_alert(order) -> None:
    recipient = _stripped_or_none(config.ADMIN_ORDER_ALERT_EMAIL)
    if recipient is None:
        # Not an error, and deliberately not logged at warning level: a
        # store that has not configured an address has chosen not to get
        # these, and this runs on every single confirmed order.
        return

    try:
        subject, message = render_email_or_default(
            NotificationTemplate.Key.ADMIN_NEW_ORDER_EMAIL,
            build_admin_alert_context(order),
            default_subject=_DEFAULT_EMAIL_SUBJECT,
            default_body=_DEFAULT_EMAIL_BODY,
        )
        send_mail(
            subject=subject,
            message=message,
            from_email=get_sender_email(),
            recipient_list=[recipient],
        )
    except Exception:
        logger.exception(
            "send_admin_order_alert: failed to email the admin about "
            "reference=%s -- the order itself is unaffected.",
            order.payment_reference,
        )


def _send_admin_sms_alert(order) -> None:
    """Task 61b. Deliberately short: mNotify bills per 160 characters and
    this fires on every confirmed order, so an item list here would
    multiply the store's whole messaging bill. Reference, total and
    customer name are what identifies the order and what fits.

    A separate setting from the email, not a single "alerts on/off":
    SMS costs real money per message and email does not, so wanting one
    without the other is a normal choice rather than an edge case.
    """
    recipient = _stripped_or_none(config.ADMIN_ORDER_ALERT_SMS_NUMBER)
    if recipient is None:
        return

    try:
        send_sms(
            recipient,
            render_or_default(
                NotificationTemplate.Key.ADMIN_NEW_ORDER_SMS,
                build_admin_alert_context(order),
                default_body=_DEFAULT_SMS_BODY,
            ),
        )
    except Exception:
        logger.exception(
            "send_admin_order_alert: failed to SMS the admin about "
            "reference=%s -- the order itself is unaffected.",
            order.payment_reference,
        )
