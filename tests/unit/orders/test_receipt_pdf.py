"""Task 62. The PDF receipt attached to the order confirmation email.

Chosen over a public "download receipt" link after an adversarial design
review found 13 problems with the link, almost all caused by it needing a
public URL: an anonymous CPU-exhaustion path against the site's single
Daphne process, a leaked SECRET_KEY exposing every order's home address,
no per-link revocation, credentials in access logs, and WeasyPrint exposed
to unauthenticated traffic. An attachment has no public URL.

NONE OF THESE TESTS IMPORT WEASYPRINT. On this project's dev Mac, importing
it a second time after a failed dlopen segfaulted the interpreter (Task
45), which is why tests/conftest.py must remain its only import site.
Every renderer here is patched or injected. The real render is verified on
the production server, where Pango is installed.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.core import mail

import pytest

from apps.catalog.models import Category, Product
from apps.orders import receipt_pdf
from apps.orders.models import Order, OrderItem
from apps.orders.receipt_email import send_order_receipt_email

FAKE_PDF = b"%PDF-1.7 fake receipt bytes"


def _make_order(**overrides):
    defaults = {
        "full_name": "Kofi Mensah",
        "phone_number": "+233241234567",
        "email": "kofi@example.com",
        "delivery_method": Order.DeliveryMethod.HOME_DELIVERY,
        "delivery_zone": Order.DeliveryZone.ACCRA,
        "address": "12 High Street",
        "area": "Osu",
        "landmark": "",
        "subtotal": Decimal("350.00"),
        "delivery_fee": Decimal("50.00"),
        "discount_amount": Decimal("0.00"),
        "total": Decimal("400.00"),
        "status": Order.Status.CONFIRMED,
        "payment_reference": "order-95f556e7ffd74681bc322711b6e5704b",
    }
    defaults.update(overrides)
    return Order.objects.create(**defaults)


def _add_item(order):
    category, _ = Category.objects.get_or_create(name="Watches", slug="watches")
    product = Product.objects.create(
        name="Emerald Aura Watch",
        category=category,
        price=Decimal("350.00"),
        pv_value=10,
        stock=5,
        is_active=True,
    )
    OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        quantity=1,
        unit_price=Decimal("350.00"),
        unit_pv=10,
    )


@pytest.fixture
def locmem(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


# --- The attachment --------------------------------------------------------


@pytest.mark.django_db
def test_the_receipt_email_carries_a_pdf_attachment(locmem):
    order = _make_order()
    _add_item(order)

    with patch.object(receipt_pdf, "render_receipt_pdf", return_value=FAKE_PDF):
        send_order_receipt_email(order)

    (message,) = mail.outbox
    (attachment,) = message.attachments
    filename, content, mimetype = attachment
    assert mimetype == "application/pdf"
    assert content == FAKE_PDF
    assert filename.endswith(".pdf")


@pytest.mark.django_db
def test_the_filename_is_distinct_per_order_not_order_prefix_collisions():
    """A design review caught that "first 8 characters of the reference"
    is always "order-XY" -- only 256 possible names, colliding in the
    customer's Downloads folder. The "order-" prefix is stripped first."""
    order = _make_order()

    name = receipt_pdf.receipt_pdf_filename(order)

    assert "order-" not in name
    assert "95f556e7" in name
    assert name.startswith("bancostore-receipt-")
    assert name.endswith(".pdf")


# --- The PDF must never cost the customer their receipt ---------------------


@pytest.mark.django_db
def test_a_pdf_failure_still_sends_the_receipt_email(locmem):
    """Losing the whole receipt because the PDF step broke would be far
    worse than a receipt with no PDF."""
    order = _make_order()
    _add_item(order)

    with patch.object(
        receipt_pdf, "render_receipt_pdf", side_effect=RuntimeError("render broke")
    ):
        send_order_receipt_email(order)

    (message,) = mail.outbox
    assert message.attachments == []
    # Code review (PR #93): the bare reference also sits in the HTML's
    # <title>, so that alone passed with the visible receipt body missing.
    assert order.payment_reference in message.alternatives[0][0]
    assert f">{order.payment_reference}</td>" in message.alternatives[0][0]


