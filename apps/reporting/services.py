from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Count, Exists, F, Min, OuterRef, Sum
from django.utils import timezone

from apps.commissions.services import COMMISSION_TRANSACTION_TYPES
from apps.orders.models import Order, OrderItem
from apps.wallet.models import WalletTransaction

from .models import DailyOrderRollup, DailyProductSales, ReportRollupRun

# Order.Status values whose orders did NOT actually collect payment --
# excluded from revenue/delivery-fees-by-zone everywhere in this module,
# matching apps.admin_portal.views.dashboard's own established
# "pending is unpaid, cancelled/refunded gave the money back, neither is
# real revenue" convention exactly (Task 27).
_UNPAID_STATUSES = (Order.Status.PENDING, Order.Status.CANCELLED, Order.Status.REFUNDED)


def _day_start(target_date):
    """Timezone-aware midnight for a calendar date. Used to build
    half-open [start, end) timestamp ranges instead of filtering with
    `created_at__date=...` -- a CodeRabbit-caught real perf gap: the
    `__date` lookup wraps Order.created_at in a DATE() transform, which
    MySQL cannot use the plain column index (ADR-0010) to range-scan.
    A `created_at__gte=start` / `created_at__lt=end` pair compares the
    column directly, so the index stays usable."""
    return timezone.make_aware(datetime.combine(target_date, time.min))


def _range_bounds(start_date, end_date):
    """Half-open [start, end) timestamp bounds covering every moment of
    every calendar day from start_date through end_date, inclusive."""
    return _day_start(start_date), _day_start(end_date + timedelta(days=1))


def compute_daily_rollup(target_date):
    """Task 46b (ADR-0010). Idempotent upsert of one calendar day's
    DailyOrderRollup + DailyProductSales rows, both written inside a
    single atomic transaction -- all of a day's figures are written
    together or not at all, so there's no partial-rollup state a report
    could ever read mid-write. Safe to call repeatedly for the same
    target_date (a backfill): each call fully recomputes that day's rows
    from the live Order/OrderItem tables, never incrementally adds to a
    previous computation.

    Always records a ReportRollupRun row, success or failure, OUTSIDE
    the rollup's own atomic block (a failure there must not roll back
    the audit record documenting it) -- mirrors
    apps.commissions.tasks._persist_cycle_audit_record's own "the audit
    trail must survive the failure it's recording" reasoning."""
    run_at = timezone.now()
    try:
        with transaction.atomic():
            _compute_daily_order_rollup(target_date)
            _compute_daily_product_sales(target_date)
    except Exception as exc:
        ReportRollupRun.objects.create(
            rollup_date=target_date, run_at=run_at, succeeded=False, error=str(exc)
        )
        raise
    else:
        ReportRollupRun.objects.create(
            rollup_date=target_date, run_at=run_at, succeeded=True
        )


def _compute_daily_order_rollup(target_date):
    day_start, day_end = _range_bounds(target_date, target_date)
    day_orders = Order.objects.filter(created_at__gte=day_start, created_at__lt=day_end)
    # "Paid" orders for this day -- the same subset revenue and delivery
    # fees are both scoped to, matching _UNPAID_STATUSES' own reasoning.
    paid_orders = day_orders.exclude(status__in=_UNPAID_STATUSES)

    revenue = paid_orders.aggregate(total=Sum("total"))["total"] or Decimal("0")

    status_counts = dict(
        day_orders.values("status")
        .annotate(count=Count("pk"))
        .values_list("status", "count")
    )

    delivery_fees_by_zone = dict(
        paid_orders.exclude(delivery_zone="")
        .values("delivery_zone")
        .annotate(total=Sum("delivery_fee"))
        .values_list("delivery_zone", "total")
    )

    DailyOrderRollup.objects.update_or_create(
        date=target_date,
        defaults={
            "revenue": revenue,
            "orders_pending": status_counts.get(Order.Status.PENDING, 0),
            "orders_confirmed": status_counts.get(Order.Status.CONFIRMED, 0),
            "orders_processing": status_counts.get(Order.Status.PROCESSING, 0),
            "orders_dispatched": status_counts.get(Order.Status.DISPATCHED, 0),
            "orders_delivered": status_counts.get(Order.Status.DELIVERED, 0),
            "orders_cancelled": status_counts.get(Order.Status.CANCELLED, 0),
            "orders_refunded": status_counts.get(Order.Status.REFUNDED, 0),
            "delivery_fees_kumasi": delivery_fees_by_zone.get(
                Order.DeliveryZone.KUMASI, Decimal("0")
            ),
            "delivery_fees_accra": delivery_fees_by_zone.get(
                Order.DeliveryZone.ACCRA, Decimal("0")
            ),
            "delivery_fees_other_regions": delivery_fees_by_zone.get(
                Order.DeliveryZone.OTHER_REGIONS, Decimal("0")
            ),
        },
    )


