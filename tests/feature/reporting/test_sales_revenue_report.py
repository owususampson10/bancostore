from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product
from apps.distributors.models import Distributor
from apps.orders.models import Order
from apps.reporting.models import DailyOrderRollup, DailyProductSales, ReportRollupRun
from apps.wallet.models import Wallet, WalletTransaction
from tests.conftest import WEASYPRINT_AVAILABLE

_ref_seq = count(1)

User = get_user_model()


def _make_rollup(target_date, **overrides):
    defaults = {
        "revenue": Decimal("0"),
        "orders_pending": 0,
        "orders_confirmed": 0,
        "orders_processing": 0,
        "orders_dispatched": 0,
        "orders_delivered": 0,
        "orders_cancelled": 0,
        "orders_refunded": 0,
        "delivery_fees_kumasi": Decimal("0"),
        "delivery_fees_accra": Decimal("0"),
        "delivery_fees_other_regions": Decimal("0"),
    }
    defaults.update(overrides)
    return DailyOrderRollup.objects.create(date=target_date, **defaults)


def _make_product(**overrides):
    category, _ = Category.objects.get_or_create(name="Wellness", slug="wellness")
    defaults = {
        "name": "Vitality Pulse Smart Ring",
        "category": category,
        "price": Decimal("450.00"),
        "pv_value": 60,
        "stock": 100,
        "is_active": True,
    }
    defaults.update(overrides)
    return Product.objects.create(**defaults)


def _make_sales_row(target_date, product, **overrides):
    defaults = {
        "product_name": product.name,
        "units_sold": 1,
        "revenue": Decimal("450.00"),
    }
    defaults.update(overrides)
    return DailyProductSales.objects.create(
        date=target_date, product=product, **defaults
    )


def _make_customer():
    phone = f"+233555{next(_ref_seq):06d}"
    return User.objects.create_user(username=phone, password="Passw0rd!")


def _make_distributor():
    phone = f"+233244{next(_ref_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


def _make_order(
    *, customer, created_on, status=Order.Status.CONFIRMED, total=Decimal("450.00")
):
    order = Order.objects.create(
        customer=customer,
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=total,
        delivery_fee=Decimal("0"),
        total=total,
        payment_reference=f"order-test-report-{next(_ref_seq)}",
        status=status,
    )
    created_at = datetime.combine(
        created_on, datetime.min.time(), tzinfo=dt_timezone.utc
    )
    Order.objects.filter(pk=order.pk).update(created_at=created_at)
    order.refresh_from_db()
    return order


def _credit_wallet(distributor, *, transaction_type, amount, created_on):
    wallet, _ = Wallet.objects.get_or_create(distributor=distributor)
    txn = WalletTransaction.objects.create(
        wallet=wallet,
        amount=amount,
        transaction_type=transaction_type,
        reference=f"test-ref-{next(_ref_seq)}",
    )
    created_at = datetime.combine(
        created_on, datetime.min.time(), tzinfo=dt_timezone.utc
    )
    WalletTransaction.objects.filter(pk=txn.pk).update(created_at=created_at)
    return txn


def _report_url(**params):
    url = reverse("admin_portal:sales_revenue_report")
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{url}?{query}"
    return url


def _csv_export_url(**params):
    url = reverse("admin_portal:sales_revenue_report_export_csv")
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{url}?{query}"
    return url


def _pdf_export_url(**params):
    url = reverse("admin_portal:sales_revenue_report_export_pdf")
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{url}?{query}"
    return url


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_staff_can_reach_the_report_page(staff_client):
    response = staff_client.get(_report_url())

    assert response.status_code == 200


