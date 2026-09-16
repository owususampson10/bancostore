"""Task 62. The branded, till-receipt style HTML email.

Several of these exist because of a real Gmail test rather than theory:
the logo travelled as an inline attachment and the email landed in Spam,
where Gmail blocks images and attachment downloads alike; the header then
collapsed to a broken strip; and Gmail restyled the domain and phone number
as blue underlined links.
"""

import uuid
from decimal import Decimal
from unittest.mock import patch

from django.core import mail

import pytest

from apps.catalog.models import Category, Product
from apps.orders.models import Order, OrderItem
from apps.orders.receipts import build_receipt_html_context, receipt_logo_url


def _make_order(**overrides):
    defaults = {
        # A REAL reference, generated per call. Order.payment_reference has
        # no default, so omitting it left every order with "" -- and
        # `assert order.payment_reference in html` is then `assert "" in
        # html`, which is true of ANY text. Those checks could never fail,
        # whether or not the reference was rendered at all (CodeRabbit,
        # PR #93). Unique per call because the field is unique=True.
        "payment_reference": f"order-{uuid.uuid4().hex}",
        "full_name": "Kofi Mensah",
        "phone_number": "+233241234567",
        "email": "kofi@example.com",
        "delivery_method": Order.DeliveryMethod.HOME_DELIVERY,
        "delivery_zone": Order.DeliveryZone.ACCRA,
        "address": "12 High Street",
        "area": "Osu",
        "landmark": "Near the Shell Station",
        "subtotal": Decimal("590.00"),
        "delivery_fee": Decimal("50.00"),
        "discount_amount": Decimal("20.00"),
        "total": Decimal("620.00"),
        "status": Order.Status.CONFIRMED,
    }
    defaults.update(overrides)
    return Order.objects.create(**defaults)


def _add_item(order, name="Emerald Aura Watch", quantity=1, unit_price="350.00"):
    category, _ = Category.objects.get_or_create(name="Watches", slug="watches")
    product = Product.objects.create(
        name=name,
        category=category,
        price=Decimal(unit_price),
        pv_value=10,
        stock=5,
        is_active=True,
    )
    return OrderItem.objects.create(
        order=order,
        product=product,
        product_name=name,
        quantity=quantity,
        unit_price=Decimal(unit_price),
        unit_pv=10,
    )


def _send(order, settings):
    """Builds and sends the receipt the way the Celery worker does.

    Task 62 moved the email out of _send_confirmation_notifications (which
    now only enqueues it) and into send_order_receipt_email, so the email's
    CONTENT is tested here. The PDF renderer is patched to None: these
    tests are about the email body, the PDF has its own file, and WeasyPrint
    must never be imported in tests on the dev Mac (Task 45)."""
    from apps.orders.receipt_email import send_order_receipt_email

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    with patch("apps.orders.receipt_pdf.render_receipt_pdf", return_value=None):
        send_order_receipt_email(order)
    assert len(mail.outbox) == 1
    return mail.outbox[0]


def _html(message):
    html = [body for body, mime in message.alternatives if mime == "text/html"]
    assert html, "no text/html alternative was attached"
    return html[0]


# --- The email itself ------------------------------------------------------


@pytest.mark.django_db
def test_the_receipt_is_sent_as_both_plain_text_and_html(settings):
    """Plain text is kept alongside the HTML on purpose: it helps spam
    scoring and means nobody ever receives an unreadable email."""
    order = _make_order()
    _add_item(order)

    message = _send(order, settings)

    assert message.body.strip()  # the plain-text part
    assert "<table" in _html(message)


@pytest.mark.django_db
def test_the_html_receipt_lists_items_and_totals(settings):
    order = _make_order()
    _add_item(order, name="Emerald Aura Watch", quantity=1, unit_price="350.00")
    _add_item(order, name="Always Young Cream", quantity=2, unit_price="120.00")

    html = _html(_send(order, settings))

    assert "Emerald Aura Watch" in html
    assert "Always Young Cream" in html
    assert "240.00" in html  # 2 x 120.00, the LINE total
    assert "620.00" in html
    assert order.payment_reference in html


@pytest.mark.django_db
def test_the_logo_is_a_hosted_image_not_an_attachment(settings):
    """The first design embedded the logo as an inline attachment. In a
    real Gmail test that surfaced as a file the user could not download,
    and attachments in Spam are blocked outright. A hosted image on the
    store's own domain adds nothing to the attachment list."""
    order = _make_order()
    _add_item(order)

    message = _send(order, settings)

    assert message.attachments == []
    assert receipt_logo_url() in _html(message)
    assert "cid:" not in _html(message)


@pytest.mark.django_db
def test_the_logo_url_is_absolute_https_on_the_site_domain():
    """Email is read far from the site, so a relative /static/ path would
    resolve against the reader's mail client and load nothing. And Gmail
    fetches images through its own proxy, which cannot reach localhost."""
    url = receipt_logo_url()

    assert url.startswith("https://bancostore.com/")
    assert url.endswith(".png")  # Gmail does not render SVG


# --- Things a real Gmail test broke -----------------------------------------


@pytest.mark.django_db
def test_the_header_is_a_proper_banner_when_images_are_blocked(settings):
    """In the real test the email landed in Spam, where Gmail blocks all
    images. The header's zeroed font metrics crushed the alt text, so it
    collapsed to a thin strip with the name chopped off. The cell now has
    a real height and styled alt text."""
    order = _make_order()
    _add_item(order)

    html = _html(_send(order, settings))

    assert 'height="127"' in html
    assert 'alt="Bancostore"' in html
    header = html[html.index('height="127"') : html.index('alt="Bancostore"')]
    assert "font-size:0" not in header


