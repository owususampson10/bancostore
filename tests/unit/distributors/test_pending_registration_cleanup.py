"""Pending registration cleanup.

Task 67 replaced the original rule ("delete anything unconsumed older than one
hour") after it deleted a registration whose checkout page was still open: the
fee was paid 43 minutes later, on 2026-09-16, and no account was ever created.
A pending registration is now deleted only once Paystack has given a definite
"not paid" answer for it, or its payment has been turned into an account or a
recorded PaymentIssue.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone

import pytest

from apps.distributors.models import Distributor, PaymentIssue, PendingRegistration
from apps.distributors.paystack import PaystackError, PaystackNotFoundError
from apps.distributors.services import (
    PendingRegistrationResolution,
    RegistrationPaymentOutcome,
    resolve_unconsumed_pending_registration,
)
from apps.distributors.tasks import (
    PENDING_REGISTRATION_IDLE_TTL,
    PENDING_REGISTRATION_MAX_AGE,
    cleanup_expired_pending_registrations,
)

User = get_user_model()
Resolution = PendingRegistrationResolution
VERIFY = "apps.distributors.services.verify_transaction"
CONSUME = "apps.distributors.services.consume_paid_registration"


@pytest.fixture(autouse=True)
def _run_on_commit_now(monkeypatch):
    monkeypatch.setattr(
        "apps.distributors.payment_issues.transaction.on_commit", lambda fn: fn()
    )


def _make_sponsor(ir_id="IR00001", phone_number="+233209999999"):
    user = User.objects.create_user(username=phone_number, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone_number, ir_id=ir_id)


def _make_pending(sponsor, phone_number, *, reference="ref-1", idle=None, age=None):
    pending = PendingRegistration.objects.create(
        full_name="Test Person",
        phone_number=phone_number,
        email="person@example.test",
        address="1 Test Street",
        area="Osu",
        landmark="",
        password_hash="hashed",
        sponsor=sponsor,
        payment_reference=reference,
        fee_amount_pesewas=10000,
    )
    now = timezone.now()
    updates = {}
    if age is not None:
        updates["created_at"] = now - age
    if idle is not None:
        updates["payment_initialized_at"] = now - idle
    if updates:
        PendingRegistration.objects.filter(pk=pending.pk).update(**updates)
    pending.refresh_from_db()
    return pending


def _resolve_like_cleanup(pending):
    return resolve_unconsumed_pending_registration(
        pending.pk,
        idle_for=PENDING_REGISTRATION_IDLE_TTL,
        max_age=PENDING_REGISTRATION_MAX_AGE,
        delete_unconfirmed=True,
    )


def _exists(pending):
    return PendingRegistration.objects.filter(pk=pending.pk).exists()


# --- The windows ---------------------------------------------------------------


def test_the_idle_window_is_72_hours_and_the_hard_limit_30_days():
    assert PENDING_REGISTRATION_IDLE_TTL == timedelta(hours=72)
    assert PENDING_REGISTRATION_MAX_AGE == timedelta(days=30)


@pytest.mark.django_db
@patch(VERIFY)
def test_a_registration_idle_for_less_than_72_hours_is_left_alone(mock_verify):
    """The 2026-09-16 incident: two hours after the checkout opened, it was
    still payable."""
    pending = _make_pending(
        _make_sponsor(),
        "+233241111111",
        idle=timedelta(hours=71),
        age=timedelta(days=5),
    )

    assert _resolve_like_cleanup(pending) == Resolution.KEPT
    assert _exists(pending)
    mock_verify.assert_not_called()


@pytest.mark.django_db
@patch(VERIFY)
def test_idle_is_measured_from_the_last_checkout_not_the_form(mock_verify):
    mock_verify.return_value = {"status": "abandoned"}
    pending = _make_pending(
        _make_sponsor(), "+233241111111", idle=timedelta(hours=1), age=timedelta(days=4)
    )

    cleanup_expired_pending_registrations()

    assert _exists(pending)


@pytest.mark.django_db
@patch(VERIFY)
def test_a_registration_that_never_reached_payment_is_deleted(mock_verify):
    pending = _make_pending(
        _make_sponsor(), "+233241111111", reference=None, age=timedelta(hours=73)
    )

    assert _resolve_like_cleanup(pending) == Resolution.DELETED
    assert not _exists(pending)
    mock_verify.assert_not_called()


# --- What Paystack says --------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["abandoned", "failed", "reversed"])
@patch(VERIFY)
def test_a_definite_not_paid_answer_deletes_it(mock_verify, status):
    mock_verify.return_value = {"status": status}
    pending = _make_pending(_make_sponsor(), "+233241111111", idle=timedelta(hours=73))

    assert _resolve_like_cleanup(pending) == Resolution.DELETED
    assert not _exists(pending)


@pytest.mark.django_db
@patch(VERIFY)
def test_a_reference_paystack_never_saw_is_deleted(mock_verify):
    mock_verify.side_effect = PaystackNotFoundError("not found")
    pending = _make_pending(_make_sponsor(), "+233241111111", idle=timedelta(hours=73))

    assert _resolve_like_cleanup(pending) == Resolution.DELETED


@pytest.mark.django_db
@patch(CONSUME)
@patch(VERIFY)
def test_a_paid_registration_is_turned_into_the_account_not_deleted(
    mock_verify, mock_consume
):
    mock_verify.return_value = {"status": "success"}
    mock_consume.return_value = RegistrationPaymentOutcome.CREATED
    pending = _make_pending(_make_sponsor(), "+233241111111", idle=timedelta(hours=73))

    assert _resolve_like_cleanup(pending) == Resolution.CONSUMED
    mock_consume.assert_called_once_with("ref-1")
    assert _exists(pending)


@pytest.mark.django_db
@patch(CONSUME)
@patch(VERIFY)
def test_a_paid_registration_that_became_an_issue_is_deleted(mock_verify, mock_consume):
    mock_verify.return_value = {"status": "success"}
    mock_consume.return_value = RegistrationPaymentOutcome.ISSUE_RECORDED
    pending = _make_pending(_make_sponsor(), "+233241111111", idle=timedelta(hours=73))

    assert _resolve_like_cleanup(pending) == Resolution.DELETED


@pytest.mark.django_db
@patch(CONSUME)
@patch(VERIFY)
def test_a_paid_registration_whose_consume_failed_is_kept(mock_verify, mock_consume):
    """An unrelated, older PaymentIssue for the same reference must not be
    read as "handled" -- only this call's own outcome counts."""
    mock_verify.return_value = {"status": "success"}
    mock_consume.return_value = RegistrationPaymentOutcome.VERIFY_FAILED
    pending = _make_pending(_make_sponsor(), "+233241111111", idle=timedelta(hours=73))
    PaymentIssue.objects.create(
        reference="ref-1", kind=PaymentIssue.Kind.REGISTRATION_UNCONFIRMED
    )

    assert _resolve_like_cleanup(pending) == Resolution.KEPT
    assert _exists(pending)


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["ongoing", "pending", "processing", "queued"])
@patch(VERIFY)
def test_an_in_progress_payment_is_kept(mock_verify, status):
    mock_verify.return_value = {"status": status}
    pending = _make_pending(_make_sponsor(), "+233241111111", idle=timedelta(hours=73))

    assert _resolve_like_cleanup(pending) == Resolution.KEPT


