from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model

import pytest

from apps.catalog.models import Category, Product
from apps.distributors.models import Distributor
from apps.orders.models import Order
from apps.reporting.models import DailyProductSales
from apps.reporting.services import (
    get_best_selling_products_report,
    get_commissions_vs_revenue_report,
    get_new_vs_returning_customers_report,
)
from apps.wallet.models import Wallet, WalletTransaction

User = get_user_model()
_ref_seq = count(1)


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


# ---------------------------------------------------------------------------
# get_best_selling_products_report
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_sums_units_and_revenue_per_product_across_the_range():
    product = _make_product()
    _make_sales_row(date(2026, 8, 1), product, units_sold=2, revenue=Decimal("900.00"))
    _make_sales_row(date(2026, 8, 2), product, units_sold=1, revenue=Decimal("450.00"))

    report = get_best_selling_products_report(date(2026, 8, 1), date(2026, 8, 2))

    assert report == [
        {
            "product_id": product.pk,
            "product_name": product.name,
            "units_sold": 3,
            "revenue": Decimal("1350.00"),
        }
    ]


@pytest.mark.django_db
def test_orders_by_units_sold_descending():
    low_seller = _make_product(name="Low Seller")
    high_seller = _make_product(name="High Seller")
    _make_sales_row(date(2026, 8, 1), low_seller, units_sold=1)
    _make_sales_row(date(2026, 8, 1), high_seller, units_sold=10)

    report = get_best_selling_products_report(date(2026, 8, 1), date(2026, 8, 1))

    assert [row["product_name"] for row in report] == ["High Seller", "Low Seller"]


@pytest.mark.django_db
def test_excludes_rows_outside_the_range():
    product = _make_product()
    _make_sales_row(date(2026, 7, 31), product, units_sold=99)

    report = get_best_selling_products_report(date(2026, 8, 1), date(2026, 8, 1))

    assert report == []


@pytest.mark.django_db
def test_respects_the_limit():
    for i in range(5):
        _make_sales_row(
            date(2026, 8, 1), _make_product(name=f"Product {i}"), units_sold=1
        )

    report = get_best_selling_products_report(
        date(2026, 8, 1), date(2026, 8, 1), limit=2
    )

    assert len(report) == 2


# ---------------------------------------------------------------------------
# get_new_vs_returning_customers_report
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_customers_first_ever_order_inside_the_range_counts_as_new():
    customer = _make_customer()
    _make_order(customer=customer, created_on=date(2026, 8, 5))

    report = get_new_vs_returning_customers_report(date(2026, 8, 1), date(2026, 8, 10))

    assert report == {"new_customers": 1, "returning_customers": 0}


@pytest.mark.django_db
def test_a_customer_with_an_earlier_order_before_the_range_counts_as_returning():
    customer = _make_customer()
    _make_order(
        customer=customer, created_on=date(2026, 7, 1)
    )  # first order, before range
    _make_order(customer=customer, created_on=date(2026, 8, 5))  # order inside range

    report = get_new_vs_returning_customers_report(date(2026, 8, 1), date(2026, 8, 10))

    assert report == {"new_customers": 0, "returning_customers": 1}


@pytest.mark.django_db
def test_a_customer_is_counted_once_even_with_multiple_orders_in_range():
    customer = _make_customer()
    _make_order(customer=customer, created_on=date(2026, 8, 2))
    _make_order(customer=customer, created_on=date(2026, 8, 5))

    report = get_new_vs_returning_customers_report(date(2026, 8, 1), date(2026, 8, 10))

    assert report == {"new_customers": 1, "returning_customers": 0}


@pytest.mark.django_db
def test_guest_orders_are_excluded_entirely():
    _make_order(customer=None, created_on=date(2026, 8, 5))

    report = get_new_vs_returning_customers_report(date(2026, 8, 1), date(2026, 8, 10))

    assert report == {"new_customers": 0, "returning_customers": 0}


@pytest.mark.django_db
def test_unpaid_orders_do_not_count_toward_either_bucket():
    customer = _make_customer()
    _make_order(
        customer=customer, created_on=date(2026, 8, 5), status=Order.Status.PENDING
    )

    report = get_new_vs_returning_customers_report(date(2026, 8, 1), date(2026, 8, 10))

    assert report == {"new_customers": 0, "returning_customers": 0}


@pytest.mark.django_db
def test_a_range_with_no_orders_returns_zeroed_counts():
    report = get_new_vs_returning_customers_report(date(2026, 1, 1), date(2026, 1, 31))

    assert report == {"new_customers": 0, "returning_customers": 0}


# ---------------------------------------------------------------------------
# get_commissions_vs_revenue_report
# ---------------------------------------------------------------------------


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


@pytest.mark.django_db
def test_sums_only_commission_transaction_types_in_range():
    distributor = _make_distributor()
    _credit_wallet(
        distributor,
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        amount=Decimal("100.00"),
        created_on=date(2026, 8, 1),
    )
    _credit_wallet(
        distributor,
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        amount=Decimal("50.00"),
        created_on=date(2026, 8, 2),
    )
    _credit_wallet(
        distributor,
        transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS,
        amount=Decimal("25.00"),
        created_on=date(2026, 8, 2),
    )
    # A withdrawal-related transaction type must never inflate this figure.
    _credit_wallet(
        distributor,
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_REVERSAL,
        amount=Decimal("999.00"),
        created_on=date(2026, 8, 1),
    )

    report = get_commissions_vs_revenue_report(date(2026, 8, 1), date(2026, 8, 2))

    assert report["commissions_paid"] == Decimal("175.00")


@pytest.mark.django_db
def test_excludes_commission_transactions_outside_the_range():
    distributor = _make_distributor()
    _credit_wallet(
        distributor,
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        amount=Decimal("100.00"),
        created_on=date(2026, 7, 31),
    )

    report = get_commissions_vs_revenue_report(date(2026, 8, 1), date(2026, 8, 2))

    assert report["commissions_paid"] == Decimal("0.00")


@pytest.mark.django_db
def test_a_range_with_no_data_returns_zeroed_totals():
    report = get_commissions_vs_revenue_report(date(2026, 1, 1), date(2026, 1, 31))

    assert report == {"revenue": Decimal("0.00"), "commissions_paid": Decimal("0.00")}
