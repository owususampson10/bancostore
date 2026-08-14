from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from itertools import count

import pytest

from apps.catalog.models import Category, Product
from apps.orders.models import Order, OrderItem
from apps.reporting.models import DailyOrderRollup, DailyProductSales, ReportRollupRun
from apps.reporting.services import compute_daily_rollup

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


def _make_order(
    *,
    status,
    created_on,
    total=Decimal("450.00"),
    delivery_zone="",
    delivery_fee=Decimal("0"),
):
    order = Order.objects.create(
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=(
            Order.DeliveryMethod.PICKUP
            if not delivery_zone
            else Order.DeliveryMethod.HOME_DELIVERY
        ),
        delivery_zone=delivery_zone,
        address="12 High St" if delivery_zone else "",
        area="Osu" if delivery_zone else "",
        subtotal=total - delivery_fee,
        delivery_fee=delivery_fee,
        total=total,
        payment_reference=f"order-test-rollup-{next(_ref_seq)}",
        status=status,
    )
    # created_at is auto_now_add=True -- can't be set at creation time,
    # matching this codebase's own established test convention (Task
    # 18d's _make_order(created_at=...) helper).
    created_at = datetime.combine(
        created_on, datetime.min.time(), tzinfo=dt_timezone.utc
    )
    Order.objects.filter(pk=order.pk).update(created_at=created_at)
    order.refresh_from_db()
    return order


def _add_item(order, product, *, quantity=1):
    return OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        quantity=quantity,
        unit_price=product.price,
        unit_pv=product.pv_value,
    )


TARGET_DATE = date(2026, 8, 10)


@pytest.mark.django_db
def test_revenue_only_counts_paid_orders_on_the_target_date():
    product = _make_product()
    confirmed = _make_order(
        status=Order.Status.CONFIRMED, created_on=TARGET_DATE, total=Decimal("450.00")
    )
    _add_item(confirmed, product)
    pending = _make_order(
        status=Order.Status.PENDING, created_on=TARGET_DATE, total=Decimal("100.00")
    )
    _add_item(pending, product)
    cancelled = _make_order(
        status=Order.Status.CANCELLED, created_on=TARGET_DATE, total=Decimal("200.00")
    )
    _add_item(cancelled, product)
    refunded = _make_order(
        status=Order.Status.REFUNDED, created_on=TARGET_DATE, total=Decimal("300.00")
    )
    _add_item(refunded, product)
    other_day = _make_order(
        status=Order.Status.CONFIRMED,
        created_on=date(2026, 8, 9),
        total=Decimal("999.00"),
    )
    _add_item(other_day, product)

    compute_daily_rollup(TARGET_DATE)

    rollup = DailyOrderRollup.objects.get(date=TARGET_DATE)
    assert rollup.revenue == Decimal("450.00")


@pytest.mark.django_db
def test_status_counts_include_every_order_regardless_of_payment():
    product = _make_product()
    for status in [
        Order.Status.PENDING,
        Order.Status.CONFIRMED,
        Order.Status.PROCESSING,
        Order.Status.DISPATCHED,
        Order.Status.DELIVERED,
        Order.Status.CANCELLED,
        Order.Status.REFUNDED,
    ]:
        order = _make_order(status=status, created_on=TARGET_DATE)
        _add_item(order, product)

    compute_daily_rollup(TARGET_DATE)

    rollup = DailyOrderRollup.objects.get(date=TARGET_DATE)
    assert rollup.orders_pending == 1
    assert rollup.orders_confirmed == 1
    assert rollup.orders_processing == 1
    assert rollup.orders_dispatched == 1
    assert rollup.orders_delivered == 1
    assert rollup.orders_cancelled == 1
    assert rollup.orders_refunded == 1


@pytest.mark.django_db
def test_delivery_fees_grouped_by_zone_only_for_paid_orders():
    product = _make_product()
    kumasi_paid = _make_order(
        status=Order.Status.CONFIRMED,
        created_on=TARGET_DATE,
        total=Decimal("470.00"),
        delivery_zone=Order.DeliveryZone.KUMASI,
        delivery_fee=Decimal("20.00"),
    )
    _add_item(kumasi_paid, product)
    accra_pending = _make_order(
        status=Order.Status.PENDING,
        created_on=TARGET_DATE,
        total=Decimal("500.00"),
        delivery_zone=Order.DeliveryZone.ACCRA,
        delivery_fee=Decimal("50.00"),
    )
    _add_item(accra_pending, product)

    compute_daily_rollup(TARGET_DATE)

    rollup = DailyOrderRollup.objects.get(date=TARGET_DATE)
    assert rollup.delivery_fees_kumasi == Decimal("20.00")
    assert rollup.delivery_fees_accra == Decimal("0")