@pytest.mark.django_db
@patch(VERIFY)
def test_a_paystack_error_keeps_it_for_the_next_run(mock_verify):
    mock_verify.side_effect = PaystackError("timed out")
    pending = _make_pending(_make_sponsor(), "+233241111111", idle=timedelta(hours=73))

    assert _resolve_like_cleanup(pending) == Resolution.KEPT
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch(VERIFY)
def test_after_30_days_without_an_answer_it_becomes_an_issue_and_is_deleted(
    mock_verify,
):
    """Personal data can't be kept forever on the strength of Paystack never
    answering. The issue keeps the payer's details for the admin."""
    mock_verify.side_effect = PaystackError("timed out")
    pending = _make_pending(
        _make_sponsor(),
        "+233241111111",
        idle=timedelta(hours=1),
        age=timedelta(days=31),
    )

    assert _resolve_like_cleanup(pending) == Resolution.DELETED

    issue = PaymentIssue.objects.get(reference="ref-1")
    assert issue.kind == PaymentIssue.Kind.REGISTRATION_UNCONFIRMED
    assert issue.payer_phone == "+233241111111"
    assert not _exists(pending)


@pytest.mark.django_db
@patch(VERIFY)
def test_the_30_day_limit_applies_even_if_checkout_keeps_being_reopened(mock_verify):
    """Otherwise reopening the payment page once an hour would keep a row
    (and the phone number it holds) forever."""
    mock_verify.return_value = {"status": "abandoned"}
    pending = _make_pending(
        _make_sponsor(),
        "+233241111111",
        idle=timedelta(minutes=5),
        age=timedelta(days=31),
    )

    assert _resolve_like_cleanup(pending) == Resolution.DELETED


