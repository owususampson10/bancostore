"""Task 60. The order-confirmation receipt body and its placeholders.

Split from test_confirm_order_payment.py deliberately: that file covers
the money/stock/PV side of confirmation, this one covers only what the
customer is told about it.
"""

import uuid
from decimal import Decimal

from django.utils import timezone

import pytest

from apps.catalog.models import Category, Product
from apps.orders.models import Order, OrderItem
from apps.orders.receipts import build_receipt_context, render_items_block


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
        "address": "12 High St",
        "area": "Osu",
        "landmark": "Near the Shell Station",
        "subtotal": Decimal("350.00"),
        "delivery_fee": Decimal("50.00"),
        "discount_amount": Decimal("0.00"),
        "total": Decimal("400.00"),
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


@pytest.mark.django_db
def test_items_block_lists_each_line_with_quantity_and_line_total():
    order = _make_order()
    _add_item(order, name="Emerald Aura Watch", quantity=2, unit_price="175.00")

    block = render_items_block(order)

    assert "Emerald Aura Watch" in block
    assert "2 x" in block
    assert "350.00" in block  # 2 x 175.00, the LINE total, not the unit price


@pytest.mark.django_db
def test_items_block_uses_the_snapshotted_name_not_the_live_product():
    """Order lines snapshot product_name at creation time (Task 17a), so a
    receipt must never re-read the live Product -- renaming a product
    later must not rewrite what an already-sent receipt said."""
    order = _make_order()
    item = _add_item(order, name="Original Name")
    item.product.name = "Renamed Later"
    item.product.save(update_fields=["name"])

    block = render_items_block(order)

    assert "Original Name" in block
    assert "Renamed Later" not in block


@pytest.mark.django_db
def test_receipt_context_carries_every_money_figure():
    order = _make_order(
        subtotal=Decimal("350.00"),
        discount_amount=Decimal("20.00"),
        delivery_fee=Decimal("50.00"),
        total=Decimal("380.00"),
    )
    _add_item(order)

    context = build_receipt_context(order)

    assert context["subtotal"] == "350.00"
    assert context["discount_amount"] == "20.00"
    assert context["delivery_fee"] == "50.00"
    assert context["total"] == "380.00"


@pytest.mark.django_db
def test_receipt_context_shows_the_home_delivery_address():
    order = _make_order()
    _add_item(order)

    context = build_receipt_context(order)

    assert "12 High St" in context["delivery_details"]
    assert "Osu" in context["delivery_details"]
    assert "Near the Shell Station" in context["delivery_details"]


@pytest.mark.django_db
def test_receipt_context_says_pickup_rather_than_an_empty_address():
    """A pickup order carries no address at all (the model constraint
    enforces blank). The receipt must say so, not render empty lines."""
    order = _make_order(
        delivery_method=Order.DeliveryMethod.PICKUP,
        delivery_zone="",
        address="",
        area="",
        landmark="",
        delivery_fee=Decimal("0.00"),
        total=Decimal("350.00"),
    )
    _add_item(order)

    context = build_receipt_context(order)

    assert "ickup" in context["delivery_details"]
    assert "12 High St" not in context["delivery_details"]


@pytest.mark.django_db
def test_receipt_context_omits_a_landmark_that_was_never_given():
    """landmark stays optional even for home delivery -- a blank one must
    not leave a dangling empty line in the address block."""
    order = _make_order(landmark="")
    _add_item(order)

    context = build_receipt_context(order)

    assert not any(
        line.strip() == "" for line in context["delivery_details"].split("\n")
    )


@pytest.mark.django_db
def test_receipt_context_includes_reference_name_and_date():
    order = _make_order()
    _add_item(order)

    context = build_receipt_context(order)

    assert context["reference"] == order.payment_reference
    assert context["customer_name"] == "Kofi Mensah"
    assert str(timezone.localtime(order.created_at).year) in context["order_date"]


@pytest.mark.django_db
def test_every_receipt_placeholder_is_a_plain_string():
    """apps.notifications.rendering substitutes {{name}} with str(value)
    against a fixed context dict. A Decimal or a model instance leaking in
    would render as a repr in a real customer's receipt."""
    order = _make_order()
    _add_item(order)

    context = build_receipt_context(order)

    for name, value in context.items():
        assert isinstance(value, str), f"{name} is {type(value).__name__}, not str"