def _compute_daily_product_sales(target_date):
    day_start, day_end = _range_bounds(target_date, target_date)
    paid_orders = Order.objects.filter(
        created_at__gte=day_start, created_at__lt=day_end
    ).exclude(status__in=_UNPAID_STATUSES)
    # Grouped by product_id ALONE, not (product_id, product_name):
    # OrderItem.product_name is a per-order-line snapshot (see
    # DailyProductSales' own docstring), so a product renamed mid-day
    # would otherwise split into two groups for the same
    # DailyProductSales(date, product) unique constraint -- a real
    # CodeRabbit-caught bug where bulk_create would then violate that
    # constraint and roll back the whole day's rollup. Min() picks one
    # deterministic snapshot name for the day rather than an arbitrary
    # "last row wins" DB-dependent choice.
    product_sales = (
        OrderItem.objects.filter(order__in=paid_orders)
        .values("product_id")
        .annotate(
            product_name=Min("product_name"),
            units=Sum("quantity"),
            revenue=Sum(F("unit_price") * F("quantity")),
        )
    )

    # Delete-then-bulk_create, not update_or_create per product: a
    # product with zero sales on a re-backfilled date must not keep a
    # stale nonzero row from a previous compute, and this stays O(1)
    # queries regardless of how many distinct products sold that day,
    # matching this codebase's own "bulk, never per-row" convention
    # (apps.pv_ledger.services.record_purchase_pv).
    DailyProductSales.objects.filter(date=target_date).delete()
    DailyProductSales.objects.bulk_create(
        DailyProductSales(
            date=target_date,
            product_id=row["product_id"],
            product_name=row["product_name"],
            units_sold=row["units"],
            revenue=row["revenue"],
        )
        for row in product_sales
    )


# ---------------------------------------------------------------------------
# 46b: revenue-by-period, orders-per-status, delivery-fees-by-zone
# ---------------------------------------------------------------------------