# --- Races ---------------------------------------------------------------------


@pytest.mark.django_db
@patch(VERIFY)
def test_a_new_checkout_opened_during_the_check_stops_the_delete(mock_verify):
    pending = _make_pending(_make_sponsor(), "+233241111111", idle=timedelta(hours=73))

    def reopen_checkout(reference):
        PendingRegistration.objects.filter(pk=pending.pk).update(
            payment_reference="ref-2", payment_initialized_at=timezone.now()
        )
        return {"status": "abandoned"}

    mock_verify.side_effect = reopen_checkout

    assert _resolve_like_cleanup(pending) == Resolution.KEPT
    assert _exists(pending)


@pytest.mark.django_db
@patch(VERIFY)
def test_a_registration_consumed_during_the_check_is_not_deleted(mock_verify):
    pending = _make_pending(_make_sponsor(), "+233241111111", idle=timedelta(hours=73))

    def consumed_meanwhile(reference):
        PendingRegistration.objects.filter(pk=pending.pk).update(
            consumed_at=timezone.now(), consumed_reference="ref-1"
        )
        return {"status": "abandoned"}

    mock_verify.side_effect = consumed_meanwhile

    assert _resolve_like_cleanup(pending) == Resolution.CONSUMED
    assert _exists(pending)


@pytest.mark.django_db
@patch(VERIFY)
def test_a_row_that_no_longer_exists_counts_as_deleted(mock_verify):
    pending = _make_pending(_make_sponsor(), "+233241111111", idle=timedelta(hours=73))
    PendingRegistration.objects.filter(pk=pending.pk).delete()

    assert _resolve_like_cleanup(pending) == Resolution.DELETED


# --- The task ------------------------------------------------------------------


@pytest.mark.django_db
@patch(VERIFY)
def test_cleanup_never_touches_consumed_registrations(mock_verify):
    pending = _make_pending(_make_sponsor(), "+233241111111", age=timedelta(days=60))
    PendingRegistration.objects.filter(pk=pending.pk).update(consumed_at=timezone.now())

    cleanup_expired_pending_registrations()

    assert _exists(pending)
    mock_verify.assert_not_called()


@pytest.mark.django_db
@patch(VERIFY)
def test_one_failing_row_does_not_stop_the_rest(mock_verify):
    sponsor = _make_sponsor()
    first = _make_pending(sponsor, "+233241111111", idle=timedelta(hours=80))
    second = _make_pending(
        sponsor, "+233241111112", reference="ref-2", idle=timedelta(hours=80)
    )

    def verify(reference):
        if reference == "ref-1":
            raise RuntimeError("unexpected")
        return {"status": "abandoned"}

    mock_verify.side_effect = verify

    cleanup_expired_pending_registrations()

    assert _exists(first)
    assert not _exists(second)


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["ongoing", "pending"])
@patch(VERIFY)
def test_past_the_limit_an_unfinished_charge_is_deleted_without_an_issue(
    mock_verify, status
):
    """A charge started and never approved is not a payment; a late success
    still reaches the webhook as an unmatched issue."""
    mock_verify.return_value = {"status": status}
    pending = _make_pending(
        _make_sponsor(),
        "+233241111111",
        idle=timedelta(days=31),
        age=timedelta(days=31),
    )

    assert _resolve_like_cleanup(pending) == Resolution.DELETED
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch(VERIFY)
def test_a_row_checked_within_the_hour_waits_its_turn(mock_verify):
    """Rows Paystack keeps failing on must not sit at the front of every
    batch and starve newer ones."""
    mock_verify.side_effect = PaystackError("down")
    sponsor = _make_sponsor()
    recently_checked = _make_pending(sponsor, "+233241111111", idle=timedelta(days=4))
    PendingRegistration.objects.filter(pk=recently_checked.pk).update(
        last_checked_at=timezone.now() - timedelta(minutes=10)
    )
    never_checked = _make_pending(
        sponsor, "+233241111112", reference="ref-2", idle=timedelta(days=3, hours=1)
    )

    cleanup_expired_pending_registrations()

    assert [c.args[0] for c in mock_verify.call_args_list] == ["ref-2"]
    never_checked.refresh_from_db()
    assert never_checked.last_checked_at is not None