@pytest.mark.django_db
def test_product_sales_sums_units_and_revenue_across_orders():
    product = _make_product(price=Decimal("450.00"))
    order_one = _make_order(
        status=Order.Status.CONFIRMED, created_on=TARGET_DATE, total=Decimal("450.00")
    )
    _add_item(order_one, product, quantity=1)
    order_two = _make_order(
        status=Order.Status.CONFIRMED, created_on=TARGET_DATE, total=Decimal("900.00")
    )
    _add_item(order_two, product, quantity=2)

    compute_daily_rollup(TARGET_DATE)

    sales = DailyProductSales.objects.get(date=TARGET_DATE, product=product)
    assert sales.units_sold == 3
    assert sales.revenue == Decimal("1350.00")
    assert sales.product_name == product.name


@pytest.mark.django_db
def test_product_sales_groups_by_product_even_when_snapshot_names_differ():
    """CodeRabbit-caught real bug: OrderItem.product_name is a per-order-
    line snapshot, so a product renamed mid-day used to split into two
    groups for the same DailyProductSales(date, product) unique
    constraint -- bulk_create then raised an IntegrityError and rolled
    back the whole day's rollup (compute_daily_rollup is one atomic
    transaction)."""
    product = _make_product(price=Decimal("450.00"))
    order_one = _make_order(
        status=Order.Status.CONFIRMED, created_on=TARGET_DATE, total=Decimal("450.00")
    )
    _add_item(order_one, product, quantity=1)
    order_two = _make_order(
        status=Order.Status.CONFIRMED, created_on=TARGET_DATE, total=Decimal("450.00")
    )
    item_two = _add_item(order_two, product, quantity=1)
    # Simulate the product having been renamed between the two orders --
    # item_two's snapshot no longer matches the live (or item_one's)
    # product_name.
    OrderItem.objects.filter(pk=item_two.pk).update(
        product_name=f"{product.name} (Renamed)"
    )

    compute_daily_rollup(TARGET_DATE)  # must not raise

    sales = DailyProductSales.objects.get(date=TARGET_DATE, product=product)
    assert sales.units_sold == 2
    assert sales.revenue == Decimal("900.00")


@pytest.mark.django_db
def test_product_sales_excludes_items_from_unpaid_orders():
    product = _make_product()
    pending = _make_order(
        status=Order.Status.PENDING, created_on=TARGET_DATE, total=Decimal("450.00")
    )
    _add_item(pending, product)

    compute_daily_rollup(TARGET_DATE)

    assert not DailyProductSales.objects.filter(
        date=TARGET_DATE, product=product
    ).exists()


@pytest.mark.django_db
def test_recompute_is_idempotent_and_overwrites_stale_product_rows():
    """A backfill that finds a product no longer sold that day (e.g. the
    order was cancelled after the first compute) must not leave a stale
    nonzero DailyProductSales row behind."""
    product_a = _make_product(name="A")
    product_b = _make_product(name="B")
    order = _make_order(
        status=Order.Status.CONFIRMED, created_on=TARGET_DATE, total=Decimal("900.00")
    )
    item_a = _add_item(order, product_a, quantity=1)
    _add_item(order, product_b, quantity=1)
    compute_daily_rollup(TARGET_DATE)
    assert DailyProductSales.objects.filter(date=TARGET_DATE).count() == 2

    item_a.delete()

    compute_daily_rollup(TARGET_DATE)

    remaining = DailyProductSales.objects.filter(date=TARGET_DATE)
    assert remaining.count() == 1
    assert remaining.first().product == product_b


@pytest.mark.django_db
def test_recompute_overwrites_the_order_rollup_not_double_counts():
    product = _make_product()
    order = _make_order(
        status=Order.Status.CONFIRMED, created_on=TARGET_DATE, total=Decimal("450.00")
    )
    _add_item(order, product)
    compute_daily_rollup(TARGET_DATE)
    compute_daily_rollup(TARGET_DATE)

    assert DailyOrderRollup.objects.filter(date=TARGET_DATE).count() == 1
    assert DailyOrderRollup.objects.get(date=TARGET_DATE).revenue == Decimal("450.00")


@pytest.mark.django_db
def test_a_day_with_no_orders_still_creates_a_zeroed_rollup_row():
    compute_daily_rollup(TARGET_DATE)

    rollup = DailyOrderRollup.objects.get(date=TARGET_DATE)
    assert rollup.revenue == Decimal("0")
    assert rollup.orders_confirmed == 0


@pytest.mark.django_db
def test_successful_compute_records_a_succeeded_run():
    compute_daily_rollup(TARGET_DATE)

    run = ReportRollupRun.objects.get(rollup_date=TARGET_DATE)
    assert run.succeeded is True
    assert run.error == ""


@pytest.mark.django_db
def test_a_failed_compute_records_a_failed_run_and_reraises(monkeypatch):
    import apps.reporting.services as services_module

    def _boom(target_date):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(services_module, "_compute_daily_order_rollup", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        compute_daily_rollup(TARGET_DATE)

    run = ReportRollupRun.objects.get(rollup_date=TARGET_DATE)
    assert run.succeeded is False
    assert "simulated failure" in run.error
    assert not DailyOrderRollup.objects.filter(date=TARGET_DATE).exists()