@pytest.mark.django_db
def test_an_unavailable_pdf_library_still_sends_the_receipt_email(locmem):
    """render_receipt_pdf returns None when WeasyPrint cannot load -- the
    normal state on this project's dev Mac, where Pango is missing."""
    order = _make_order()
    _add_item(order)

    with patch.object(receipt_pdf, "render_receipt_pdf", return_value=None):
        send_order_receipt_email(order)

    (message,) = mail.outbox
    assert message.attachments == []


# --- It must never slow down payment confirmation ---------------------------


@pytest.mark.django_db
def test_confirmation_enqueues_the_receipt_rather_than_building_it_inline(
    django_capture_on_commit_callbacks,
):
    """confirm_order_payment runs INLINE in the Paystack webhook and in the
    customer's post-payment redirect, in the site's single Daphne process.
    Rendering a PDF there would make Paystack and the customer wait on it.
    The email is handed to the Celery worker instead, after commit."""
    from apps.orders.services import _send_confirmation_notifications

    order = _make_order()
    _add_item(order)

    with (
        patch("apps.orders.services.send_sms"),
        patch("apps.orders.services.send_order_receipt_email_task") as task,
        patch.object(receipt_pdf, "render_receipt_pdf") as render,
        django_capture_on_commit_callbacks(execute=True),
    ):
        _send_confirmation_notifications(order)

    task.delay.assert_called_once_with(order.pk)
    render.assert_not_called()  # nothing rendered in the request path


@pytest.mark.django_db
def test_the_task_sends_the_receipt_for_an_existing_order(locmem):
    from apps.orders.tasks import send_order_receipt_email_task

    order = _make_order()
    _add_item(order)

    with patch.object(receipt_pdf, "render_receipt_pdf", return_value=FAKE_PDF):
        send_order_receipt_email_task(order.pk)

    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_the_task_quietly_ignores_a_deleted_order(locmem):
    """The order can be deleted between enqueue and run. That must not
    raise, or Celery would retry a task that can never succeed."""
    from apps.orders.tasks import send_order_receipt_email_task

    send_order_receipt_email_task(999_999_999)

    assert mail.outbox == []


@pytest.mark.django_db
def test_the_task_skips_an_order_whose_email_was_cleared(locmem):
    """The address can be blanked between enqueue and run. The task must
    not try to send to an empty address."""
    from apps.orders.tasks import send_order_receipt_email_task

    order = _make_order()
    _add_item(order)
    Order.objects.filter(pk=order.pk).update(email="")

    with patch("apps.orders.receipt_email.send_order_receipt_email") as send:
        send_order_receipt_email_task(order.pk)

    send.assert_not_called()


@pytest.mark.django_db
def test_a_broker_outage_never_escapes_the_notification_boundary():
    """Code review (PR #93): nothing exercised the guard around the enqueue.
    Production calls this after confirm_order_payment's transaction has
    committed, in autocommit mode, where on_commit runs its callback
    immediately -- so a Redis outage raises right there. on_commit is
    patched to run the callback immediately, exactly as production does;
    the test's own wrapping transaction would otherwise defer it past the
    guard. Escaping would land in retry_on_lock_contention after the money
    has already moved."""
    from apps.orders.services import _send_confirmation_notifications

    order = _make_order()
    _add_item(order)

    with (
        patch("apps.orders.services.send_sms"),
        patch("apps.orders.services.send_order_receipt_email_task") as task,
        patch(
            "apps.orders.services.transaction.on_commit",
            side_effect=lambda callback, *args, **kwargs: callback(),
        ),
    ):
        task.delay.side_effect = ConnectionError("Redis is down")
        _send_confirmation_notifications(order)  # must not raise

    task.delay.assert_called_once_with(order.pk)


# --- Hardening the PDF renderer --------------------------------------------