@pytest.mark.django_db
@patch(VERIFY)
def test_one_run_is_capped(mock_verify):
    from apps.distributors.tasks import CLEANUP_BATCH_SIZE

    mock_verify.side_effect = PaystackError("down")
    sponsor = _make_sponsor()
    for i in range(CLEANUP_BATCH_SIZE + 3):
        _make_pending(
            sponsor, f"+2332411{i:05d}", reference=f"ref-{i}", idle=timedelta(days=4)
        )

    cleanup_expired_pending_registrations()

    assert mock_verify.call_count == CLEANUP_BATCH_SIZE


# --- Claiming the batch (agent code review of the CodeRabbit fix) --------------


@pytest.mark.django_db
@patch(VERIFY)
def test_a_row_paystack_keeps_failing_on_is_stamped_as_checked(mock_verify):
    """The point of claiming up front: a row that stays KEPT must not walk
    straight back into the next batch."""
    mock_verify.side_effect = PaystackError("down")
    pending = _make_pending(_make_sponsor(), "+233241111111", idle=timedelta(days=4))

    cleanup_expired_pending_registrations()

    pending.refresh_from_db()
    assert pending.last_checked_at is not None


@pytest.mark.django_db
@patch(VERIFY)
def test_only_the_batch_is_claimed_not_every_waiting_row(mock_verify):
    """Locking the whole candidate query would make an overlapping run skip
    every row and do nothing at all."""
    from apps.distributors.tasks import CLEANUP_BATCH_SIZE

    mock_verify.side_effect = PaystackError("down")
    sponsor = _make_sponsor()
    for i in range(CLEANUP_BATCH_SIZE + 5):
        _make_pending(
            sponsor, f"+2332411{i:05d}", reference=f"ref-{i}", idle=timedelta(days=4)
        )

    cleanup_expired_pending_registrations()

    assert (
        PendingRegistration.objects.filter(last_checked_at__isnull=False).count()
        == CLEANUP_BATCH_SIZE
    )


@pytest.mark.django_db
@patch(VERIFY)
def test_the_least_recently_checked_rows_go_first(mock_verify):
    mock_verify.side_effect = PaystackError("down")
    sponsor = _make_sponsor()
    now = timezone.now()
    never_checked = _make_pending(
        sponsor, "+233241111111", reference="never", idle=timedelta(days=4)
    )
    checked_long_ago = _make_pending(
        sponsor, "+233241111112", reference="old", idle=timedelta(days=4)
    )
    PendingRegistration.objects.filter(pk=checked_long_ago.pk).update(
        last_checked_at=now - timedelta(hours=6)
    )
    checked_recently_enough = _make_pending(
        sponsor, "+233241111113", reference="recent", idle=timedelta(days=4)
    )
    PendingRegistration.objects.filter(pk=checked_recently_enough.pk).update(
        last_checked_at=now - timedelta(hours=2)
    )

    cleanup_expired_pending_registrations()

    assert [c.args[0] for c in mock_verify.call_args_list] == [
        "never",
        "old",
        "recent",
    ]
    assert PendingRegistration.objects.filter(pk=never_checked.pk).exists()


@pytest.mark.django_db
@patch(VERIFY)
def test_a_never_checked_row_is_never_skipped(mock_verify):
    """last_checked_at is NULL for most rows; a filter() instead of the
    exclude() would drop them all and stop cleanup dead, silently."""
    mock_verify.return_value = {"status": "abandoned"}
    pending = _make_pending(_make_sponsor(), "+233241111111", idle=timedelta(days=4))
    assert pending.last_checked_at is None

    cleanup_expired_pending_registrations()

    assert not _exists(pending)