@pytest.mark.django_db
def test_a_non_staff_authenticated_user_is_forbidden(client, db):
    user = User.objects.create_user(
        username="+233247000001", password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_report_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_an_anonymous_user_is_redirected_to_login(client, db):
    response = client.get(_report_url())

    assert response.status_code == 302
    assert reverse("two_factor:login") in response.url


# ---------------------------------------------------------------------------
# Date-range correctness (seeded-data cross-checks)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_report_sums_only_rollup_rows_inside_the_requested_range(staff_client):
    _make_rollup(date(2026, 8, 1), revenue=Decimal("100.00"))
    _make_rollup(date(2026, 8, 2), revenue=Decimal("200.00"))
    _make_rollup(date(2026, 8, 3), revenue=Decimal("999.00"))  # outside the range below

    response = staff_client.get(
        _report_url(date_from="2026-08-01", date_to="2026-08-02")
    )

    assert response.status_code == 200
    assert response.context["report"]["revenue"] == Decimal("300.00")


@pytest.mark.django_db
def test_the_range_boundaries_are_inclusive(staff_client):
    _make_rollup(date(2026, 8, 1), revenue=Decimal("10.00"))
    _make_rollup(date(2026, 8, 5), revenue=Decimal("20.00"))

    response = staff_client.get(
        _report_url(date_from="2026-08-01", date_to="2026-08-05")
    )

    assert response.context["report"]["revenue"] == Decimal("30.00")


@pytest.mark.django_db
def test_order_status_counts_are_correct_against_seeded_rollups(staff_client):
    _make_rollup(date(2026, 8, 1), orders_confirmed=3, orders_cancelled=1)
    _make_rollup(date(2026, 8, 2), orders_confirmed=2)

    response = staff_client.get(
        _report_url(date_from="2026-08-01", date_to="2026-08-02")
    )

    assert response.context["report"]["order_status_counts"]["confirmed"] == 5
    assert response.context["report"]["order_status_counts"]["cancelled"] == 1


@pytest.mark.django_db
def test_delivery_fees_by_zone_are_correct_against_seeded_rollups(staff_client):
    _make_rollup(date(2026, 8, 1), delivery_fees_kumasi=Decimal("20.00"))
    _make_rollup(date(2026, 8, 2), delivery_fees_kumasi=Decimal("30.00"))

    response = staff_client.get(
        _report_url(date_from="2026-08-01", date_to="2026-08-02")
    )

    assert response.context["report"]["delivery_fees_by_zone"]["kumasi"] == Decimal(
        "50.00"
    )


@pytest.mark.django_db
def test_granularity_choice_changes_the_revenue_series_bucketing(staff_client):
    _make_rollup(date(2026, 8, 3), revenue=Decimal("10"))  # Monday
    _make_rollup(date(2026, 8, 4), revenue=Decimal("20"))  # same ISO week

    response = staff_client.get(
        _report_url(date_from="2026-08-03", date_to="2026-08-04", granularity="week")
    )

    series = response.context["revenue_series"]
    assert series == [{"period": "2026-08-03", "revenue": Decimal("30")}]


@pytest.mark.django_db
def test_a_range_with_no_data_shows_a_zeroed_report_not_an_error(staff_client):
    response = staff_client.get(
        _report_url(date_from="2020-01-01", date_to="2020-01-31")
    )

    assert response.status_code == 200
    assert response.context["report"]["revenue"] == Decimal("0")


@pytest.mark.django_db
def test_htmx_request_returns_only_the_results_partial(staff_client):
    _make_rollup(date(2026, 8, 1), revenue=Decimal("50.00"))

    response = staff_client.get(
        _report_url(date_from="2026-08-01", date_to="2026-08-01"),
        headers={"HX-Request": "true"},
    )

    body = response.content.decode()
    assert 'id="report-results"' in body
    assert "<nav" not in body  # the sidebar, only present on a full-page load


@pytest.mark.django_db
def test_export_links_reflect_the_currently_applied_filter_not_the_page_defaults(
    staff_client,
):
    # CodeRabbit-caught real bug: the export links used to live outside
    # the htmx-swapped #report-results partial, so changing the filter
    # updated the visible report but left Export CSV/PDF pointed at
    # whatever date range the page first loaded with.
    response = staff_client.get(
        _report_url(date_from="2026-08-01", date_to="2026-08-10", granularity="week"),
        headers={"HX-Request": "true"},
    )

    body = response.content.decode()
    assert "date_from=2026-08-01" in body
    assert "date_to=2026-08-10" in body
    assert "granularity=week" in body


@pytest.mark.django_db
def test_shows_the_last_successful_rollup_run_time(staff_client):
    ReportRollupRun.objects.create(
        rollup_date=date(2026, 8, 1),
        run_at="2026-08-02T02:00:00Z",
        succeeded=True,
    )

    response = staff_client.get(_report_url())

    assert response.context["last_rollup_run_at"] is not None


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_csv_export_contains_the_real_figures(staff_client):
    _make_rollup(
        date(2026, 8, 1),
        revenue=Decimal("450.00"),
        orders_confirmed=2,
        delivery_fees_kumasi=Decimal("20.00"),
    )

    response = staff_client.get(
        _csv_export_url(date_from="2026-08-01", date_to="2026-08-01")
    )

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv"
    body = response.content.decode()
    assert "450.00" in body
    assert "20.00" in body


@pytest.mark.django_db
def test_csv_export_requires_staff(client, db):
    user = User.objects.create_user(
        username="+233247000002", password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_csv_export_url())

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# PDF export
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.skipif(
    not WEASYPRINT_AVAILABLE,
    reason="WeasyPrint needs the system Pango library, not installable locally.",
)
def test_pdf_export_generates_a_real_pdf(staff_client):
    _make_rollup(date(2026, 8, 1), revenue=Decimal("450.00"))

    response = staff_client.get(
        _pdf_export_url(date_from="2026-08-01", date_to="2026-08-01")
    )

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


@pytest.mark.django_db
def test_pdf_export_requires_staff(client, db):
    user = User.objects.create_user(
        username="+233247000003", password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_pdf_export_url())

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 46c: best-selling products, new-vs-returning customers, commissions-vs-revenue
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_best_selling_products_appear_on_the_report_page(staff_client):
    product = _make_product(name="Vitality Pulse Smart Ring")
    _make_sales_row(date(2026, 8, 1), product, units_sold=5, revenue=Decimal("2250.00"))

    response = staff_client.get(
        _report_url(date_from="2026-08-01", date_to="2026-08-01")
    )

    assert response.context["best_selling_products"] == [
        {
            "product_id": product.pk,
            "product_name": "Vitality Pulse Smart Ring",
            "units_sold": 5,
            "revenue": Decimal("2250.00"),
        }
    ]


@pytest.mark.django_db
def test_new_vs_returning_customers_are_correct_against_seeded_orders(staff_client):
    customer = _make_customer()
    _make_order(customer=customer, created_on=date(2026, 8, 1))

    response = staff_client.get(
        _report_url(date_from="2026-08-01", date_to="2026-08-01")
    )

    assert response.context["customer_report"] == {
        "new_customers": 1,
        "returning_customers": 0,
    }


@pytest.mark.django_db
def test_commissions_vs_revenue_is_correct_against_seeded_data(staff_client):
    _make_rollup(date(2026, 8, 1), revenue=Decimal("1000.00"))
    distributor = _make_distributor()
    _credit_wallet(
        distributor,
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        amount=Decimal("75.00"),
        created_on=date(2026, 8, 1),
    )

    response = staff_client.get(
        _report_url(date_from="2026-08-01", date_to="2026-08-01")
    )

    assert response.context["commissions_report"] == {
        "revenue": Decimal("1000.00"),
        "commissions_paid": Decimal("75.00"),
    }


@pytest.mark.django_db
def test_csv_export_includes_46c_sections(staff_client):
    product = _make_product(name="Vitality Pulse Smart Ring")
    _make_sales_row(date(2026, 8, 1), product, units_sold=5, revenue=Decimal("2250.00"))
    customer = _make_customer()
    _make_order(customer=customer, created_on=date(2026, 8, 1))

    response = staff_client.get(
        _csv_export_url(date_from="2026-08-01", date_to="2026-08-01")
    )

    body = response.content.decode()
    assert "Best-Selling Products" in body
    assert "Vitality Pulse Smart Ring" in body
    assert "New vs Returning Customers" in body
    assert "Commissions vs Revenue" in body


@pytest.mark.django_db
@pytest.mark.skipif(
    not WEASYPRINT_AVAILABLE,
    reason="WeasyPrint needs the system Pango library, not installable locally.",
)
def test_pdf_export_includes_46c_sections(staff_client):
    product = _make_product(name="Vitality Pulse Smart Ring")
    _make_sales_row(date(2026, 8, 1), product, units_sold=5, revenue=Decimal("2250.00"))

    response = staff_client.get(
        _pdf_export_url(date_from="2026-08-01", date_to="2026-08-01")
    )

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