@pytest.mark.django_db
def test_confirmation_email_body_lists_the_items_and_totals():
    """The whole point of Task 60: the f-string this replaced was one
    line with no items, no breakdown and no delivery details."""
    from apps.notifications.models import NotificationTemplate
    from apps.notifications.rendering import render_email_or_default

    order = _make_order()
    _add_item(order, name="Emerald Aura Watch", quantity=2, unit_price="175.00")

    subject, body = render_email_or_default(
        NotificationTemplate.Key.ORDER_CONFIRMED_EMAIL,
        build_receipt_context(order),
        default_subject="",
        default_body="",
    )

    assert "Emerald Aura Watch" in body
    assert "Kofi Mensah" in body
    assert "12 High St" in body
    assert order.payment_reference in subject
    assert "{{" not in body  # every placeholder resolved


@pytest.mark.django_db
def test_a_live_admin_edit_reaches_the_next_confirmation_email():
    """Matches the per-send-site convention Task 48 established: prove the
    admin-editable row is really what gets rendered, not the fallback."""
    from apps.notifications.models import NotificationTemplate
    from apps.notifications.rendering import render_email_or_default

    order = _make_order()
    _add_item(order)
    NotificationTemplate.objects.filter(
        key=NotificationTemplate.Key.ORDER_CONFIRMED_EMAIL
    ).update(body="Akwaaba {{customer_name}}! Total GHS {{total}}.")

    _, body = render_email_or_default(
        NotificationTemplate.Key.ORDER_CONFIRMED_EMAIL,
        build_receipt_context(order),
        default_subject="",
        default_body="",
    )

    assert body == "Akwaaba Kofi Mensah! Total GHS 400.00."


@pytest.mark.django_db
def test_the_real_send_site_uses_the_admin_editable_template(settings):
    """Task 60's actual wiring. Task 48 added a dedicated test per send
    site proving a live admin edit reaches the next real send -- this is
    the one that was missing, because this send site was never migrated.

    Since Task 62 the real send happens in send_order_receipt_email, run by
    the Celery worker, so that is what this calls. That the confirmation
    path enqueues it is proven separately, in test_receipt_pdf.py."""
    from unittest.mock import patch

    from django.core import mail

    from apps.notifications.models import NotificationTemplate
    from apps.orders.receipt_email import send_order_receipt_email

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    order = _make_order()
    _add_item(order)
    NotificationTemplate.objects.filter(
        key=NotificationTemplate.Key.ORDER_CONFIRMED_EMAIL
    ).update(subject="Edited subject", body="Edited body for {{customer_name}}")

    # Task 62: the email is built by send_order_receipt_email in the Celery
    # worker; _send_confirmation_notifications now only enqueues it. The
    # PDF renderer is patched so WeasyPrint is never imported in tests.
    with patch("apps.orders.receipt_pdf.render_receipt_pdf", return_value=None):
        send_order_receipt_email(order)

    assert len(mail.outbox) == 1
    assert mail.outbox[0].subject == "Edited subject"
    assert mail.outbox[0].body == "Edited body for Kofi Mensah"


@pytest.mark.django_db
def test_no_confirmation_email_is_sent_for_a_legacy_order_with_no_email(
    settings, django_capture_on_commit_callbacks
):
    """Order.email stays blank=True for orders placed before email became
    required at checkout. Those must still skip the email cleanly rather
    than sending to an empty address.

    Code review (PR #93) proved the previous version could never fail: since
    Task 62 this function only enqueues, so mail.outbox stayed empty for
    every order, with or without an address. It now checks the enqueue."""
    from unittest.mock import patch

    from apps.orders.services import _send_confirmation_notifications

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    order = _make_order(email="")
    _add_item(order)

    with (
        patch("apps.orders.services.send_sms"),
        patch("apps.orders.services.send_order_receipt_email_task") as task,
        django_capture_on_commit_callbacks(execute=True),
    ):
        _send_confirmation_notifications(order)

    task.delay.assert_not_called()


@pytest.mark.django_db
def test_a_broken_receipt_never_escapes_the_notification_boundary(settings):
    """CodeRabbit (PR #91): _send_confirmation_notifications runs inside
    confirm_order_payment's _attempt, AFTER the money/stock/PV
    transaction commits, and _attempt is wrapped by
    retry_on_lock_contention. Building the receipt context outside both
    channels' try blocks meant a failure there escaped the helper
    entirely -- skipping both notifications on an already-confirmed
    order, and, if it resembled an OperationalError, re-running an
    already-committed transaction.

    That is exactly what this function's per-channel guards exist to
    prevent, so the context must be built inside them."""
    from unittest.mock import patch

    from apps.orders.services import _send_confirmation_notifications

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    order = _make_order()
    _add_item(order)

    with patch(
        "apps.orders.services.build_receipt_context",
        side_effect=RuntimeError("receipt blew up"),
    ):
        _send_confirmation_notifications(order)  # must not raise
