from datetime import timedelta
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

import pytest

from apps.catalog.models import Category, Product
from apps.orders.models import Order, OrderItem

try:
    import weasyprint  # noqa: F401

    _WEASYPRINT_AVAILABLE = True
except OSError:
    # WeasyPrint's own __init__ eagerly dlopen()s the system Pango
    # library at import time -- this raises OSError, not ImportError,
    # when Pango isn't present (source-driven-development, 2026-07-26).
    _WEASYPRINT_AVAILABLE = False

User = get_user_model()
_ref_seq = count(1)


def _make_order(
    *,
    status=Order.Status.CONFIRMED,
    full_name="Ama Mensah",
    phone_number="+233241234567",
    created_at=None,
    total=Decimal("450.00"),
):
    order = Order.objects.create(
        full_name=full_name,
        phone_number=phone_number,
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=total,
        delivery_fee=Decimal("0"),
        total=total,
        payment_reference=f"order-test-ref-{next(_ref_seq)}",
        status=status,
        confirmed_at=timezone.now() if status != Order.Status.PENDING else None,
    )
    if created_at is not None:
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        order.refresh_from_db()
    return order


def _queue_url(**params):
    url = reverse("admin_portal:order_management_queue")
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{url}?{query}"
    return url


def _action_url(order):
    return reverse("admin_portal:order_management_action", args=[order.pk])


def _detail_url(order):
    return reverse("admin_portal:order_detail", args=[order.pk])


def _invoice_url(order):
    return reverse("admin_portal:order_invoice_pdf", args=[order.pk])


def _add_item(order, *, product_name="Vitality Pulse Smart Ring", quantity=2):
    category, _ = Category.objects.get_or_create(name="Wellness", slug="wellness")
    product = Product.objects.create(
        name=product_name,
        category=category,
        price=Decimal("450.00"),
        pv_value=60,
        stock=5,
        is_active=True,
    )
    return OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product_name,
        quantity=quantity,
        unit_price=product.price,
    )


