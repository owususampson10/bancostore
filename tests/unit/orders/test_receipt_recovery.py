"""Task 62 follow-up (CodeRabbit, PR #93): a receipt must never be lost.

The receipt email is queued to Celery after payment confirmation. Before
this, a Redis outage at that moment, or a Gmail hiccup in the worker, only
wrote a log line -- the customer never got a receipt, and nothing tried
again. Now:

- each order records when its receipt was actually sent;
- the worker retries a failed send, with growing pauses;
- a scheduled sweep re-queues any confirmed order still missing its
  receipt after a grace period.

NONE OF THESE TESTS IMPORT WEASYPRINT (see test_receipt_pdf.py).
"""

import importlib
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.apps import apps as django_apps
from django.core import mail
from django.core.cache import cache
from django.utils import timezone

import pytest
from celery.exceptions import Retry

from apps.catalog.models import Category, Product
from apps.orders import receipt_pdf
from apps.orders.models import Order, OrderItem
from apps.orders.receipt_email import (
    RECEIPT_LOCK_TIMEOUT_SECONDS,
    RECEIPT_SEND_MAX_RETRIES,
    RECEIPT_SWEEP_MAX_SWEEPS,
    RECEIPT_SWEEPS_GAVE_UP,
    receipt_email_lock_key,
    send_order_receipt_email_task,
)
from apps.orders.tasks import (
    RECEIPT_SWEEP_GRACE,
    RECEIPT_SWEEP_MAX_AGE,
    resend_missing_order_receipts,
)


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
        "confirmed_at": timezone.now(),
    }
    defaults.update(overrides)
    defaults.setdefault("payment_reference", f"order-{Order.objects.count() + 1:032d}")
    order = Order.objects.create(**defaults)
    category, _ = Category.objects.get_or_create(name="Watches", slug="watches")
    product, _ = Product.objects.get_or_create(
        name="Emerald Aura Watch",
        defaults={
            "category": category,
            "price": Decimal("350.00"),
            "pv_value": 10,
            "stock": 5,
            "is_active": True,
        },
    )
    OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        quantity=1,
        unit_price=Decimal("350.00"),
        unit_pv=10,
    )
    return order