# Every DailyOrderRollup field this report sums, and the Decimal-vs-int
# zero each one normalizes to when Sum() returns None (an empty date
# range, or a range with no rows yet) -- one place naming every summed
# field, so get_order_summary_report's aggregate() and its own
# None-normalization loop can never drift out of sync with each other.
_ROLLUP_SUM_FIELDS = {
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
_MONEY_FIELDS = {
    "revenue",
    "delivery_fees_kumasi",
    "delivery_fees_accra",
    "delivery_fees_other_regions",
}


def _quantize_money(amount):
    """Task 46b. SQLite's Sum() over a DecimalField(decimal_places=2)
    doesn't preserve the original 2dp scale the way direct field access
    does (e.g. a summed Decimal("20.00") can come back as Decimal("20"))
    -- caught by this task's own CSV export test expecting "20.00" and
    getting "20". Quantized once here, at the source every consumer
    (the HTML page, CSV export, PDF export) reads from, matching this
    codebase's own established money-rounding convention
    (apps.commissions.services, Decimal.quantize(..., ROUND_HALF_UP) to
    the pesewa) rather than each template/export re-deriving its own
    formatting fix independently."""
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_order_summary_report(start_date, end_date):
    """Task 46b (ADR-0010). Sums DailyOrderRollup rows within
    [start_date, end_date] inclusive -- query cost is proportional to
    the number of DAYS requested, not to how many orders exist in this
    project's history, regardless of how wide a range an admin picks.
    A date with no rollup row yet (today, or a day compute_daily_rollup
    hasn't reached) simply contributes nothing -- this function never
    falls back to a live Order-table scan (ADR-0010's own decision).

    Returns the totals plus a per-day series (`daily_series`) an admin
    UI re-buckets into week/month views via bucket_revenue_series below
    -- one small, already-bounded dataset grouped differently, rather
    than three near-identical SQL query variants for what's really the
    same underlying rollup rows."""
    rollups = DailyOrderRollup.objects.filter(date__gte=start_date, date__lte=end_date)

    totals = rollups.aggregate(**{field: Sum(field) for field in _ROLLUP_SUM_FIELDS})
    for field, zero in _ROLLUP_SUM_FIELDS.items():
        if totals[field] is None:
            totals[field] = zero
        elif field in _MONEY_FIELDS:
            totals[field] = _quantize_money(totals[field])

    daily_series = [
        {"date": row["date"], "revenue": _quantize_money(row["revenue"])}
        for row in rollups.order_by("date").values("date", "revenue")
    ]

    return {
        "revenue": totals["revenue"],
        "order_status_counts": {
            "pending": totals["orders_pending"],
            "confirmed": totals["orders_confirmed"],
            "processing": totals["orders_processing"],
            "dispatched": totals["orders_dispatched"],
            "delivered": totals["orders_delivered"],
            "cancelled": totals["orders_cancelled"],
            "refunded": totals["orders_refunded"],
        },
        "delivery_fees_by_zone": {
            "kumasi": totals["delivery_fees_kumasi"],
            "accra": totals["delivery_fees_accra"],
            "other_regions": totals["delivery_fees_other_regions"],
        },
        "daily_series": daily_series,
    }


def bucket_revenue_series(daily_series, *, granularity):
    """Re-buckets `get_order_summary_report`'s already-fetched
    `daily_series` in Python -- granularity is "day"/"week"/"month".
    Cheap: the input is already bounded to whatever date range the admin
    requested (at most a few thousand rows even for a multi-year range),
    so grouping it in Python avoids a second/third round-trip to the
    database for what's the same small rollup dataset viewed
    differently. Returns a list of {"period": <ISO date string>,
    "revenue": Decimal} dicts, ordered chronologically.

    "week" buckets to that week's Monday (ISO week start); "month"
    buckets to the 1st of that month."""
    if granularity == "day":
        return [
            {"period": row["date"].isoformat(), "revenue": row["revenue"]}
            for row in daily_series
        ]

    if granularity not in ("week", "month"):
        raise ValueError(f"Unknown granularity: {granularity!r}")

    buckets = {}
    for row in daily_series:
        row_date = row["date"]
        if granularity == "week":
            bucket_key = row_date - timedelta(days=row_date.weekday())
        else:
            bucket_key = row_date.replace(day=1)
        buckets[bucket_key] = buckets.get(bucket_key, Decimal("0")) + row["revenue"]

    return [
        {"period": key.isoformat(), "revenue": value}
        for key, value in sorted(buckets.items())
    ]


# ---------------------------------------------------------------------------
# 46c: best-selling products, new-vs-returning customers, commissions-vs-revenue
# ---------------------------------------------------------------------------


def get_best_selling_products_report(start_date, end_date, *, limit=20):
    """Task 46c (ADR-0010). Sums DailyProductSales rows within
    [start_date, end_date] inclusive, grouped by product -- same "sum
    the small rollup table, never scan OrderItem directly" shape as
    get_order_summary_report, just grouped by product instead of by
    day. Ordered by units sold, most first; `limit` bounds how many
    products come back (a "best sellers" report has no use for a full,
    unbounded ranking of every product that sold even once)."""
    rows = (
        DailyProductSales.objects.filter(date__gte=start_date, date__lte=end_date)
        .values("product_id", "product_name")
        .annotate(units_sold=Sum("units_sold"), revenue=Sum("revenue"))
        .order_by("-units_sold", "-revenue")[:limit]
    )
    return [
        {
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "units_sold": row["units_sold"],
            "revenue": _quantize_money(row["revenue"]),
        }
        for row in rows
    ]


def get_new_vs_returning_customers_report(start_date, end_date):
    """Task 46c (ADR-0010). A live, indexed, bounded query -- doesn't
    reduce to a per-day rollup since it needs each customer's FULL order
    history (was this order this customer's first ever), not just
    activity within the requested range.

    Scoped to the same "paid" convention as every other report in this
    module (a pending/cancelled/refunded order never actually completed,
    so it can't meaningfully make someone a "returning" customer).
    Guest orders (Order.customer_id is NULL) have no persistent identity
    across orders, so they're excluded entirely -- "new vs returning" is
    undefined for a guest.

    A customer counts as "new" if their earliest-ever paid order falls
    inside [start_date, end_date] (their first order ever happened
    during this window); "returning" if their earliest-ever paid order
    is before start_date. Since every customer_id considered here comes
    from an order already inside the range, their earliest order can
    never fall after end_date -- only the lower bound needs checking.

    Classification stays entirely database-side (a `returning` COUNT via
    a correlated Exists subquery, not a Python list()/dict() of every
    matching customer id and first-order timestamp) -- a CodeRabbit-
    caught real memory/query-overhead gap for a wide date range with
    many distinct customers."""
    start_at, end_at = _range_bounds(start_date, end_date)
    orders_in_range = Order.objects.filter(
        created_at__gte=start_at,
        created_at__lt=end_at,
        customer_id__isnull=False,
    ).exclude(status__in=_UNPAID_STATUSES)

    total_customers = orders_in_range.values("customer_id").distinct().count()
    if total_customers == 0:
        return {"new_customers": 0, "returning_customers": 0}

    had_earlier_paid_order = Exists(
        Order.objects.filter(customer_id=OuterRef("customer_id"))
        .exclude(status__in=_UNPAID_STATUSES)
        .filter(created_at__lt=start_at)
    )
    returning_count = (
        orders_in_range.filter(had_earlier_paid_order)
        .values("customer_id")
        .distinct()
        .count()
    )
    new_count = total_customers - returning_count

    return {"new_customers": new_count, "returning_customers": returning_count}


def get_commissions_vs_revenue_report(start_date, end_date):
    """Task 46c (ADR-0010). Revenue side reuses the rollup
    (DailyOrderRollup, same as get_order_summary_report); commissions
    side is a live, indexed, date-bounded WalletTransaction query
    (transaction_type IN COMMISSION_TRANSACTION_TYPES, reused directly
    from apps.commissions.services rather than a fresh definition -- see
    that constant's own docstring for why it lives there, not here or in
    admin_portal/views.py). The two are joined at the report layer, not
    via a shared rollup table -- building a "commissions rollup" for a
    single ratio isn't worth a fifth table (ADR-0010's own "Alternatives
    Considered" reasoning against forcing every report into one shape)."""
    start_at, end_at = _range_bounds(start_date, end_date)
    revenue = DailyOrderRollup.objects.filter(
        date__gte=start_date, date__lte=end_date
    ).aggregate(total=Sum("revenue"))["total"] or Decimal("0")
    commissions_paid = WalletTransaction.objects.filter(
        transaction_type__in=COMMISSION_TRANSACTION_TYPES,
        created_at__gte=start_at,
        created_at__lt=end_at,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    return {
        "revenue": _quantize_money(revenue),
        "commissions_paid": _quantize_money(commissions_paid),
    }