def test_the_url_fetcher_serves_only_the_local_logo():
    """The PDF is built from customer-typed names and addresses. Those are
    autoescaped, so markup cannot get in -- but the fetcher is the second
    line of defence: anything other than the one known logo file is
    refused, whatever asked for it."""
    FakeResponse = MagicMock()
    fetch = receipt_pdf.make_receipt_url_fetcher(FakeResponse)

    fetch(receipt_pdf.RECEIPT_LOGO_URI)

    FakeResponse.assert_called_once()
    assert FakeResponse.call_args.kwargs["body"]  # real logo bytes served


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://169.254.169.254/latest/meta-data/",
        "http://localhost:6379/",
        "https://bancostore.com/static/bancostore-brand/logo/other.png",
    ],
)
def test_the_url_fetcher_refuses_everything_else(url):
    """WeasyPrint catches a fetcher exception, logs "Failed to load image"
    and keeps rendering -- verified in weasyprint/urls.py and images.py --
    so refusing fails closed without breaking the PDF."""
    fetch = receipt_pdf.make_receipt_url_fetcher(MagicMock())

    with pytest.raises(ValueError):
        fetch(url)


@pytest.mark.django_db
def test_write_pdf_is_never_given_the_advisory_affected_options():
    """PYSEC-2026-3940 (weasyprint 69.0, fixed in 70.0): write_pdf()'s
    `stylesheets` and `xmp_metadata` options ignore the url_fetcher, giving
    SSRF and arbitrary local file read. This feature needs neither -- all
    styling is inline -- so it is not exposed, provided nobody ever adds
    them. This test is what enforces that."""
    order = _make_order()
    _add_item(order)
    fake_weasyprint = MagicMock()
    fake_weasyprint.HTML.return_value.write_pdf.return_value = FAKE_PDF

    with patch.object(receipt_pdf, "_load_weasyprint", return_value=fake_weasyprint):
        assert receipt_pdf.render_receipt_pdf(order) == FAKE_PDF

    # Code review (PR #93): reading call_args directly failed with a
    # confusing AttributeError on None if write_pdf was never called, and a
    # positional argument would have slipped past a kwargs-only check.
    write_pdf = fake_weasyprint.HTML.return_value.write_pdf
    write_pdf.assert_called_once()
    assert write_pdf.call_args.args == ()
    write_pdf_kwargs = write_pdf.call_args.kwargs
    assert "stylesheets" not in write_pdf_kwargs
    assert "xmp_metadata" not in write_pdf_kwargs
    html_kwargs = fake_weasyprint.HTML.call_args.kwargs
    assert "url_fetcher" in html_kwargs  # the restricted one is always set


@pytest.mark.django_db
def test_the_pdf_loads_the_logo_from_disk_not_the_internet():
    """A design review caught that reusing the email context would make
    the worker fetch its own logo back through the public site on every
    render -- slow, and broken whenever DNS, TLS or Nginx has a bad day."""
    order = _make_order()
    _add_item(order)
    fake_weasyprint = MagicMock()
    fake_weasyprint.HTML.return_value.write_pdf.return_value = FAKE_PDF

    with patch.object(receipt_pdf, "_load_weasyprint", return_value=fake_weasyprint):
        receipt_pdf.render_receipt_pdf(order)

    html = fake_weasyprint.HTML.call_args.kwargs["string"]
    assert receipt_pdf.RECEIPT_LOGO_URI in html
    assert "https://" not in html.split("<img", 1)[1].split(">", 1)[0]


def test_a_failed_library_load_is_never_retried_in_the_same_process():
    """On the dev Mac, a second failed WeasyPrint dlopen in one process
    segfaulted the interpreter (Task 45). A long-running local server
    confirming several orders would hit exactly that, so the first failure
    is remembered and never retried."""
    receipt_pdf._reset_weasyprint_cache()
    with patch.object(
        receipt_pdf.importlib, "import_module", side_effect=OSError("no pango")
    ) as imp:
        assert receipt_pdf._load_weasyprint() is None
        assert receipt_pdf._load_weasyprint() is None

    assert imp.call_count == 1
    receipt_pdf._reset_weasyprint_cache()