@pytest.fixture
def locmem(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def no_pdf():
    with patch.object(receipt_pdf, "render_receipt_pdf", return_value=None):
        yield


# --- The worker records a send, and never sends twice -----------------------


@pytest.mark.django_db
def test_a_sent_receipt_is_recorded_on_the_order(locmem, no_pdf):
    order = _make_order()

    send_order_receipt_email_task(order.pk)

    assert len(mail.outbox) == 1
    order.refresh_from_db()
    assert order.receipt_email_sent_at is not None


@pytest.mark.django_db
def test_an_already_sent_receipt_is_never_sent_again(locmem, no_pdf):
    """The sweep and the original task can both reach the same order. The
    record on the order is what stops a second email."""
    order = _make_order(receipt_email_sent_at=timezone.now())

    send_order_receipt_email_task(order.pk)

    assert mail.outbox == []


@pytest.mark.django_db
def test_a_receipt_another_worker_is_sending_is_skipped(locmem, no_pdf):
    """Two copies of the task running at once must not both send."""
    order = _make_order()
    cache.add(receipt_email_lock_key(order.pk), 1, timeout=60)

    send_order_receipt_email_task(order.pk)

    assert mail.outbox == []
    order.refresh_from_db()
    assert order.receipt_email_sent_at is None


@pytest.mark.django_db
def test_the_sent_check_is_repeated_after_taking_the_lock(locmem, no_pdf):
    """Design review: two queued copies can both see "not sent", and the
    first can finish and release the lock before the second takes it. The
    second must re-read the order once it holds the lock."""
    order = _make_order()
    real_add = cache.add

    def add_after_another_run_finished(*args, **kwargs):
        Order.objects.filter(pk=order.pk).update(receipt_email_sent_at=timezone.now())
        return real_add(*args, **kwargs)

    with patch(
        "apps.orders.receipt_email.cache.add",
        side_effect=add_after_another_run_finished,
    ):
        send_order_receipt_email_task(order.pk)

    assert mail.outbox == []


@pytest.mark.django_db
def test_a_run_never_releases_a_lock_another_run_now_holds(locmem, no_pdf):
    """If a stalled send outlived its lock and another run took it over,
    the first run finishing must not free the second run's lock."""
    order = _make_order()
    key = receipt_email_lock_key(order.pk)

    def stalled_send(order_arg):
        cache.set(key, "another-run", timeout=60)  # lock expired, retaken

    with patch(
        "apps.orders.receipt_email.send_order_receipt_email", side_effect=stalled_send
    ):
        send_order_receipt_email_task(order.pk)

    assert cache.get(key) == "another-run"


def test_a_send_can_never_outlive_its_lock(settings):
    """EMAIL_TIMEOUT bounds each Gmail call and the hard time limit bounds
    the whole run, so the lock cannot expire while a send is still going."""
    assert settings.EMAIL_TIMEOUT
    assert send_order_receipt_email_task.time_limit < RECEIPT_LOCK_TIMEOUT_SECONDS
    assert (
        send_order_receipt_email_task.soft_time_limit
        < send_order_receipt_email_task.time_limit
    )


@pytest.mark.django_db
@pytest.mark.parametrize("status", [Order.Status.CANCELLED, Order.Status.REFUNDED])
def test_a_job_that_runs_after_cancellation_sends_nothing(locmem, no_pdf, status):
    """CodeRabbit (PR #93): the job is queued at confirmation but can run
    after the order was cancelled or refunded. A "your order is confirmed"
    email at that point would be wrong."""
    order = _make_order(status=status)

    send_order_receipt_email_task(order.pk)

    assert mail.outbox == []


@pytest.mark.django_db
def test_a_cancellation_just_before_sending_is_still_caught(locmem, no_pdf):
    """Agent review (PR #93): the status must be checked AFTER the lock is
    taken, not only in the early check, or a cancellation landing between
    the two still gets a "your order is confirmed" email."""
    order = _make_order()
    real_add = cache.add

    def add_after_cancellation(*args, **kwargs):
        Order.objects.filter(pk=order.pk).update(status=Order.Status.CANCELLED)
        return real_add(*args, **kwargs)

    with patch(
        "apps.orders.receipt_email.cache.add", side_effect=add_after_cancellation
    ):
        send_order_receipt_email_task(order.pk)

    assert mail.outbox == []


# --- A failed send is retried, then left for the sweep ----------------------


@pytest.mark.django_db
def test_a_failed_send_is_retried_with_a_pause(no_pdf):
    order = _make_order()

    with (
        patch(
            "apps.orders.receipt_email.send_order_receipt_email",
            side_effect=ConnectionError("Gmail hiccup"),
        ),
        patch.object(
            send_order_receipt_email_task, "retry", side_effect=Retry()
        ) as retry,
    ):
        with pytest.raises(Retry):
            send_order_receipt_email_task(order.pk)

    assert retry.call_args.kwargs["countdown"] == 60
    order.refresh_from_db()
    assert order.receipt_email_sent_at is None
    # The lock is released, so the retry itself is not skipped as "busy".
    assert cache.get(receipt_email_lock_key(order.pk)) is None


@pytest.mark.django_db
def test_a_send_that_hit_the_time_limit_is_retried(no_pdf):
    """A stalled Gmail connection ends in the task's soft time limit."""
    from celery.exceptions import SoftTimeLimitExceeded

    order = _make_order()

    with (
        patch(
            "apps.orders.receipt_email.send_order_receipt_email",
            side_effect=SoftTimeLimitExceeded(),
        ),
        patch.object(
            send_order_receipt_email_task, "retry", side_effect=Retry()
        ) as retry,
    ):
        with pytest.raises(Retry):
            send_order_receipt_email_task(order.pk)

    retry.assert_called_once()


@pytest.mark.django_db
def test_a_bug_is_not_retried_by_the_worker_but_left_for_the_sweep(no_pdf):
    """A broken template fails identically on every try, so retrying it
    within minutes only repeats the error. The order stays unmarked, so the
    sweep's few, widely spaced attempts still pick it up."""
    order = _make_order()

    with (
        patch(
            "apps.orders.receipt_email.send_order_receipt_email",
            side_effect=RuntimeError("template blew up"),
        ),
        patch.object(send_order_receipt_email_task, "retry") as retry,
    ):
        send_order_receipt_email_task(order.pk)  # must not raise

    retry.assert_not_called()
    order.refresh_from_db()
    assert order.receipt_email_sent_at is None
    assert cache.get(receipt_email_lock_key(order.pk)) is None


@pytest.mark.django_db
def test_the_pause_grows_between_retries(no_pdf):
    order = _make_order()
    send_order_receipt_email_task.push_request(retries=2)
    try:
        with (
            patch(
                "apps.orders.receipt_email.send_order_receipt_email",
                side_effect=ConnectionError("Gmail hiccup"),
            ),
            patch.object(
                send_order_receipt_email_task, "retry", side_effect=Retry()
            ) as retry,
        ):
            with pytest.raises(Retry):
                send_order_receipt_email_task.run(order.pk)
    finally:
        send_order_receipt_email_task.pop_request()

    assert retry.call_args.kwargs["countdown"] == 240


@pytest.mark.django_db
def test_after_the_last_retry_the_failure_is_logged_not_raised(no_pdf, caplog):
    """Out of retries: the order stays unmarked, so the sweep picks it up
    later. Raising here would only add a Celery error on top of the log."""
    order = _make_order()
    send_order_receipt_email_task.push_request(retries=RECEIPT_SEND_MAX_RETRIES)
    try:
        with (
            patch(
                "apps.orders.receipt_email.send_order_receipt_email",
                side_effect=ConnectionError("Gmail down"),
            ),
            patch.object(send_order_receipt_email_task, "retry") as retry,
        ):
            send_order_receipt_email_task.run(order.pk)
    finally:
        send_order_receipt_email_task.pop_request()

    retry.assert_not_called()
    order.refresh_from_db()
    assert order.receipt_email_sent_at is None
    assert "failed to send the receipt" in caplog.text


@pytest.mark.django_db
def test_a_refused_address_is_not_retried_by_the_worker_or_the_sweep(no_pdf, caplog):
    """Retrying cannot fix an address the mail server rejects outright."""
    from smtplib import SMTPRecipientsRefused

    order = _make_order()

    with (
        patch(
            "apps.orders.receipt_email.send_order_receipt_email",
            side_effect=SMTPRecipientsRefused({order.email: (550, b"no such user")}),
        ),
        patch.object(send_order_receipt_email_task, "retry") as retry,
    ):
        send_order_receipt_email_task(order.pk)

    retry.assert_not_called()
    order.refresh_from_db()
    assert order.receipt_email_sent_at is None
    assert order.receipt_email_sweeps == RECEIPT_SWEEPS_GAVE_UP
    assert order.email not in caplog.text  # logs hold order ids, not addresses


# --- The sweep re-queues receipts that never went out -----------------------


def _age(minutes):
    return timezone.now() - timedelta(minutes=minutes)


@pytest.mark.django_db
def test_the_sweep_requeues_a_confirmed_order_still_missing_its_receipt():
    order = _make_order(confirmed_at=_age(RECEIPT_SWEEP_GRACE.total_seconds() / 60 + 5))

    with patch("apps.orders.tasks.send_order_receipt_email_task") as task:
        resend_missing_order_receipts()

    task.delay.assert_called_once_with(order.pk)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"confirmed_at": "recent"}, id="still-inside-the-grace-period"),
        pytest.param({"receipt_email_sent_at": "now"}, id="already-sent"),
        pytest.param({"email": ""}, id="no-email-address"),
        pytest.param({"status": Order.Status.CANCELLED}, id="cancelled"),
        pytest.param({"status": Order.Status.REFUNDED}, id="refunded"),
        pytest.param(
            {"status": Order.Status.PENDING, "confirmed_at": None}, id="never-paid"
        ),
    ],
)
def test_the_sweep_leaves_alone_orders_that_need_no_receipt(overrides):
    # Times are resolved here, not at collection, so a slow test run can't
    # quietly move an order across the grace boundary.
    fields = {"confirmed_at": _age(RECEIPT_SWEEP_GRACE.total_seconds() / 60 + 5)}
    for key, value in overrides.items():
        if value == "recent":
            value = _age(1)
        elif value == "now":
            value = timezone.now()
        fields[key] = value
    _make_order(**fields)

    with patch("apps.orders.tasks.send_order_receipt_email_task") as task:
        resend_missing_order_receipts()

    task.delay.assert_not_called()


