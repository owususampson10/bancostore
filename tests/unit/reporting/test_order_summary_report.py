from datetime import date
from decimal import Decimal

import pytest

from apps.reporting.models import DailyOrderRollup
from apps.reporting.services import bucket_revenue_series, get_order_summary_report


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


# ---------------------------------------------------------------------------
# get_order_summary_report
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_sums_revenue_across_the_requested_range():
    _make_rollup(date(2026, 8, 1), revenue=Decimal("100.00"))
    _make_rollup(date(2026, 8, 2), revenue=Decimal("200.00"))
    _make_rollup(date(2026, 8, 3), revenue=Decimal("300.00"))

    report = get_order_summary_report(date(2026, 8, 1), date(2026, 8, 2))

    assert report["revenue"] == Decimal("300.00")


@pytest.mark.django_db
def test_excludes_rollup_rows_outside_the_range():
    _make_rollup(date(2026, 7, 31), revenue=Decimal("999.00"))
    _make_rollup(date(2026, 8, 1), revenue=Decimal("50.00"))
    _make_rollup(date(2026, 8, 4), revenue=Decimal("999.00"))

    report = get_order_summary_report(date(2026, 8, 1), date(2026, 8, 1))

    assert report["revenue"] == Decimal("50.00")


@pytest.mark.django_db
def test_range_boundaries_are_inclusive():
    _make_rollup(date(2026, 8, 1), revenue=Decimal("10.00"))
    _make_rollup(date(2026, 8, 5), revenue=Decimal("20.00"))

    report = get_order_summary_report(date(2026, 8, 1), date(2026, 8, 5))

    assert report["revenue"] == Decimal("30.00")


@pytest.mark.django_db
def test_a_range_with_no_rollup_rows_returns_zeroed_totals_not_none():
    report = get_order_summary_report(date(2026, 1, 1), date(2026, 1, 31))

    assert report["revenue"] == Decimal("0")
    assert report["order_status_counts"]["confirmed"] == 0
    assert report["delivery_fees_by_zone"]["kumasi"] == Decimal("0")


@pytest.mark.django_db
def test_order_status_counts_sum_across_days():
    _make_rollup(date(2026, 8, 1), orders_confirmed=2, orders_cancelled=1)
    _make_rollup(date(2026, 8, 2), orders_confirmed=3, orders_cancelled=0)

    report = get_order_summary_report(date(2026, 8, 1), date(2026, 8, 2))

    assert report["order_status_counts"]["confirmed"] == 5
    assert report["order_status_counts"]["cancelled"] == 1


@pytest.mark.django_db
def test_delivery_fees_by_zone_sum_across_days():
    _make_rollup(date(2026, 8, 1), delivery_fees_kumasi=Decimal("20.00"))
    _make_rollup(date(2026, 8, 2), delivery_fees_kumasi=Decimal("40.00"))

    report = get_order_summary_report(date(2026, 8, 1), date(2026, 8, 2))

    assert report["delivery_fees_by_zone"]["kumasi"] == Decimal("60.00")


@pytest.mark.django_db
def test_money_totals_always_render_with_two_decimal_places():
    """Regression guard: SQLite's Sum() over a DecimalField(decimal_
    places=2) doesn't preserve the original 2dp scale (a summed
    Decimal("20.00") can come back as Decimal("20")) -- caught first by
    a CSV export test expecting "20.00" and getting "20". Every monetary
    total (revenue, each delivery-fee-by-zone figure, and every
    daily_series row) must always be exactly 2dp regardless of what the
    underlying aggregate returns."""
    _make_rollup(
        date(2026, 8, 1), revenue=Decimal("20.00"), delivery_fees_kumasi=Decimal("5.00")
    )

    report = get_order_summary_report(date(2026, 8, 1), date(2026, 8, 1))

    assert str(report["revenue"]) == "20.00"
    assert str(report["delivery_fees_by_zone"]["kumasi"]) == "5.00"
    assert str(report["daily_series"][0]["revenue"]) == "20.00"


@pytest.mark.django_db
def test_daily_series_is_ordered_chronologically():
    _make_rollup(date(2026, 8, 3), revenue=Decimal("3"))
    _make_rollup(date(2026, 8, 1), revenue=Decimal("1"))
    _make_rollup(date(2026, 8, 2), revenue=Decimal("2"))

    report = get_order_summary_report(date(2026, 8, 1), date(2026, 8, 3))

    dates = [row["date"] for row in report["daily_series"]]
    assert dates == [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)]


# ---------------------------------------------------------------------------
# bucket_revenue_series
# ---------------------------------------------------------------------------


def test_day_granularity_returns_each_row_unchanged():
    series = [{"date": date(2026, 8, 1), "revenue": Decimal("10")}]

    result = bucket_revenue_series(series, granularity="day")

    assert result == [{"period": "2026-08-01", "revenue": Decimal("10")}]


def test_week_granularity_buckets_to_the_iso_week_monday():
    # 2026-08-03 is a Monday; 2026-08-05 (Wed) and 2026-08-09 (Sun) fall
    # in that same ISO week.
    series = [
        {"date": date(2026, 8, 3), "revenue": Decimal("10")},
        {"date": date(2026, 8, 5), "revenue": Decimal("20")},
        {"date": date(2026, 8, 9), "revenue": Decimal("5")},
        {"date": date(2026, 8, 10), "revenue": Decimal("100")},  # next week (Monday)
    ]

    result = bucket_revenue_series(series, granularity="week")

    assert result == [
        {"period": "2026-08-03", "revenue": Decimal("35")},
        {"period": "2026-08-10", "revenue": Decimal("100")},
    ]


def test_month_granularity_buckets_to_the_first_of_the_month():
    series = [
        {"date": date(2026, 8, 1), "revenue": Decimal("10")},
        {"date": date(2026, 8, 31), "revenue": Decimal("20")},
        {"date": date(2026, 9, 1), "revenue": Decimal("5")},
    ]

    result = bucket_revenue_series(series, granularity="month")

    assert result == [
        {"period": "2026-08-01", "revenue": Decimal("30")},
        {"period": "2026-09-01", "revenue": Decimal("5")},
    ]


def test_an_empty_series_returns_an_empty_list():
    assert bucket_revenue_series([], granularity="week") == []


def test_an_unknown_granularity_raises():
    with pytest.raises(ValueError, match="Unknown granularity"):
        bucket_revenue_series([], granularity="year")
