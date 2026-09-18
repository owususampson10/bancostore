"""Task 67. Records a payment Paystack confirmed that did not become what it
paid for, and tells the admin once.

Found 2026-09-16: a registration fee was paid on a checkout page left open
past the pending registration's cleanup. No account was created and the only
trace was a server log line nobody reads. The payer could not log in and the
admin could not see why.

A PaymentIssue row is the durable record; the email and bell are the push.
Both fire only when the row is first created, so however many webhook
retries, cleanup runs and reconciliation passes notice the same payment, the
admin hears about it once -- and a Redis flush cannot bring the alerts back,
unlike the cache-keyed limit apps.notifications.sms_alerts uses.
"""

import json
import logging
import re

from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from constance import config

from apps.notifications.email import get_sender_email
from apps.notifications.models import AdminNotification
from apps.notifications.services import send_admin_notification

from .models import PaymentIssue
from .paystack import PLACEHOLDER_EMAIL_DOMAIN

logger = logging.getLogger(__name__)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_PLACEHOLDER_EMAIL = re.compile(
    rf"^guest-(\d+)@{re.escape(PLACEHOLDER_EMAIL_DOMAIN)}$", re.IGNORECASE
)


def record_payment_issue(
    reference,
    kind,
    *,
    verified=None,
    pending=None,
    distributor=None,
    order=None,
    detail="",
    resolved=False,
):
    """Returns the PaymentIssue (new or already existing), or None if it
    could not be written. Never raises: callers run after money or an
    account has already committed, and a failure to record the problem must
    not look like that work failed too.

    The alert is sent on transaction commit, so a caller inside a
    transaction that later rolls back never announces an issue that no
    longer exists."""
    verified = verified or {}
    try:
        fields = _issue_fields(kind, verified, pending, distributor, order, detail)
        if resolved:
            # Task 68e: already put right (the stock-out refund). It still
            # belongs on the Payments screen -- an admin should see money
            # come in and go back out -- but it needs no action and must not
            # add to the "still to sort" count.
            fields["resolved_at"] = timezone.now()
        with transaction.atomic():
            issue, created = PaymentIssue.objects.get_or_create(
                reference=_clean(reference, 100), defaults=fields
            )
    except Exception:
        logger.exception(
            "record_payment_issue: could not record %s for reference=%s -- "
            "this payment needs manual investigation.",
            kind,
            reference,
        )
        return None

    if not created and issue.kind != kind:
        # Agent code review (Task 68): one row per reference is right for
        # "the admin refunds this once", but a DIFFERENT problem on the same
        # payment is new news -- a dispute raised on a payment already
        # recorded as refunded has a bank deadline and is lost by default if
        # nobody answers it. Update and alert again.
        try:
            with transaction.atomic():
                PaymentIssue.objects.filter(pk=issue.pk).update(
                    kind=kind, detail=fields["detail"], resolved_at=None
                )
            issue.refresh_from_db()
        except Exception:
            logger.exception(
                "record_payment_issue: could not update %s to %s",
                reference,
                kind,
            )
            return None
        created = True

    # Third security review: the caller needs to know whether THIS call
    # created the row, not just that a row exists -- the stock-out refund
    # uses it to decide whether it owns the refund or another worker does.
    issue.was_created = created

    if created:
        logger.error(
            "record_payment_issue: %s for reference=%s (payment_issue_id=%s)",
            kind,
            reference,
            issue.pk,
        )
        transaction.on_commit(lambda: _alert_admin(issue))
    return issue


def _issue_fields(kind, verified, pending, distributor, order, detail):
    metadata = _metadata(verified)
    customer = verified.get("customer") if isinstance(verified, dict) else None
    payer_email = _clean(
        (customer or {}).get("email") if isinstance(customer, dict) else "", 254
    )

    if pending is not None:
        payer_name = _clean(pending.full_name, 255)
        payer_phone = _clean(pending.phone_number, 32)
        payer_email = _clean(pending.email, 254) or payer_email
    elif order is not None:
        # Task 68d: an order carries the buyer's own details, guest or not.
        payer_name = _clean(order.full_name, 255)
        payer_phone = _clean(order.phone_number, 32)
        payer_email = _clean(order.email, 254) or payer_email
    elif distributor is not None:
        # Task 68d: a starter-pack payment's payer is a real distributor, so
        # their own record beats anything Paystack echoes back.
        payer_name = _clean(distributor.full_name, 255)
        payer_phone = _clean(distributor.phone_number, 32)
        payer_email = _clean(getattr(distributor.user, "email", ""), 254) or payer_email
    else:
        payer_name = _clean(metadata.get("full_name"), 255)
        payer_phone = _clean(metadata.get("phone_number"), 32)
        if not payer_phone:
            match = _PLACEHOLDER_EMAIL.match(payer_email)
            if match:
                payer_phone = f"+{match.group(1)}"[:32]

    amount = verified.get("amount")
    paid_at = verified.get("paid_at")
    return {
        "kind": kind,
        "amount_pesewas": amount if isinstance(amount, int) and amount >= 0 else None,
        "paid_at": parse_datetime(paid_at) if isinstance(paid_at, str) else None,
        "payer_name": payer_name,
        "payer_phone": payer_phone,
        "payer_email": payer_email,
        "detail": _clean(detail, 1000),
    }