@pytest.mark.django_db
def test_the_sweep_counts_each_requeue():
    order = _make_order(confirmed_at=_age(RECEIPT_SWEEP_GRACE.total_seconds() / 60 + 5))

    with patch("apps.orders.tasks.send_order_receipt_email_task"):
        resend_missing_order_receipts()
        resend_missing_order_receipts()  # the next sweep, moments later

    order.refresh_from_db()
    assert order.receipt_email_sweeps == 1  # not re-queued again straight away


@pytest.mark.django_db
def test_a_failed_requeue_does_not_use_up_an_attempt():
    """CodeRabbit (PR #93): if the broker refuses the job, nothing was
    queued. Counting it anyway would let a flaky broker exhaust every
    attempt without a single send."""
    order = _make_order(confirmed_at=_age(RECEIPT_SWEEP_GRACE.total_seconds() / 60 + 5))

    with patch("apps.orders.tasks.send_order_receipt_email_task") as task:
        task.delay.side_effect = ConnectionError("Redis is down")
        resend_missing_order_receipts()  # must not raise

    order.refresh_from_db()
    assert order.receipt_email_sweeps == 0

    with patch("apps.orders.tasks.send_order_receipt_email_task") as task:
        resend_missing_order_receipts()  # the broker is back

    task.delay.assert_called_once_with(order.pk)


