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

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from celery import shared_task

from apps.notifications.email import get_sender_email
from apps.notifications.models import NotificationTemplate
from apps.notifications.rendering import render_email_or_default, render_or_default

from . import receipt_pdf
from .receipts import build_receipt_context, build_receipt_html_context

logger = logging.getLogger(__name__)

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


@shared_task
def send_order_receipt_email_task(order_id) -> None:
    """Celery entry point. Takes an id, not an Order: task arguments are
    serialised, and the worker should read the order as it is when the task
    runs.

    A deleted order is ignored rather than raised: the order can disappear
    between enqueue and run, and raising would only make Celery retry a
    task that can never succeed.
    """
    from .models import Order

    order = Order.objects.filter(pk=order_id).first()
    if order is None or not order.email:
        return

    try:
        send_order_receipt_email(order)
    except Exception:
        logger.exception(
            "send_order_receipt_email_task: failed to send the receipt for "
            "order_id=%s. The order itself is confirmed and unaffected.",
            order_id,
        )
