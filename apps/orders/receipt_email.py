"""Task 62. Sends the customer's order receipt email, with a PDF attached.

Runs in the CELERY WORKER, not the request. confirm_order_payment is
called inline from both the Paystack webhook and the customer's
post-payment redirect, inside the site's single Daphne process -- building
a PDF there would make Paystack and the customer wait on it, and a slow or
failed render would sit directly in the payment path. The same reasoning
moved Didit's webhook processing to Celery.

Deliberately imports nothing from apps.orders.services: tasks.py already
imports from services, so defining this task there and importing it into
services would be circular.

The email is always sent, even when the PDF is not. Losing a customer's
whole receipt because the PDF step failed would be far worse than a
receipt with no attachment -- and on the local dev Mac, where WeasyPrint
cannot load, "no PDF" is the normal case.
"""

import logging
import uuid
from smtplib import SMTPRecipientsRefused

from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

from apps.notifications.email import get_sender_email
from apps.notifications.models import NotificationTemplate
from apps.notifications.rendering import render_email_or_default, render_or_default

from . import receipt_pdf
from .models import Order
from .receipts import build_receipt_context, build_receipt_html_context

logger = logging.getLogger(__name__)

# How many times the sweep (apps.orders.tasks.resend_missing_order_receipts)
# re-queues one receipt before giving up. Defined here, not in tasks.py,
# because the task below also sets it when an address is refused, and
# tasks.py imports this module (not the other way round).
RECEIPT_SWEEP_MAX_SWEEPS = 3

# A paid order later cancelled or refunded gets no receipt: arriving after
# the cancellation, a "your order is confirmed" email would be wrong. Shared
# by the task below and the sweep, so the two can never disagree.
RECEIPT_SKIP_STATUSES = (Order.Status.CANCELLED, Order.Status.REFUNDED)

_DEFAULT_INTRO = "Your order is confirmed. Thank you for shopping with Bancostore."
_DEFAULT_CLOSING = "We'll send you an SMS each time your order status changes."


def send_order_receipt_email(order) -> None:
    """Build and send the receipt: plain text, branded HTML, and a PDF.

    Raises on an email failure, so the Celery task can log it; never raises
    because of the PDF.
    """
    context = build_receipt_context(order)
    subject, text_body = render_email_or_default(
        NotificationTemplate.Key.ORDER_CONFIRMED_EMAIL,
        context,
        default_subject="Your Bancostore order {{reference}} is confirmed",
        default_body=(
            "Your order (GHS {{total}}) is confirmed. Reference: {{reference}}"
        ),
    )

    # Only intro and closing are admin-editable. They are plain strings,
    # autoescaped by the template: an admin changes words, never markup.
    intro = render_or_default(
        NotificationTemplate.Key.ORDER_RECEIPT_INTRO,
        context,
        default_body=_DEFAULT_INTRO,
    )
    closing = render_or_default(
        NotificationTemplate.Key.ORDER_RECEIPT_CLOSING,
        context,
        default_body=_DEFAULT_CLOSING,
    )

    html_context = build_receipt_html_context(order)
    html_context["intro"] = intro
    html_context["closing"] = closing

    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=get_sender_email(),
        to=[order.email],
    )
    email.attach_alternative(
        render_to_string("emails/order_receipt.html", html_context), "text/html"
    )

    pdf = _render_pdf_or_none(order, intro, closing)
    if pdf:
        email.attach(receipt_pdf.receipt_pdf_filename(order), pdf, "application/pdf")

    email.send()


def _render_pdf_or_none(order, intro, closing):
    """The PDF bytes, or None -- never an exception.

    Called through the module attribute (receipt_pdf.render_receipt_pdf)
    rather than a from-import, so tests can patch the one real renderer and
    never import WeasyPrint.
    """
    try:
        return receipt_pdf.render_receipt_pdf(order, intro=intro, closing=closing)
    except Exception:
        logger.exception(
            "send_order_receipt_email: PDF render failed for reference=%s -- "
            "sending the receipt email without an attachment.",
            order.payment_reference,
        )
        return None


# --- Delivery: retried, recorded, and never sent twice at once --------------
#
# Task 62 follow-up (CodeRabbit, PR #93): a failed send used to be logged and
# forgotten, so a Gmail hiccup cost the customer their receipt for good. Now
# the worker retries with growing pauses, the order records when its receipt
# actually went out, and apps.orders.tasks.resend_missing_order_receipts
# re-queues any confirmed order still without one.

RECEIPT_SEND_MAX_RETRIES = 3
RECEIPT_RETRY_BASE_SECONDS = 60  # 1, 2, then 4 minutes between tries