@pytest.mark.django_db
def test_giving_an_attempt_back_never_undoes_a_change_made_meanwhile():
    """Agent review (PR #93): on a final attempt, the job can give up on a
    refused address between the sweep's claim and a failed queue. Giving the
    attempt back must not make that address eligible again."""
    grace_minutes = RECEIPT_SWEEP_GRACE.total_seconds() / 60
    final_attempt = RECEIPT_SWEEP_MAX_SWEEPS - 1
    order = _make_order(
        confirmed_at=_age(grace_minutes * 2**final_attempt + 5),
        receipt_email_sweeps=final_attempt,
    )

    def job_gave_up_then_queue_failed(order_id):
        Order.objects.filter(pk=order_id).update(
            receipt_email_sweeps=RECEIPT_SWEEPS_GAVE_UP
        )
        raise ConnectionError("Redis is down")

    with patch("apps.orders.tasks.send_order_receipt_email_task") as task:
        task.delay.side_effect = job_gave_up_then_queue_failed
        resend_missing_order_receipts()

    order.refresh_from_db()
    assert order.receipt_email_sweeps == RECEIPT_SWEEPS_GAVE_UP


@pytest.mark.django_db
def test_each_requeue_waits_longer_than_the_last():
    grace_minutes = RECEIPT_SWEEP_GRACE.total_seconds() / 60
    not_yet = _make_order(
        confirmed_at=_age(grace_minutes * 2 - 5), receipt_email_sweeps=1
    )
    due = _make_order(confirmed_at=_age(grace_minutes * 2 + 5), receipt_email_sweeps=1)

    with patch("apps.orders.tasks.send_order_receipt_email_task") as task:
        resend_missing_order_receipts()

    task.delay.assert_called_once_with(due.pk)
    assert not_yet.pk != due.pk


@pytest.mark.django_db
def test_the_sweep_stops_after_its_last_attempt():
    """A receipt that can never be delivered must not be retried forever,
    or hold a batch slot a newer missing receipt needs."""
    _make_order(
        confirmed_at=_age(60 * 24),
        receipt_email_sweeps=RECEIPT_SWEEP_MAX_SWEEPS,
    )

    with patch("apps.orders.tasks.send_order_receipt_email_task") as task:
        resend_missing_order_receipts()

    task.delay.assert_not_called()


@pytest.mark.django_db
def test_the_sweep_gives_up_on_receipts_too_old_to_be_useful():
    """A receipt arriving days later is confusing, and the cap keeps the
    query bounded after a long outage."""
    too_old = RECEIPT_SWEEP_MAX_AGE.total_seconds() / 60 + 5
    _make_order(confirmed_at=_age(too_old))

    with patch("apps.orders.tasks.send_order_receipt_email_task") as task:
        resend_missing_order_receipts()

    task.delay.assert_not_called()


# --- Queueing at confirmation ---------------------------------------------


@pytest.mark.django_db
def test_a_broker_error_at_commit_time_never_escapes(
    django_capture_on_commit_callbacks,
):
    """Agent review (PR #93): when confirmation runs inside a transaction,
    the job is queued at commit time, after the code that called on_commit
    has returned. The guard must travel with the callback."""
    from apps.orders.services import _send_confirmation_notifications

    order = _make_order()

    with (
        patch("apps.orders.services.send_sms"),
        patch("apps.orders.services.send_order_receipt_email_task") as task,
    ):
        task.delay.side_effect = ConnectionError("Redis is down")
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            _send_confirmation_notifications(order)  # must not raise at commit

    assert len(callbacks) == 1
    task.delay.assert_called_once_with(order.pk)


# --- Deploy safety ----------------------------------------------------------


@pytest.mark.django_db
def test_the_backfill_marks_existing_confirmed_orders_as_already_sent():
    """Orders confirmed before this shipped already had their receipt
    attempted by the old code. Without this, the first sweep after deploy
    would email every recent customer a second receipt."""
    migration = importlib.import_module(
        "apps.orders.migrations.0010_backfill_receipt_email_sent_at"
    )
    confirmed = _make_order()
    unpaid = _make_order(status=Order.Status.PENDING, confirmed_at=None)

    migration.backfill_receipt_email_sent_at(django_apps, None)

    confirmed.refresh_from_db()
    unpaid.refresh_from_db()
    assert confirmed.receipt_email_sent_at == confirmed.confirmed_at
    assert unpaid.receipt_email_sent_at is None


@pytest.mark.django_db
def test_the_sweep_is_scheduled():
    from django_celery_beat.models import PeriodicTask

    task = PeriodicTask.objects.get(name="resend-missing-order-receipts")
    assert task.task == "apps.orders.tasks.resend_missing_order_receipts"
    assert task.enabled
