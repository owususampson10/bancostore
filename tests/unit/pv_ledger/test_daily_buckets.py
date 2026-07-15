import threading
import time
from datetime import date
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, connection

import pytest

from apps.binary_tree.models import BinaryTreeEdge
from apps.distributors.models import Distributor
from apps.pv_ledger.models import PvDailyBucket
from apps.pv_ledger.services import _credit_daily_buckets
from bancostore.concurrency import retry_on_lock_contention

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233245{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


@pytest.mark.django_db
def test_first_credit_of_the_day_creates_a_row():
    ancestor = _make_distributor()
    today = date(2026, 7, 15)

    _credit_daily_buckets([ancestor.pk], BinaryTreeEdge.Leg.LEFT, 60, today)

    row = PvDailyBucket.objects.get(distributor=ancestor)
    assert row.leg == BinaryTreeEdge.Leg.LEFT
    assert row.date == today
    assert row.pv == 60


@pytest.mark.django_db
def test_a_second_credit_the_same_day_adds_rather_than_overwrites():
    ancestor = _make_distributor()
    today = date(2026, 7, 15)

    _credit_daily_buckets([ancestor.pk], BinaryTreeEdge.Leg.LEFT, 60, today)
    _credit_daily_buckets([ancestor.pk], BinaryTreeEdge.Leg.LEFT, 40, today)

    row = PvDailyBucket.objects.get(distributor=ancestor)
    assert row.pv == 100


@pytest.mark.django_db
def test_multiple_ancestors_credited_in_one_bulk_call():
    already_has_a_row = _make_distributor()
    is_new_today = _make_distributor()
    today = date(2026, 7, 15)
    PvDailyBucket.objects.create(
        distributor=already_has_a_row, leg=BinaryTreeEdge.Leg.LEFT, date=today, pv=10
    )

    _credit_daily_buckets(
        [already_has_a_row.pk, is_new_today.pk], BinaryTreeEdge.Leg.LEFT, 25, today
    )

    assert PvDailyBucket.objects.get(distributor=already_has_a_row).pv == 35
    assert PvDailyBucket.objects.get(distributor=is_new_today).pv == 25


@pytest.mark.django_db
def test_different_legs_get_separate_rows():
    ancestor = _make_distributor()
    today = date(2026, 7, 15)

    _credit_daily_buckets([ancestor.pk], BinaryTreeEdge.Leg.LEFT, 60, today)
    _credit_daily_buckets([ancestor.pk], BinaryTreeEdge.Leg.RIGHT, 30, today)

    assert PvDailyBucket.objects.count() == 2
    left = PvDailyBucket.objects.get(distributor=ancestor, leg=BinaryTreeEdge.Leg.LEFT)
    right = PvDailyBucket.objects.get(
        distributor=ancestor, leg=BinaryTreeEdge.Leg.RIGHT
    )
    assert left.pv == 60
    assert right.pv == 30


@pytest.mark.django_db
def test_different_dates_get_separate_rows():
    ancestor = _make_distributor()

    _credit_daily_buckets([ancestor.pk], BinaryTreeEdge.Leg.LEFT, 60, date(2026, 7, 14))
    _credit_daily_buckets([ancestor.pk], BinaryTreeEdge.Leg.LEFT, 40, date(2026, 7, 15))

    assert PvDailyBucket.objects.count() == 2
    day1 = PvDailyBucket.objects.get(distributor=ancestor, date=date(2026, 7, 14))
    day2 = PvDailyBucket.objects.get(distributor=ancestor, date=date(2026, 7, 15))
    assert day1.pv == 60
    assert day2.pv == 40


@pytest.mark.django_db(transaction=True)
def test_a_concurrent_insert_race_retries_and_still_credits_correctly():
    """Deterministically forces the exact race the retry loop exists
    for: two real threads both find no bucket for today, both try to
    create one, and one must lose the unique-constraint race. Uses
    threading.Event + a short fixed delay to force the interleaving
    (rather than the broader, non-deterministic test below) so this test
    reliably exercises the IntegrityError-retry path specifically, not
    just "10 threads, hopefully some raced." A mocked single-connection
    version can't prove this: simulating "a concurrent row appeared" by
    writing on the SAME connection inside this function's own atomic
    attempt would just get undone by that attempt's own rollback, which
    isn't how a genuinely separate transaction behaves.

    Thread A holds its whole attempt (update+read+create, one atomic
    block since the fix below for the connection-state bug) open for a
    brief fixed delay rather than waiting on Thread B to signal it --
    Thread B must actually queue behind Thread A's real table lock
    (retry_on_lock_contention absorbing the resulting OperationalErrors)
    since, on SQLite, nothing else can run while Thread A's transaction
    is open; waiting on each other would deadlock instead of race."""
    ancestor = _make_distributor()
    today = date(2026, 7, 15)
    thread_a_reached_bulk_create = threading.Event()
    errors = []

    real_bulk_create = PvDailyBucket.objects.bulk_create

    def delayed_bulk_create(objs, *args, **kwargs):
        thread_a_reached_bulk_create.set()
        time.sleep(0.3)
        return real_bulk_create(objs, *args, **kwargs)

    def thread_a():
        try:
            with patch.object(
                PvDailyBucket.objects, "bulk_create", side_effect=delayed_bulk_create
            ):
                retry_on_lock_contention(
                    lambda: _credit_daily_buckets(
                        [ancestor.pk], BinaryTreeEdge.Leg.LEFT, 60, today
                    )
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    def thread_b():
        thread_a_reached_bulk_create.wait(timeout=5)
        try:
            retry_on_lock_contention(
                lambda: _credit_daily_buckets(
                    [ancestor.pk], BinaryTreeEdge.Leg.LEFT, 15, today
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    t_a = threading.Thread(target=thread_a)
    t_b = threading.Thread(target=thread_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    assert errors == []
    row = PvDailyBucket.objects.get(distributor=ancestor)
    assert row.pv == 75


@pytest.mark.django_db
def test_a_mixed_batch_retries_the_whole_attempt_not_just_the_missing_ones():
    """Regression test for a real bug found in review: when a batch mixes
    an ancestor who already has today's bucket with one who doesn't, and
    bulk_create raises IntegrityError (forcing a retry), the WHOLE
    attempt rolls back -- including the bulk UPDATE that had already
    incremented the ancestor who already had a row. An earlier version
    of this function narrowed the retry set to just the missing
    ancestor(s) on IntegrityError, which was correct back when only the
    create step was wrapped in a savepoint (the update had already
    durably committed by then) but silently dropped the already-existing
    ancestor's credit once the whole attempt became one atomic block --
    with no exception raised anywhere."""
    already_has_a_row = _make_distributor()
    brand_new_today = _make_distributor()
    today = date(2026, 7, 15)
    PvDailyBucket.objects.create(
        distributor=already_has_a_row, leg=BinaryTreeEdge.Leg.LEFT, date=today, pv=20
    )

    real_bulk_create = PvDailyBucket.objects.bulk_create
    call_count = {"n": 0}

    def fail_once_then_succeed(objs, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise IntegrityError("duplicate key (simulated concurrent insert)")
        return real_bulk_create(objs, *args, **kwargs)

    with patch.object(
        PvDailyBucket.objects, "bulk_create", side_effect=fail_once_then_succeed
    ):
        _credit_daily_buckets(
            [already_has_a_row.pk, brand_new_today.pk],
            BinaryTreeEdge.Leg.LEFT,
            5,
            today,
        )

    assert PvDailyBucket.objects.get(distributor=already_has_a_row).pv == 25
    assert PvDailyBucket.objects.get(distributor=brand_new_today).pv == 5
    assert call_count["n"] == 2


@pytest.mark.django_db
def test_exhausting_retries_raises_instead_of_silently_dropping_the_credit():
    ancestor = _make_distributor()
    today = date(2026, 7, 15)

    with patch.object(
        PvDailyBucket.objects,
        "bulk_create",
        side_effect=IntegrityError("duplicate key (simulated persistent race)"),
    ):
        with pytest.raises(RuntimeError):
            _credit_daily_buckets(
                [ancestor.pk], BinaryTreeEdge.Leg.LEFT, 60, today, max_retries=2
            )

    assert PvDailyBucket.objects.filter(distributor=ancestor).count() == 0


@pytest.mark.django_db(transaction=True)
def test_concurrent_first_of_day_credits_to_the_same_ancestor_never_lose_pv():
    """Real multi-threaded race for the interesting case: nobody has a
    bucket yet for today, and several purchases confirm at the same
    instant, all crediting the same single ancestor's same leg. Proven
    fully only under MySQL (SQLite serializes writes at the database
    level so this can't distinguish "worked because of correct retry
    logic" from "worked because SQLite let only one writer through at a
    time") -- see CLAUDE.md's note that commission/wallet/PV-ledger
    concurrency needs real MySQL in CI. Kept locally anyway since it
    still catches a regression that removes the retry loop entirely.

    SQLite serializes ALL writes at the whole-database level (not
    per-row like MySQL/InnoDB), and this function does more work per
    attempt than a single-statement counter update (bulk update + a
    select + a savepoint-wrapped insert), so under 10-way real
    contention on a loaded machine, retry_on_lock_contention's bounded
    budget can occasionally still be exhausted for a thread or two --
    this is an inherent SQLite limitation, not evidence of a bug (same
    reasoning test_stock.py's own concurrency test documents). So this
    doesn't assert zero failures; it asserts the one property that
    actually matters for money-correctness: whichever attempts DID
    succeed are reflected exactly once each (no lost credit, no
    double-credit), and any attempt that didn't succeed failed loudly
    with a real contention error -- never silently, never with a wrong
    number."""
    ancestor = _make_distributor()
    today = date(2026, 7, 15)
    errors = []
    lock = threading.Lock()

    def attempt():
        try:
            # _credit_daily_buckets deliberately does not catch
            # OperationalError itself (see its docstring) -- it relies on
            # exactly this outer wrapper, exactly as it's always called
            # in production from within consume_paid_starter_pack's
            # already-retried attempt. Calling it unwrapped here would
            # test a usage pattern that doesn't reflect real deployment.
            retry_on_lock_contention(
                lambda: _credit_daily_buckets(
                    [ancestor.pk], BinaryTreeEdge.Leg.LEFT, 10, today
                ),
                max_retries=30,
            )
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            with lock:
                errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for exc in errors:
        assert isinstance(
            exc, OperationalError
        ), f"expected only contention errors, got {exc!r}"
    successes = len(threads) - len(errors)
    row = PvDailyBucket.objects.get(distributor=ancestor)
    assert row.pv == successes * 10