# The lock must outlive any single send, or a second worker could start
# sending while the first is still stalled on Gmail. EMAIL_TIMEOUT (settings)
# bounds each SMTP call; the task's hard time limit bounds the whole run,
# PDF render included. Both sit well under the lock's lifetime.
RECEIPT_SEND_SOFT_TIME_LIMIT_SECONDS = 240
RECEIPT_SEND_TIME_LIMIT_SECONDS = 300
RECEIPT_LOCK_TIMEOUT_SECONDS = 600


# Worth retrying within minutes: the mail server or network had a moment.
# smtplib's errors, socket timeouts and refused connections are all OSError.
# SMTPRecipientsRefused is an OSError too, but is caught first, since
# retrying a refused address can't help.
_TRANSIENT_SEND_ERRORS = (OSError, SoftTimeLimitExceeded)


def receipt_email_lock_key(order_id) -> str:
    return f"orders:receipt_email_lock:{order_id}"


def _release_lock(key, token):
    """Deletes the lock only if this run still owns it. If a stalled send
    outlived the lock and another run took it over, deleting blindly would
    free that run's lock too and let a third run in. (get-then-delete is
    not atomic; the window is tiny, and the worst case is a duplicate
    receipt, never a lost one.)"""
    if cache.get(key) == token:
        cache.delete(key)


@shared_task(
    bind=True,
    max_retries=RECEIPT_SEND_MAX_RETRIES,
    soft_time_limit=RECEIPT_SEND_SOFT_TIME_LIMIT_SECONDS,
    time_limit=RECEIPT_SEND_TIME_LIMIT_SECONDS,
)
def send_order_receipt_email_task(self, order_id) -> None:
    """Celery entry point. Takes an id, not an Order: task arguments are
    serialised, and the worker should read the order as it is when the task
    runs.

    A deleted order is ignored rather than raised: the order can disappear
    between enqueue and run, and raising would only make Celery retry a
    task that can never succeed.
    """
    order = Order.objects.filter(pk=order_id).first()
    if order is None or not order.email or order.receipt_email_sent_at is not None:
        return

    key = receipt_email_lock_key(order_id)
    token = uuid.uuid4().hex
    try:
        acquired = cache.add(key, token, timeout=RECEIPT_LOCK_TIMEOUT_SECONDS)
        if acquired:
            # Re-read AFTER taking the lock (design review): two queued
            # copies can both pass the check above, and the first may have
            # finished and released the lock before the second got here.
            order.refresh_from_db()
            # CodeRabbit (PR #93): the status is re-checked here too. A job
            # queued at confirmation can run after the order was cancelled.
            if (
                order.receipt_email_sent_at is not None
                or not order.email
                or order.status in RECEIPT_SKIP_STATUSES
            ):
                _release_lock(key, token)
                return
            send_order_receipt_email(order)
    except SMTPRecipientsRefused:
        _release_lock(key, token)
        # The address itself was refused. Retrying cannot fix that, so the
        # sweep is told to stop too.
        Order.objects.filter(pk=order_id).update(
            receipt_email_sweeps=RECEIPT_SWEEP_MAX_SWEEPS
        )
        # logger.error, not .exception: SMTPRecipientsRefused's text holds
        # the customer's email address, and these logs record order ids,
        # never contact details.
        logger.error(
            "send_order_receipt_email_task: the mail server refused the "
            "address for order_id=%s; not retrying. The order itself is "
            "confirmed and unaffected.",
            order_id,
        )
        return
    except _TRANSIENT_SEND_ERRORS as exc:
        _release_lock(key, token)
        if self.request.retries < self.max_retries:
            raise self.retry(
                exc=exc,
                countdown=RECEIPT_RETRY_BASE_SECONDS * 2**self.request.retries,
            )
        logger.exception(
            "send_order_receipt_email_task: failed to send the receipt for "
            "order_id=%s after %s retries; the receipt sweep will try again "
            "later. The order itself is confirmed and unaffected.",
            order_id,
            self.max_retries,
        )
        return
    except Exception:
        _release_lock(key, token)
        # A bug (say, a broken template) fails the same way on every try,
        # so the worker does not retry it within minutes. The order stays
        # unmarked: the sweep's few, widely spaced attempts will pick it up,
        # which also covers a fix being deployed in the meantime.
        logger.exception(
            "send_order_receipt_email_task: failed to send the receipt for "
            "order_id=%s (not a mail or network error, so not retried now). "
            "The order itself is confirmed and unaffected.",
            order_id,
        )
        return

    if not acquired:
        logger.info(
            "send_order_receipt_email_task: order_id=%s is already being "
            "sent by another run; skipping.",
            order_id,
        )
        return

    Order.objects.filter(pk=order_id).update(receipt_email_sent_at=timezone.now())
    _release_lock(key, token)
