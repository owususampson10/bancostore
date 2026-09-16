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
from apps.notifications.models import AdminNotification, NotificationTemplate
from apps.notifications.rendering import render_email_or_default, render_or_default
from apps.notifications.services import send_admin_notification
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
# mNotify bills per 160 characters and this fires on every confirmed
# order, so the rendered body is capped, not merely written short.
MAX_ADMIN_SMS_CHARS = 160
# Code review (PR #92): a CHARACTER cap is not a SEGMENT cap. A message
# containing any character outside GSM 03.38 is encoded UCS-2, where one
# segment holds 70 characters, not 160 -- so a 160-character body with a
# single accented name or emoji in it bills as THREE segments. The
# customer's own full_name reaches this body and is never constrained to
# GSM-7, and the template is admin-editable, so both are real inputs.
MAX_ADMIN_SMS_CHARS_UCS2 = 70

# GSM 03.38 basic alphabet plus its extension table. Anything outside
# this forces UCS-2 for the WHOLE message.
_GSM7_CHARS = set(
    "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
    "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e\u00c6\u00e6\u00df\u00c9"
    " !\"#\u00a4%&'()*+,-./0123456789:;<=>?"
    "\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
    "\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0"
    "\f^{}\\[~]|\u20ac"
)


def _sms_char_limit(body: str) -> int:
    """160 when every character is GSM-7, otherwise 70.

    Counting Python characters is still an approximation of billing
    units for astral-plane characters (an emoji is one Python char but
    two UTF-16 units), which is why the UCS-2 limit is the conservative
    70 rather than a computed figure.
    """
    return (
        MAX_ADMIN_SMS_CHARS
        if all(ch in _GSM7_CHARS for ch in body)
        else MAX_ADMIN_SMS_CHARS_UCS2
    )


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
    _send_admin_bell_notification(order)


def _send_admin_email_alert(order) -> None:
    # CodeRabbit (PR #92): the constance lookup is INSIDE the try, not
    # above it. config reads through Redis, so an outage there would
    # otherwise raise before the guard and skip every later channel --
    # the same class of bug as the receipt context on PR #91.
    try:
        recipient = _stripped_or_none(config.ADMIN_ORDER_ALERT_EMAIL)
        if recipient is None:
            # Not an error, and deliberately not logged at warning level:
            # a store that has not configured an address has chosen not
            # to get these, and this runs on every confirmed order.
            return

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
    # Setting lookup inside the guard -- see _send_admin_email_alert.
    try:
        recipient = _stripped_or_none(config.ADMIN_ORDER_ALERT_SMS_NUMBER)
        if recipient is None:
            return

        body = render_or_default(
            NotificationTemplate.Key.ADMIN_NEW_ORDER_SMS,
            build_admin_alert_context(order),
            default_body=_DEFAULT_SMS_BODY,
        )
        # CodeRabbit (PR #92): the cap was only ever asserted against the
        # DEFAULT wording. An admin edit, or simply a long customer name,
        # could push the rendered body past one segment and spend several
        # credits per order -- defeating the whole reason it is short.
        # Truncated rather than refused: a clipped alert still tells the
        # admin an order arrived, which is the point, and refusing would
        # mean an admin silently learns nothing because of their own edit.
        #
        # Code review (PR #92): the marker is three ASCII dots, NOT
        # U+2026. The ellipsis character is outside GSM 03.38, so adding
        # it forced the whole message to UCS-2 and billed THREE segments
        # where two were billed before -- the fix cost more than the bug.
        limit = _sms_char_limit(body)
        if len(body) > limit:
            body = body[: limit - 3].rstrip() + "..."
        send_sms(recipient, body)
    except Exception:
        logger.exception(
            "send_admin_order_alert: failed to SMS the admin about "
            "reference=%s -- the order itself is unaffected.",
            order.payment_reference,
        )


def _send_admin_bell_notification(order) -> None:
    """Task 61c. The in-app bell.

    Unlike the email and SMS above this has no recipient setting, because
    there is nobody to address: it writes one row that every logged-in
    admin sees. Nothing to configure means nothing to leave misconfigured.

    send_admin_notification never raises (it owns its own guard), so this
    wrapper exists only to keep the module's "every channel is
    independently guarded" contract literally true rather than true by
    the grace of a function defined elsewhere.
    """
    try:
        send_admin_notification(
            AdminNotification.EventType.NEW_ORDER,
            f"New order {order.payment_reference} -- GHS {order.total} "
            f"from {order.full_name}",
            order=order,
        )
    except Exception:
        logger.exception(
            "send_admin_order_alert: failed to record the bell notification "
            "for reference=%s -- the order itself is unaffected.",
            order.payment_reference,
        )