@pytest.mark.django_db
def test_queue_requires_admin_portal_staff(client, db):
    non_staff = User.objects.create_user(username="+233241111111", password="Passw0rd!")
    client.force_login(non_staff)

    response = client.get(_queue_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_queue_lists_orders(staff_client):
    order = _make_order()

    response = staff_client.get(_queue_url())

    assert response.status_code == 200
    assert order in response.context["page_obj"].object_list


@pytest.mark.django_db
def test_queue_filters_by_status(staff_client):
    confirmed = _make_order(status=Order.Status.CONFIRMED)
    cancelled = _make_order(status=Order.Status.CANCELLED)

    response = staff_client.get(_queue_url(status="cancelled"))

    results = list(response.context["page_obj"].object_list)
    assert cancelled in results
    assert confirmed not in results


@pytest.mark.django_db
def test_queue_filters_by_date_range(staff_client):
    now = timezone.now()
    old_order = _make_order(created_at=now - timedelta(days=10))
    recent_order = _make_order(created_at=now - timedelta(hours=1))

    response = staff_client.get(
        _queue_url(date_from=(now - timedelta(days=1)).date().isoformat())
    )

    results = list(response.context["page_obj"].object_list)
    assert recent_order in results
    assert old_order not in results


@pytest.mark.django_db
def test_queue_filters_by_customer_search(staff_client):
    match = _make_order(full_name="Kofi Boateng")
    other = _make_order(full_name="Ama Mensah")

    response = staff_client.get(_queue_url(q="Kofi"))

    results = list(response.context["page_obj"].object_list)
    assert match in results
    assert other not in results


@pytest.mark.django_db
def test_queue_search_matches_payment_reference(staff_client):
    order = _make_order()

    response = staff_client.get(_queue_url(q=order.payment_reference))

    results = list(response.context["page_obj"].object_list)
    assert order in results


@pytest.mark.django_db
def test_queue_ordering_has_a_stable_pk_tie_breaker():
    """Task 15's own pagination bug (order_by with no tie-breaker skipping/
    duplicating a row on a timestamp collision) must not be reintroduced
    here -- created_at alone isn't guaranteed unique, especially across
    orders confirmed in the same batch/second. Checked directly against
    the queryset's own order_by, matching how this exact bug class is
    normally verified in this codebase (the ordering clause itself, not
    a full page-boundary simulation)."""
    from django.test import RequestFactory

    from apps.admin_portal.views import _filtered_orders

    request = RequestFactory().get("/")
    orders, *_ = _filtered_orders(request)

    assert orders.query.order_by[-1] in ("pk", "-pk")


@pytest.mark.django_db
def test_htmx_queue_request_returns_only_the_results_partial(staff_client):
    """Real-time search (no Filter button): a keyup-triggered htmx request
    must get back just the results fragment, not the full page shell --
    otherwise the sidebar/header would get swapped into the results div
    on every keystroke. Mirrors test_distributor_directory.py's own
    test_htmx_search_request_returns_only_the_results_partial exactly."""
    order = _make_order(full_name="Ama Mensah")

    response = staff_client.get(_queue_url(q="Ama"), headers={"HX-Request": "true"})

    body = response.content.decode()
    assert order.payment_reference in body
    assert "Bancostore Admin Portal" not in body


@pytest.mark.django_db
def test_non_htmx_queue_request_returns_the_full_page(staff_client):
    order = _make_order(full_name="Ama Mensah")

    response = staff_client.get(_queue_url(q="Ama"))

    body = response.content.decode()
    assert order.payment_reference in body
    assert "Bancostore Admin Portal" in body


# ---------------------------------------------------------------------------
# apps.admin_portal.views.order_detail
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_order_detail_requires_admin_portal_staff(client, db):
    order = _make_order(status=Order.Status.CONFIRMED)
    non_staff = User.objects.create_user(username="+233241111114", password="Passw0rd!")
    client.force_login(non_staff)

    response = client.get(_detail_url(order))

    assert response.status_code == 403


@pytest.mark.django_db
def test_order_detail_shows_the_order_and_its_items(staff_client):
    order = _make_order(status=Order.Status.CONFIRMED)
    _add_item(order, product_name="Vitality Pulse Smart Ring", quantity=2)

    response = staff_client.get(_detail_url(order))

    assert response.status_code == 200
    assert response.context["order"] == order
    assert list(response.context["order"].items.all())[0].product_name == (
        "Vitality Pulse Smart Ring"
    )


@pytest.mark.django_db
def test_order_detail_returns_404_for_a_missing_order(staff_client):
    response = staff_client.get(reverse("admin_portal:order_detail", args=[999999]))

    assert response.status_code == 404


@pytest.mark.django_db
def test_order_detail_offers_only_the_legal_next_statuses(staff_client):
    """A CONFIRMED order can only advance to PROCESSING next (18a's own
    transition graph) -- Dispatched/Delivered must not be offered yet."""
    order = _make_order(status=Order.Status.CONFIRMED)

    response = staff_client.get(_detail_url(order))

    offered = dict(response.context["advanceable_statuses"])
    assert Order.Status.PROCESSING in offered
    assert Order.Status.DISPATCHED not in offered
    assert Order.Status.DELIVERED not in offered


@pytest.mark.django_db
def test_order_detail_hides_cancel_once_dispatched(staff_client):
    """Cancellation is pre-dispatch only (18a's transition graph) -- a
    DISPATCHED order must not offer a Cancel action."""
    order = _make_order(status=Order.Status.DISPATCHED)

    response = staff_client.get(_detail_url(order))

    assert response.context["can_cancel"] is False
    assert response.context["can_refund"] is True


@pytest.mark.django_db
def test_order_detail_never_offers_cancel_for_a_pending_order(staff_client):
    """cancel_or_refund_order (18b) only operates on already-paid orders
    -- calling it on a PENDING order is a documented caller bug (it
    silently no-ops, raising nothing), even though is_legal_order_status_
    transition alone would say PENDING -> CANCELLED is legal (that edge
    exists for Task 17c/17d's own automatic pre-payment cancellation and
    Task 18d's auto-cancel, not for this admin action). can_cancel must
    exclude PENDING explicitly so the Cancel Order button is never shown
    for an order this call would silently do nothing to."""
    order = _make_order(status=Order.Status.PENDING)

    response = staff_client.get(_detail_url(order))

    assert response.context["can_cancel"] is False


# ---------------------------------------------------------------------------
# apps.admin_portal.views.order_management_action
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_action_view_requires_admin_portal_staff(client, db):
    order = _make_order(status=Order.Status.CONFIRMED)
    non_staff = User.objects.create_user(username="+233241111112", password="Passw0rd!")
    client.force_login(non_staff)

    response = client.post(_action_url(order), {"action": "cancel"})

    assert response.status_code == 403
    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED


@pytest.mark.django_db
def test_cancel_action_calls_the_real_service_function(staff_client):
    order = _make_order(status=Order.Status.CONFIRMED)

    response = staff_client.post(_action_url(order), {"action": "cancel"})

    order.refresh_from_db()
    assert order.status == Order.Status.CANCELLED
    assert response.status_code == 302


@pytest.mark.django_db
def test_refund_action_requires_a_restock_choice(staff_client):
    order = _make_order(status=Order.Status.CONFIRMED)

    response = staff_client.post(_action_url(order), {"action": "refund"})

    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED  # rejected, no restock given
    assert response.status_code == 302


@pytest.mark.django_db
def test_refund_action_with_restock_choice_succeeds(staff_client):
    order = _make_order(status=Order.Status.CONFIRMED)

    response = staff_client.post(
        _action_url(order), {"action": "refund", "restock": "false"}
    )

    order.refresh_from_db()
    assert order.status == Order.Status.REFUNDED
    assert response.status_code == 302


@pytest.mark.django_db
def test_advance_action_calls_the_real_service_function_with_tracking_note(
    staff_client,
):
    order = _make_order(status=Order.Status.CONFIRMED)

    response = staff_client.post(
        _action_url(order),
        {
            "action": "advance",
            "to_status": "processing",
            "tracking_note": "Packed and ready.",
        },
    )

    order.refresh_from_db()
    assert order.status == Order.Status.PROCESSING
    assert order.tracking_note == "Packed and ready."
    assert response.status_code == 302


@pytest.mark.django_db
def test_illegal_transition_shows_an_error_message_not_a_500(staff_client):
    order = _make_order(status=Order.Status.CONFIRMED)

    response = staff_client.post(
        _action_url(order), {"action": "advance", "to_status": "delivered"}
    )

    assert response.status_code == 302
    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    messages_list = list(get_messages(response.wsgi_request))
    assert any("isn't allowed" in str(m) for m in messages_list)
    assert not any("Illegal order status transition" in str(m) for m in messages_list)


@pytest.mark.django_db
def test_error_messages_strip_the_internal_function_name_prefix(staff_client):
    """security-and-hardening review (2026-07-26): apps.orders.services's
    own ValueError messages are prefixed with the raising function's
    name for logs -- that internal detail shouldn't leak into an
    admin-facing flash message. advance_order_status's own to_status
    validation is the one ValueError this view's "advance" branch can
    actually trigger (the "refund" branch's own restock validation
    happens in the view itself, before the service call)."""
    order = _make_order(status=Order.Status.CONFIRMED)

    response = staff_client.post(
        _action_url(order), {"action": "advance", "to_status": "banana"}
    )

    messages_list = list(get_messages(response.wsgi_request))
    assert any(
        "to_status must be one of" in str(m) and "advance_order_status:" not in str(m)
        for m in messages_list
    )


@pytest.mark.django_db
def test_unrecognized_action_is_a_safe_no_op(staff_client):
    order = _make_order(status=Order.Status.CONFIRMED)

    response = staff_client.post(_action_url(order), {"action": "nonsense"})

    assert response.status_code == 302
    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED


@pytest.mark.django_db
def test_invoice_pdf_requires_admin_portal_staff(client, db):
    order = _make_order()
    non_staff = User.objects.create_user(username="+233241111113", password="Passw0rd!")
    client.force_login(non_staff)

    response = client.get(_invoice_url(order))

    assert response.status_code == 403


@pytest.mark.django_db
def test_invoice_template_renders_every_adr_0006_field(staff_client):
    """Template-level check, no WeasyPrint involved: proves the HTML that
    WeasyPrint will convert actually contains ADR-0006 decision 7's exact
    field list (Order ID, customer name, items ordered, delivery method/
    address, total, payment status) -- independent of whether WeasyPrint
    itself can run on this machine."""
    from django.template.loader import render_to_string

    order = _make_order(full_name="Kofi Boateng")
    _add_item(order, product_name="Vitality Pulse Smart Ring", quantity=2)

    html = render_to_string("admin_portal/order_invoice.html", {"order": order})

    assert str(order.pk) in html
    assert order.payment_reference in html
    assert "Kofi Boateng" in html
    assert "Vitality Pulse Smart Ring" in html
    assert "450.00" in html
    assert order.get_status_display() in html
    assert order.get_delivery_method_display() in html


@pytest.mark.django_db
@pytest.mark.skipif(
    not _WEASYPRINT_AVAILABLE,
    reason=(
        "WeasyPrint's system-level Pango library isn't installed on this "
        "machine (2026-07-26: this Mac is macOS 12, an unsupported "
        "Homebrew Tier-3 configuration -- `brew install weasyprint` "
        "failed after ~50 minutes trying to compile Pango's dependency "
        "chain from source, including Python 3.13 itself). Verified for "
        "real in CI instead (.github/workflows/ci.yml installs "
        "libpango-1.0-0 via apt, an ordinary pre-built Ubuntu package). "
        "Self-healing: this skip lifts automatically the moment "
        "`import weasyprint` succeeds, here or anywhere else."
    ),
)
def test_invoice_pdf_end_to_end_generates_a_real_pdf(staff_client):
    """Real, unmocked WeasyPrint call -- requires the system-level Pango
    library to actually be installed. This is a pure library-integration
    smoke test (does WeasyPrint actually run on this machine and produce
    valid PDF bytes) -- the *content* correctness is already proven
    independently by test_invoice_template_renders_every_adr_0006_field
    above, which needs no PDF library at all."""
    order = _make_order()
    _add_item(order)

    response = staff_client.get(_invoice_url(order))

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response.content[:5] == b"%PDF-"
    assert len(response.content) > 500  # a trivially empty/broken PDF is tiny