def _metadata(verified):
    """Paystack hands metadata back as whatever was sent: a dict from our own
    initialize call, but a JSON string or "" from other checkouts."""
    metadata = verified.get("metadata") if isinstance(verified, dict) else None
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return {}
    return metadata if isinstance(metadata, dict) else {}


def _clean(value, max_length):
    if value is None:
        return ""
    return _CONTROL_CHARS.sub(" ", str(value)).strip()[:max_length]


_PAID_ACTION = (
    "Paystack confirmed this payment, but nothing on Bancostore came of it. "
    "Refund it from the Paystack dashboard, or contact the payer to sort it out."
)
# Code review (Task 67): an unconfirmed registration is most likely a
# checkout nobody paid, so telling the admin to "refund" it would be wrong.
_ACTION_BY_KIND = {
    PaymentIssue.Kind.REGISTRATION_UNCONFIRMED: (
        "Paystack could not be reached to confirm whether this registration "
        "fee was ever paid, and the registration has now been removed. Look "
        "the reference up in the Paystack dashboard: if it shows as paid, "
        "refund it or contact the payer; if not, there is nothing to do."
    ),
    PaymentIssue.Kind.REGISTRATION_DUPLICATE: (
        "This person's account already exists from an earlier payment, and "
        "Paystack confirmed this second one. Refund this payment from the "
        "Paystack dashboard."
    ),
}


# Task 68g (adversarial security review): these are recorded and belled, but
# never emailed. The first run after the window widened to 30 days sweeps a
# month of transactions, and every Paystack payment link on the same merchant
# account would be one more email on the SAME Gmail quota that carries
# registration and password-reset mail. The screen and the bell carry them.
_KINDS_WITHOUT_EMAIL = frozenset({PaymentIssue.Kind.UNRECOGNISED_PAYMENT})


def _alert_admin(issue):
    summary = f"Payment {issue.reference} needs attention: {issue.get_kind_display()}"
    if issue.kind not in _KINDS_WITHOUT_EMAIL:
        _email_admin(issue, summary)
    try:
        send_admin_notification(AdminNotification.EventType.PAYMENT_ISSUE, summary)
    except Exception:
        logger.exception(
            "record_payment_issue: the bell notification failed for %s",
            issue.reference,
        )


def _email_admin(issue, summary):
    try:
        recipient = (config.PAYMENT_ISSUE_ALERT_EMAIL or "").strip() or (
            config.ADMIN_ORDER_ALERT_EMAIL or ""
        ).strip()
        if not recipient:
            return
        amount = (
            f"GHS {issue.amount_pesewas / 100:.2f}"
            if issue.amount_pesewas is not None
            else "unknown"
        )
        send_mail(
            subject=f"Bancostore payment needs attention: {issue.reference}",
            message=(
                f"{summary}.\n\n"
                f"{_ACTION_BY_KIND.get(issue.kind, _PAID_ACTION)}\n\n"
                f"Reference: {issue.reference}\n"
                f"Amount:    {amount}\n"
                f"Paid at:   {issue.paid_at or 'unknown'}\n"
                f"Name:      {issue.payer_name or 'unknown'}\n"
                f"Phone:     {issue.payer_phone or 'unknown'}\n"
                f"Email:     {issue.payer_email or 'unknown'}\n"
                + (f"\nDetails: {issue.detail}\n" if issue.detail else "")
            ),
            from_email=get_sender_email(),
            recipient_list=[recipient],
        )
    except Exception:
        logger.exception(
            "record_payment_issue: the email alert failed for %s", issue.reference
        )