@pytest.mark.django_db
def test_the_phone_number_is_a_styled_link_not_a_gmail_autolink(settings):
    """Gmail turned the bare phone number into a blue underlined link,
    breaking the receipt's monochrome look."""
    order = _make_order()
    _add_item(order)

    html = _html(_send(order, settings))

    assert 'href="tel:+233241234567"' in html
    tel = html[html.index('href="tel:') :]
    assert "text-decoration:none" in tel[:200]


# --- Delivery variants -----------------------------------------------------


@pytest.mark.django_db
def test_a_pickup_order_says_collection_not_an_empty_address():
    order = _make_order(
        delivery_method=Order.DeliveryMethod.PICKUP,
        delivery_zone="",
        address="",
        area="",
        landmark="",
        delivery_fee=Decimal("0.00"),
        total=Decimal("570.00"),
    )
    _add_item(order)

    context = build_receipt_html_context(order)

    assert context["is_pickup"] is True
    assert context["delivery_lines"] == ["Pickup from our Bancostore location."]


@pytest.mark.django_db
def test_a_zero_discount_is_hidden_rather_than_shown_as_zero():
    """A till receipt only prints a discount line when there is one."""
    order = _make_order(discount_amount=Decimal("0.00"), total=Decimal("640.00"))
    _add_item(order)

    assert build_receipt_html_context(order)["show_discount"] is False


@pytest.mark.django_db
def test_free_delivery_reads_free_rather_than_ghs_zero():
    order = _make_order(delivery_fee=Decimal("0.00"), total=Decimal("570.00"))
    _add_item(order)

    assert build_receipt_html_context(order)["delivery_is_free"] is True


@pytest.mark.django_db
def test_no_email_is_sent_for_a_legacy_order_with_no_address(settings):
    """Order.email stays blank=True for orders placed before email became
    required at checkout. Those must still be skipped cleanly."""
    from apps.orders.services import _send_confirmation_notifications

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    order = _make_order(email="")
    _add_item(order)

    with patch("apps.orders.services.send_sms"):
        _send_confirmation_notifications(order)

    assert mail.outbox == []


# --- Admin-editable wording (Task 62c) -------------------------------------


@pytest.mark.django_db
def test_an_admin_edit_to_the_intro_reaches_the_real_receipt(settings):
    """Per-send-site proof the admin-editable row is what renders, matching
    the convention Task 48 set for every other send site."""
    from apps.notifications.models import NotificationTemplate

    NotificationTemplate.objects.filter(
        key=NotificationTemplate.Key.ORDER_RECEIPT_INTRO
    ).update(body="Akwaaba {{customer_name}}! Your order is on its way.")
    order = _make_order()
    _add_item(order)

    html = _html(_send(order, settings))

    assert "Akwaaba Kofi Mensah! Your order is on its way." in html


@pytest.mark.django_db
def test_an_admin_edit_to_the_closing_reaches_the_real_receipt(settings):
    from apps.notifications.models import NotificationTemplate

    NotificationTemplate.objects.filter(
        key=NotificationTemplate.Key.ORDER_RECEIPT_CLOSING
    ).update(body="Call us on 024 000 0000 with any questions.")
    order = _make_order()
    _add_item(order)

    html = _html(_send(order, settings))

    assert "Call us on 024 000 0000 with any questions." in html


@pytest.mark.django_db
def test_markup_typed_by_an_admin_is_shown_as_text_never_run(settings):
    """The design is fixed in code; an admin changes WORDS. Anything typed
    as markup must reach the customer as visible text, not as live HTML in
    their mail client -- otherwise one compromised admin account could
    restyle or booby-trap every receipt the store sends."""
    from apps.notifications.models import NotificationTemplate

    NotificationTemplate.objects.filter(
        key=NotificationTemplate.Key.ORDER_RECEIPT_INTRO
    ).update(body='<script>alert(1)</script><a href="https://evil.test">Click</a>')
    order = _make_order()
    _add_item(order)

    html = _html(_send(order, settings))

    assert "<script>" not in html
    assert 'href="https://evil.test"' not in html
    assert "&lt;script&gt;" in html  # present, but escaped into plain text


@pytest.mark.django_db
def test_a_customer_name_with_markup_is_escaped_too(settings):
    """full_name comes from the checkout form, so it is customer-supplied
    input landing in HTML. Same guarantee as admin text."""
    order = _make_order(full_name='<img src=x onerror="alert(1)">')
    _add_item(order)

    html = _html(_send(order, settings))

    assert 'onerror="alert(1)"' not in html
    assert "&lt;img" in html


@pytest.mark.django_db
def test_a_broken_html_render_never_escapes_the_notification_guard(settings):
    """A template failure must never crash anything.

    Originally the HTML was built inside _send_confirmation_notifications,
    where an escaping failure would reach confirm_order_payment's retry
    wrapper after money had already moved (CodeRabbit, PR #91). Task 62
    moved rendering into the Celery worker, so the payment path no longer
    renders anything -- and this guarantee now belongs to the task, which
    must swallow the failure rather than raise and have Celery retry a
    send that will fail identically every time."""
    from apps.orders.receipt_email import send_order_receipt_email_task

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    order = _make_order()
    _add_item(order)

    with patch(
        "apps.orders.receipt_email.render_to_string",
        side_effect=RuntimeError("template blew up"),
    ):
        send_order_receipt_email_task(order.pk)  # must not raise
